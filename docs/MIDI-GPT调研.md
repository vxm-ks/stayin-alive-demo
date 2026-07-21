# MIDI-GPT 调研与工程接入说明

> 调研对象：Metacreation Lab 的 MIDI-GPT（AAAI-25）  
> 调研日期：2026-07-16；2026-07-17 使用官方 `0.3.2` wheel 复核  
> 文档性质：论文与当前官方工程实现的中文整理；版本、模型和许可证状态会继续变化。

## 1. 结论摘要

MIDI-GPT 是面向计算机辅助作曲的多轨符号音乐生成系统。它采用 GPT-2 类自回归 Transformer，但处理的是经过结构化 token 化的 General MIDI，而不是自然语言或音频波形。它主要解决以下问题：

- 根据已有轨道生成新轨道；
- 对指定轨道、指定小节进行 infilling（补写或重写）；
- 从空白乐谱自回归生成多轨 MIDI；
- 控制乐器、音符密度、复音程度、音符时值等属性；
- 新版模型进一步控制调号、音域、静音、音级集合、风格、力度与微时序。

其关键设计不是简单扩大 Transformer，而是改变多轨音乐的序列表示：每条轨道内部按时间排列，轨道之间串联，而不是把所有轨道事件按全局时间交错。这使轨道和小节的边界显式可见，便于进行局部补写和属性控制。[1][2]

截至 2026-07-16，官方工程已经发展为可通过 PyPI 安装的 `midigpt 0.3.2`，支持 Python API、HTTP 服务和实时 OSC 服务；论文描述的是 2025 年模型和实验，不能把论文参数直接等同于当前所有 checkpoint。[3][4]

## 2. 名称辨析

### 2.1 本文所指 MIDI-GPT

- 项目：Metacreation Lab / Simon Fraser University；
- 论文：*MIDI-GPT: A Controllable Generative Model for Computer-Assisted Multitrack Music Composition*；
- 会议：AAAI 2025；
- 作者：Philippe Pasquier、Jeff Ens、Nathan Fradet、Paul Triana、Davide Rizzotti、Jean-Baptiste Rolland、Maryam Safi；
- DOI：`10.1609/aaai.v39i2.32138`；
- 官方代码：<https://github.com/Metacreation-Lab/MIDI-GPT>；
- Python 包：<https://pypi.org/project/midigpt/>；
- 模型：<https://huggingface.co/Metacreation/MIDI-GPT>。

### 2.2 不应混淆的同名项目

`wileymc/midi-gpt` 是另一个 TypeScript Web 应用，通过 OpenAI API 把文本请求转换为 MIDI。它不是 AAAI-25 论文中的模型，也不使用相同的训练数据、token 化和 checkpoint。仓库地址：<https://github.com/wileymc/midi-gpt>。[10]

此外，`MusicGPT`、`MIDI-LLM`、`M^6(GPT)^3` 等名称相近的系统也不是本文对象。

## 3. 输入、输出与生成任务

### 3.1 数据模态

MIDI-GPT 的输入和输出都是符号音乐：

- 输入：General MIDI 文件或 `midigpt.Score`；
- 输出：多轨 MIDI；
- 不直接读取 WAV/MP3；
- 不直接生成可听音频；
- 最终音色仍取决于 DAW、SoundFont 或软音源。

论文选择 General MIDI 是因为它是广泛兼容的符号音乐交换格式。模型覆盖 128 种 General MIDI program，并支持鼓轨；它不要求固定的乐器组合。[1]

### 3.2 四类生成任务

论文讨论四种任务：[1]

1. **无条件生成**：不提供已有音乐，从头生成。
2. **续写**：根据时间上先出现的内容继续生成。
3. **Infilling**：保留前后文和其他轨道，只补写选定空缺。
4. **属性控制**：通过乐器、密度、复音、时值、风格等条件约束结果。

当前 API 对工程上最有价值的是小节级 infilling。调用者可以指定某条轨道的第 4–7 小节需要重写，同时把另一条轨道标记为保持不变。[3]

## 4. 核心表示方法

### 4.1 Multi-Track 表示

每个小节包含音符事件；多个小节组成轨道；多个轨道组成整段作品。主要 token 包括：[1]

- `NOTE_ON`：128 个音高；
- `TIME_POSITION`：音符在当前小节内的绝对起点；
- `DURATION`：音符时值；
- `BAR_START` / `BAR_END`：小节边界；
- `TRACK_START` / `TRACK_END`：轨道边界；
- `INSTRUMENT`：轨道的 MIDI program；
- `CONTROL`：密度、复音、时值等条件。

