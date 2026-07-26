# 第一阶段故事 LLM Agent：开发与交接说明

> 更新日期：2026-07-20  
> 当前版本：Stage 1 同一 DeepSeek 后端的两层结构化规划；已实现内容计划 Schema `0.2-draft`
>
> 2026-07-25：正式模式先执行 1–6 段规模与主题复用决策，再执行详细规划；
> Stage 2 仍不调用规划 LLM。旧文中的“同一次规划调用”由此说明取代。
> 当前状态（2026-07-20）：内容规划、测试模式 MuseCoco 旋律硬约束、张力驱动鼓轨计划、MIDI 速度归一化、强制主音移调、逐段第二阶段交接和分消费者发布已完成，90 项离线测试通过；此前 2 项真实 DeepSeek 集成测试通过。  
>
> 2026-07-20 职责更正：以 [`三阶段职责边界.md`](三阶段职责边界.md) 为权威。Stage 1 包含 MuseCoco 生成与主题 MIDI 修正；Stage 2 生成完整心跳鼓轨、组装完整 MIDI 并执行 MIDI-GPT；Stage 3 只统一渲染混音完整 MIDI。本文后续“后置心跳重生成”描述为历史方案。
>
> 最新交付边界：不得把所有信息放入一个文件或一个业务目录。Agent 分别发布 MuseCoco、心跳规划兼容/审计视图、第二阶段和审计目录。心跳鼓轨的正式执行接口属于 Stage 2。
> 目标模块：`stage1_story_agent/`

本文件定义开发范围和交付要求。字段级内容模型和现有状态机见 [`第一阶段故事Agent详细设计.md`](第一阶段故事Agent详细设计.md)；`music_plan.json` 的可执行 Schema 以 `midigpt_scaffold_builder/models.py` 为准。

## 0. 已冻结的架构修正

全流程只有一个规划 LLM，位于本模块。它在同一次规划运行中完成故事/曲式、MuseCoco 主题要求、动机放置、逐段心音方案和 MIDI-GPT 填充区域规划。第二阶段只有 Python 框架装配器，不再调用 LLM。

为了兼容现有代码，单次运行可以原子发布两个文件视图：

- `content_plan.json`：故事、曲式、主题家族、MuseCoco 要求和变奏任务；
- `music_plan.json`：轨道、动机放置、`heartbeat_arrangement` 和 MIDI-GPT 填充区域。

两个文件必须共享同一 `story_id`、段落/主题引用、prompt 版本和 run ID。BPM、拍号和总小节只由 `content_plan.json` 持有权威，`music_plan.json` 不重复这些字段；其所有小节范围必须通过装配器与内容计划交叉校验。它们不是两个 LLM 阶段。

## 1. 开发目标

实现一个可从 Python 和 CLI 调用的唯一规划 Agent。它读取用户故事和约束，通过 DeepSeek 生成统一规划草稿，再由 Python 校验、丰富并原子发布 `content_plan.json` 与 `music_plan.json`。

Agent 必须把故事规划为音乐曲式，而不是只输出摘要：

```text
故事叙事段落
  -> 曲式 A / B / A' / C
  -> 主题家族 A / B / C
  -> A/B/C 各由 MuseCoco 生成一次主题种子
  -> A' 等引用 A，由后续 MIDI-GPT 变奏
  -> 同时规划每段心音节奏、动机放置和 MIDI-GPT 填充区域
  -> 每段精确小节范围供框架生成层执行
```

## 2. 必须实现的能力

- 故事摘要、情绪弧和有依据的叙事分段；
- `A-B-A'-C` 等曲式结构规划；
- 区分曲式段落实例和主题家族；
- 区分新主题、再现、变奏和发展；
- 全曲 BPM、拍号、主音和 major/minor 总体调性；
- 每个曲式段落精确 `bar_count`、`bar_start`、`bar_end`；
- 每个新主题家族一条 MuseCoco 生成 brief；
- 每条 MuseCoco 文本覆盖全部 12 类非 `NA` 属性；
- A′/A″等段落生成对上游主题的 `variation_tasks`，供 MIDI-GPT 层转换；
- 每个曲式段落具有且仅具有一条量化的心音规则；
- 基于稳定主题家族/`motif_id` 规划动机放置，不依赖读取实际 MIDI；
- 规划轨道角色、保护区域和 MIDI-GPT 填充区域；
- 严格 Pydantic、业务校验、有限 repair、错误报告和原子产物；
- Python API、CLI、FakeBackend 和全离线单元测试。

