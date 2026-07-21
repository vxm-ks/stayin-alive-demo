# Tempo Bar Renderer 方法说明

## 1. 输入边界与只读策略

输入必须是 `heart_extraction` 输出的 9 个 WAV 之一，而不是原始录音或分析 JSON。新模块不调用第一模块的输出写入流程，不修改输入 WAV，也不在输入目录创建伴随文件。程序在处理前后核对输入 SHA-256，并将路径、哈希、采样率和时长写入结果 JSON。

## 2. 重新检测当前节奏

由于用户可从 Base、Clean、Enhanced、Stable、Authentic 或 Regularized 中任选一个文件，新模块必须以实际选中的 WAV 为准重新检测 BPM。它复用 `heart_extraction` 中的带通、Hilbert/Shannon 包络、自相关周期估计、S1/S2 时序约束、周期质量筛选和 stable 区间选择，但不覆盖第一模块的任何内容。检测方法与引用见 [`heart_extraction/METHODS.md`](../heart_extraction/METHODS.md)。

通过质量筛选的 stable S1/S2 被截成真实事件示例。检测时间通常接近能量峰而非物理 attack；若直接从该采样点开始，会造成静音到高幅值的边界跳变。程序因此在峰前 30 ms 内使用 2 ms 平滑绝对值包络寻找持续起点，阈值取背景中位值的两倍与峰包络 15% 的较大者，再在 ±2 ms 内吸附到最近过零点。事件保留约 5 ms 前置段，并使用短 raised-cosine 淡入和 10 ms 淡出；主体不插值、不拉伸、不重采样。

