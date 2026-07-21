# 第三方软件、规范与引用

## 运行依赖

- NumPy：数组与采样域数值运算。https://numpy.org/
- SciPy：WAV 读写与 `scipy.signal.resample_poly` 多相重采样。https://scipy.org/

版本会写入每组 `*_midi_mapping.json`。本模块没有复制第三方源代码，也没有捆绑第三方音色或样本。

## 文件规范

- MIDI Association, *Standard MIDI Files*：MIDI 文件头、轨道、delta time、速度、拍号和音符事件。https://midi.org/standard-midi-files
- E-mu Systems / Creative Technology, *SoundFont 2.04 Technical Specification*：RIFF `sfbk`、INFO/sdta/pdta 与 Hydra 表结构。https://www.synthfont.com/sfspec24.pdf

## 目标软件

- MuseScore Studio Handbook, *Opening and saving scores*：MIDI 打开/导入。https://handbook.musescore.org/file-management/opening-and-saving-scores
- MuseScore Studio Handbook, *SoundFonts*：SF2/SF3 安装和选择。https://handbook.musescore.org/sound-and-playback/soundfonts

研究报告中应同时引用实际使用的软件版本、上述规范，并注明 SoundFont 样本来自研究对象的已授权数字听诊器录音。