## 3. 范围边界

本模块不得：

- 调用 MuseCoco 或 MIDI-GPT；
- 输出 MIDI 音符或实际 `Score` / `GenerationRequest`；
- 把项目 `variation_tasks` 声称为 MIDI-GPT 官方请求；
- 把 `musecoco_attribute_targets` 声称为 MuseCoco 官方 JSON Schema；
- 读取或处理心音 WAV、S1/S2 波形、SF2 或实际心音事件；
- 在 `plan` 命令内执行最终 MIDI 移调、配器、和声或音频渲染；MuseCoco 输出后的强制主音移调由同包的 `normalize-key` 边界命令负责。

本模块必须规划 `heartbeat_arrangement`，但只使用 `S1`、`S2`、`s1_s2_pair` 等符号事件类型和拍点规则，不接触患者素材。第二阶段确定性框架层读取两个计划文件、实际主题清单和心音事件清单后执行绑定。

## 4. 关键数据决定

### 4.1 叙事和曲式分开

- `story_analysis.narrative_segments`：故事语义单位；
- `form_plan.sections`：音乐时间单位；
- 曲式段落通过 `narrative_segment_ids` 引用故事段落。

### 4.2 主题家族和曲式段落实例分开

以 `A-B-A'-C` 为例：

- 4 个曲式段落实例；
- 3 个主题家族 A/B/C；
- 3 条 MuseCoco 主题 brief；
- 1 条 A′ 的 MIDI-GPT variation task。

### 4.3 全曲总体调性

`global.global_tonality` 包含规范主音和 `major|minor`。所有主题家族的 MuseCoco `K1` 必须由全局 mode 派生，不能各自选择。

MuseCoco 不保证精确主音控制；精确 tonic 作为项目规划字段，在 MuseCoco 输出后由 `normalize-key` 自动分析并强制移调。若 major/minor 不一致，程序拒绝，因为纯移调不能改变调式。

### 4.4 两种小节长度

- `form_sections[*].bar_count`：框架层必须执行的段落长度；
- `theme_families[*].seed_bars`：MuseCoco 主题种子长度，1–16；
- A′可以是 8 小节，但不得因此产生第二个 A 主题种子。

## 5. 输入摘要

```json
{
  "schema_version": "0.2-draft",
  "story_id": "story-001",
  "story_text": "用户故事",
  "language": "zh-CN",
  "constraints": {
    "total_bars": 40,
    "target_form_sections": 4,
    "max_theme_families": 4,
    "max_variants_per_family": 2,
    "allowed_time_signatures": ["4/4"],
    "tempo_bpm_min": 70,
    "tempo_bpm_max": 130,
    "allowed_modes": ["major", "minor"],
    "allowed_tonics": ["C", "D", "E", "F", "G", "A", "B"]
  }
}
```

未提供 `target_form_sections` 时由 Agent 按故事明显分段决定。`max_theme_families` 是上限，不要求每段都新建主题。

分段数量必须由 LLM 根据故事决定，Python 不能套用固定曲式模板。没有明显转折、篇幅过短或情绪持续一致的文本允许只输出一个覆盖全曲的 A 段；空曲式仍然非法。

## 6. 最终输出顶层

```text
content_plan.json
├── schema_version: 0.2-draft
├── story_id
├── global
│   ├── total_bars
│   ├── tempo_bpm
│   ├── time_signature
│   └── global_tonality
├── story_analysis
│   ├── summary
│   ├── emotional_arc
│   └── narrative_segments
├── form_plan
│   ├── form_string
│   └── sections
├── theme_families
├── variation_tasks
├── musecoco_requests: []
└── provenance
```

