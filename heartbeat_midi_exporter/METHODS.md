# 方法说明

## 数据来源与时序

导出器以 `tempo_bar_renderer` 的 JSON/CSV 审计数据为唯一时序来源，不再次估计 S1/S2。每个事件按

`tick = round(time_s × target_bpm / 60 × PPQ)`

映射到 MIDI tick。默认 PPQ 为 480，音符长度固定为八分音符（240 tick）；一个文件只含一个 4/4、3/4 或 2/4 小节。MIDI 采用格式 1：轨道 1 保存速度与拍号元事件，轨道 2 保存通道 10 的心音音符。

默认 S1→MIDI 36、力度 100；S2→MIDI 38、力度 85。`uniform` 模式便于生成整齐、可编辑的乐谱；`source` 模式根据两类事件的中位源幅度计算 45–115 的力度，同时避免把录音中单次异常峰直接写入谱面。

## 患者音色

对每类事件，程序从事件表中选择 `exemplar_quality_score` 最高、幅度次优先的真实事件。采样窗与心音提取模块 v1.3.0 一致：峰前 30 ms，S1 峰后 180 ms，S2 峰后 150 ms，并加 6 ms 两端淡化。随后仅执行去直流、S1/S2 共用增益归一化到 −3 dBFS，以及 SciPy 多相重采样至 44.1 kHz。共用增益保持两类心音之间的真实相对响度。

正式 SF2 仅包含 bank 128/program 0 的 `Heartbeat Perc` 预设。这样 Stage 3 在通用音源之后加载患者心跳音源时，只替换通道 10 的打击乐预设，不会覆盖 bank 0 的钢琴等旋律乐器。每个样本为非循环单声道，原始音高和覆盖键均固定到其 MIDI 音符。

## 可重复性与完整性

程序在开始和结束时计算分析 JSON、事件 CSV、源 WAV 的 SHA-256；源 WAV 还必须匹配节拍报告内的哈希。任一输入在导出期间发生变化就终止。输出 JSON 逐项记录事件时间、tick、音符、力度、所选采样位置、软件版本、输入与输出哈希。

格式依据：[Standard MIDI Files](https://midi.org/standard-midi-files) 与 [SoundFont 2.04 Technical Specification](https://www.synthfont.com/sfspec24.pdf)。
