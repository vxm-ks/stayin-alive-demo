# Stage 3 手动测试指南

本文用于手动验证：不同的、符合 Stage 2 接口的完整 MIDI，能否由 Stage 3 使用真实 S1/S2
素材渲染，并得到可接受的最终响度。

## 1. 测试范围

这里的“任意 MIDI”不是任意来源的普通 MIDI，而是符合 Stage 2→Stage 3 接口的完整 MIDI：

- 文件是有效的 Standard MIDI File（`.mid` 或 `.midi`）。
- MIDI 中同时存在音乐事件和心跳事件。
- 心跳事件已经由 Stage 2 安排完毕；Stage 3 不新增、删除或移动事件。
- 心跳通道与 `render plan` 的 `heartbeat_channel` 一致。
- 心跳音高可以被 `note_map` 解释为 S1 或 S2。
- 通用 SoundFont 能覆盖 MIDI 使用的乐器。

如果输入只是普通音乐 MIDI、没有心跳事件，它不属于本测试的合格输入，应由 Stage 2 先完成心跳轨。

## 2. 测试前准备

需要以下四类输入：

1. 一个或多个 Stage 2 完整 MIDI。
2. 通用乐器 SoundFont，例如 `GeneralUser-GS.sf2`。
3. 一个或多个 Stage 1 `heartbeat_package`。
4. 一个 Stage 3 render plan。

还需要：

- Python 环境及项目依赖。
- FluidSynth 可执行文件。
- 足够的磁盘空间；诊断模式会为每首 MIDI 保留最终混音、音乐 stem 和心跳 stem。

患者音频、SoundFont 和运行输出不会进入公开 Git 仓库。若在新克隆的仓库中测试，必须从合规的
本地存储单独提供这些资产。

## 3. 安装依赖并确认命令

在 PowerShell 中进入项目根目录：

```powershell
cd "C:\path\to\legasynth"
```

安装 Stage 3 Python 依赖：

```powershell
D:\conda\python.exe -m pip install -r .\stage3_midi_renderer\requirements.txt
```

确认批量测试命令存在：

```powershell
D:\conda\python.exe -m stage3_midi_renderer batch-validate --help
```

若帮助信息中没有 `batch-validate`，通常表示当前工作目录不是新版项目根目录，或者 Python
导入了其他位置的同名模块。可以执行：

```powershell
D:\conda\python.exe -c "import stage3_midi_renderer; print(stage3_midi_renderer.__file__)"
```

输出路径应指向当前项目中的 `stage3_midi_renderer`。

## 4. 准备 render plan

复制示例：

```powershell
Copy-Item `
  ".\stage3_midi_renderer\examples\stage3_batch_validation_plan.example.json" `
  ".\stage3_batch_validation_plan.json"
```

至少检查以下字段：

```json
{
  "heartbeat_channel": 10,
  "note_map": {
    "36": "S1",
    "38": "S2"
  },
  "default_material_rule": {
    "mode": "fixed",
    "package_ids": ["patient_a"],
    "gain_db": 0.0
  },
  "mix": {
    "mode": "event_window_relative",
    "music_gain_db": -4.0,
    "heartbeat_over_music_db": 4.0,
    "maximum_heartbeat_boost_db": 12.0,
    "event_window_ms": 250,
    "true_peak_ceiling_dbtp": -1.0,
    "publish_stems": false
  }
}
```

注意：

- 批量适配测试强制要求 `mix.mode` 为 `event_window_relative`。
- `heartbeat_channel` 使用人类习惯的 1–16 编号，鼓通道通常为 10。
- `note_map` 必须与 Stage 2 输出一致；部分旧测试 MIDI 使用音符 42 表示 S1。
- `package_ids` 必须与命令行中 `--heartbeat-package` 左侧的 ID 完全一致。
- 测试器会在临时 plan 中启用 stems，不会改写原始 plan。

## 5. 先运行离线测试

在接入真实音频前，先确认代码逻辑：

```powershell
D:\conda\python.exe -m unittest `
  stage3_midi_renderer.tests.test_batch_validator `
  stage3_midi_renderer.tests.test_package_renderer `
  stage3_midi_renderer.tests.test_heartbeat_conditioning `
  stage3_midi_renderer.tests.test_renderer -v
```

结尾应显示 `OK`。这一步不证明真实 MIDI 一定合格，只证明验证器、渲染边界和错误处理没有
已知回归。

## 6. 手动测试一首 MIDI

建议先用一首 MIDI 验证路径和 plan：

```powershell
D:\conda\python.exe -m stage3_midi_renderer batch-validate `
  --input-midi "D:\stage2_outputs\song01_complete.mid" `
  --general-sf2 "D:\soundfonts\GeneralUser-GS.sf2" `
  --render-plan ".\stage3_batch_validation_plan.json" `
  --heartbeat-package "patient_a=D:\packages\patient_a\heartbeat_package" `
  --output-dir ".\stage3_midi_renderer\outputs\manual-test-song01" `
  --fluidsynth "D:\tools\fluidsynth\bin\fluidsynth.exe"
```

若使用两个 package，可重复参数：

```powershell
--heartbeat-package "patient_a=D:\packages\patient_a\heartbeat_package" `
--heartbeat-package "patient_b=D:\packages\patient_b\heartbeat_package"
```

如果输出目录已经存在，程序会拒绝覆盖。确认旧结果不再需要后，在命令末尾添加 `--force`。

## 7. 批量测试一个目录

目录会被递归搜索，所有 `.mid` 和 `.midi` 都会进入测试：

```powershell
D:\conda\python.exe -m stage3_midi_renderer batch-validate `
  --input-dir "D:\stage2_outputs\completed_midis" `
  --general-sf2 "D:\soundfonts\GeneralUser-GS.sf2" `
  --render-plan ".\stage3_batch_validation_plan.json" `
  --heartbeat-package "patient_a=D:\packages\patient_a\heartbeat_package" `
  --output-dir ".\stage3_midi_renderer\outputs\manual-batch-test" `
  --fluidsynth "D:\tools\fluidsynth\bin\fluidsynth.exe"
