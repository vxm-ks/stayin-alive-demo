# Stage 3 心跳音色规整组件规范

组件标识：`stage3_midi_renderer.heartbeat_conditioning`

当前组件版本：`1.0.0`

## 1. 职责

该组件只处理用于制作心跳 SoundFont 的独立 S1/S2 音色样本，目标是在需要较高心跳响度时，减少超低频负担、带外噪声和样本边界突变对扬声器的影响。

组件不会：

- 修改、创建或移动 MIDI 事件；
- 改变完整 MIDI 的速度、调性或段落；
- 对完整音乐 WAV 进行低通；
- 将心跳 WAV 与音乐 WAV 分轨混音。

Stage 3 保留两种渲染入口：旧版使用完整 MIDI、通用 SoundFont 和仅含打击乐预设的心跳 SoundFont；新版 `render-packages` 使用完整 MIDI、通用 SoundFont 与一个或多个 `heartbeat_package`，以便独立平衡音乐/心跳响度和轮换真实样本。两种入口都不改变完整 MIDI 的心跳事件时序。

## 2. 标准调用流程

```text
独立 S1/S2 样本
  -> heartbeat_conditioning
  -> 规整后的样本 + JSON 审计
  -> heartbeat_midi_exporter 制作 percussion-only SF2
  -> Stage 2 完整 MIDI 已包含心跳鼓轨
  -> Stage 3 validate
  -> Stage 3 render
```

心跳 MIDI 音符、叠加层数、MIDI 力度和 FluidSynth 总增益不是规整配置档的一部分。它们由各自组件单独配置，避免把某首测试曲的音符 42、24 层或某个总增益固化到 DSP 规则中。

## 3. 注册配置档

### `none`

审计式旁路。输出样本数据不变，用于对照实验和禁用处理的工作流。

### `speaker_safe_loud_v1`

适用于心跳需要明显放大、并可能通过普通耳机或小型扬声器播放的场景：

- 四阶、零相位 35 Hz 高通；
- 四阶、零相位 220 Hz 低通；
- 阈值 −18 dBFS、比例 4:1 的包络压缩；
- 3 ms attack、80 ms release；
- 5 ms 首尾淡入淡出；
- 峰值统一到 −3 dBFS。

配置档名称包含版本号。将来若改变任何听感相关参数，应新增 `speaker_safe_loud_v2`，不能静默修改 v1 的定义。

## 4. 命令行接口

查看所有注册配置档：

```powershell
D:\conda\python.exe -m stage3_midi_renderer conditioning-profiles
```

规整一个独立心跳样本：

```powershell
D:\conda\python.exe -m stage3_midi_renderer condition-heartbeat `
  --input-wav "D:\path\patient_s1.wav" `
  --profile speaker_safe_loud_v1
```

默认输出目录按时间戳命名。也可以明确指定：

```powershell
D:\conda\python.exe -m stage3_midi_renderer condition-heartbeat `
  --input-wav "D:\path\patient_s1.wav" `
  --output-dir ".\stage3_midi_renderer\outputs\patient-001-conditioning" `
  --profile speaker_safe_loud_v1
```

输出固定包含：

```text
heartbeat_conditioned.wav
heartbeat_conditioning_manifest.json
```

除非使用 `--force`，程序不会覆盖已经存在的输出目录。输入 WAV 永远不会被修改。

## 5. Python 接口

处理内存中的数组：

```python
from stage3_midi_renderer import condition_heartbeat_audio

conditioned, audit = condition_heartbeat_audio(
    audio,
    sample_rate,
    profile="speaker_safe_loud_v1",
)
```

处理 WAV 并发布审计产物：

```python
from stage3_midi_renderer import condition_heartbeat_wav

result = condition_heartbeat_wav(
    "patient_s1.wav",
    "stage3_outputs/patient-001-conditioning",
    profile="speaker_safe_loud_v1",
)
```

Python 调用方还可以构造不可变的 `HeartbeatConditioningProfile`。自定义配置会完整写入审计，但正式生产流程应优先使用注册且带版本号的配置档，以确保可复现。

## 6. SoundFont 导出器集成

`heartbeat_midi_exporter` 不再维护自己的滤波与压缩实现，而是调用本组件：

```powershell
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py `
  "D:\path\xxx_bar_analysis.json" `
  --soundfont-conditioning speaker_safe_loud_v1
```

导出器的 `*_midi_mapping.json` 会记录组件标识、组件版本、配置档参数、处理前后响度和频段能量。因此通过导出器生成的 SoundFont 与直接调用 Stage 3 组件使用相同算法和审计规则。

## 7. 审计与失败条件

`heartbeat_conditioning_manifest.json` 必须记录：

- 输入与输出 SHA-256；
- 组件版本和完整配置档；
- 采样率、声道数和帧数；
- 处理前后 peak/RMS；
- 处理前后频段能量比例；
- `complete_mix_filtered: false`；
- `midi_events_modified: false`。

以下情况必须失败，不得静默回退：

- 输入不存在、为空、含非有限数或 WAV 类型不支持；
- 采样率不在 8–384 kHz；
- 截止频率不满足 `0 < highpass < lowpass < Nyquist`；
- 样本短到无法执行所选零相位滤波器；
- 输入文件在处理过程中发生变化；
- 输出目录已存在且未明确传入 `--force`。

## 8. 验证

```powershell
D:\conda\python.exe -m unittest `
  stage3_midi_renderer.tests.test_heartbeat_conditioning `
  stage3_midi_renderer.tests.test_renderer `
  heartbeat_midi_exporter.tests.test_heartbeat_midi_exporter -v
```

测试覆盖注册配置档、旁路、自定义配置、频段抑制、峰值上限、输入不变性、原子发布、CLI、SoundFont 导出器复用和完整 MIDI 单进程渲染。