论文版本使用 96 个 `TIME_POSITION` 和 96 个 `DURATION` token，时间量化范围以十六分音符三连音为基本增量。轨道信息与 `NOTE_ON`、`TIME_POSITION`、`DURATION` 解耦，因此词表不会随轨道数线性膨胀。[1]

需要注意：token 序列中的轨道虽然前后串联，但播放时仍是同时发生的多轨音乐，并非把轨道首尾拼接播放。

### 4.2 Bar-Fill 表示

Multi-Track 表示便于逐轨生成，但难以补写轨道中间的小节。Bar-Fill 表示采用以下方式：[1]

1. 用 `FILL_IN` 替换待生成小节；
2. 保留其他轨道和前后小节作为上下文；
3. 在序列末尾依次生成缺失小节；
4. 使用 `FILL_START` / `FILL_END` 标出每个生成片段。

因为缺失小节在输入中的位置和输出顺序都是确定的，模型可以进行跨轨道、跨时间的局部补写。

### 4.3 表情性扩展

论文的 expressive 表示增加：[1]

- 128 个 `VELOCITY` token，表示 MIDI 力度；
- `DELTA` token，表示真实起音相对量化网格的微小时差。

这允许模型同时表达力度变化和 microtiming，而不必把整个时间网格提高到非常高的分辨率。当前官方 `expressive` checkpoint 仍标记为训练中，因此其稳定性应与已完成的 `yellow` 模型区别对待。[4]

## 5. 控制机制

论文把控制分为三类：[1]

| 类型 | 例子 | 含义 |
|---|---|---|
| 分类控制 | 乐器、风格 | 从离散类别中选择 |
| 数值控制 | 音符密度 | 选择按乐器统计得到的等级 |
| 范围控制 | 音符时值、复音程度 | 约束统计分布落入给定区间 |

当前 API 中，控制值通过 `TrackPrompt.attributes`、`controls`、`bar_attributes` 和 `bar_controls` 传入。属性值通常不是原始物理量，而是 checkpoint encoder 定义的量化等级。使用前应查询当前模型支持的属性大小和标签，不能假设所有 checkpoint 共享相同编号。[3]

模型还支持规则采样形成的硬约束，例如 `polyphony_hard_limit`。硬约束通过屏蔽不合法 token 实现，比单纯的“软属性提示”更可靠。

## 6. 论文模型与训练

论文版本基于 GPT-2 架构，主要配置为：[1]

| 项目 | 论文配置 |
|---|---:|
| Transformer 层数 | 6 |
| 注意力头数 | 8 |
| embedding 维度 | 512 |
| 上下文窗口 | 2048 token |
| 参数量 | 约 2000 万 |
| 训练片段 | 随机 4 或 8 小节 |
| batch size | 32 个 MIDI 文件 |
| infilling 概率 | 75% |
| 训练硬件 | 4 张 V100 |
| 典型训练时间 | 2–3 天至收敛 |

训练期间还会：

- 随机选择 2–12 条轨道；
- 最多遮蔽片段中 75% 的小节；
- 对非鼓轨随机移调；
- 随机打乱轨道顺序，使模型学习不同轨道之间的条件关系；
- 同时预测小节、轨道和乐器相关 token。

2048 token 在论文中通常对应约 8–16 小节，但实际长度取决于轨道数和音符密度。长曲需要滑动窗口和多轮续写；单次生成不具备完整歌曲级全局规划。[1]

## 7. 数据集

论文模型使用 GigaMIDI。GigaMIDI 论文报告的数据规模超过 140 万个唯一 MIDI、18 亿个音符事件和 530 万条轨道。[6]

截至调研日期，Hugging Face 上的数据集为 gated dataset：

- 需要登录并同意访问条件；
- 页面标记为 `CC-BY-NC-4.0`；
- 条款限制为非商业研究或教育用途；
- 数据包含来自网络的 MIDI，存在风格分布和版权来源风险。[7]

论文伦理声明指出，训练语料偏向可由 MIDI 表达且在网络上可获得的音乐，流行和西方调性材料相对较多，许多非西方音乐风格不足或缺失。[1]

## 8. 论文评估结论

### 8.1 原创性

研究使用 piano-roll Hamming distance 比较生成结果与训练集，并用 Jaccard index 比较 infilling 前后的材料。论文结论是：生成片段越长，直接复制训练材料或原片段的概率越低；生成 4 小节或更多时，可以较可靠地产生原创变化。[1]

