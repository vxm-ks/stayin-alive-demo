# LegaSynth 本地全流程主控

该模块在没有前端的情况下串联完整业务流程。当前只支持 Stage 2 测试合同：48 小节 `A-B-A`，每段为 8 小节主题加 8 小节 MIDI-GPT 填充，同时只允许一个任务运行。

## 真实运行

```powershell
D:\conda\python.exe -m legasynth_orchestrator `
  --story "你的故事" `
  --heartbeat-wav "D:\private\heartbeat.wav" `
  --rhythm-plan ".\heartbeat_stage1\examples\even_halfbeat_4_4_103p15.json" `
  --render-plan ".\legasynth_orchestrator\examples\single_patient_render_plan.json" `
  --midigpt-python "D:\path\.venv_midigpt\Scripts\python.exe"
```

主控依次执行：

1. 原始 WAV 到 `heartbeat_package`；
2. DeepSeek 故事规划、WSL MuseCoco 生成与 8 小节后处理；
3. Stage 2 自动发现 A/B 主题，删除输入原有通道 10，重建心跳轨并运行 MIDI-GPT；
4. Stage 3 读取带哈希 handoff，使用 FluidSynth 和真实心音素材生成 `final_mix.wav`。

默认从外层工作区的 `tools/` 查找 FluidSynth 与 `GeneralUser-GS.sf2`，也可以通过 `--fluidsynth`、`--general-sf2` 显式覆盖。MIDI-GPT Python 应显式传入或设置环境变量 `LEGASYNTH_MIDIGPT_PYTHON`。

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
