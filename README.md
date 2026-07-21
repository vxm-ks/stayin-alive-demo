# LegaSynth Track B：心音处理与节拍小节生成

本仓库是可公开分享的源码版本。它包含三阶段 Python 模块、Stage 1
Story Agent、离线测试、JSON Schema、MuseCoco 任务适配器和部署文档；不包含
患者音频、生成输出、API Key、SoundFont、FluidSynth 二进制、MuseCoco/MIDI-GPT
权重或官方 MuseCoco 源码。

## 克隆与离线安装

要求 Windows 或 Linux、Python 3.11+。Windows 用户如需运行 MuseCoco，还需要
WSL2 Ubuntu。

```powershell
git clone https://github.com/OWNER/legasynth.git
Set-Location .\legasynth
.\scripts\setup_windows.ps1 -Python python
.\scripts\test_offline.ps1 -Python python
```

在线 Story Agent 使用前，将 `.env.example` 复制为 `.env` 并在本机填写
`DEEPSEEK_API_KEY`。`.env` 已被 Git 忽略。

MuseCoco 官方代码与权重采用外部部署，LegaSynth 不修改或转存它们。完整步骤见
[`docs/MUSECOCO_DEPLOYMENT.md`](docs/MUSECOCO_DEPLOYMENT.md)。仓库级开发 Agent
规则见 [`AGENTS.md`](AGENTS.md)。

## 数据与模型边界

- 真实心音只能放在 Git 忽略的私有数据目录或仓库外部。
- 公共测试在运行时生成合成 WAV/MIDI，不依赖患者材料。
- Stage 3 所需 FluidSynth、通用 SF2 和患者 heartbeat package 由用户通过命令行传入。
- MuseCoco/MIDI-GPT 权重保存在 WSL 或模型缓存中，不进入 Git/LFS。
- 发布前运行 `.\scripts\audit_repository.ps1` 检查大文件、密钥和本机路径。

项目正式流程包含九个已实现的处理、规划、框架与渲染模块：

1. [`heart_extraction/`](heart_extraction/)：从原始 WAV 自动分析稳定区间，筛选 S1/S2，输出 Stable、Authentic、Regularized 三组 Base/Clean/Enhanced 音频及每个音频的时间—能量图。
2. [`tempo_bar_renderer/`](tempo_bar_renderer/)：读取第一模块生成的 9 个 WAV 中任意一个，先返回建议 BPM 范围，再让用户选择目标 BPM，最后生成一个 4/4、3/4、2/4 小节；不拉伸 S1/S2 本体。
3. [`wav_track_mixer/`](wav_track_mixer/)：将任意两个 WAV 叠加为一个音轨，自动统一采样率和声道，并提供分轨增益、第二轨延迟和峰值保护。
4. [`heartbeat_midi_exporter/`](heartbeat_midi_exporter/)：读取第二模块生成的节拍分析 JSON/事件 CSV，输出 4/4、3/4、2/4 MIDI、患者 S1/S2 专属 SF2 音色库及完整映射审计清单；不会重新检测或修改心音。
5. [`midi_motif_detector/`](midi_motif_detector/)：分析通用 MIDI 的旋律轨，通过移调无关的音程与节奏重复检测第一动机的起止、重复位置和置信度。
6. [`stage1_story_agent/`](stage1_story_agent/)：完整的第一阶段故事规划 Agent；已包含故事分析、曲式与主题家族、MuseCoco 属性编码、DeepSeek 后端、分消费者产物，以及 MuseCoco MIDI 的强制 BPM/主音修正。
7. [`midigpt_scaffold_builder/`](midigpt_scaffold_builder/)：第二阶段确定性框架装配器 `0.1.1`；将内容计划、结构化音乐计划、心音事件和动机清单编译成等长多轨 `Score`、`GenerationRequest`、HTTP 请求体、可视 MIDI 与审计产物，不依赖 MIDI-GPT 权重。
8. [`heartbeat_stage1/`](heartbeat_stage1/)：心音第一部分总入口；读取原始 WAV，严格生成 `regularized_events_enhanced`，再按用户/LLM提供的 `rhythm_plan.json` 输出真实事件心跳小节、MIDI、时间—能量图和可供后置鼓轨渲染使用的 `heartbeat_package`。
9. [`stage3_midi_renderer/`](stage3_midi_renderer/)：读取第二阶段的完整 MIDI；支持旧版心跳 SoundFont 统一渲染，以及新版一次传入多个 `heartbeat_package`、按 plan 选择或轮换真实 S1/S2 音色、独立控制音乐/心跳响度并输出最终 WAV。完整 MIDI 始终是事件时序权威。

