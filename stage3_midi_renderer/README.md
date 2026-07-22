# Stage 3：完整 MIDI 与真实心音素材渲染

Stage 3 读取第二阶段输出的完整 MIDI。完整 MIDI 是节奏、速度、S1/S2 类型和事件位置的唯一权威；Stage 3 不生成、移动或删除心跳事件，也不对真实心音做时间拉伸或移调。

Stage 3 不假定固定曲式或固定总小节数。当前 Stage 1 测试模式会产生 48 小节 `A-B-A`（每段 8 小节主题加 8 小节填充），正式模式的段落标签和数量可变；只要 Stage 2 交付的是包含音乐轨与既有心跳轨的完整合规 MIDI，Stage 3 就按 MIDI 自身的终点、速度和拍号统一渲染。48 小节输入已纳入离线回归测试。

模块保留两条兼容工作流：

- `render`：旧版单心跳 SoundFont，一次 FluidSynth 渲染。
- `render-packages`：新版多 `heartbeat_package` 渲染，支持按 plan 选择/轮换真实 S1/S2 样本，并独立控制音乐与心跳响度。

## 推荐：多 package 渲染

如果 Stage 2 已生成 `stage3_handoff.json`，推荐直接使用自动交接入口。完整 MIDI、render plan 与 heartbeat packages 都会从 handoff 解析并重新校验 SHA-256：

```powershell
D:\conda\python.exe -m stage3_midi_renderer render-packages `
  --stage2-handoff ".\stage2\outputs\story-001\stage3_handoff.json" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --output-dir ".\stage3_midi_renderer\outputs\story-001" `
  --fluidsynth ".\tools\fluidsynth\bin\fluidsynth.exe"
```

通用乐器 SF2 仍由 Stage 3 明确传入，因为它不是 Stage 1 患者心音资产。以下手动参数方式继续保留。

先复制并修改 [`examples/stage3_render_plan.example.json`](examples/stage3_render_plan.example.json)。命令中的 package ID 必须与 plan 的 `package_ids` 完全一致：

```powershell
D:\conda\python.exe -m stage3_midi_renderer render-packages `
  --input-midi ".\stage2_outputs\final_complete.mid" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --render-plan ".\stage3_render_plan.json" `
  --heartbeat-package "patient_a=D:\packages\patient_a\heartbeat_package" `
  --heartbeat-package "patient_b=D:\packages\patient_b\heartbeat_package" `
  --output-dir ".\stage3_midi_renderer\outputs\final-v2" `
  --fluidsynth ".\tools\fluidsynth\bin\fluidsynth.exe"
```

package 选择模式：

- `fixed`：始终使用一个 package。
- `round_robin_per_cycle`：按心动周期轮换；一个 S1 及其后续 S2 使用同一 package。
- `round_robin_per_bar`：每小节轮换一次。

`section_material_rules` 可按不重叠的小节范围覆盖默认选择规则。每个 package 内部，同类型事件样本按清单顺序轮换。

## 独立响度控制

plan 的 `mix.mode` 支持：

- `manual`：直接使用 `music_gain_db` 和 `heartbeat_gain_db`。
- `event_window_relative`：先应用 `music_gain_db`，再测量每个心跳附近的局部音乐与心跳 RMS，使心跳约高出 `heartbeat_over_music_db`，增幅不超过 `maximum_heartbeat_boost_db`。
- `perceptual_event_adaptive`：逐个事件使用 BS.1770 K-weighting 比较心跳与同期音乐；对每个 S1/S2 独立计算并平滑增益，设置心跳增益下限，必要时短暂闪避音乐，最后按目标 LUFS 和真峰值限制统一母带化。推荐用于真实心音。

三种模式最后都会估计 4 倍过采样真峰值；若超过 `true_peak_ceiling_dbtp`，音乐和心跳共同衰减相同数值，因此二者的相对响度不变。`publish_stems` 默认为 `false`。

推荐的感知自适应参数是：心跳高于同期音乐 `7 dB`、事件增益 `0..18 dB`、相邻同类事件变化不超过 `2 dB`、音乐最大闪避 `4 dB`、力度增益下限 `0.75`、最终 `-16 LUFS / -1 dBTP`。逐事件测量、增益、闪避和预测平衡均写入 CSV；清单记录增益范围、最终 LUFS 和真峰值。

默认只输出：

```text
final_mix.wav
heartbeat_event_assignments.csv
stage3_render_manifest.json
```

不会输出整曲时间—能量图。CSV 可逐事件追溯 MIDI tick、时间、小节、S1/S2、package、源样本哈希、增益与放置位置；JSON 记录输入哈希、plan、FluidSynth 调用、响度和真峰值保护。

## 旧版兼容命令

```powershell
D:\conda\python.exe -m stage3_midi_renderer render `
  --input-midi ".\stage2_outputs\final_complete.mid" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --heartbeat-sf2 "D:\heartbeat_assets\patient_heartbeat.sf2" `
  --fluidsynth ".\tools\fluidsynth\bin\fluidsynth.exe"