```

也可以重复 `--input-midi`，只测试指定文件：

```powershell
--input-midi "D:\stage2_outputs\song01.mid" `
--input-midi "D:\stage2_outputs\song02.mid"
```

单首失败不会终止整个批次。

## 8. 查看输出

输出目录结构为：

```text
manual-batch-test/
├── batch_validation_report.json
├── batch_validation_report.csv
└── renders/
    └── <MIDI文件名-输入哈希>/
        ├── final_mix.wav
        ├── music_stem.wav
        ├── heartbeat_stem.wav
        ├── heartbeat_event_assignments.csv
        └── stage3_render_manifest.json
```

先查看 `batch_validation_report.csv`。每行对应一首 MIDI，重点字段包括：

| 字段 | 含义 |
| --- | --- |
| `status` | 该 MIDI 是否通过全部自动验收 |
| `failures` | 失败原因；多个原因以分号分隔 |
| `integrated_lufs` | 最终混音的整曲综合响度 |
| `true_peak_dbtp` | 4 倍过采样估计的最终真峰值 |
| `heartbeat_over_music_db` | 心跳窗口内，心跳相对音乐的中位 RMS 差 |
| `heartbeat_event_count` | 实际渲染的心跳事件数 |

默认合格条件：

- `integrated_lufs` 在 `-18` 至 `-12 LUFS`。
- `true_peak_dbtp` 不高于 `-1 dBTP`，允许 `0.1 dB` 数值估计误差。
- `heartbeat_over_music_db` 在 `+2` 至 `+8 dB`。
- `midi_timing_modified` 为 `false`。
- 没有渲染错误、缺失素材或心跳事件错误。

这些默认响度范围是项目验收范围，不是所有发行平台的统一强制值。若课程展示环境或目标平台有
明确要求，应在命令行调整阈值并记录理由。

## 9. 必须进行的听感检查

自动指标通过后，依次试听：

1. `music_stem.wav`：检查通用 SoundFont 是否错音色、缺乐器或产生异常噪声。
2. `heartbeat_stem.wav`：检查是否存在爆破、截断、沙沙声、错用 S1/S2 或事件重叠。
3. `final_mix.wav`：检查心跳是否清楚但不遮盖旋律，以及安静段和密集段是否都协调。

建议至少使用：

- Windows Media Player 或可靠的桌面播放器。
- 项目最终展示使用的扬声器。
- 一副有线耳机。

如果只在某一设备出现沙沙声，应保留测试 WAV，并记录设备、播放器、音量和接口；不要仅凭单一
播放器判断文件损坏。

## 10. 常见失败原因

### `integrated_lufs_out_of_range`

整曲过小或过响。当前 Stage 3 会保护相对平衡和真峰值，但不会自动保证所有歌曲达到同一 LUFS。
如果大量 MIDI 都偏小，需要增加最终 LUFS 归一化，而不是无限提高心跳增益。

### `true_peak_above_limit`

最终真峰值超过验收上限。先确认 plan 的 `true_peak_ceiling_dbtp`，再检查是否使用了正确版本的
Stage 3。不要用硬削波代替峰值保护。

### `heartbeat_balance_out_of_range`

心跳相对音乐过小或过大。可调整：

- `heartbeat_over_music_db`
- `maximum_heartbeat_boost_db`
- `event_window_ms`
- `music_gain_db`

修改后必须重新测试整批 MIDI，不能只测试一首。

### `render_error: ... unsupported note`

心跳通道出现 `note_map` 未定义的音符。应核对 Stage 2 输出和 plan，不能让 Stage 3 猜测或移动
事件。

### `render_error: ... no package ... S1/S2`

被 plan 选中的 heartbeat package 缺少该类样本，或者 package ID 绑定错误。

### `render_error: ... no music / no heartbeat`

输入不是合规的完整 MIDI。回到 Stage 2 修正，不要在 Stage 3 自动补事件。

### FluidSynth 或 SoundFont 错误

检查 `--fluidsynth`、`--general-sf2` 路径，以及 SoundFont 是否覆盖 MIDI 使用的 program/bank。

## 11. 调整验收阈值

示例：要求 `-17` 至 `-13 LUFS`、心跳高出音乐 `3` 至 `6 dB`：

```powershell
--min-integrated-lufs -17 `
--max-integrated-lufs -13 `
--max-true-peak-dbtp -1 `
--min-heartbeat-over-music-db 3 `
--max-heartbeat-over-music-db 6
```

改变阈值前应记录测试目的、播放环境和决定人，避免为了让报告变绿而放宽标准。

## 12. 推荐的正式验收流程

1. 固定 Python、FluidSynth、通用 SF2、heartbeat package 和 render plan 版本。
2. 保存所有输入文件哈希。
3. 运行离线测试。
4. 用一首已知 MIDI 做冒烟测试。
5. 对全部 Stage 2 完整 MIDI 运行 `batch-validate`。
6. 检查 JSON/CSV，解决所有 `FAIL`。
7. 对通过的文件进行多设备人工试听。
8. 保存最终报告和 Stage 3 manifest，但不要把患者派生音频提交到公开 Git。

测量实现依据 [ITU-R BS.1770-5](https://www.itu.int/rec/R-REC-BS.1770-5-202311-I/en)
和 [EBU R128 v5.0](https://tech.ebu.ch/publications/r128)。项目使用的真峰值是 4 倍过采样估计；
正式发行前建议再用经过认证的响度表复核。
