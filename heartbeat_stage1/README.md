# Heartbeat Stage 1

第一阶段总入口把患者原始 WAV 转换成可供第三阶段直接使用的 `heartbeat_package`。它严格复用现有 `heart_sound_processor` 的 `regularized_events_enhanced` 标准，并让用户或 LLM 通过结构化 `rhythm_plan.json` 决定 BPM、拍号和 S1/S2 排列。

## 输入与输出

输入：

1. 数字听诊器原始 WAV。
2. 符合 [`rhythm_plan.schema.json`](rhythm_plan.schema.json) 的节奏计划。

输出组：

```text
样本编号_采集时间_计划名_计划哈希_stage1/
├─ heart_processing/                 完整 regularized_events_enhanced 处理及审计结果
└─ heartbeat_package/
   ├─ event_samples/                 本小节实际使用的真实 S1/S2 事件
   ├─ heartbeat_bar.wav              指定节奏型的一小节心音
   ├─ heartbeat_bar.mid              通道10可视化 MIDI
   ├─ heartbeat_events.csv           逐事件来源与时间映射
   ├─ heartbeat_bar_time_energy.png
   ├─ heartbeat_bar_time_energy.svg
   ├─ rhythm_plan.json               已规范化、验证的计划
   └─ heartbeat_manifest.json        输入输出哈希、版本、参数与警告
```

## 运行

在项目根目录执行：

```powershell
D:\conda\python.exe .\heartbeat_stage1\heartbeat_stage1.py `
  "D:\path\raw_patient.wav" `
  ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json"
```

示例计划产生 103.15 BPM、4/4 一小节：每拍 S1 位于拍点，S2 位于半拍。

## 用户/LLM接口

LLM 只生成计划，不接触音频样本。核心字段：

```json
{
  "schema_version": "1.0",
  "plan_id": "custom_pattern",
  "meter": "4/4",
  "target_bpm": 103.15,
  "pattern_length_beats": 1.0,
  "repeat_to_fill_bar": true,
  "events": [
    {"type": "S1", "offset_beats": 0.0},
    {"type": "S2", "offset_beats": 0.5, "gain_db": -2.0}
  ]
}
```

每个事件必须使用 `offset_beats` 或 `offset_seconds` 之一。程序支持任意拍号和规则事件序列，但会拒绝：

- 拉伸或变调请求；
- 小于80 ms的事件间隔；
- 越出模式或小节边界的事件；
- 无法整除小节却要求自动重复的模式；
- 未声明字段、非法力度或非法 BPM。

默认 `paired_rotate` 会尽量让同一次模式重复中的 S1/S2 来自同一个真实检测周期，并在合格周期间轮换。最终只做事件线性增益和统一真峰值归一化，不增加降噪、压缩或电影化声部。

## 测试

```powershell
D:\conda\python.exe -m unittest discover -s .\heartbeat_stage1\tests -v
```
