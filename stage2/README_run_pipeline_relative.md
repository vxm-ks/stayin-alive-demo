# MIDI 完整生成模块

> 当前状态：**测试模式**。只接受三段 `A-B-A`，每段固定为前 8 小节主题、后 8 小节 MIDI-GPT 填充，总计 48 小节。正式模式的任意曲式尚未在本入口启用。

## 与 Stage 1 / Stage 3 的正式适配

正式入口仍是 `run_pipeline_relative.py`。传入 Stage 1 生成的
`stage2_plan.json` 后，入口会验证三段 A/B/A 的边界、8 小节主题、8 小节扩展、
BPM、拍号以及保护/编辑范围都与当前算法一致；任何不一致都会停止运行。

```powershell
python .\run_pipeline_relative.py .\inputs\A.mid .\inputs\B.mid `
  --stage2-plan .\inputs\stage2_plan.json `
  --output .\outputs\final_completed.mid
```

传入计划后，鼓轨按逐段 tension/drum 指令生成通道 10 的 S1(36)/S2(38)
心跳事件。MIDI-GPT 前后会比较这些事件；发生改动则拒绝输出交接清单。
装配时会先检测并删除 A/B 输入中所有原有 MIDI 通道 10 消息，再填入计划规定的心跳轨，防止旧鼓点与真实心跳叠加。清理数量写入完成清单。
最终产物为完整 MIDI 和 `stage2_completion_manifest.json`，可直接作为
`stage3_midi_renderer --input-midi` 的输入。不传 `--stage2-plan` 时继续使用
旧的 42 号闭镲行为，仅用于兼容原来的独立运行方式。

## Stage 1 → Stage 2 → Stage 3 自动衔接

当 Stage 1 已发布四个同前缀目录，并已完成 `generated_themes` 后处理时，可以只传 Stage 1 的输出基名：

```powershell
python .\stage2\run_pipeline_relative.py `
  --stage1-output-base ".\stage1_story_agent\outputs\story-001" `
  --heartbeat-package "patient_a=.\heartbeat_stage1\outputs\patient_a\heartbeat_package" `
  --stage3-render-plan ".\stage3_midi_renderer\examples\stage3_render_plan.example.json" `
  --output ".\stage2\outputs\story-001\final_completed.mid"
```

入口会自动定位并验哈希：

- `story-001-stage2/stage2_plan.json`；
- `story-001-musecoco/generated_themes/theme-A/final.mid`；
- `story-001-musecoco/generated_themes/theme-B/final.mid`。

成功后额外输出 `stage3_handoff.json`。它冻结完整 MIDI、render plan 和所有 heartbeat package 清单的 SHA-256，Schema 位于 `stage2/schemas/stage3_handoff.schema.json`。Stage 3 可直接运行：

```powershell
python -m stage3_midi_renderer render-packages `
  --stage2-handoff ".\stage2\outputs\story-001\stage3_handoff.json" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --output-dir ".\stage3_midi_renderer\outputs\story-001"
```

该模块会依次完成：

```text
A.mid + B.mid
        ↓
拼接 A、B，并添加闭镲鼓轨
        ↓
combined_with_drums.mid
        ↓
使用 MIDI-GPT 补全三段空白
        ↓
final_completed.mid
```

## 1. 文件结构

请将以下文件放在同一目录：

```text
stage2/
├─ run_pipeline_relative.py
├─ combine_midi_with_drums.py
├─ complete_all_gaps_v3.py
├─ inputs/
│  ├─ A.mid
│  └─ B.mid
└─ outputs/
```

要求：

- `A.mid` 和 `B.mid` 都是 8 小节；
- 两个 MIDI 的拍号一致；
- `inputs`、`outputs` 和 MIDI 文件名建议使用英文；
- 使用已经安装 `midigpt` 的 Python 虚拟环境。

## 2. 激活虚拟环境

当前已验证的 Windows 原生部署为：

```text
Python: D:\LegaSynth\stage2\.conda_midigpt\python.exe
Package: midigpt 0.3.2
Model: yellow
HF_HOME: D:\LegaSynth\stage2\.cache\huggingface
```

推荐不激活 Conda，直接使用专用解释器：

```powershell
D:\LegaSynth\stage2\.conda_midigpt\python.exe .\stage2\run_pipeline_relative.py ...
```

以下激活方式仅作为等价的手工运行方法。

在项目目录中运行：

```powershell
..\.venv_midigpt\Scripts\Activate.ps1
```

激活成功后，PowerShell 开头会出现：

```text
(.venv_midigpt)
```

## 3. 运行完整流程

例如 BPM 为 120、拍号为 3/4：

```powershell
python .\run_pipeline_relative.py `
  .\inputs\A.mid `
  .\inputs\B.mid `
  --bpm 120 `
  --time-signature 3/4 `
  --output .\outputs\final_completed.mid
```

