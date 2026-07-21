# MuseCoco 与 MIDI-GPT 输入要求：字段、格式与项目映射

> 用途：供后续 Agent 和开发者在实现 `stage1_story_agent`、MuseCoco 适配器、第二阶段规划器与 MIDI-GPT 框架装配器时回顾。  
> 调研日期：2026-07-16；2026-07-17 使用 PyPI 实时索引和官方 wheel 再复核  
> 项目决策更新：2026-07-17，每个新主题家族各生成一条覆盖全部 12 类属性的 MuseCoco 文本；A′/A″由 MIDI-GPT 变奏，不重复生成主题。  
> 原则：代码实现优先于说明文档；官方接口与 LegaSynth 项目自定义字段严格分开。  
> MuseCoco 代码快照：`microsoft/muzic@a2efda0bfb297b1520282a61ca12513dc3517744`。  
> MIDI-GPT 代码快照：`Metacreation-Lab/MIDI-GPT@0f969d1a7045b306571e14dd1dc7835c00005bd3`，包版本 `0.3.2`。
> 复核结论：`0.3.2` 确实已发布；网页搜索缓存可能仍显示旧版，版本判断应以 PyPI 实时索引和下载产物元数据为准。

## 1. 先记住的结论

1. **MuseCoco 没有已发布的稳定 JSON Schema、Python SDK 或 HTTP 推理 API。**官方实现是 Linux/Python 3.8 下的研究型两阶段批处理流水线：`predict.json` 文本列表 → 文本属性分类 → `infer_test.bin` Python pickle → Fairseq 属性到音乐生成。
2. MuseCoco 的公开文本入口只需要 `text`。所谓 `I1s2`、`R3`、`B1s1` 等控制字段是模型内部的离散属性和 one-hot 向量，不是可直接提交的官方 JSON 请求字段。
3. MuseCoco 的小节控制 `B1s1` 是区间分类，不保证精确小节数；官方脚本还以 REMI token 长度控制生成，不以 `seed_bars` 精确截断。因此 LegaSynth 的主题种子长度只能先作为项目后置验证目标，不能代替曲式段落的精确小节数。
4. **MIDI-GPT 0.3.2 有明确的 Python 数据类和 HTTP JSON 形态。**核心输入是 `Score` 加 `GenerationRequest`，而不是普通 MIDI 路径加一段自然语言。
5. MIDI-GPT 的小节和轨道 ID 都是 **0-based**；所有轨道必须具有相同的小节数；请求必须为 Score 中每条轨道提供一个 `TrackPrompt`。
6. MIDI-GPT 的属性值是 checkpoint 专属的量化等级，必须运行时查询 `engine._analyzer.attribute_sizes()` 等接口，不能由 LLM 猜测。
7. 当前已发布的 MIDI-GPT checkpoint 都不支持 `mask_mode="token"`；首版应使用 `mask_mode="attention"`。
8. 对心音或主题动机使用 `ignore=True` 会使该轨道不参与模型条件上下文。若要“保持不变且参与条件”，应使用 `bars=[]`、`ignore=False`，并在生成后逐事件验证锚点没有变化。
9. LegaSynth 为 MuseCoco 准备的每条英文文本必须显式覆盖全部 12 类属性，且不允许任何一类使用 `NA`。曲式规划版 Agent 为每个新主题家族只让 LLM 选择 7 类语义属性；Python 从主题种子长度和全曲 BPM、拍号、调式补齐其余 5 类，再按固定模板生成文本。A′/A″等变奏不产生新的 MuseCoco 文本。
10. MIDI-GPT 会自动把 Score 的 PPQ 归一化到 checkpoint 内部分辨率，因此项目 PPQ 不必硬编码为模型值；仍需保证输入 tick 合法并在输出审计中记录换算。
11. `0.3.2` 的请求校验会在规范化返回时丢失 piece-level `controls`；当前项目继续输出空 controls，直到上游修复并通过回归测试。

## 2. MuseCoco 官方输入链

### 2.1 实际流水线

```text
predict.json
  -> 1-text2attribute_model/main.py
  -> predict_attributes.json + softmax_probs.json
  -> 1-text2attribute_model/stage2_pre.py
  -> infer_test.bin
  -> 2-attribute2music_model/interactive_1billion.sh
  -> REMI token + MIDI 文件
```