这不等于法律意义上的“不侵权保证”。论文的检索实验采用特定 piano-roll 距离和近似筛选，也不能发现所有旋律、和声或风格层面的相似性。

### 8.2 风格相似度

研究使用 StyleRank 评估生成结果与数据集风格的相似程度。温度为 1.0 时，infilling 结果在该指标上与原有材料接近；温度高于 1.0 后，生成结果更容易偏离数据集风格，而且生成小节越多，偏离越明显。[1]

### 8.3 属性控制

论文的 100 次、8 小节生成实验表明：[1]

- 音符密度控制通常能命中目标等级或相邻等级；
- 音符时值范围控制总体有效；
- 复音程度的软控制相对较弱；
- 需要严格限制复音时，应使用规则采样的硬上限。

因此，属性控制应被视为统计约束，而不是所有音符都必然满足的形式验证。

## 9. 2026 年官方工程状态

### 9.1 Python 包

截至 2026-07-16：[3][4]

- PyPI 最新版：`midigpt 0.3.2`；
- 发布日期：2026-07-09；
- Python 要求：`>=3.10`；
- 官方预编译 wheel：CPython 3.10–3.12；
- 平台：Windows AMD64、Linux x86_64、macOS x86_64/arm64；
- 推理依赖包括 PyTorch、Hugging Face Hub、Safetensors、tqdm；
- 官方包提供 Sigstore/PyPI Trusted Publishing provenance。

本项目当前存在 Python 3.10 与 Conda Python 3.13 两套解释器。由于官方 wheel 明确覆盖 3.10–3.12，建议为 MIDI-GPT 固定使用 Python 3.10 或 3.12 独立环境，不要直接复用当前 3.13 Conda 环境。

### 9.2 当前 checkpoint

官方仓库当前列出的模型：[3][4][5]

| 名称 | 上下文小节 | 状态 | 主要控制 | 文件规模（HF 当前文件） |
|---|---|---|---|---:|
| `yellow` | 4、8 | 完成 | 密度、复音范围、时值范围 | small 约 82.5 MB；medium 约 351 MB |
| `prism_medium` | 4、8、12、16 | 训练中 | 调号、音域、静音、逐小节密度/复音、音级集合、风格等 | 约 358 MB |
| `expressive` | 4、8、12、16 | 训练中 | prism 类控制，加力度与微时序能力 | 约 359 MB |
| `ghost` | 4、8、12、16 | 未发布 | 规划中的架构 | 无 |

`InferenceConfig.model_dim` 表示上下文包含多少小节，不是 embedding 维度。其值必须属于所选模型的 `num_bars_map`。[3]

### 9.3 推理接口

核心类型：[3]

| 类型 | 用途 |
|---|---|
| `Score` / `Track` / `Bar` | 内部乐谱数据结构 |
| `InferenceEngine` | 加载 checkpoint 并创建推理 session |
| `GenerationRequest` | 整体生成请求 |
| `TrackPrompt` | 指定每条轨道的生成小节、属性和控制 |
| `InferenceConfig` | temperature、top-p、mask mode、上下文小节数等 |
| `SamplingSession` | 实际 token 采样循环 |

采样过滤器包括 `top_k`、`top_p`、`mask_k` 和 `mask_p`。后两项会主动移除最可能 token，以增加新颖性，但也可能降低音乐连贯性。[3]

### 9.4 服务模式

官方代码提供：[3]

- Python 本地推理 API；
- FastAPI/uvicorn HTTP 服务：`GET /health`、`GET /info`、`POST /generate`；
- OSC 实时服务，可接收轨道、音符、小节结束和参数消息，并返回生成音符与逐小节统计。

HTTP API 是无状态的，每次请求携带完整 score 和生成参数。对于桌面 DAW 或本项目批处理，直接使用 Python API 更简单；跨进程或多语言集成时再考虑 HTTP。

## 10. 安装与最小示例

### 10.1 Windows 独立环境

```powershell
C:\Python310\python.exe -m venv .venv-midigpt
.\.venv-midigpt\Scripts\python.exe -m pip install --upgrade pip
.\.venv-midigpt\Scripts\python.exe -m pip install "midigpt[inference]==0.3.2"
```

首次调用 `from_pretrained()` 会从 Hugging Face 下载 checkpoint，之后缓存在 `~/.cache/huggingface/hub/`。模型权重下载和 PyTorch 安装会占用明显的磁盘空间。[3]

### 10.2 从空白乐谱生成四小节

以下接口按 `midigpt 0.3.2` 官方示例整理：[3]

