# 第三方依赖与规范

- NumPy：数组、线性增益、事件叠加和数值统计。https://numpy.org/
- SciPy：WAV 读取及 `scipy.signal.resample_poly` 真峰值过采样。https://scipy.org/
- JSON Schema Draft 2020-12：`rhythm_plan.schema.json` 的机器可读接口声明。https://json-schema.org/draft/2020-12
- MIDI Association, *Standard MIDI Files*：心跳 token MIDI 的文件、轨道、速度、拍号和音符事件结构。https://midi.org/standard-midi-files

本模块没有复制或嵌入任何第三方音色、录音或模型。患者心音事件只来自用户提供并经第一模块处理的录音。运行时实际版本与输入输出哈希写入 `heartbeat_manifest.json`。