官方 README 只支持这条文件式工作流，没有定义 REST 请求体或独立的生产 Schema。属性到音乐 checkpoint 约 14.5 GB，官方默认环境是 Linux、Python 3.8、PyTorch 1.11 和 Fairseq 相关依赖。

### 2.2 第一阶段文本输入：`predict.json`

仓库中的 `predict.json` 是空文件，但读取代码明确只访问每条记录的 `text`，并且 `stage2_pre.py` 使用 `json.load` 读取整个文件。因此可确认的最小格式是一个 JSON 数组：

```json
[
  {
    "text": "A calm four-bar piano piece in a minor key with low rhythmic intensity."
  },
  {
    "text": "An energetic eight-bar electronic piece with drums and a fast tempo."
  }
]
```

上例只用于说明官方文件的最小 JSON 结构，不满足 LegaSynth 已冻结的“12 类属性全部覆盖”要求；项目实际生成的每条 `text` 必须使用下述全属性规则。

要求与限制：

- `text`：字符串；这是公开推理入口唯一必需的业务字段。
- 官方 checkpoint 基于 `bert-large-uncased`，模型卡语言为 English；项目适配器应把中文故事规划转换为受控英文描述，而不是直接假设中文控制精度。
- `predict.sh` 没有覆盖 `max_seq_length`，因此代码默认最大长度为 256 个 tokenizer token；更长输入会截断。
- 官方建议只描述训练中支持的属性和值；任意自由文学描述可以输入，但控制准确率无法保证。
- 训练格式还可包含 `labels`，但纯推理输入不需要，也不应由项目填写。

#### LegaSynth 的全属性文本规则

每条发给 MuseCoco 的 `text` 必须包含以下 12 类属性各一次或至少一次明确表达：

| 属性 | 文本必须表达的内容 | 取值来源 |
|---|---|---|
| `I1s2` | 至少一种支持的乐器大类 | LLM 从支持枚举选择 |
| `R1` | danceable 或 not danceable | LLM 选择 |
| `R3` | low、medium 或 high rhythmic intensity | LLM 选择 |
| `S2s1` | 一位支持的作曲家/艺术家 | LLM 选择；不得省略 |
| `S4` | 至少一种支持的 genre | LLM 选择 |
| `B1s1` | 主题种子小节数及其官方区间 | Python 从 `seed_bars` 映射 |
| `TS1s1` | 支持的拍号 | 来自已校验的全局/动机约束 |
| `K1` | major 或 minor | Python 从全曲 `global_tonality.mode` 继承 |
| `T1s1` | slow、moderate 或 fast | Python 从 BPM 映射；精确 BPM 不写入属性文本 |
| `P4` | 0–11 个八度的音域跨度 | LLM 选择 |
| `EM1` | Q1/Q2/Q3/Q4 对应的自然语言情绪 | Python 从主题引入段的 valence/tension 派生 |
| `TM1` | 计算时长及其秒数区间 | Python 从小节数、拍号和 BPM 计算 |

这里的“全部属性”指 12 个属性类型全部出现，不是把 28 种乐器、17 位艺术家和 22 种流派全部写入同一段文本。所有属性必须选择有效值，不能通过省略或写 `unknown`、`NA` 来满足覆盖要求。

派生规则：

- `seed_bars` 限制为 `1..16`，并映射为 `1-4`、`5-8`、`9-12`、`13-16`；超过 16 会在当前 MuseCoco 实现中落入 `NA`，不符合全属性要求。
- `tempo_bpm <= 76` 映射为 slow，`76 < tempo_bpm < 120` 映射为 moderate，`tempo_bpm >= 120` 映射为 fast。
- 若项目 BPM 表示四分音符每分钟拍数，则 `duration_seconds = seed_bars * numerator * 4 / denominator * 60 / tempo_bpm`，再映射为 `(0,15]`、`(15,30]`、`(30,45]`、`(45,60]` 或 `>60` 秒。

推荐固定句式（`official-template-aligned-v1`）：

