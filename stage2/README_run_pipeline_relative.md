# Stage 2 正式任意曲式流水线

`run_pipeline_relative.py` 是 Stage 2 的正式入口。它不再假定 `A-B-A`、固定段数、固定 48 小节或统一速度，而是严格执行 Stage 1 发布的 `stage2_plan.json`。

## 输入契约

推荐直接传入 Stage 1 输出基名：

```powershell
D:\path\to\stage2\.conda_midigpt\python.exe .\stage2\run_pipeline_relative.py `
  --stage1-output-base ".\stage1_story_agent\outputs\story-001" `
  --heartbeat-package "patient_a=.\heartbeat_package" `
  --stage3-render-plan ".\stage3_midi_renderer\examples\stage3_render_plan.example.json" `
  --output ".\stage2\outputs\story-001\final_completed.mid"
```

该方式会自动定位并校验：

- `*-stage2/stage2_plan.json`；
- `*-musecoco/generated_themes/theme-*/final.mid`；
- `theme_manifest.json` 中每个主题文件的 SHA-256。

也可手动绑定任意数量的主题族：

```powershell
D:\path\to\stage2\.conda_midigpt\python.exe .\stage2\run_pipeline_relative.py `
  --stage2-plan ".\inputs\stage2_plan.json" `
  --theme "theme-A=.\inputs\A.mid" `
  --theme "theme-B=.\inputs\B.mid" `
  --theme "theme-C=.\inputs\C.mid" `
  --output ".\outputs\final_completed.mid"
```

位置参数 `A.mid B.mid` 仅保留为旧命令兼容形式；正式接入应使用 `--stage1-output-base` 或重复的 `--theme`。

## 任意曲式如何执行

Stage 1 的每个 section 必须明确给出：

- `section_id`、`form_label` 和连续的小节边界；
- `theme_family_id`；
- `relation`、`source_section_id` 和 `material_source`；
- `midigpt_access`、`protected_ranges` 和 `editable_ranges`；
- 逐段速度、拍号和心跳鼓轨计划。

装配器按 section 顺序处理：

- `fixed`：放置主题素材且不开放 MIDI-GPT 区域；
- `extension_only`：保护开头主题，将其余声明区间作为续写目标；
- `modifiable`：从较早的 `source_section_id` 取上下文，在声明区间生成变奏或发展。

因此 `A`、`A-B-A`、`A-B-A'-C` 以及其他 1–16 段的合规结构使用同一套代码路径。主题 MIDI 的 PPQ 可不同，装配时统一换算；输入中的 MIDI 通道 10 会先全部移除，再严格按计划重建并保护心跳事件。MIDI-GPT 推理完成后会恢复逐段速度图。

## MIDI-GPT 旋律质量软门槛

每个 4 小节生成块仍会检查旋律事件数、有效小节数、不同音高数、音高变化和
和弦式起奏比例，但这些指标不再硬性终止流程：

1. 首次候选达标则立即采用；
2. 首次不达标时最多更换 seed 和 temperature 重试两次；
3. 第三次仍不达标但包含音符时，记录具体未达标指标并降级放行；
4. 如果第三次为静音，则使用本轮较早的最近一个非静音候选；
5. 只有三次都完全没有生成音符时才失败，因为此时不存在可以交付的 MIDI 内容。

日志中的 `质量门槛降级放行` 可用于后续人工听感评估。

## 输出

输出目录包含：

```text
combined_with_drums.mid
stage2_plan.resolved.json
final_completed.mid
stage2_completion_manifest.json
stage2_repetition_report.json       # 仅 detect/regenerate 时
final_completed.pre_repetition_gate.mid # 仅 regenerate 时
stage3_handoff.json                 # 提供 Stage 3 参数时
```

`stage2_plan.resolved.json` 是经过 Stage 2 严格校验、补齐显式素材关系后的冻结计划。`stage2_completion_manifest.json` 记录曲式、总小节数、各段速度、输入/输出哈希和通道 10 审计。`stage3_handoff.json` 使用 `production` 状态，并明确携带 `form_string` 与 `total_bars`。

## 可选反重复质量门

默认 `--repetition-mode off`。用户可选择：

- `detect`：只报告连续两个小节的整体音高织体重复；
- `regenerate`：仅对计划声明的 editable 区间局部重生成。

```powershell
--repetition-mode regenerate `
--repetition-threshold 0.82 `
--repetition-max-occurrences 2 `
--repetition-candidates 4
```

节奏不作为独立重复分数。通道 10、保护主题区和最终终止式不会被该模块改写。

## 中断续跑与验证

使用相同输出路径加 `--resume` 可从 MIDI-GPT checkpoint 继续。运行前可检查入口与测试：

```powershell
D:\path\to\stage2\.conda_midigpt\python.exe .\stage2\run_pipeline_relative.py --help
D:\conda\python.exe -m unittest discover -s .\stage2\tests -v
```

Stage 3 可直接消费 handoff：

```powershell
D:\conda\python.exe -m stage3_midi_renderer render-packages `
  --stage2-handoff ".\stage2\outputs\story-001\stage3_handoff.json" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --output-dir ".\stage3_midi_renderer\outputs\story-001"
```
