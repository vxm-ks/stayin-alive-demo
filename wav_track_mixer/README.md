# WAV Track Mixer

将两个 WAV 从时间轴起点叠加为一个音轨。较短音轨默认自动循环到较长音轨结束，保证两轨在整个共同时间段内持续并存。程序会自动将较低采样率重采样到较高采样率、将单声道扩展到另一输入的声道数，并在混合峰值过高时整体衰减，避免数字削波。输入文件不会被修改，输出为兼容性较好的 16-bit PCM WAV。

## 安装

在项目根目录运行：

```powershell
python -m pip install -r .\wav_track_mixer\requirements.txt
```

## 使用

最简单的合并方式：

```powershell
python .\wav_track_mixer\wav_track_mixer.py "D:\audio\voice.wav" "D:\audio\music.wav" "D:\audio\mixed.wav"
```

分别调节两轨音量，并让第二轨延后 1.5 秒开始：

```powershell
python .\wav_track_mixer\wav_track_mixer.py "voice.wav" "music.wav" "mixed.wav" --gain-a-db 2 --gain-b-db -8 --offset-b 1.5
```

常用选项：

- `--gain-a-db`、`--gain-b-db`：两轨各自的线性增益，单位为 dB。
- `--offset-b`：第二轨延迟开始的秒数。
- `--sample-rate 48000`：指定输出采样率；默认采用两输入中较高的采样率。
- `--ceiling-dbfs -1`：防削波峰值上限，默认为 -1 dBFS。
- `--normalize`：除了压低过响音频，也将较安静的结果提升到峰值上限。
- `--no-loop-shorter`：让每个音轨只播放一次；较短音轨结束后，其余位置为静音。默认不加此选项时会循环较短音轨。

程序接受 SciPy 能读取的整数 PCM 或浮点 WAV。声道数相同时直接混合；单声道可以自动扩展成立体声或多声道。两个不同的非单声道布局不会被程序猜测转换，以免错误映射声道。

也可以在 Python 中调用：

```python
from wav_track_mixer import mix_wav_tracks

result = mix_wav_tracks(
    "voice.wav",
    "music.wav",
    "mixed.wav",
    gain_b_db=-8.0,
    offset_b_s=1.5,
)
print(result.duration_s, result.peak_gain_db)
```

## 测试

```powershell
python -m unittest discover -s .\wav_track_mixer\tests -v
```