```text
The musical piece is a representative example of the {genres} style and
is in the vein of {artist}. This music is composed in the {key_mode} key
and follows a {time_signature} meter. The music is brought to life through
the use of {instruments}. Its pitch range is within {pitch_range_octaves}
octaves. The tempo of this song is {tempo_class}. This music is
{danceability}, and the song has {rhythm_phrase}. The music conveys
{emotion_words}. The song spans approximately {bar_range} bars and has a
duration of {duration_range} seconds.
```

该句式对齐本地官方 `predict.json`、`template.json` 和 `refined_template.json` 中反复出现的属性陈述方式。最终英文文本应由 Python 从结构化属性目标渲染。这样可以用字段级校验证明覆盖完整，而不需要对 LLM 自由文本做脆弱的关键词猜测。精确主音、精确 BPM 和计算出的精确秒数仍保存在结构化规划或审计字段中，供后续 MIDI 处理使用，不在 MuseCoco 属性文本中声称可被稳定控制。

### 2.3 文本属性模型输出

`main.py` 输出两个 JSON 对象：

- `predict_attributes.json`：每个属性键对应按输入顺序排列的 one-hot 预测；
- `softmax_probs.json`：相同键和顺序，对应每个类别的浮点概率。

原始键不是 12 个简单字段。`I1s2` 的 28 个乐器和 `S4` 的 22 个流派分别展开成独立分类头，例如 `I1s2_piano`、`S4_classical`。`stage2_pre.py` 随后才把它们重新组合为 `I1s2` 和 `S4` 的二维数组。

### 2.4 `infer_test.bin` 的确切对象形状

`stage2_pre.py` 通过 `pickle.dump` 写入以下 Python 对象。它不是 JSON，也不是跨语言协议：

```python
[
    {
        "text": str,
        "pred_labels": {
            "I1s2": list[list[int]],   # [28][3]
            "R1": list[int],           # [3]
            "R3": list[int],           # [4]
            "S2s1": list[int],         # [18]
            "S4": list[list[int]],     # [22][3]
            "B1s1": list[int],         # [5]
            "TS1s1": list[int],        # [8]
            "K1": list[int],           # [3]
            "T1s1": list[int],         # [4]
            "P4": list[int],           # [13]
            "EM1": list[int],          # [5]
            "TM1": list[int],          # [6]
        },
        "pred_probs": {                # 键和形状同 pred_labels，元素为 float
            "...": "..."
        },
    }
]
```

属性到音乐脚本实际使用 `pred_labels` 生成命令 token；`pred_probs` 被保留但不用于默认生成决策。因为 pickle 可执行对象反序列化逻辑，`infer_test.bin` 只能加载由受信任本地代码生成的文件。

### 2.5 MuseCoco 属性、类别顺序和含义

以下顺序来自官方 `attribute_unit` 实现和 `num_labels.json`。最后一个类别通常是 `NA`，表示未指定或无法判断。

| 代码 | 内部形状 | 类别顺序/含义 |
|---|---:|---|
| `I1s2` | `[28][3]` | 每个乐器独立使用 `[yes, no, NA]` |
| `R1` | `[3]` | `[danceable, not_danceable, NA]`；代码主要依据鼓点强拍/弱拍分布判断 |
| `R3` | `[4]` | `[low, medium, high, NA]`；onset/beat `<=1`、`(1,2)`、`>=2` |
| `S2s1` | `[18]` | 17 位固定作曲家，加 `NA` |
| `S4` | `[22][3]` | 每个流派独立使用 `[yes, no, NA]` |
| `B1s1` | `[5]` | `[1-4, 5-8, 9-12, 13-16, NA]` |
| `TS1s1` | `[8]` | `[4/4, 2/4, 3/4, 1/4, 6/8, 3/8, other, NA]` |
| `K1` | `[3]` | `[major, minor, NA]`；不控制具体主音 |
| `T1s1` | `[4]` | `[slow, moderate, fast, NA]`；`<=76`、`76<tempo<120`、`>=120` BPM |
| `P4` | `[13]` | 跨越 `[0,1,...,11]` 个八度，加 `NA` |
| `EM1` | `[5]` | `[Q1, Q2, Q3, Q4, NA]` |
| `TM1` | `[6]` | `[(0,15], (15,30], (30,45], (45,60], >60, NA]` 秒 |

