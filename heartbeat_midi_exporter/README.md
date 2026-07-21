# Heartbeat MIDI Exporter

该模块把 `tempo_bar_renderer` 已生成并审核过的节拍事件转换成 MuseScore 可打开的 MIDI，并同时制作保留患者真实 S1/S2 音色的 SoundFont。它不会重新检测心音，也不会修改前三个模块或任何输入文件。

## 输入

输入是节拍小节输出组中的 `*_bar_analysis.json`。程序会自动读取同目录下报告指定的 `*_bar_events.csv`，并校验报告记录的原始 WAV SHA-256。推荐输入 `--pair-rhythm even --beat-peak both` 生成的报告，这样导入 MuseScore 后就是均匀交替的 S1/S2 八分音符。

## 启动

在项目根目录打开 PowerShell，使用项目当前依赖完整的 Conda Python：

```powershell
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py "D:\path\xxx_bar_analysis.json"
```

不要把某个 WAV 直接传给本模块；本模块需要 JSON 与 CSV 中已经确定的 BPM、拍号和事件时间。

默认产生同一组文件夹：

- `*_heartbeat_4-4.mid`、`*_heartbeat_3-4.mid`、`*_heartbeat_2-4.mid`：三个一小节 MIDI。
- `*_heartbeat.sf2`：患者专属、仅含 percussion bank 128/program 0 的 SoundFont；S1 映射 MIDI 36，S2 映射 MIDI 38。只保留打击乐预设是为了让 Stage 3 后加载该音源时不会覆盖钢琴等普通 bank 0 乐器。
- `*_soundfont_s1.wav`、`*_soundfont_s2.wav`：SF2 实际使用的可审计采样。
- `*_midi_mapping.json`：输入哈希、事件到 tick 的逐项映射、参数、输出哈希和依赖来源。

默认 MIDI 采用格式 1、PPQ 480、通道 10（文件内部编号 9），S1/S2 均记为八分音符。S1 力度 100，S2 力度 85；这只影响 MIDI 播放力度，不改变真实采样波形。

## 导入 MuseScore Studio

1. 用“文件 → 打开”选择需要拍号的 `.mid`。
2. 在 MIDI 导入后的谱面中确认速度和拍号。
3. 安装或加载同组的 `.sf2`，打开混音器，把心跳谱表声音改为 `Heartbeat Perc`。
4. 如果只需要乐谱记号而不需要患者音色，可保留 MuseScore 默认打击乐音色。

MuseScore Studio 可以打开 MIDI 文件，且支持 SF2/SF3 SoundFont；不同版本中音色加载入口的名称可能略有差异，详见 [打开 MIDI](https://handbook.musescore.org/file-management/opening-and-saving-scores) 与 [SoundFonts](https://handbook.musescore.org/sound-and-playback/soundfonts)。

## 常用选项

```powershell
# 按源事件相对能量计算力度，而不是固定 100/85
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py "D:\path\xxx_bar_analysis.json" --velocity-mode source

# 自定义音符和力度
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py "D:\path\xxx_bar_analysis.json" --s1-note 36 --s2-note 38 --s1-velocity 110 --s2-velocity 92

# 只生成一个 4/4 MIDI：每拍一个 S1，每个 S1 为四分音符（1拍）
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py "D:\path\xxx_bar_analysis.json" --event-mode s1 --meters 4/4 --note-division 4 --midi-only
```

完整选项：

```powershell
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py -h
```

## 测试

```powershell
D:\conda\python.exe -m unittest discover -s .\heartbeat_midi_exporter\tests -v
```

本模块只依赖 NumPy 与 SciPy；MIDI 和 SF2 容器由脚本按照公开规范直接写入，不要求安装 `mido`、FluidSynth 或 Polyphone。
