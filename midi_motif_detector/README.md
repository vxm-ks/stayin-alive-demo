# MIDI Motif Detector

本模块分析 Standard MIDI File（`.mid`/`.midi`），估计第一段音乐动机的起止位置，并报告后续重复或移调重复。输出同时包含秒数、四分音符拍坐标、小节位置、音符序列、匹配位置和置信度。

## 方法概览

1. 排除鼓轨，默认根据平均音高、单声部程度和音符数量选择最可能的旋律轨。
2. 同一时刻存在多个音符时取最高音，得到可比较的旋律轮廓；也可以用 `--track` 明确选择轨道。
3. 从第一个旋律事件开始，在 0.5–4 小节范围内枚举候选边界。
4. 比较相对音程、相对起音位置和时值，允许整体移调及有限的节奏伸缩。
5. 优先选择紧邻重复、接近标准小节长度且具有高相似度的最早模式。
6. 找不到重复时，使用休止与小节边界给出低置信度乐句边界。

自动检测是工程估计，并不等同于唯一的音乐学分析。复调编曲应先用 `--list-tracks` 检查轨道，再明确指定旋律轨。

## 安装

```powershell
python -m pip install -r .\midi_motif_detector\requirements.txt
```

## 使用

自动选择旋律轨并分析：

```powershell
python .\midi_motif_detector\midi_motif_detector.py "D:\music\example.mid"
```

默认报告写入：

```text
midi_motif_detector\outputs\example_motif_analysis.json
```

先查看可用轨道：

```powershell
python .\midi_motif_detector\midi_motif_detector.py "example.mid" --list-tracks
```

明确分析编号为 2 的轨道：

```powershell
python .\midi_motif_detector\midi_motif_detector.py "example.mid" --track 2
```

调整候选长度和相似度阈值：

```powershell
python .\midi_motif_detector\midi_motif_detector.py "example.mid" --min-bars 0.5 --max-bars 4 --min-notes 4 --similarity 0.78
```

终端输出示例：

```text
status=PASS
track=2:Lead Melody
method=recurring_pitch_interval_and_rhythm_pattern
motif_start_s=0.500000
motif_end_s=2.500000
motif_start_beat=1.000000
motif_end_beat=5.000000
confidence=0.970
matches=2
report=...\example_motif_analysis.json
```

JSON 中的 `sounding_end_s` 是最后一个动机音符实际结束的时间；`end_s` 是结构边界，通常是下一次重复或下一乐句的起点。编辑和切片时应根据用途选择相应字段。

## Python API

```python
from midi_motif_detector import detect_first_motif

report = detect_first_motif("example.mid", track_index=2)
print(report["motif"]["start_s"], report["motif"]["end_s"])
```

## 测试

```powershell
python -m unittest discover -s .\midi_motif_detector\tests -v
```