`EM1` 的语义：

- `Q1`：快乐、兴奋、积极；
- `Q2`：紧张、不安、焦虑；
- `Q3`：悲伤、低落、忧郁；
- `Q4`：平静、放松、安宁。

`I1s2` 的 28 个乐器大类按索引依次为：

```text
piano, keyboard, percussion, organ, guitar, bass, violin, viola,
cello, harp, strings, voice, trumpet, trombone, tuba, horn, brass,
sax, oboe, bassoon, clarinet, piccolo, flute, pipe, synthesizer,
ethnic_instruments, sound_effects, drum
```

`S2s1` 的 17 个作曲家按索引依次为：

```text
Beethoven, Mozart, Chopin, Schubert, Schumann, Bach, Haydn, Brahms,
Handel, Tchaikovsky, Mendelssohn, Dvorak, Liszt, Stravinsky, Mahler,
Prokofiev, Shostakovich
```

`S4` 的 22 个流派按索引依次为：

```text
New Age, Electronic, Rap, Religious, International, Easy Listening,
Avant-Garde, RnB, Latin, Children, Jazz, Classical, Comedy/Spoken,
Pop/Rock, Reggae, Stage, Folk, Blues, Vocal, Holiday, Country, Symphony
```

### 2.6 属性命令 token

属性到音乐模型最终接收的不是上述 JSON 字段，而是按固定顺序拼接的 token：

```text
I1s2_0_0 I1s2_1_2 ... R1_0 R3_2 ... B1s1_1 TS1s1_0 ... <sep>
```

固定 `key_order` 为：

```text
I1s2, I4, C1, R1, R3, S2s1, S4, B1s1, TS1s1, K1,
T1s1, P4, ST1, EM1, TM1
```

当前文本属性模型不输出 `I4`、`C1`、`ST1`；官方推理脚本分别补成 `I4_28`、`C1_4`、`ST1_14`，即 NA。项目不应把这三个补位 token 误写成对外业务字段。

### 2.7 MuseCoco 对 LegaSynth 的直接影响

- 当前 `stage1_story_agent` 仍应保持 `musecoco_requests: []`，直到单独冻结 MuseCoco 适配器版本和项目 Schema。
- 每个新 `theme_family` 保存完整的 `musecoco_attribute_targets` 和由 Python 渲染的 `musecoco_text`；未来适配器最安全的公开输入是“项目元数据 + 该受控英文 `text`”。内部 one-hot、pickle 和命令 token 由适配器生成，不由 LLM 直接生成。
- `request_id`、`theme_family_id`、`seed_bars`、checkpoint、随机种子和哈希都是 **LegaSynth 自定义审计字段**，不是 MuseCoco 官方字段。
- `seed_bars` 必须由生成后 MIDI 解析器验证。`B1s1` 只能提供 1–4、5–8、9–12、13–16 四档，16 小节以上在当前实现中落入 NA。曲式段落 `bar_count` 是独立的框架字段，不受该 16 小节限制。
- 官方 `interactive_1billion.sh` 默认使用 `top-k=15`、`temperature=1.0`、每条输入生成 2 个候选、最小 512/最大 2560 REMI token；这些是脚本参数，不是请求 Schema。
- 需要固定 `microsoft/muzic` commit、两个 checkpoint 文件哈希、属性字典和推理脚本版本，才能形成可复现的适配器。

## 3. MIDI-GPT 0.3.2 输入契约

### 3.1 两个顶层对象

Python 调用：

```python
result = engine.session(score, generation_request).run()
```

HTTP 调用 `POST /generate`：

```json
{
  "score": {},
  "request": {}
}
```

HTTP 服务使用 `Score.from_dict()` 和 `GenerationRequest.from_dict()` 解析这两个字典；成功响应包含 `score` 和采样耗时 `timing`。

### 3.2 `Score` JSON

```json
{
  "resolution": 480,
  "tempo": 500000,
  "tracks": [
    {
      "instrument": 0,
      "track_type": "melodic",
      "bars": [
        {
          "ts_numerator": 4,
          "ts_denominator": 4,
          "future": false,
          "notes": [
            {
              "pitch": 60,
              "velocity": 90,
              "onset_ticks": 0,
              "duration_ticks": 480,
              "delta": 0
            }
          ]
        }
      ]
    }
  ]
}
```

