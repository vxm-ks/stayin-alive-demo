# LegaSynth Studio Web UI

中英双语的课程展示界面，通过薄 FastAPI 层调用现有 `legasynth_orchestrator`。

## 开发运行

```powershell
python -m pip install -r .\web_app\requirements.txt
Set-Location .\web_app\frontend
npm.cmd ci
npm.cmd run dev
```

另开一个终端，在仓库根目录运行：

```powershell
python -m uvicorn web_app.api:app --reload --port 8000
```

前端默认访问 `http://localhost:5173`。在界面开启「演示模式」会运行主控的 `--dry-run`，不调用模型，适合快速展示完整交互。

Web API 只是一层外壳，直接调用 `main` 的
`legasynth_orchestrator.pipeline.run_pipeline()`，不复制或改写任何三阶段业务逻辑。
真实运行前，应在仓库根目录的 `.env` 中保留正式部署配置：

```dotenv
LEGASYNTH_MIDIGPT_PYTHON=D:\LegaSynth\stage2\.conda_midigpt\python.exe
LEGASYNTH_MIDIGPT_MODEL=yellow
LEGASYNTH_TONALITY_POLICY=strict
LEGASYNTH_STAGE2_REPETITION_MODE=off
HF_HOME=D:\LegaSynth\stage2\.cache\huggingface
```

Web 外壳还支持以下可选变量，未设置时与主控默认值一致：

```dotenv
LEGASYNTH_STAGE2_REPETITION_THRESHOLD=0.82
LEGASYNTH_STAGE2_REPETITION_MAX_OCCURRENCES=2
LEGASYNTH_STAGE2_REPETITION_CANDIDATES=4
LEGASYNTH_WSL_DISTRO=Ubuntu
LEGASYNTH_FLUIDSYNTH=D:\path\to\fluidsynth.exe
LEGASYNTH_GENERAL_SF2=D:\path\to\GeneralUser-GS.sf2
```

## 生产预览

```powershell
Set-Location .\web_app\frontend
npm.cmd run build
Set-Location ..\..
python -m uvicorn web_app.api:app --port 8000
```

然后访问 `http://localhost:8000`。

## 边界

- `web_app/` 只负责上传、状态轮询、结果播放和配置转接。
- 故事规划、MuseCoco、MIDI-GPT、任意曲式装配、反重复和 Stage 3
  渲染均继续由 `main` 原模块负责。
- Web 层不修改 `stage1_story_agent/`、`stage2/`、
  `stage3_midi_renderer/` 或 `legasynth_orchestrator/`。
