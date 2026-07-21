# Stage 1 Story Agent

把用户故事编译为严格、可追溯且按消费者隔离的规划产物：MuseCoco 计划、供 Stage 2 生成心跳鼓轨的规划、第二阶段计划，以及独立审计计划。

交接状态：现有内容规划主体功能已完成；2026-07-20 已通过 90 项离线测试；此前的 2 项真实 DeepSeek 集成测试（JSON Output 连通性＋测试模式完整 Agent 旋律画像）继续作为显式启用的网络测试基线。

架构修正（2026-07-20）：全流程只保留本模块一个规划 LLM，第二阶段不再设置框架 LLM。一次运行会原子发布四个同前缀的独立目录：`*-musecoco`、`*-heartbeat`、`*-stage2`、`*-audit`。Stage 2 根据规划生成完整心跳鼓轨，并在 MIDI-GPT 处理中保护它；Stage 3 不重新生成心跳事件，只统一渲染和混音完整 MIDI。`*-heartbeat` 当前保留为兼容/审计视图，正式执行接口应迁移到 Stage 2。

`plan` 命令不调用 MuseCoco 或 MIDI-GPT，也不生成 MIDI。它会把每个主题家族的 12 类结构化属性确定性编码为 MuseCoco 官方 60 分类头格式；模块另提供 `normalize-tempo` 和 `normalize-key` 后处理命令，用于在 MuseCoco 产出后、第二阶段装配前把 MIDI 强制修正到 `content_plan.json` 的全局 BPM、主音和 major/minor 调式。`musecoco_requests` 固定为空；`variation_tasks` 是 LegaSynth 项目任务，不是 MIDI-GPT 官方 `GenerationRequest`。

## 安装

```powershell
python -m pip install -r .\stage1_story_agent\requirements.txt
```

要求 Python 3.11+、Pydantic 2 和 HTTPX。当前代码使用 `datetime.UTC`，不能由 Python 3.10 直接导入运行。

## 完全离线演示

离线演示使用 `FakeBackend`，不会读取 API Key，也不会访问网络：

```powershell
python -m stage1_story_agent.demo_agent `
  --output-dir .\stage1_story_agent\outputs\offline-demo `
  --force
```

固定示例预期得到 `A-B-A'-C`、4 个曲式段落、3 个主题家族、3 条 MuseCoco 全属性文本和 1 个变奏任务。

## MuseCoco 文本风格

`theme_families[*].musecoco_text` 由 Python 使用 `official-template-aligned-v1` 确定性渲染，句式对齐本地 MuseCoco 官方 `predict.json`、`template.json` 和 `refined_template.json` 的属性描述风格。例如：`This music is composed in the minor key`、`The music is in the vein of Chopin`、`The music conveys tension, unease, and anxiety`。文本显式覆盖 12 类受支持属性，不再使用 `Create a ...` 命令式写法。

精确 BPM 和主音仍保留在结构化计划中，供速度归一化和后续 MIDI 处理使用；MuseCoco 文本只写其离散速度类别与 `major/minor`，避免暗示文本属性模型能稳定控制未受支持的精确值。

## MuseCoco 直接编码输出

每次成功运行都会在 `*-musecoco` 目录生成：

```text
musecoco_plan.json               人类可读的主题与12类属性计划
predict.json                     官方Text-to-Attribute文本输入格式
predict_index.json               数组索引到theme_family_id的映射
direct_attribute_labels.json     12类组合one-hot与最终63个属性Token
predict_attributes.json          官方att_key顺序的60分类头批量格式
encoding_manifest.json           契约哈希、编码策略和文件哈希
task_packages/task_xxx/          每个主题一个可原子入队的单样本任务包
```

编码器不会调用 DeepSeek，也不会从 WSL 动态导入 MuseCoco。`I1s2` 与 `S4` 使用封闭世界规则：选中项为 `[1,0,0]`，同字段所有未选项为硬性“不使用” `[0,1,0]`，已知字段不产生 NA。`bach`、`handel` 会显式转换为官方 `bach-js`、`Handel` 索引。

`predict_attributes.json` 可以绕过 Text-to-Attribute 模型。每个 `task_packages/task_xxx/` 严格包含单样本 `predict_attributes.json`、对应的真实 `predict.json`、`predict_index.json` 和 `audit.json`。外部 WSL worker 安装任务时，将属性文件按字节同形复制成官方 `stage2_pre.py` 强制要求的 `softmax_probs.json`；该文件只表示 `deterministic_one_hot` 兼容输入，不是真实 Stage1 模型概率。随后由未修改的官方 `stage2_pre.py` 生成 `infer_test.bin`。

将现有 Stage1 产物入队并运行 MuseCoco：