字段定义：

| 层级 | 字段 | 类型/默认 | 含义 |
|---|---|---|---|
| Score | `resolution` | int，默认 `480` | ticks per quarter note，TPQ/PPQ |
| Score | `tempo` | int，默认 `500000` | 每四分音符微秒数；BPM=`60000000/tempo` |
| Score | `tracks` | list | 至少一条轨道 |
| Track | `instrument` | int，默认 `0` | General MIDI program，期望 `0..127` |
| Track | `track_type` | str，默认 `melodic` | `melodic` 或 `drum` |
| Track | `bars` | list | 每条轨道至少一个小节，所有轨道小节数必须相同 |
| Bar | `ts_numerator` | int，默认 `4` | 拍号分子 |
| Bar | `ts_denominator` | int，默认 `4` | 拍号分母 |
| Bar | `future` | bool，默认 `false` | 把小节标记为未知/未来；不能与同小节 infill/AR 目标冲突 |
| Bar | `notes` | list，默认 `[]` | 小节内音符 |
| Note | `pitch` | 必需 int | MIDI pitch，期望 `0..127` |
| Note | `velocity` | 必需 int | MIDI velocity，期望 `0..127` |
| Note | `onset_ticks` | 必需 int | **相对当前小节起点**的 tick |
| Note | `duration_ticks` | 必需 int | tick 时值，必须为正才能可靠往返 |
| Note | `delta` | int，默认 `0` | expressive checkpoint 的微时序偏移 |

代码与 API 文档存在一个细节差异：API 文档为 `Note` 写了默认值，但当前 Python dataclass 的前四个字段没有默认值，且 HTTP 路径执行 `Note(**dict)`。因此项目 JSON 必须总是显式提供 `pitch`、`velocity`、`onset_ticks` 和 `duration_ticks`。

另两个序列化细节：

- Python `Bar` dataclass 有 `beat_length`，但当前 `Score.from_dict()/to_dict()` 不读写它；HTTP JSON 不应依赖该字段。
- Python `Track` dataclass 有 `attributes`，但当前 `Score.from_dict()/to_dict()` 不读写它；生成控制应放在 `TrackPrompt.attributes`。

### 3.3 `GenerationRequest` JSON

```json
{
  "tracks": [
    {
      "id": 0,
      "bars": [],
      "autoregressive": false,
      "ignore": false,
      "mask_bars": [],
      "attributes": {},
      "controls": {},
      "bar_attributes": {},
      "bar_controls": {}
    },
    {
      "id": 1,
      "bars": [0, 1, 2, 3],
      "autoregressive": false,
      "ignore": false,
      "mask_bars": [],
      "attributes": {
        "note_density": 4,
        "max_polyphony": 3
      },
      "controls": {},
      "bar_attributes": {},
      "bar_controls": {}
    }
  ],
  "config": {
    "temperature": 1.0,
    "seed": 20260716,
    "max_attempts": 3,
    "novelty_check": true,
    "silence_check": true,
    "temperature_escalation": 1.0,
    "bars_per_step": 1,
    "tracks_per_step": 1,
    "model_dim": 4,
    "shuffle": false,
    "mask_mode": "attention",
    "polyphony_hard_limit": 0,
    "density_hard_limit": 0,
    "top_p": 0.95,
    "top_k": 0,
    "mask_p": 0.0,
    "mask_k": 0
  },
  "controls": {}
}
```

#### `TrackPrompt`

| 字段 | 类型/默认 | 语义 |
|---|---|---|
| `id` | 必需 int | Score 中的 0-based 轨道索引 |
| `bars` | 必需 list[int] | 需要生成的 0-based 绝对小节索引 |
| `autoregressive` | bool=`false` | `true` 表示 AR 生成；空 `bars` 表示生成整条轨道 |
| `ignore` | bool=`false` | 从模型 token 上下文排除该轨道，并保持它不参与生成 |
| `mask_bars` | list[int]=`[]` | 隐藏但不作为本次生成目标的小节，必须与 `bars` 不相交 |
| `attributes` | dict[str,int] | 轨道级、分析器派生的量化属性 |
| `controls` | dict[str,Any] | 非分析器控制；当前仅 `time_signature` 索引 |
| `bar_attributes` | dict[int,dict[str,int]] | 小节级属性覆盖；HTTP JSON 的对象键是字符串，解析时转为 int |
| `bar_controls` | dict[int,dict[str,Any]] | 小节级非属性控制；当前仅 `time_signature` 索引 |