```

旧版要求心跳 SoundFont 只含 percussion bank 128/program 0。`validate` 可在渲染前检查完整 MIDI 和两个 SoundFont。

## 心音样本规整

`heartbeat_conditioning` 只处理孤立的真实 S1/S2 样本，不处理整曲。`render-packages` 默认使用 `speaker_safe_loud_v1`，也可在 plan 中设为 `none`。完整参数与算法见 [`HEARTBEAT_CONDITIONING.md`](HEARTBEAT_CONDITIONING.md)。

## 批量验证任意合规的 Stage 2 MIDI

完整的人工操作、报告判读和听感检查流程见
[`MANUAL_BATCH_TESTING.md`](MANUAL_BATCH_TESTING.md)。

`batch-validate` 用于回答“不同完整 MIDI 是否都能得到可接受的最终响度”。这里的任意 MIDI
必须通过正式接口：含音乐事件，并在 plan 指定的心跳通道上包含可由 `note_map` 解释的 S1/S2
事件。测试器不会新增、删除或移动 MIDI 事件。

批量测试必须使用 `mix.mode=event_window_relative`。可从
[`examples/stage3_batch_validation_plan.example.json`](examples/stage3_batch_validation_plan.example.json)
复制 plan。测试器只在诊断副本中强制 `publish_stems=true`，以测量局部音乐/心跳比例；原 plan
不会被改写。

```powershell
D:\conda\python.exe -m stage3_midi_renderer batch-validate `
  --input-dir ".\stage2_outputs" `
  --general-sf2 "D:\soundfonts\general.sf2" `
  --render-plan ".\stage3_batch_validation_plan.json" `
  --heartbeat-package "patient_a=D:\packages\patient_a\heartbeat_package" `
  --output-dir ".\stage3_midi_renderer\outputs\batch-validation" `
  --fluidsynth ".\tools\fluidsynth\bin\fluidsynth.exe"
```

也可重复传入 `--input-midi`。默认验收范围为：整曲 `-18` 至 `-12 LUFS`、真峰值不高于
`-1 dBTP`、心跳事件窗口内的中位数 RMS 比音乐高 `2` 至 `8 dB`。所有阈值都有对应 CLI
参数，可按播放平台或作品需求修改。命令会继续处理不合规文件，并输出：

```text
batch_validation_report.json
batch_validation_report.csv
renders/<MIDI名-哈希>/final_mix.wav
renders/<MIDI名-哈希>/music_stem.wav
renders/<MIDI名-哈希>/heartbeat_stem.wav
```

退出码 `0` 表示全部通过，`1` 表示至少一首未通过，`2` 表示批次配置本身无效。测量依据为
[ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I/en) 与
[EBU R128 v5.0](https://tech.ebu.ch/publications/r128)。真峰值为 4 倍过采样估计；它适合自动化
回归验收，但正式发行前仍建议用经过认证的响度表复核。

## 依赖与测试

```powershell
D:\conda\python.exe -m pip install -r .\stage3_midi_renderer\requirements.txt

D:\conda\python.exe -m unittest `
  stage3_midi_renderer.tests.test_package_renderer `
  stage3_midi_renderer.tests.test_batch_validator `
  stage3_midi_renderer.tests.test_heartbeat_conditioning `
  stage3_midi_renderer.tests.test_renderer `
  heartbeat_midi_exporter.tests.test_heartbeat_midi_exporter -v
```

运行渲染还需要 FluidSynth 可执行文件和通用 SF2。Python 数值处理使用 NumPy 与 SciPy；版本约束见 `requirements.txt`，所有实际参数和输入文件哈希写入渲染清单，便于学术复现。
