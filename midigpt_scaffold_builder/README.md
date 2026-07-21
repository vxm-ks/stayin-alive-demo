# MIDI-GPT Scaffold Builder

第二阶段的确定性框架装配器。第二阶段没有 LLM。离线编译不调用任何规划模型、MIDI-GPT 或 checkpoint；它把第一阶段唯一 LLM 在同一次规划中产生的内容计划与结构化编排，以及后续得到的心音事件清单和 MuseCoco 动机清单，编译成 MIDI-GPT 0.3.2 可消费的 `Score` 与 `GenerationRequest`。

## 输入边界

- `content_plan.json`：第一阶段唯一规划 Agent 输出的 BPM、拍号、总小节和曲式段落；
- `music_plan.json`：同一次 Agent 规划输出的逐轨角色、心音逐段规则、动机放置和填充区域；
- `*_midi_mapping.json`：`heartbeat_midi_exporter` 产生的心音事件映射；
- `motif_manifest.json`：本模块冻结的 `0.1-draft` 动机清单，MIDI 路径相对清单文件解析。

计划中的小节使用 1-based 闭区间；输出给 MIDI-GPT 的 `TrackPrompt.bars` 自动转换成 0-based。心音和动机锚点使用空 `bars`、`ignore=false`，使其保留在条件上下文中。生成后仍必须由推理适配器逐事件比较保护内容。

两个计划文件是单次 LLM 规划的两个严格视图，不代表两个 LLM 阶段。装配器不读取故事、不修改音乐意图，也不根据实际动机重新规划；素材不符合计划时直接拒绝。

`music_plan.json` 的核心形态：

```json
{
  "schema_version": "0.1-draft",
  "story_id": "story-001",
  "ppq": 480,
  "tracks": [
    {"track_id": "heartbeat", "role": "heartbeat", "instrument": 0, "track_type": "drum", "behavior": "context"},
    {"track_id": "theme", "role": "melody", "instrument": 0, "track_type": "melodic", "behavior": "generate"}
  ],
  "motif_placements": [
    {"placement_id": "place-A", "motif_id": "motif-A", "target_track_id": "theme", "target_section_id": "S1", "target_bar": 1, "repeat_count": 2, "protected": true}
  ],
  "heartbeat_arrangement": {
    "role": "protected_rhythm_anchor",
    "preserve_complete_events": true,
    "sections": [
      {"section_id": "S1", "bar_start": 1, "bar_end": 4, "intent": "克制", "pattern": {"mode": "hits_per_bar", "hit_unit": "s1_s2_pair", "hits_per_bar": 1, "beat_positions": [1.0], "velocity_scale": 0.65}}
    ],
    "unplanned_bars_policy": "error"
  },
  "midigpt": {
    "checkpoint": "yellow",
    "model_dim_bars": 4,
    "fill_regions": [{"track_id": "theme", "bar_start": 5, "bar_end": 8, "mode": "infill"}],
    "generation_config": {"temperature": 1.0, "seed": 20260717, "top_p": 0.95, "mask_mode": "attention"}
  }
}
```

每个曲式段落必须有且只有一条心音规则。`introduce/reprise` 段必须显式放置对应主题家族的动机；`variation/development` 段必须在某一生成轨上完整覆盖。受保护动机不能与填充小节重叠。

## 运行

```powershell
D:\conda\python.exe -m midigpt_scaffold_builder `
  --content-plan content_plan.json `
  --music-plan music_plan.json `
  --heartbeat-manifest heartbeat_midi_mapping.json `
  --motif-manifest motif_manifest.json `
  --output-dir outputs\story-001
```

输出目录原子创建，已存在时拒绝覆盖：

- `heartbeat_arranged.mid`
- `scaffold.mid`
- `score.json`
- `generation_request.json`
- `generate_payload.json`：可直接作为 HTTP `/generate` 的请求体
- `heartbeat_render_manifest.json`
- `assembly_manifest.json`
- `validation_report.json`

生成 JSON Schema：

```powershell
D:\conda\python.exe -m midigpt_scaffold_builder `
  --schema-dir schemas
```

## 测试

```powershell
D:\conda\python.exe -m unittest discover -s .\midigpt_scaffold_builder\tests -v
python -m compileall .\midigpt_scaffold_builder
```

推理前仍需从 `engine._analyzer` 或 HTTP `GET /info` 验证 checkpoint 的 `model_dim`、拍号、分辨率和属性量化标签；本模块不会硬编码这些 checkpoint 专属数值。

加载实际 checkpoint 后，可在采样前调用：

```python
from midigpt_scaffold_builder import validate_with_engine

report = validate_with_engine(engine, score_dict, request_dict)
```

该入口执行 MIDI-GPT 自身的解析和请求校验，但不启动采样。运行时依赖单独固定在 `requirements-midigpt.txt`；离线装配仍只需要 Pydantic。