```python
from midigpt import Bar, Score, Track
from midigpt.inference import (
    GenerationRequest,
    InferenceConfig,
    InferenceEngine,
    TrackPrompt,
)

engine = InferenceEngine.from_pretrained("yellow")

score = Score(
    tracks=[
        Track(bars=[Bar() for _ in range(4)]),
    ]
)

request = GenerationRequest(
    tracks=[TrackPrompt(id=0, bars=[0, 1, 2, 3])],
    config=InferenceConfig(
        model_dim=4,
        temperature=1.0,
        top_p=0.95,
        mask_mode="attention",
    ),
)

result = engine.session(score, request).run()
result.to_midi("output.mid")
```

### 10.3 补写现有 MIDI

```python
from midigpt import Score
from midigpt.inference import (
    GenerationRequest,
    InferenceConfig,
    InferenceEngine,
    TrackPrompt,
)

engine = InferenceEngine.from_pretrained("yellow")
score = Score.from_midi("input.mid")

request = GenerationRequest(
    tracks=[
        TrackPrompt(id=0, bars=[4, 5, 6, 7]),
        TrackPrompt(id=1, bars=[], ignore=True),
    ],
    config=InferenceConfig(
        model_dim=8,
        temperature=1.0,
        top_p=0.95,
        mask_mode="attention",
    ),
)

result = engine.session(score, request).run()
result.to_midi("infilled.mid")
```

### 10.4 HTTP 服务

```powershell
python -m pip install "midigpt[http]==0.3.2"
midigpt-http --pretrained yellow --port 8000
```

启动后可访问 `http://localhost:8000/docs` 查看交互式 API 文档。[3]

## 11. 许可证与合规

许可证必须按“代码、权重、数据”分别判断，不能只看到 GitHub 的 MIT 就推断所有内容都可商用。

| 对象 | 当前页面标记 | 工程含义 |
|---|---|---|
| GitHub 源代码 / PyPI 包 | MIT | 允许较宽松地使用和修改代码，但仍需保留许可证 |
| Hugging Face 模型仓库 | CC-BY-NC-4.0 | 当前页面标记为非商业；使用具体权重前应再次核对模型卡和文件许可 |
| GigaMIDI 数据集 | CC-BY-NC-4.0 + gated 条款 | 仅非商业研究/教育，并要求同意访问条件 |
| 2025 论文中的模型描述 | Open RAIL-M；论文称当时限制非商业使用 | 反映论文发表时状态，不应覆盖当前具体 artifact 的许可证 |

当前不同页面的许可证表述并不完全一致。最保守的做法是：

1. 固定所用代码版本和 checkpoint 文件；
2. 保存当日 LICENSE、模型卡和数据条款；
3. 商业使用前取得法律审查或作者的明确许可；
4. 不把论文的“原创性实验”视为版权免责证明。

## 12. 能力边界

### 12.1 它擅长什么

- 多轨 MIDI 局部补写；
- 在现有编曲上下文中增加伴奏轨；
- 生成固定 4/8 小节的候选素材；
- 根据统计属性快速产生多个可供人工筛选的版本；
- 通过 MIDI 接入 DAW、软音源和乐谱软件。

### 12.2 它不解决什么

- 不分析 WAV 的 BPM、首拍或动机边界；
- 不把自然语言直接转换为成熟作品；
- 不直接输出人声或高质量音频；
- 不保证长达数分钟的全局曲式一致性；
- 不保证每次生成都符合和声、配器或演奏法要求；
- 不保证生成结果没有版权或数据来源风险。

### 12.3 已知技术限制

- 自回归采样随 token 数增加而变慢；
- 上下文受 checkpoint 的小节映射和 token 窗口限制；
- 多轨、高密度素材更快耗尽上下文；
- 复音软控制弱于密度和时值控制；
- 高 temperature、`mask_k` 或 `mask_p` 会提高多样性，也可能破坏连贯性；
- `prism_medium` 和 `expressive` 当前仍标记为训练中，不应默认用于稳定生产流程。

## 13. 与当前 LegaSynth 项目的关系

当前仓库已经具备：

- `heart_extraction`：从 WAV 提取心音事件；
- `tempo_bar_renderer`：把心音事件排列成固定 BPM 小节；
- `heartbeat_midi_exporter`：输出心跳 MIDI 与患者专属 SoundFont；
- `midi_motif_detector`：检测现有 MIDI 的第一动机及重复；
- `wav_track_mixer`：叠加最终 WAV。

