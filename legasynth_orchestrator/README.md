# LegaSynth 本地全流程主控

该模块在没有前端的情况下串联完整业务流程。Stage 2 消费 Stage 1 的正式任意曲式计划，不假定固定段数或总小节数；主控同时只允许一个任务运行。

## 真实运行

```powershell
D:\conda\python.exe -m legasynth_orchestrator `
  --story "你的故事" `
  --heartbeat-wav "D:\private\heartbeat.wav" `
  --rhythm-plan ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json" `
  --render-plan ".\legasynth_orchestrator\examples\single_patient_render_plan.json"
```

主控默认使用正式模式：`story_request.json` 写入 `test_mode=false`，且不会向
Stage 1 传递 `--test-mode`。正式模式由故事决定曲式、主题和音乐属性。

如需复现固定的 48 小节 A–B–A 测试档，必须显式加入：

```powershell
--test-mode
```

测试模式仍会真实调用 DeepSeek、MuseCoco、MIDI-GPT 和 Stage 3；它不同于
下文完全不调用模型的 `--dry-run`。每个 `job.json` 都会记录
`generation_mode=production|test|dry_run`。

主控依次执行：

1. 原始 WAV 到 `heartbeat_package`；
2. DeepSeek 故事规划、WSL MuseCoco 生成与 8 小节后处理；
3. Stage 2 自动发现计划引用的全部主题家族，按任意 section 序列装配，删除输入原有通道 10，重建心跳轨并只在 editable 范围运行 MIDI-GPT；
4. Stage 3 读取带哈希 handoff，使用 FluidSynth 和真实心音素材生成 `final_mix.wav`。

正式模式下，Stage 1 会先执行故事驱动的曲式规模决策：在 1–6 段之间选择长度，
并确定主题引入/复现/变奏/发展关系，再生成详细计划。例如故事内容回到先前记忆时，
可形成 `A-B-A-C` 而不是机械地产生四个新主题。测试模式不经过该自动决策，仍固定
为 48 小节 `A-B-A`。规模和复用依据写入
`stage1/story-audit/form_scale_decision.json`。

调性后处理通过 `--tonality-policy soft|strict|loose` 选择。默认 `soft`：先尝试检测实际主音与调式并移到计划调性；检测或改写不可靠时保留速度归一化 MIDI、写入审计警告并继续流程。`strict` 在相同情况下中止；`loose` 完全跳过检测与改写。

Stage 2 尾部的整体重复检查通过
`--stage2-repetition-mode off|detect|regenerate` 选择，默认 `off`。
`detect` 只报告连续两小节音高织体的过度重复；`regenerate` 对计划允许编辑的位置局部调用
MIDI-GPT，并保留主题、终止式和通道 10 心跳事件。常用完整运行参数为：

```powershell
--stage2-repetition-mode regenerate `
--stage2-repetition-threshold 0.82 `
--stage2-repetition-max-occurrences 2 `
--stage2-repetition-candidates 4
```

默认从外层工作区的 `tools/` 查找 FluidSynth 与 `GeneralUser-GS.sf2`，也可以通过 `--fluidsynth`、`--general-sf2` 显式覆盖。

主控启动时会读取仓库根目录中不提交 Git 的 `.env`。当前 Windows 原生 MIDI-GPT 部署可配置为：

```dotenv
LEGASYNTH_MIDIGPT_PYTHON=D:\path\to\stage2\.conda_midigpt\python.exe
LEGASYNTH_MIDIGPT_MODEL=yellow
HF_HOME=D:\path\to\huggingface-cache
```

也可以用 `--midigpt-python` 和 `--midigpt-model` 临时覆盖。Stage 2 由该解释器启动，其内部补全子进程继续使用同一个 `sys.executable`，因此不需要激活 Conda 环境。MIDI-GPT 使用 PyPI Python API，不依赖 HTTP 服务或源码仓库。

## 不调用模型的流程验证

```powershell
D:\conda\python.exe -m legasynth_orchestrator `
  --story "dry run" `
  --heartbeat-wav "D:\private\heartbeat.wav" `
  --rhythm-plan ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json" `
  --render-plan ".\legasynth_orchestrator\examples\single_patient_render_plan.json" `
  --dry-run
```

`--dry-run` 不启动 DeepSeek、MuseCoco、MIDI-GPT 或 FluidSynth，只建立合成产物并验证状态推进、目录、哈希与交接文件。

## 任务目录与失败语义

每次运行创建 `runtime/jobs/<job_id>/`，包含只读副本输入、逐阶段日志、Stage 1/2/3 产物和原子更新的 `job.json`。`runtime/active_job.lock` 保证单任务运行。任何阶段失败都会停止后续阶段，`job.json` 记录失败阶段、错误类型和简明信息。

阶段超时相互独立，默认分别为：心音处理 900 秒、Story/MuseCoco 7200 秒、Stage 2 7200 秒、Stage 3 1800 秒。子进程始终以参数数组和 `shell=False` 启动，故事文本不会被拼入 shell 命令。

## 离线测试

```powershell
D:\conda\python.exe -m unittest discover -s .\legasynth_orchestrator\tests -v
```

测试包括完整 dry-run，以及使用假执行器经过真实分支构造四个阶段命令；两者都不会调用任何模型。
