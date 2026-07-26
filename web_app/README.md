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

前端默认访问 `http://localhost:5173`。创建页提供三个互斥模式：

- 「正式生成」（默认）：故事决定曲式和音乐属性，调用全部模型；
- 「测试模式」：调用全部模型，固定全曲调性/速度及 48 小节 A–B–A 结构，
  但故事、心跳、主题配器/风格和张力仍可变化；
- 「快速演示」：运行主控 `--dry-run`，不调用模型。

Web API 的 `POST /api/tasks` 对应接收 `test_mode` 和 `dry_run` 两个布尔字段，
两者不能同时为 `true`。后端任务状态会返回
`generation_mode=production|test|dry_run`。

Web API 只是一层外壳，直接调用 `main` 的
`legasynth_orchestrator.pipeline.run_pipeline()`，不复制或改写任何三阶段业务逻辑。
真实运行前，应在仓库根目录的 `.env` 中保留正式部署配置：

```dotenv
LEGASYNTH_MIDIGPT_PYTHON=D:\path\to\stage2\.conda_midigpt\python.exe
LEGASYNTH_MIDIGPT_MODEL=yellow
LEGASYNTH_TONALITY_POLICY=soft
LEGASYNTH_STAGE2_REPETITION_MODE=off
HF_HOME=D:\path\to\huggingface-cache
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

## 静态 Demo 案例

Demo Page 从 `web_app/frontend/public/demo/case-01/demo_manifest.json`
读取静态案例，不会在页面加载时调用本地模型。导出案例时必须显式指定要发布的
Stage 2 和 Stage 3 产物目录。例如，以下命令只使用任务中的软质量门控重跑结果，
不会读取首次失败的 Stage 2 支线：

```powershell
.\.venv\Scripts\python.exe .\web_app\tools\export_demo_case.py `
  ".\web_app\runtime\pipeline\jobs\20260725-210857-08f0a18c" `
  ".\web_app\frontend\public\demo\case-01" `
  --stage2-variant stage2_soft_gate_test `
  --stage3-variant stage3_soft_gate_test
```

导出器会校验成功支线清单及文件哈希，复制公开页面需要的 WAV、MIDI、JSON
和时间—能量图，并从真实 MIDI 生成钢琴卷帘 SVG。清单会保留原始任务状态与
选中支线名称，避免将一次失败的原始编排记录误写成完整成功任务。

## 边界

- `web_app/` 只负责上传、状态轮询、结果播放和配置转接。
- 故事规划、MuseCoco、MIDI-GPT、任意曲式装配、反重复和 Stage 3
  渲染均继续由 `main` 原模块负责。
- Web 层不修改 `stage1_story_agent/`、`stage2/`、
  `stage3_midi_renderer/` 或 `legasynth_orchestrator/`。