常用状态：

```python
# 固定上下文：保留且让模型看见
TrackPrompt(id=0, bars=[], ignore=False)

# 完全排除：保留但不让模型看见
TrackPrompt(id=0, bars=[], ignore=True)

# 中间小节补写
TrackPrompt(id=1, bars=[4, 5, 6, 7], autoregressive=False)

# 整轨从头生成
TrackPrompt(id=2, bars=[], autoregressive=True)

# 续写右侧后缀
TrackPrompt(id=2, bars=[4, 5, 6, 7], autoregressive=True)
```

AR 的非空 `bars` 必须是连续的最右后缀。Infill 可选择轨道内部小节，但所用 checkpoint 必须支持 infill。

#### `InferenceConfig`

| 字段 | 默认 | 关键约束 |
|---|---:|---|
| `temperature` | `1.0` | `>0` |
| `seed` | `-1` | `-1` 表示不固定；重试时使用 `seed + attempt` |
| `max_attempts` | `3` | `>=1`，每个生成 step 的尝试数 |
| `novelty_check` | `true` | 拒绝与原目标小节完全相同的候选 |
| `silence_check` | `true` | 拒绝目标小节零音符候选 |
| `temperature_escalation` | `1.0` | `>=1.0`，超过 `3.0` 会被钳制 |
| `bars_per_step` | `1` | `1..model_dim` |
| `tracks_per_step` | `1` | `>0` |
| `model_dim` | `4` | 必须属于 checkpoint 的 `num_bars_map` |
| `shuffle` | `false` | 是否打乱生成 step 顺序 |
| `mask_mode` | `token` | 当前发布模型应显式改为 `attention` 等非 token 模式 |
| `polyphony_hard_limit` | `0` | `0` 关闭；全局同时起音硬上限 |
| `density_hard_limit` | `0` | `0` 关闭；每小节 onset 数硬上限 |
| `top_p` | `1.0` | `(0,1]` |
| `top_k` | `0` | `>=0`，`0` 关闭 |
| `mask_p` | `0.0` | `[0,1)`；启用时且 `top_p<1`，必须 `<top_p` |
| `mask_k` | `0` | `>=0`；同时启用 `top_k` 时必须 `<top_k` |

#### `GenerationRequest.controls`

piece-level 控制只接受：

- `velocity: bool`：checkpoint 必须支持对应 velocity token/可切换能力；
- `microtiming: bool`：checkpoint 必须支持 delta token/可切换能力；
- `genre: str`：必须是该 checkpoint `genre_groups` 中的规范名称或别名。

未知控制会被拒绝。Yellow 不支持把这些字段当作通用自由控制使用。

当前固定 commit 还有一个必须规避的实现缺陷：`validate_request()` 在返回规范化请求时只重建了 `tracks` 和 `config`，没有把 `GenerationRequest.controls` 复制到返回对象。因此 `velocity`、`microtiming`、`genre` 即使通过前置校验，也会在进入 `SamplingSession` 前丢失。项目在升级到包含修复的版本并增加回归测试前，不应依赖 piece-level `controls`。

### 3.4 checkpoint 决定可用字段和值

| checkpoint | `model_dim` | MaskBar | 主要属性 |
|---|---|---|---|
| `yellow` / `yellow_small` | `4, 8` | 不支持 | `note_density`、`min/max_polyphony`、`min/max_note_duration` |
| `prism_medium` | `4, 8, 12, 16` | 不支持 | 更广的调号、音域、静音、逐小节密度/复音、音级集合、genre；仍在训练 |
| `expressive` | `4, 8, 12, 16` | 不支持 | prism 类控制，加 microtiming/velocity；仍在训练 |
| `ghost` | 规划 `4,8,12,16` | 规划支持 | 尚无 checkpoint，不能加载 |