```powershell
python -m stage1_story_agent enqueue-musecoco `
  --musecoco-dir .\stage1_story_agent\outputs\<时间戳>-musecoco `
  --run `
  --max-attempts 3
```

也可以在 `plan` 成功后直接追加 `--enqueue-musecoco --run-musecoco-queue --musecoco-max-attempts 3`。普通 Coco 生成失败默认最多尝试 3 次，每次重试前后都严格清理；任务包、官方哈希、环境或清理契约冲突仍立即停止。队列自动化、失败恢复、输入留档与严格清理契约见 [`../docs/MuseCoco任务包队列自动化.md`](../docs/MuseCoco任务包队列自动化.md)。

Stage 2负责消费本模块的逐段鼓轨指令并生成完整心跳鼓轨；Stage 3只对包含该轨道的完整MIDI统一渲染混音。正式边界见 [`../docs/三阶段职责边界.md`](../docs/三阶段职责边界.md)。

## MuseCoco MIDI 速度归一化

MuseCoco 生成每个动机 MIDI 后执行：

```powershell
python -m stage1_story_agent normalize-tempo `
  --input-midi .\musecoco_outputs\motif_01.mid `
  --content-plan .\stage1_story_agent\outputs\story-001-audit\content_plan.json `
  --output-midi .\musecoco_outputs\motif_01.tempo-normalized.mid
```

工具会将全部 `Set Tempo` 元事件改成 `global.tempo_bpm`，并在 tick 0 插入同一速度；音符的 tick 网格不变。也可用 `--target-bpm 96` 手动指定。该命令完全离线，不读取 API Key，默认不覆盖已有输出。

## MuseCoco MIDI 强制移调修正

速度归一化后执行：

```powershell
python -m stage1_story_agent normalize-key `
  --input-midi .\musecoco_outputs\motif_01.tempo-normalized.mid `
  --content-plan .\stage1_story_agent\outputs\story-001-audit\content_plan.json `
  --output-midi .\musecoco_outputs\motif_01.final.mid
```

程序优先读取 MIDI 的 `Key Signature`；没有调号时，使用按音符时值加权的 Krumhansl–Kessler 24 调性轮廓估计源调。随后计算到计划主音的最短半音距离，改写全部非鼓轨 Note On/Off 音高，保持 General MIDI 通道 10 的打击乐不变，并把所有调号改为目标调号、在 tick 0 补写目标调号。标准输出 JSON 记录源/目标调性、检测方法、半音偏移、音域、事件计数及输入/输出 SHA-256。

这是 fail-closed 操作：自动检测含糊、文件包含多个不同调号、major/minor 与计划不一致，或移调会让音高越过 MIDI 0–127 时均拒绝输出。纯移调不能把 major 改成 minor；此时应让 MuseCoco 重新生成。只有经过人工或独立分析确认源调时，才可同时使用 `--source-tonic` 和 `--source-mode` 覆盖自动检测。

调性检测轮廓依据：Krumhansl, C. L. (1990), *Cognitive Foundations of Musical Pitch*, Oxford University Press。算法采用全局时值加权音级直方图与 major/minor 轮廓的 Pearson 相关，不引入新的第三方依赖。

## DeepSeek CLI

故事内容会发送到 DeepSeek。不要在输入中放入病历号、患者身份信息、心音文件或其他不必要的敏感数据。API Key 可以通过系统环境变量或项目根目录的本地 `.env` 提供，不存在 `--api-key` 参数。系统环境变量优先于 `.env`。

本地 `.env` 用法：

```powershell
Copy-Item .\.env.example .\.env
notepad .\.env
```

在 `.env` 中把占位符替换为真实 Key：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
```

`.env` 已加入 `.gitignore`；程序启动时会自动读取，不需要再执行 `$env:DEEPSEEK_API_KEY=...`。

直接输入中文自然文本（其余 JSON 字段使用默认值）：

```powershell
python -m stage1_story_agent plan --story-text "这是一个需要转化为音乐结构的中文故事。" --force
```

该命令在内存中转换为严格的 `StoryPlanRequest`：默认 `story_id=cli-story`、`language=zh-CN`、`total_bars=32`。未指定 `--output-dir` 时，以本地时间戳为前缀创建四个独立目录。可以用 `--story-id`、`--language` 和 `--output-dir` 覆盖对应值。

固定 ABA 测试模式：

```powershell
python -m stage1_story_agent plan --story-text "平静开始，冲突逐渐展开，最终回到最初的主题。" --test-mode
```