例如 BPM 为 96、拍号为 4/4：

```powershell
python .\run_pipeline_relative.py `
  .\inputs\A.mid `
  .\inputs\B.mid `
  --bpm 96 `
  --time-signature 4/4 `
  --output .\outputs\final_completed.mid
```

## 4. 参数说明

| 参数 | 含义 |
|---|---|
| `A.mid` | A 片段路径 |
| `B.mid` | B 片段路径 |
| `--bpm` | 全曲 BPM |
| `--time-signature` | 拍号，例如 `3/4`、`4/4` |
| `--output` | 最终 MIDI 输出路径 |
| `--model` | MIDI-GPT 模型，默认 `yellow` |
| `--seed` | 随机种子，默认 `42` |
| `--resume` | 从第二阶段检查点继续 |

指定模型和随机种子：

```powershell
python .\run_pipeline_relative.py `
  .\inputs\A.mid `
  .\inputs\B.mid `
  --bpm 120 `
  --time-signature 3/4 `
  --model yellow `
  --seed 42 `
  --output .\outputs\final_completed.mid
```

## 5. 输出文件

程序会在 `outputs` 目录生成：

```text
outputs/
├─ combined_with_drums.mid
├─ final_completed.mid
├─ stage2_completion_manifest.json
└─ stage3_handoff.json
```

其中：

- `combined_with_drums.mid`：第一阶段生成的中间 MIDI；
- `final_completed.mid`：补全三段空白后的最终 MIDI。

运行过程中还可能暂时生成：

```text
final_completed.v3.checkpoint.mid
```

它用于中断后继续运行。全部完成后，检查点通常会自动删除。

## 6. 中断后继续

第二阶段中断后，使用相同的输出路径并添加 `--resume`：

```powershell
python .\run_pipeline_relative.py `
  .\inputs\A.mid `
  .\inputs\B.mid `
  --bpm 120 `
  --time-signature 3/4 `
  --output .\outputs\final_completed.mid `
  --resume
```

使用 `--resume` 时：

- 第一阶段不会重新执行；
- `outputs\combined_with_drums.mid` 必须仍然存在；
- `--output` 必须与上一次运行相同。

## 7. 相对路径注意事项

请使用：

```text
.\inputs\A.mid
.\inputs\B.mid
.\outputs\final_completed.mid
```

不要手动改成包含中文目录的完整绝对路径。

该入口会让两个子脚本在项目目录中运行，并向 MIDI-GPT 传递英文相对路径，避免底层 MIDI 读取器无法打开绝对中文路径。

## 8. 常见问题

### 找不到 `midigpt`

确认已经激活虚拟环境：

```powershell
..\.venv_midigpt\Scripts\Activate.ps1
```

也可以检查：

```powershell
python -c "import midigpt; print(midigpt.__file__)"
```

### 找不到脚本

确认以下三个文件在同一目录：

```text
run_pipeline_relative.py
combine_midi_with_drums.py
complete_all_gaps_v3.py
```

### MIDI 长度或拍号错误

确认：

- `A.mid` 和 `B.mid` 都是 8 小节；
- 命令中的 `--time-signature` 与输入 MIDI 一致；
- BPM 大于 0。

### 不要修改脚本文件名

`run_pipeline_relative.py` 会自动查找：

```text
combine_midi_with_drums.py
complete_all_gaps_v3.py
```

因此这两个文件名必须保持不变。
