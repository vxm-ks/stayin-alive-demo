# Tempo Bar Renderer

本模块的输入不是原始录音，也不是分析 JSON，而是 `heart_extraction` 已生成的 9 个 WAV 中任意一个：Stable、Authentic、Regularized 各自的 Base、Clean 或 Enhanced。

程序首先只读所选 WAV，自动检测当前 BPM、S1/S2 时序和稳定事件，显示建议调整范围；用户在终端输入目标 BPM 后，程序分别生成一个 4/4、3/4、2/4 小节。`both` 每拍对应完整 S1–S2，单峰模式每拍只放一个经过边界修复的 S1 或 S2。

## 交互运行

从项目根目录运行：

```powershell
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\样本_regularized_events_clean.wav"
```

终端流程示例：

```text
status=READY
Select beat peak [both/s1/s2] (default both): s1
beat_peak_mode=s1
detected_bpm=87.21
recommended_bpm_range=69.8-109.0
technical_bpm_range=30.0-300.0
请输入目标 BPM [69.8-109.0]: 100
status=PASS
```

程序允许用户选择建议范围之外的 BPM，但如果相邻 S1/S2 起点会被压缩到 80 ms 以下，就会拒绝生成，以防明显事件重叠。

`both` 模式执行上述 S1/S2 间距保护；单独 S1 或 S2 时每拍只有一个峰，技术上限采用 300 BPM。

程序还会比较最终周期拟合与最强自相关候选；二者相差超过 18% 且候选相关性足够强时，会在候选 BPM 附近自动重新检测，从而排除常见的半速/倍速误判。校正过程写入 JSON。

## 非交互运行

批处理或已经确定目标 BPM 时：

```powershell
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\样本_regularized_events_clean.wav" --target-bpm 100
```

直接指定节拍峰：

```powershell
# 每拍只保留 S1
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1

# 每拍只保留 S2，并将 S2 对齐拍点
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s2
```

`--beat-peak both` 保持原模式：S1 位于拍点，S2 保留原心动周期相位。完全交互运行时先选择峰模式，再显示对应的建议范围并选择 BPM；指定 BPM 但未指定峰模式时默认 `both`。

S1/S2 对的音乐性节奏：

```powershell
# 自然模式（默认）：保留录音中 S2 的生理相位
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak both --pair-rhythm natural

# 等间隔模式：S1 在拍点，S2 在半拍处
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak both --pair-rhythm even
```

- `natural`：S2 位于 `nT + pT`，其中 `p` 是输入心音检测出的 S2 相位。
- `even`：S2 位于 `nT + 0.5T`，因此 S1→S2 和 S2→下一 S1 的时长完全相等。
- `even` 只改变事件起点，不裁剪、不拉伸、不重采样 S1/S2 波形；每个事件仍使用真实心音片段。
- `even` 必须与 `--beat-peak both` 一起使用。单独选择 `s1` 或 `s2` 时没有可供等间隔排列的心音对，程序会明确拒绝。

默认 `natural` 保持旧版行为和旧文件名。`even` 输出会增加 `_even`，例如 `_bpm100_even_rotate_4-4.wav`，不会覆盖自然节奏结果。

事件库策略：

```powershell
# auto：单峰自动重复最佳事件，both 自动轮换事件（默认）
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1 --event-bank auto

# 强制轮换多个事件
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1 --event-bank rotate
```

`best` 重复质量评分最高的真实事件，适合作为稳定打击乐；`rotate` 保留多个事件的自然差异。单峰默认采用 `best`。

可使用 `--output-dir "D:\path\bar_results"` 修改输出父目录。

## 响度与电影化增强

`--loudness-mode` 提供三个互不覆盖的输出档位：

```powershell
# 保持 1.4 版行为：单峰 −3 dBFS，both −1 dBFS，仅线性增益
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1 --loudness-mode safe

# 并行压缩，并以 4 倍过采样估计真峰值后控制到约 −1 dBTP
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1 --loudness-mode loud

# 在 loud 的基础上加入只从当前心音派生的低频身体层和谐波层
python .\tempo_bar_renderer\tempo_bar_renderer.py "D:\path\输入.wav" --target-bpm 100 --beat-peak s1 --loudness-mode cinematic
```

- `safe`（默认）：不使用压缩或附加声部，确保旧调用和旧文件名兼容。
- `loud`：保留未经压缩的干声 attack，同时混入快速压缩分支，提高主体和衰减尾部的可闻度。
- `cinematic`：70% 干声、20% 由同一心音得到的 45–120 Hz 身体层、10% 由同一心音饱和派生的 120–900 Hz 谐波层，再进行较轻的并行压缩。没有鼓声、外部样本或低频振荡器。

`loud` 与 `cinematic` 会在目录名和文件名中分别加入 `_loud`、`_cinematic`，因此不会覆盖 `safe` 输出。每个拍号的 JSON 会记录处理前后样本峰值、4 倍过采样真峰值估计、全小节 RMS、有效区间 RMS、crest factor、压缩参数及派生声部参数。这里的 dBFS/RMS 是工程检查指标，不是临床量值，也不是完整节目响度 LUFS。

## 建议范围的含义

建议范围是项目的保守工程规则，不是医学诊断范围：

- 下界：检测 BPM 的 80%，且不低于 40 BPM。
- 上界：检测 BPM 的 125%，且不高于 180 BPM，也不高于 S1/S2 技术安全上限的 95%。
- 技术范围：最低 30 BPM；`both` 的上限由 S2 相位和 80 ms 最小事件起点间距计算，单独 S1/S2 模式的项目上限为 300 BPM。

建议范围、技术范围、检测置信度及规则原文都会写入输出 JSON。

## 输入保护与输出

程序不会在输入 WAV 所在文件夹写入内容。开始分析时记录输入 SHA-256，渲染结束后再次校验；若输入发生变化会报错。

默认输出到：

```text
tempo_bar_renderer\outputs\所选WAV文件名_bpm目标值_峰模式_节奏模式_事件库_bars\
```

- `_4-4_`、`_3-4_`、`_2-4_`：拍号，每个文件一个小节。
- 每次只生成三个音频。单峰默认使用 `_bpm100_s1_best_4-4.wav` 或 `_bpm100_s2_best_4-4.wav` 等名称；`both` 默认带 `_rotate_`。
- `--pair-rhythm even` 的目录和文件名增加 `_even`；默认 `natural` 不增加标签。
- `_time_energy.png/.svg`：三个输出 WAV 各自的时间—能量图，共三组 PNG/SVG。
- `_bar_events.csv`：输出事件时间、`pair_rhythm` 及其在输入 WAV 中的源时间，可用于未来打击乐 token 化。
- `_bar_analysis.json`：输入哈希、建议范围、目标 BPM、事件参数、质量数据、输出文件及方法引用。

模块不会再次降噪。事件切片会在检测峰之前寻找真实 attack，吸附到附近过零点，保留 5 ms 前置段并施加短余弦边界窗；主体不会拉伸或重采样。`safe` 的单峰输出保留 −3 dBFS 样本峰值余量，`both` 保持 −1 dBFS；`loud/cinematic` 使用 4 倍过采样真峰值估计控制到约 −1 dBTP。JSON 会以 0.03 FS 为阈值报告边界跳变质量。

## 测试

```powershell
python -m unittest discover -s .\tempo_bar_renderer\tests -v
```

方法依据见 [`METHODS.md`](METHODS.md)，依赖许可见 [`THIRD_PARTY.md`](THIRD_PARTY.md)。
