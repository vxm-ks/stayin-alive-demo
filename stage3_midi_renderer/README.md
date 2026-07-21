# Stage 3：完整 MIDI 与真实心音素材渲染

Stage 3 读取第二阶段输出的完整 MIDI。完整 MIDI 是节奏、速度、S1/S2 类型和事件位置的唯一权威；Stage 3 不生成、移动或删除心跳事件，也不对真实心音做时间拉伸或移调。

模块保留两条兼容工作流：

- `render`：旧版单心跳 SoundFont，一次 FluidSynth 渲染。
- `render-packages`：新版多 `heartbeat_package` 渲染，支持按 plan 选择/轮换真实 S1/S2 样本，并独立控制音乐与心跳响度。

## 推荐：多 package 渲染

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

两种模式最后都会估计 4 倍过采样真峰值；若超过 `true_peak_ceiling_dbtp`，音乐和心跳共同衰减相同数值，因此二者的相对响度不变。`publish_stems` 默认为 `false`。

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

## 依赖与测试

```powershell
D:\conda\python.exe -m pip install -r .\stage3_midi_renderer\requirements.txt

D:\conda\python.exe -m unittest `
  stage3_midi_renderer.tests.test_package_renderer `
  stage3_midi_renderer.tests.test_heartbeat_conditioning `
  stage3_midi_renderer.tests.test_renderer `
  heartbeat_midi_exporter.tests.test_heartbeat_midi_exporter -v
```

运行渲染还需要 FluidSynth 可执行文件和通用 SF2。Python 数值处理使用 NumPy 与 SciPy；版本约束见 `requirements.txt`，所有实际参数和输入文件哈希写入渲染清单，便于学术复现。
