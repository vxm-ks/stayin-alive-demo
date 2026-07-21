# Third-party software and attribution

运行依赖与 `heart_extraction` 一致：

- NumPy 2.4.4 — BSD-3-Clause — 数组与数值计算 — https://numpy.org/
- SciPy 1.17.1 — BSD-3-Clause — WAV I/O、滤波及信号处理 — https://scipy.org/
- Pillow 12.2.0 — HPND — PNG 图像输出（由 `audio_diagnostics.py` 使用）— https://python-pillow.org/

本模块还导入同一项目中的 `heart_extraction`，以复用已审计的 WAV 写入、事件感知降噪和图表实现；它不是外部依赖。

Rubber Band、librosa、WSOLA 实现及任何 Transformer 模型均未被本模块导入、链接或分发。它们只在方法文档中作为未采用方案讨论。
