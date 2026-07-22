# LegaSynth 本地全流程主控

该模块在没有前端的情况下串联完整业务流程。当前只支持 Stage 2 测试合同：48 小节 `A-B-A`，每段为 8 小节主题加 8 小节 MIDI-GPT 填充，同时只允许一个任务运行。

## 真实运行

```powershell
D:\conda\python.exe -m legasynth_orchestrator `
  --story "你的故事" `
  --heartbeat-wav "D:\private\heartbeat.wav" `
  --rhythm-plan ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json" `
  --render-plan ".\legasynth_orchestrator\examples\single_patient_render_plan.json"
```

主控依次执行：

1. 原始 WAV 到 `heartbeat_package`；
2. DeepSeek 故事规划、WSL MuseCoco 生成与 8 小节后处理；
3. Stage 2 自动发现 A/B 主题，删除输入原有通道 10，重建心跳轨并运行 MIDI-GPT；
4. Stage 3 读取带哈希 handoff，使用 FluidSynth 和真实心音素材生成 `final_mix.wav`。

调性后处理通过 `--tonality-policy strict|loose` 选择。默认 `strict`：检测实际主音与调式，移到计划主音，并在大小调不一致时确定性修正自然音阶的第 3、6、7 级。`loose`：不检测、不改写生成 MIDI 的调性，只保留计划目标作为审计信息。

默认从外层工作区的 `tools/` 查找 FluidSynth 与 `GeneralUser-GS.sf2`，也可以通过 `--fluidsynth`、`--general-sf2` 显式覆盖。

主控启动时会读取仓库根目录中不提交 Git 的 `.env`。当前 Windows 原生 MIDI-GPT 部署可配置为：

```dotenv
LEGASYNTH_MIDIGPT_PYTHON=D:\LegaSynth\stage2\.conda_midigpt\python.exe
LEGASYNTH_MIDIGPT_MODEL=yellow
HF_HOME=D:\LegaSynth\stage2\.cache\huggingface
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
