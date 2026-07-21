# MIDI-GPT 后置心跳渲染器（非正式流程实验）

> 2026-07-20 架构更正：本实验实现不属于正式三阶段流程，请勿作为 Stage 3 入口。正式流程由 Stage 2 生成完整心跳鼓轨，Stage 3 只对已经包含心跳鼓轨的完整 MIDI 统一渲染混音。权威说明见 [`../docs/三阶段职责边界.md`](../docs/三阶段职责边界.md)。

本目录保存已经实现过的历史实验：在 MIDI-GPT 完成后读取最终鼓轨，再由 `heartbeat_processing_plan.json` 与 `heartbeat_package` 重新生成独立心跳音频。该流程已被 2026-07-20 的职责决定废弃，不得接入正式 Stage 3；正式心跳鼓轨应在 Stage 2 生成，Stage 3 统一渲染完整 MIDI。

## 最重要的命令

在项目根目录运行：

```powershell
D:\conda\python.exe -m stage1_story_agent render-heartbeat `
  --final-midi ".\midigpt_outputs\final.mid" `
  --heartbeat-plan ".\stage1_story_agent\outputs\story-001-heartbeat\heartbeat_processing_plan.json" `
  --heartbeat-package ".\heartbeat_stage1\outputs\某次运行\heartbeat_package" `
  --output-dir ".\heartbeat_post_renderer\outputs\story-001"
```

默认读取 General MIDI 通道 10 上的 kick（MIDI note 36）。如果最终骨架使用其他鼓音，可重复指定：

```powershell
--trigger-note 36 --trigger-note 35
```

如果同一 MIDI 通道存在多条鼓轨，可再用零起始的 `--track-index` 限定原始 MIDI track。输出目录已存在时必须加 `--force`。

## 输入职责

- `final.mid`：时间权威。支持 MIDI format 0/1、PPQ、跨段 Set Tempo；拒绝 SMPTE。
- `heartbeat_processing_plan.json`：由 Stage 1 Agent 发布，提供连续小节范围、逐段 BPM、预计拍点和 `heart_sounds`。
- `heartbeat_package`：由 `heartbeat_stage1` 生成。渲染器逐个验证样本 SHA-256，并使用 CSV 中的 S1→S2 时间差。

严格模式会逐个比较最终 kick 与 Agent 计划。数量不一致、拍点漂移、段内意外变速、拍号不一致或样本哈希变化都会停止，不会静默补事件。

低张力段的每个触发使用配对轮换的 S1+S2；高张力段使用轮换 S1。MIDI velocity 只转换成线性增益。真实样本不拉伸、不变调，整轨最后统一限制到约 −1 dBTP。

## 输出

```text
heartbeat_track.wav
heartbeat_track.mid
heartbeat_events.csv
heartbeat_track_time_energy.png
heartbeat_track_time_energy.svg
heartbeat_render_manifest.json
```

实验清单会记录输入哈希、MIDI tempo map、选择的通道/音符、S1/S2 数量、音频策略和输出哈希。这些产物仅供历史验证，不属于正式交付；正式流程不生成独立心跳成品 WAV，也不再把它与主音乐 WAV 二次叠加。

## 测试

```powershell
D:\conda\python.exe -m unittest discover -s .\heartbeat_post_renderer\tests -v
```
