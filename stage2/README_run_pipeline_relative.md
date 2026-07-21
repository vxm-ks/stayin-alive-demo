# MIDI 完整生成模块

## 与 Stage 1 / Stage 3 的正式适配

正式入口仍是 `run_pipeline_relative.py`。传入 Stage 1 生成的
`stage2_plan.json` 后，入口会验证三段 A/B/A 的边界、4 小节主题、扩展长度、
BPM、拍号以及保护/编辑范围都与当前算法一致；任何不一致都会停止运行。

```powershell
python .\run_pipeline_relative.py .\inputs\A.mid .\inputs\B.mid `
  --stage2-plan .\inputs\stage2_plan.json `
  --output .\outputs\final_completed.mid
```

传入计划后，鼓轨按逐段 tension/drum 指令生成通道 10 的 S1(36)/S2(38)
心跳事件。MIDI-GPT 前后会比较这些事件；发生改动则拒绝输出交接清单。
最终产物为完整 MIDI 和 `stage2_completion_manifest.json`，可直接作为
`stage3_midi_renderer --input-midi` 的输入。不传 `--stage2-plan` 时继续使用
旧的 42 号闭镲行为，仅用于兼容原来的独立运行方式。

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

- `A.mid` 和 `B.mid` 都是 4 小节；
- 两个 MIDI 的拍号一致；
- `inputs`、`outputs` 和 MIDI 文件名建议使用英文；
- 使用已经安装 `midigpt` 的 Python 虚拟环境。

## 2. 激活虚拟环境

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
└─ final_completed.mid
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

- `A.mid` 和 `B.mid` 都是 4 小节；
- 命令中的 `--time-signature` 与输入 MIDI 一致；
- BPM 大于 0。

### 不要修改脚本文件名

`run_pipeline_relative.py` 会自动查找：

```text
combine_midi_with_drums.py
complete_all_gaps_v3.py
```

因此这两个文件名必须保持不变。