不能在项目 Schema 中把某一 checkpoint 的量化级数硬编码为永久事实。加载模型后必须查询：

```python
engine._analyzer.attribute_sizes()
engine._analyzer.attribute_value_labels()
engine._analyzer.attribute_track_types()
```

HTTP 部署时先请求 `GET /info`，读取 `attributes`、`capabilities` 和 `resolution`。属性值必须满足 `0 <= value < size`，并符合 melodic/drum 轨道类型限制。

### 3.5 MIDI-GPT 请求的硬性校验

当前代码至少强制：

- Score 必须有轨道；每条轨道至少一个小节；所有轨道小节数相同；小节数不少于 `model_dim`。
- Score 中的拍号必须属于 checkpoint 配置支持集合。
- 请求必须覆盖 Score 的每一条轨道，轨道 ID 唯一且有效；不生成的轨道也要显式给出 prompt。
- `ignore=True` 与 `autoregressive=True` 互斥，且 ignored 轨道的 `bars`、`mask_bars` 必须为空。
- `bars`、`mask_bars`、`bar_attributes`、`bar_controls` 的索引必须在范围内。
- `bars` 与 `mask_bars` 不得重叠；`future=True` 小节不得同时作为 infill 或 AR 目标。
- 属性名称、量化范围、track/bar 层级和轨道类型必须符合当前 analyzer。
- 同一绝对小节在不同生成轨道上的拍号控制必须一致。
- 至少存在一个实际生成目标。

值得注意的是，当前 `from_dict()` 对部分未知 JSON 字段采取忽略而非拒绝。LegaSynth 适配器仍应在调用前用自身的严格 Pydantic/JSON Schema 拒绝未知字段，避免拼写错误静默失效。

## 4. LegaSynth 的建议映射

### 4.1 第一阶段故事 Agent → MuseCoco

当前不要让 LLM 输出 MuseCoco 内部向量或 token。首版边界保持：

```json
{
  "theme_families": [
    {
      "theme_family_id": "theme-A",
      "base_symbol": "A",
      "seed_bars": 4,
      "musecoco_attribute_targets": {
        "I1s2": ["piano"],
        "R1": "not_danceable",
        "R3": "low",
        "S2s1": "chopin",
        "S4": ["classical"],
        "B1s1": "1-4",
        "TS1s1": "4/4",
        "K1": "minor",
        "T1s1": "moderate",
        "P4": 2,
        "EM1": "Q3",
        "TM1": "0-15"
      },
      "musecoco_text": "由 Python 固定模板生成的全属性英文文本"
    }
  ],
  "musecoco_requests": []
}
```

MuseCoco 适配器冻结后，再由确定性代码完成：

```text
theme_family + 7 类 LLM 语义属性
  -> Python 从 seed_bars 和全局拍号/BPM/调式补齐 B1s1/TS1s1/K1/T1s1/TM1
  -> Python 渲染 12 类全属性英文 text
  -> predict.json
  -> 官方 text2attribute 模型
  -> one-hot 形状与枚举校验
  -> infer_test.bin
  -> 官方 attribute2music 模型
  -> 输出 MIDI 解析、精确小节数/拍号/轨道/哈希验证
  -> normalize-tempo 将全部 Set Tempo 统一为 content_plan.json 的 global.tempo_bpm
  -> 速度归一化后的动机 MIDI 交给第二阶段装配器
```

适配器对外 Schema 可以包含 `request_id`、`theme_family_id`、`text`、`seed_bars`、随机种子和 checkpoint，但必须明确标记为 **LegaSynth adapter schema**，不能称为 MuseCoco 官方 Schema。

速度归一化已经由 `stage1_story_agent normalize-tempo` 实现。它只重写 Standard MIDI File 的速度元事件并保持音符 tick 坐标，不负责量化、裁剪小节或音频时间拉伸。每个 MuseCoco 输出都应先使用同一计划 BPM 归一化，再进入框架装配；原始 MIDI 应保留用于审计。

### 4.2 第一阶段统一计划＋第二阶段框架装配器 → MIDI-GPT

全流程唯一的第一阶段 LLM 已在同一次规划中输出 `music_plan.json` 的语义和区域；第二阶段不调用 LLM，只由确定性 Python 装配器结合实际素材生成最终 `Score` 与 `GenerationRequest`：