MIDI-GPT 的论文、当前官方实现、模型、许可证和本项目接入建议见 [`docs/MIDI-GPT调研.md`](docs/MIDI-GPT调研.md)。

项目的完整目标流程、模块状态、数据契约、待办事项和 Agent 交接规则统一记录在 [`docs/项目全流程与Agent同步.md`](docs/项目全流程与Agent同步.md)。后续 Agent 开始跨模块工作前应先阅读该文档。

第二阶段交接请先阅读 [`docs/第二阶段MIDI-GPT框架编排交接Brief.md`](docs/第二阶段MIDI-GPT框架编排交接Brief.md)。

## 典型工作流

在本目录打开 PowerShell。

推荐使用第一部分总入口一次完成原始心音处理和节奏 token 封装：

```powershell
D:\conda\python.exe .\heartbeat_stage1\heartbeat_stage1.py `
  "D:\path\raw_patient.wav" `
  ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json"
```

详细的用户/LLM计划格式、保护真实音色的约束和输出结构见 [`heartbeat_stage1/README.md`](heartbeat_stage1/README.md)。下列命令仍保留为分模块调试入口。

三阶段正式边界见 [`docs/三阶段职责边界.md`](docs/三阶段职责边界.md)。Stage 2生成完整心跳鼓轨；Stage 3对包含该轨道的完整MIDI统一渲染混音。

```powershell
python .\heart_extraction\heart_sound_processor.py "D:\path\sample.wav" --output-dir ".\processor_validation"
```

在该分析组中选择想继续使用的一个 WAV，例如 Regularized Clean，然后运行：

```powershell
python .\tempo_bar_renderer\tempo_bar_renderer.py ".\processor_validation\样本编号_采集时间\样本编号_采集时间_regularized_events_clean.wav"
```

程序会显示检测 BPM、建议范围和技术范围，并提示输入目标 BPM。也可使用
`--target-bpm 120` 跳过交互。第二步只读所选 WAV，默认将结果写入
`tempo_bar_renderer\outputs\所选WAV文件名_bpm120_峰模式_事件库_bars\`，不会覆盖第一模块。

可用 `--beat-peak s1` 或 `--beat-peak s2` 让每拍只保留一个 S1 或 S2；完全交互运行时程序会询问峰模式，批处理未指定时默认 `both`。
单峰默认重复边界最平滑、质量评分最高的真实事件；可用 `--event-bank rotate` 改为轮换事件。

在 `--beat-peak both` 下可增加 `--pair-rhythm even`，将 S1 放在拍点、S2 放在半拍，使 S1→S2 与 S2→下一 S1 等间隔。该选项只移动事件位置，不改变任何 S1/S2 波形长度；默认 `--pair-rhythm natural` 保留原始生理相位。

第二模块还提供互不覆盖的三档响度处理：`--loudness-mode safe` 保持原线性增益，`loud` 加入并行压缩，`cinematic` 再加入完全由当前心音派生的低频身体层与谐波层。后两档以 4 倍过采样估计真峰值并控制到约 −1 dBTP；所有 WAV 仍各自生成时间—能量图，完整参数写入 JSON。

心音提取、节拍渲染和 MIDI 导出模块各自拥有 README、方法说明、第三方依赖说明和测试，便于分别引用与维护；混音模块的说明和测试位于其目录内。

把节拍小节转换为 MuseScore 可打开的 MIDI 和患者专属 SoundFont：

```powershell
D:\conda\python.exe .\heartbeat_midi_exporter\heartbeat_midi_exporter.py ".\tempo_bar_renderer\outputs\某一输出组\xxx_bar_analysis.json"
```

本步骤必须输入 `*_bar_analysis.json`，而不是 WAV。程序会自动找到同组 CSV，并把三个 MIDI、一个 SF2、S1/S2 审计 WAV 和映射 JSON 放入同一个 `heartbeat_midi_exporter\outputs\..._midi_bundle\` 文件夹。详细导入方法见 [`heartbeat_midi_exporter/README.md`](heartbeat_midi_exporter/README.md)。

合并两个 WAV 音轨时运行：

```powershell
python .\wav_track_mixer\wav_track_mixer.py "D:\path\track_a.wav" "D:\path\track_b.wav" "D:\path\mixed.wav"
```

详细的音量、延迟与采样率选项见 [`wav_track_mixer/README.md`](wav_track_mixer/README.md)。