测试模式鼓励 MuseCoco 生成约 12 小节，再严格交付 8 小节主题输入；曲式固定为 32 小节 `A-B-A`（8+16+8）、C minor、全段 96 BPM 和 4/4。长于 8 小节的 MIDI 会在精确小节边界裁剪并补齐仍发声音符的 Note Off，短于 8 小节则报错且不补静音。鼓轨张力固定为低/高/低：S1 与 S3 每小节仅第1拍触发一次，S2 每拍触发一次。Stage 2 据此生成整曲心跳鼓轨；S1/S2 音色映射在 Stage 3 对完整 MIDI 统一渲染时执行，而不是在 MIDI-GPT 后重建鼓轨。MuseCoco 旋律配置由程序硬覆盖为：A=solo piano/Chopin，B=solo violin/Schubert，统一 classical、2 个八度、not_danceable、medium rhythmic intensity。曲式 S1、S3 全段固定，S2 只允许 MIDI-GPT 修改后 8 小节延伸区。

所有规划请求还会注入版本化的 `musecoco-prompting-v4` 本地知识：只使用官方支持的离散属性和值，并在正常模式下软性偏好清晰的独奏旋律乐器、2–3 个八度、适中速度/节奏和旋律导向的古典风格。项目层硬性禁用 Stravinsky 与 synthesizer；为保持官方 60 头编码位置稳定，它们仍出现在冻结枚举中，但模型或输入一旦选择就会被拒绝。LLM 为情绪弧和叙事段评估 `valence`（-1 到 1）与 `tension`（0 到 1），不再选择 EM1；Python 使用 `valence-arousal-v1` 从主题引入段确定 Q1–Q4。旧响应中的 EM1 会保留在原始审计响应中，但不会影响正式输出。测试模式同时注入精确 melodic profile；最终仍由 Python 硬校验。

默认长度参数为 `musecoco_generation_bars=12` 与 `musecoco_output_bars=8`。命令行可用 `--musecoco-generation-bars` 和 `--musecoco-output-bars` 分别覆盖；生成目标不得短于输出目标。使用 `plan --enqueue-musecoco --run-musecoco-queue` 时，成功结果会自动收集到 `raw_results/`，随后在 `generated_themes/` 中执行小节、速度、调性归一化并发布 `final.mid`。

也可以只在当前 PowerShell 会话中设置环境变量：

```powershell
$env:DEEPSEEK_API_KEY = "..."
python -m stage1_story_agent plan `
  --input .\stage1_story_agent\examples\story_input.example.json `
  --output-dir .\stage1_story_agent\outputs\story-001
```

默认模型为 `deepseek-v4-pro`，默认启用 thinking；程序只读取最终 `content`，不会保存 `reasoning_content`。

可选环境变量：

- `DEEPSEEK_BASE_URL`，默认 `https://api.deepseek.com`
- `DEEPSEEK_MODEL`，默认 `deepseek-v4-pro`
- `STAGE1_MAX_OUTPUT_TOKENS`，默认 `8192`
- `STAGE1_TIMEOUT_SECONDS`，默认 `90`
- `STAGE1_THINKING`，`enabled|disabled`
- `STAGE1_REASONING_EFFORT`，`high|max`

CLI 退出码：0 成功；2 输入/配置；3 API/网络；4 模型内容尝试耗尽；5 产物写入失败。默认不覆盖已有目录，`--force` 使用 backup/restore 后再替换。

当前成功产物包括 MuseCoco 计划与五个直接编码文件、`heartbeat_processing_plan.json`、`stage2_plan.json`，以及独立 audit 目录中的 `content_plan.json`、`raw_response.json`、`run_manifest.json`。三个业务消费者不会读取同一份混合计划。

## 测试

```powershell
python -m unittest discover -s .\stage1_story_agent\tests -v
python -m compileall .\stage1_story_agent
```

离线测试不会调用真实 API。两项真实 DeepSeek 测试仅在 `.env` 已配置 Key 且显式打开集成测试开关时运行：一项检查 JSON Output 连通性，另一项检查测试模式的模型原始旋律选择和最终硬约束。

```powershell
# 如果项目根目录 .env 已配置 Key，只需打开集成测试开关：
$env:RUN_DEEPSEEK_INTEGRATION = "1"
python -m unittest stage1_story_agent.tests.test_backends.DeepSeekIntegrationTests -v
```

## Schema 与示例

- `examples/story_input.example.json`：用户请求示例
- `examples/test_mode_input.example.json`：固定 ABA 测试模式请求
- `examples/llm_draft.example.json`：仅供离线演示的 LLM 草稿
- `schemas/`：从 Pydantic 生成并由快照测试校验的输入、审计和三个消费者 JSON Schema

重新生成 Schema：

```powershell
python -m stage1_story_agent.export_schemas
```