同一产物目录还必须包含：

```text
music_plan.json
├── schema_version: 0.1-draft
├── story_id
├── ppq
├── tracks
├── motif_placements
├── heartbeat_arrangement
│   └── sections: 每个曲式段落恰好一条规则
└── midigpt
    ├── checkpoint
    ├── model_dim_bars
    ├── fill_regions
    └── generation_config
```

`music_plan.json` 不得包含实际 MIDI 音符或心音文件路径。它通过稳定 ID 引用未来的 `motif_manifest.json` 和心音事件清单。

## 7. 曲式关系

| relation | 示例 | 含义 | 素材来源 |
|---|---|---|---|
| `introduce` | A、B、C | 新主题首次出现 | MuseCoco seed |
| `reprise` | A | 精确再现已有主题 | 复用 seed |
| `variation` | A′、A″ | 保持主题身份的变奏 | MIDI-GPT variation |
| `development` | A′、B′ | 主题发展、重组或扩展 | MIDI-GPT development |

非 introduce 段必须引用更早的同主题家族段落。新主题按 A、B、C 顺序引入。

## 8. MuseCoco 规则

LLM 为每个新主题家族只选择 6 类属性：

```text
I1s2, R1, R3, S2s1, S4, P4
```

Python 生成 6 类全局/派生属性：

```text
B1s1 <- seed_bars
TS1s1 <- global.time_signature
K1 <- global.global_tonality.mode
T1s1 <- global.tempo_bpm
EM1 <- 主题引入段的 valence/tension
TM1 <- seed_bars + time_signature + tempo_bpm
```

最终每个主题家族精确包含：

```text
I1s2, R1, R3, S2s1, S4, B1s1, TS1s1, K1, T1s1, P4, EM1, TM1
```

全部非 `NA`。`musecoco_text` 必须由 Python 按 `official-template-aligned-v1` 固定模板生成或覆盖，使用与 MuseCoco 官方 `predict.json`/模板一致的自然陈述风格。文本只写受支持的离散速度类别与调式；精确 BPM 和主音保留在结构化计划中，不能写入文本并声称可由 MuseCoco 稳定控制。

## 9. DeepSeek 后端

使用 OpenAI 兼容 Chat Completions：

```text
DEEPSEEK_API_KEY=只存环境
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-pro
STAGE1_THINKING=enabled
```

请求设置 `response_format={"type":"json_object"}`。Prompt 必须包含 `json` 和完整草稿示例；本地 Pydantic 是最终 Schema 权威。

只读取最终 `content`，不保存 `reasoning_content`。空内容、截断、非 JSON 和业务规则失败进入有限 repair。

## 10. 执行流程

1. 校验输入并规范化故事；
2. 计算故事和请求 SHA-256；
3. 构造 `stage1-form-v7` Prompt，并注入 `musecoco-prompting-v3`；LLM 输出 valence/tension，Python 使用 `valence-arousal-v1` 派生 EM1，并按逐段 tension/BPM 编译鼓轨脉冲和后置 S1/S2计划；
4. DeepSeek 在一次规划响应中生成内容草稿和框架草稿；
5. 校验叙事段落；
6. 校验曲式总小节、主题来源、变奏序号和 family 集合；
7. Python 生成曲式标签、坐标和 `form_string`；
8. Python 为每个新 family 补齐 MuseCoco 12 属性和文本；
9. Python 从 variation/development 段构造 `variation_tasks`；
10. 构造并校验最终 `ContentPlan` 与装配器 `MusicPlan`；
11. 写入共享 provenance，并原子发布两个计划及审计产物；
12. 内容失败最多 repair 2 次，耗尽后生成失败报告。

## 11. 目录

```text
stage1_story_agent/
  agent.py
  backends.py
  config.py
  models.py
  form.py
  musecoco.py
  prompts.py
  validators.py
  artifacts.py
  errors.py
  cli.py
  schemas/
  examples/
  tests/
```

## 12. CLI