起点检测的设计背景参考 Bello et al., “A Tutorial on Onset Detection in Music Signals,” *IEEE Transactions on Speech and Audio Processing*, 2005, [DOI: 10.1109/TSA.2005.851998](https://doi.org/10.1109/TSA.2005.851998)。边界加窗参考 Harris, “On the Use of Windows for Harmonic Analysis with the Discrete Fourier Transform,” *Proceedings of the IEEE*, 1978, [DOI: 10.1109/PROC.1978.10837](https://doi.org/10.1109/PROC.1978.10837)。

循环中若交替使用不同事件音色，自相关可能同时在一个周期和两个周期处形成强峰。程序比较最终周期拟合 BPM 与最强自相关候选：候选相关性至少为 0.45、相对差异超过 18% 时，将搜索范围收窄到候选 BPM 的 75%–125% 后重新分析。该步骤用于排除半速/倍速错误，并完整记录初始值、候选强度和校正值。

## 3. 建议 BPM 范围

设检测 BPM 为 `B`，原始 S2 相位比例为 `p = (S1→S2) / 心动周期`。自然模式使用 `p_render = p`；等间隔模式使用 `p_render = 0.5`。项目建议范围为：

- `lower = max(40, 0.80B)`；
- `both` 模式：`technical_max = min(300, 60 × min(p_render, 1-p_render) / 0.080)`；
- `s1` 或 `s2` 单峰模式：`technical_max = 300`；
- `upper = min(180, 1.25B, 0.95 × technical_max)`。

这是为保留听感连续性制定的透明工程启发式，不是临床正常心率结论。用户可以选择建议范围以外的值；但目标 BPM 造成任意相邻 S1/S2 起点少于 80 ms 时，程序拒绝输出。80–120 ms 会生成并记录尾部可能重叠警告。

## 4. 小节构造

目标四分音符时长为 `T = 60 / target_BPM`：

- `both + natural`：每拍放置 S1，S2 放在 `nT + pT`；
- `both + even`：每拍放置 S1，S2 放在 `nT + 0.5T`；
- `s1`：只保留真实 S1，并将每个 S1 放在 `nT`；
- `s2`：只保留真实 S2，并将每个 S2 放在 `nT`。

在 `even` 中，相邻起点序列为 `S1(nT) → S2(nT+0.5T) → S1((n+1)T)`，两个间隔均为 `0.5T`。程序只修改事件在小节中的放置采样点，直接复用事件库中的原始波形数组；不改变片段长度，不对片段插值，也不调用 time stretching。该模式要求 `beat_peak=both`，因为单峰模式不存在 S1/S2 对。

第一个 S1 的真实 attack 对齐 `0 s` 时，其 attack 前的 5 ms 预留段位于小节边界之外，无法写入输出。如果截断后首个采样不为零，程序只对小节开头最多 5 ms 施加 raised-cosine 淡入并强制首采样为零，防止播放点击；后续事件和事件主体不受影响。CSV 以 `head_preroll_truncated_at_bar_start` 记录该情况。

因此单峰模式每拍只有一个瞬态。4/4、3/4、2/4 分别包含 4、3、2 个所选峰。采样点数取最接近的整数，因此实际时长与理论时长最多相差半个采样周期。

该过程属于事件重排，不是 time-scale modification。输入文件已经代表用户选定的 Base、Clean 或 Enhanced 等级，因此事件重排阶段不会再次滤波或降噪。`event-bank=auto` 在单峰模式重复综合置信度、峰值一致性和 RMS 一致性最高的事件，在 `both` 模式轮换合格事件；也可显式选择 `best` 或 `rotate`。

## 5. 响度与电影化增强

响度处理发生在完整小节组装之后，不改变事件时刻。`safe` 保持原行为：单峰仅线性归一到 −3 dBFS 样本峰值，`both` 归一到 −1 dBFS；不使用压缩或新声部。

`loud` 使用前馈并行压缩器。检测器为全波绝对值包络，attack/release 使用一阶指数记忆；超过阈值的增益缩减为

`gain_reduction_dB = −(1 − 1/ratio) × max(0, level_dB − threshold_dB)`。

参数为阈值 −18 dBFS、4:1、attack 0.2 ms、release 100 ms、make-up 9 dB、wet 30%。快速 attack 只作用于压缩分支；未经压缩的 dry 分支始终保留，因此原 S1/S2 瞬态仍存在。动态范围压缩的拓扑、时间常数及实现选择参考 Giannoulis, Massberg, and Reiss, “Digital Dynamic Range Compressor Design—A Tutorial and Analysis,” *Journal of the Audio Engineering Society*, 60(6), 2012, pp. 399–408，[AES E-Library](https://aes2.org/publications/elibrary-page/?id=16354)。具体参数是本项目的可审计工程设定，不宣称来自该论文。

`cinematic` 首先构造全部可追溯到输入心音的三个声部：70% 原 dry；20% 经过三阶 Butterworth 45–120 Hz 带通并 RMS 匹配的身体层；10% 经过 `tanh(2.2x)` 软饱和、减去原信号、再经 120–900 Hz 带通及 RMS 匹配的谐波层。RMS 匹配增益上限为 18 dB，防止在原信号缺少相应频带时无限放大噪声。随后使用同一前馈并行结构，参数为阈值 −18 dBFS、4:1、attack 0.2 ms、release 100 ms、make-up 8 dB、wet 25%。带通实现使用 SciPy 的 Butterworth SOS 滤波器；Butterworth 原始依据为 S. Butterworth, “On the Theory of Filter Amplifiers,” *Experimental Wireless and the Wireless Engineer*, 7, 1930, pp. 536–541。

`loud` 与 `cinematic` 最后用 `scipy.signal.resample_poly` 做 4 倍过采样以估计采样间峰值，再施加一次线性增益，使估计真峰值为 −1 dBTP。该步骤不是逐样本 brick-wall limiter；它通过预留真峰值余量避免为了响度直接硬削波。真峰值测量原则参照 [ITU-R BS.1770-5](https://www.itu.int/rec/r-rec-bs.1770/_page.print)。JSON 同时保存处理前后样本峰值、估计真峰值、全小节 RMS、有效区间 RMS、有效比例与 crest factor，避免只凭最终峰值判断听感响度。

`cinematic` 中没有鼓声、外部样本、低频振荡器或生成模型。所有派生声部都来自当前被选中的患者心音；但它属于审美性音色增强，不应被描述成未经改变的临床听诊录音。

## 6. 未采用的方法

本版本未调用 WSOLA、相位声码器、Rubber Band、librosa time-stretch 或 Transformer。WSOLA 的原始研究为 Roelands and Verhelst, “Waveform similarity based overlap-add (WSOLA) for time-scale modification of speech: structures and evaluation,” *Eurospeech 1993*, pp. 337–340. [DOI](https://doi.org/10.21437/Eurospeech.1993-59)。这些方法作为比较背景记录，但不应列为本程序实际依赖。

## 7. 可审计输出

三个拍号 WAV 均生成独立 PNG/SVG 时间—能量图。CSV 记录 `beat_peak_mode`、`pair_rhythm`、`event_bank_mode`、`loudness_mode`、真实起点相对峰值的偏移、事件质量评分、放置边界跳变和对齐起点跳变。JSON 同时保存原始和渲染 S2 相位、两个相邻间隔、是否保持生理相位、是否采用等间隔，以及未使用时伸缩/重采样的声明。每个拍号还报告最大边界跳变，验收阈值为 0.03 FS；超过阈值时标为 `WARNING`。