```text
music_plan.json
  + 心音事件素材
  + MuseCoco 动机 MIDI
  -> 统一 PPQ/BPM/拍号/小节数
  -> 构造 Score
  -> 将保护区与填充区编译为 TrackPrompt
  -> 查询 checkpoint 属性大小并映射语义控制
  -> engine.session(score, request).run()
  -> 逐事件验证保护内容
```

推荐 prompt 策略：

| 轨道角色 | `bars` | `ignore` | 用途 |
|---|---|---:|---|
| 心音保护锚点 | `[]` | `false` | 保持不生成，同时作为节奏条件 |
| 已批准动机 | `[]` 或只填充允许变化的小节 | `false` | 作为主题条件 |
| 伴奏/和声/低音待生成区 | 明确小节列表 | `false` | infill 或右后缀 AR |
| 不希望模型看到的辅助轨 | `[]` | `true` | 完全从 token 上下文排除 |

`ignore=False` 和空 `bars` 只表示“不生成但作为上下文”，不是形式化的不可变证明。生成前后仍必须比较所有保护轨的音高、起点、时值、力度和轨道元数据。

## 5. 尚未冻结的问题

1. MuseCoco 是否接受“文本入口”作为正式适配方案，还是绕过 text2attribute、直接构造 one-hot；后者更可控，但与官方内部实现耦合更深。
2. MuseCoco 生成结果如何裁剪为精确 2/4/8 小节动机，以及裁剪后是否破坏终止、拍号和音乐完整性。
3. MuseCoco 14.5 GB checkpoint、旧 Python/Fairseq 环境在 Windows 上的部署方式；很可能需要 Linux/WSL 或容器。
4. MIDI-GPT 固定使用 `yellow` 还是允许 `prism/expressive`；后两者当前仍是训练中快照。
5. 第二阶段语义属性如何映射为 checkpoint 专属量化等级；映射必须基于运行时 labels，而非固定数字。
6. 心音轨作为 drum track 参与上下文时，是否会显著挤占 token 窗口，以及需要何种降采样/分步策略。

## 6. 官方来源

### MuseCoco

- [MuseCoco 官方 README（固定 commit）](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/README.md)
- [`predict.sh`](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/1-text2attribute_model/predict.sh)
- [文本模型数据读取与预测输出 `main.py`](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/1-text2attribute_model/main.py)
- [`stage2_pre.py`：JSON 预测到 pickle 输入](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/1-text2attribute_model/stage2_pre.py)
- [`att_key.json`](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/1-text2attribute_model/data/att_key.json)
- [`num_labels.json`](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/1-text2attribute_model/num_labels.json)
- [属性到音乐交互脚本](https://github.com/microsoft/muzic/blob/a2efda0bfb297b1520282a61ca12513dc3517744/musecoco/2-attribute2music_model/linear_mask/interactive_dict_v5_1billion.py)
- [官方 MuseCoco 论文](https://arxiv.org/abs/2306.00110)
- [官方链接的 text-to-attribute checkpoint](https://huggingface.co/XinXuNLPer/MuseCoco_text2attribute)
- [官方链接的 attribute-to-music checkpoint](https://huggingface.co/XinXuNLPer/MuseCoco_attribute2music)

### MIDI-GPT

- [MIDI-GPT 官方仓库（固定 commit）](https://github.com/Metacreation-Lab/MIDI-GPT/tree/0f969d1a7045b306571e14dd1dc7835c00005bd3)
- [`Score/Track/Bar/Note` 数据类与序列化](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/src/python/midigpt/_types.py)
- [`GenerationRequest/TrackPrompt/InferenceConfig`](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/src/python/midigpt/inference/config.py)
- [请求校验实现](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/src/python/midigpt/inference/validation.py)
- [HTTP `/generate` 实现](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/src/python/midigpt/http/server.py)
- [官方模型兼容矩阵](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/docs/models.md)
- [官方推理说明](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/src/python/midigpt/inference/inference.md)
- [PyPI 包元数据 `pyproject.toml`](https://github.com/Metacreation-Lab/MIDI-GPT/blob/0f969d1a7045b306571e14dd1dc7835c00005bd3/pyproject.toml)