```powershell
python -m stage1_story_agent plan `
  --input .\stage1_story_agent\examples\story_input.example.json `
  --output-dir .\stage1_story_agent\outputs\story-001
```

退出码：0 成功；2 输入/配置；3 API/网络；4 模型内容耗尽；5 写入失败。默认不覆盖，`--force` 必须支持 backup/restore。

当前成功产物分四个同前缀目录发布：`*-musecoco/` 包含 `musecoco_plan.json` 及五个 MuseCoco 直接编码文件，`*-heartbeat/heartbeat_processing_plan.json`、`*-stage2/stage2_plan.json`，以及 `*-audit/content_plan.json + raw_response.json + run_manifest.json`。四个目录必须同成同败；失败报告只写入 audit 目录。

## 13. 必测场景

- `A-B-A'-C` 解析为 4 段、3 family、3 MuseCoco brief、1 variation task；
- A′来源指向未来、跨 family、没有 A 时被拒绝；
- reprise A 不触发 MuseCoco 或 MIDI-GPT task；
- development 触发正确 task；
- 段落小节数之和不等于总小节数时 repair；
- 段落坐标连续无缺口；
- `music_plan` 与 `content_plan` 的 story/section/theme 引用一致，且所有小节范围服从内容计划的全局权威；
- 每个曲式段落恰好命中一条 `heartbeat_arrangement` 规则；
- A 段单次心跳和 C 段 S1/S2 半拍间距 fixture 可被装配器接受；
- 动机放置引用已有主题/动机 ID，受保护区与填充区不重叠；
- 主题种子长度不超过 introduce 段；
- 所有 family `K1` 等于全局 mode；
- 每条 MuseCoco 文本覆盖 12 类非 `NA` 属性；
- 空响应、截断、非 JSON、未知字段、尝试耗尽；
- 网络重试与内容 repair 独立计数；
- API Key 和隐藏推理不出现在日志和产物；
- 原子写入、无 force 保留旧目录、force 失败恢复。

测试不得调用真实 API。仅当存在 Key 且 `RUN_DEEPSEEK_INTEGRATION=1` 时运行一次显式集成测试。

## 14. 推荐实施顺序

现有内容规划、DeepSeek 后端、CLI 和 50 个离线测试已经完成。下一位 Agent 不应重写它们，应按以下增量顺序工作：

1. 保留现有 90 项离线测试＋2 项显式启用的真实 API 集成测试基线；
2. 直接复用 `midigpt_scaffold_builder.models.MusicPlan`，不要复制一套近似模型；
3. 设计统一草稿和 `stage1-global-v3` prompt，使单次响应包含内容与框架规划；
4. 用纯函数从现有 form/theme 信息构造稳定轨道、动机放置、心音规则和填充区域；
5. 扩展 Agent repair 与跨文件 validator；
6. 扩展原子产物，使两个计划同成同败；
7. 增加 Schema、示例、FakeBackend 和跨模块回归测试；
8. 用合成动机与心音 fixture 调用现有装配器，证明计划可消费；
9. 最后执行一次显式真实 DeepSeek 测试。

## 15. 验收状态报告

完成编码后必须报告：

- 修改文件；
- 离线测试命令和结果；
- 真实 DeepSeek 是否测试；
- A/B/A′/C fixture 的 family/request/task 数量；
- `content_plan.json` 和 `music_plan.json` 示例路径；
- 装配器是否成功消费同次计划；
- MuseCoco 适配器和 MIDI-GPT task 映射仍有哪些阻塞。

## 16. 官方参考

- DeepSeek JSON Output：<https://api-docs.deepseek.com/guides/json_mode>
- DeepSeek 思考模式：<https://api-docs.deepseek.com/guides/thinking_mode>
- DeepSeek 当前模型：<https://api-docs.deepseek.com/api/list-models>
- MuseCoco/MIDI-GPT 输入要求：[`MuseCoco与MIDI-GPT输入要求.md`](MuseCoco与MIDI-GPT输入要求.md)
- 项目总流程：[`项目全流程与Agent同步.md`](项目全流程与Agent同步.md)