MIDI-GPT 可以作为“生成/补写层”，但不能替代这些确定性分析模块。

### 13.1 推荐管线

```text
音乐 MIDI
  -> midi_motif_detector：确定第一动机和候选重复
  -> MIDI-GPT：补写伴奏、生成变奏或延展轨道
  -> 结构校验：拍号、BPM、音域、复音、空小节、动机相似度
  -> DAW / SoundFont：渲染为 WAV
  -> wav_track_mixer：与真实心音或其他音频混合
```

心跳 MIDI 可以作为鼓轨上下文，让 MIDI-GPT 生成其他乐器轨；也可以把已有音乐轨设为 `ignore=True`，仅重写指定轨道和小节。由于当前心跳导出通常只有一个小节，在送入模型前需要复制为所选 checkpoint 支持的 4 或 8 小节上下文。

### 13.2 动机工作流

`midi_motif_detector` 与 MIDI-GPT 的职责应明确分开：

1. 检测器只负责找出动机边界和重复位置；
2. MIDI-GPT 根据动机所在小节生成变奏或伴奏；
3. 生成后再次运行检测器，检查动机是否仍可识别；
4. 对相似度、音符密度和复音设置项目阈值；
5. 不合格结果重新采样，而不是直接进入渲染。

### 13.3 建议的第一阶段验证

1. 使用 `yellow`，避免一开始依赖训练中的模型。
2. 固定 4 小节、单旋律轨和单伴奏轨。
3. 设置 `temperature=1.0`、`top_p=0.95`，关闭 novelty mask。
4. 每个请求生成多个候选并保留随机种子和参数。
5. 验证输出 MIDI 可解析、无空轨、拍号一致、复音不超限。
6. 用现有动机检测器检查生成前后动机关系。
7. 通过后再扩展到鼓轨、8 小节和实时 OSC。

## 14. 工程风险清单

- **环境风险**：官方 wheel 只明确覆盖 CPython 3.10–3.12；当前项目的 Python 3.13 Conda 环境不宜直接使用。
- **下载风险**：首次推理需要下载 PyTorch 和 80–360 MB 级 checkpoint。
- **版本风险**：2026 年包更新较快，应固定 `midigpt==0.3.2` 和 checkpoint 文件名。
- **接口风险**：不同 checkpoint 的 `num_bars_map`、encoder 和属性编号不同。
- **音乐风险**：生成结果是候选素材，不应跳过人工听审和结构校验。
- **法律风险**：代码、权重和数据许可证不同，当前权重与数据页面均包含非商业限制。
- **复现风险**：应保存随机种子、sampling 参数、模型 SHA-256、输入 MIDI SHA-256 和软件版本。

## 15. 参考资料

1. Pasquier et al., *MIDI-GPT: A Controllable Generative Model for Computer-Assisted Multitrack Music Composition*, arXiv HTML：<https://arxiv.org/html/2501.17011>
2. AAAI 2025 正式论文页面与 DOI：<https://ojs.aaai.org/index.php/AAAI/article/view/32138>
3. Metacreation Lab 官方 GitHub：<https://github.com/Metacreation-Lab/MIDI-GPT>
4. PyPI `midigpt`：<https://pypi.org/project/midigpt/>
5. Hugging Face 模型仓库：<https://huggingface.co/Metacreation/MIDI-GPT>
6. Lee et al., *The GigaMIDI Dataset with Features for Expressive Music Performance Detection*：<https://arxiv.org/abs/2502.17726>
7. Hugging Face GigaMIDI 数据集：<https://huggingface.co/datasets/Metacreation/GigaMIDI>
8. Metacreation Lab MIDI-GPT 项目介绍：<https://www.metacreation.net/projects/midi-gpt>
9. 官方文档站：<https://metacreation-lab.github.io/MIDI-GPT/>
10. 同名但无关的 `wileymc/midi-gpt`：<https://github.com/wileymc/midi-gpt>

## 16. 论文引用

```bibtex
@inproceedings{pasquier2025midigpt,
  title     = {MIDI-GPT: A Controllable Generative Model for
               Computer-Assisted Multitrack Music Composition},
  author    = {Philippe Pasquier and Jeff Ens and Nathan Fradet and
               Paul Triana and Davide Rizzotti and Jean-Baptiste Rolland and
               Maryam Safi},
  booktitle = {Proceedings of the AAAI Conference on Artificial Intelligence},
  volume    = {39},
  number    = {2},
  pages     = {1474--1482},
  year      = {2025},
  doi       = {10.1609/aaai.v39i2.32138}
}
```
