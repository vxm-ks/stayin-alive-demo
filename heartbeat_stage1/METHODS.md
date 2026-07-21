# 第一阶段方法说明

## 处理链

原始 WAV 首先由 `heart_sound_processor.py` v1.3.0 完成 stable 区间选择、S1/S2 检测、周期质量筛选、事件重建和增强，第一阶段只选择其 `regularized_events_enhanced.wav` 作为节奏渲染来源。

用户或 LLM 提供的计划会被规范化为四分音符拍单位。`offset_beats` 按目标 BPM 转换为秒；`offset_seconds` 保留绝对间隔。短模式可在小节中整数次重复。程序展开完整小节后进行循环边界在内的最小事件间隔检查，低于80 ms直接拒绝，80–120 ms记录尾部重叠警告。

## 真实事件选择

`tempo_bar_renderer.inspect_wav` 从 `regularized_events_enhanced` 建立质量评分后的 S1/S2事件库。默认 `paired_rotate` 按检测周期编号配对 S1/S2，同一模式重复优先使用同一个周期的两类事件；下一次重复轮换到另一合格周期。若没有完整配对，才回退到逐类轮换并记录警告。

事件以检测到的起音位置对齐，只在小节边界被截断时施加5–6 ms淡化。计划中的 `gain_db` 是纯线性乘法；整条小节最后统一做4倍多相过采样真峰值估计和增益归一化。整个阶段不做时间拉伸、变调、二次降噪、动态压缩或频谱分层。

## 可重复性

输出清单记录原始 WAV、`regularized_events_enhanced`、处理报告、计划和所有主要输出的 SHA-256，并记录 Python、NumPy、SciPy及两个被复用模块的版本。计划规范化后的哈希进入输出文件夹名称，避免不同计划混入同一分析组。

心音检测与增强的学术依据继承自 [`heart_extraction/METHODS.md`](../heart_extraction/METHODS.md)，节拍事件边界与真峰值方法继承自 [`tempo_bar_renderer/METHODS.md`](../tempo_bar_renderer/METHODS.md)。
