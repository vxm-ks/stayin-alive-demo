"""Thin FastAPI bridge between the browser UI and the existing orchestrator."""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from legasynth_orchestrator.pipeline import PipelineConfig, PipelineError, run_pipeline


WORKSPACE = Path(__file__).resolve().parents[1]
WEB_RUNTIME = WORKSPACE / "web_app" / "runtime"
UPLOAD_ROOT = WEB_RUNTIME / "uploads"
PIPELINE_RUNTIME = WEB_RUNTIME / "pipeline"
RHYTHM_PLAN = WORKSPACE / "heartbeat_stage1" / "examples" / "even_halfbeat_4_4_103p15.json"
RENDER_PLAN = WORKSPACE / "legasynth_orchestrator" / "examples" / "single_patient_render_plan.json"
FRONTEND_DIST = WORKSPACE / "web_app" / "frontend" / "dist"

STAGE_PROGRESS = {
    "queued": 4,
    "validating_inputs": 10,
    "heartbeat_stage1": 27,
    "story_and_musecoco": 50,
    "stage2_midigpt": 72,
    "stage3_render": 91,
    "completed": 100,
}


@dataclass
class WebTask:
    id: str
    story: str
    upload_path: Path
    dry_run: bool
    test_mode: bool
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    job_dir: Path | None = None
    error: str | None = None
    finished: bool = False


tasks: dict[str, WebTask] = {}
tasks_lock = threading.Lock()

app = FastAPI(title="LegaSynth Studio API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_local_env(path: Path) -> None:
    """Load local KEY=VALUE settings without overriding the host process."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _choice_setting(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default)
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {choices}")
    return value


def _float_setting(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_setting(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _config() -> PipelineConfig:
    _load_local_env(WORKSPACE / ".env")
    assets = WORKSPACE if (WORKSPACE / "tools").is_dir() else WORKSPACE.parent.parent
    fluidsynth = Path(
        os.getenv(
            "LEGASYNTH_FLUIDSYNTH",
            str(
                assets
                / "tools"
                / "fluidsynth-2.5.6"
                / "fluidsynth-v2.5.6-win10-x64-cpp11"
                / "bin"
                / "fluidsynth.exe"
            ),
        )
    )
    general_sf2 = Path(
        os.getenv(
            "LEGASYNTH_GENERAL_SF2",
            str(assets / "tools" / "soundfonts" / "GeneralUser-GS.sf2"),
        )
    )
    return PipelineConfig(
        workspace=WORKSPACE,
        runtime_root=PIPELINE_RUNTIME,
        stage1_python=Path(sys.executable),
        midigpt_python=Path(
            os.getenv("LEGASYNTH_MIDIGPT_PYTHON", sys.executable)
        ),
        midigpt_model=os.getenv("LEGASYNTH_MIDIGPT_MODEL", "yellow"),
        stage2_repetition_mode=_choice_setting(
            "LEGASYNTH_STAGE2_REPETITION_MODE",
            "off",
            {"off", "detect", "regenerate"},
        ),
        stage2_repetition_threshold=_float_setting(
            "LEGASYNTH_STAGE2_REPETITION_THRESHOLD", 0.82
        ),
        stage2_repetition_max_occurrences=_int_setting(
            "LEGASYNTH_STAGE2_REPETITION_MAX_OCCURRENCES", 2
        ),
        stage2_repetition_candidates=_int_setting(
            "LEGASYNTH_STAGE2_REPETITION_CANDIDATES", 4
        ),
        stage3_python=Path(sys.executable),
        fluidsynth=fluidsynth,
        general_sf2=general_sf2,
        wsl_distro=os.getenv("LEGASYNTH_WSL_DISTRO", "Ubuntu"),
        tonality_policy=_choice_setting(
            "LEGASYNTH_TONALITY_POLICY", "soft", {"loose", "soft", "strict"}
        ),
    )


def _run(task: WebTask) -> None:
    before = set((PIPELINE_RUNTIME / "jobs").glob("*")) if (PIPELINE_RUNTIME / "jobs").exists() else set()
    try:
        result = run_pipeline(
            story_text=task.story,
            heartbeat_wav=task.upload_path,
            rhythm_plan=RHYTHM_PLAN,
            render_plan=RENDER_PLAN,
            config=_config(),
            package_id="patient",
            dry_run=task.dry_run,
            test_mode=task.test_mode,
        )
        task.job_dir = result
    except (PipelineError, OSError, ValueError) as exc:
        task.error = str(exc)
    finally:
        if task.job_dir is None:
            jobs_root = PIPELINE_RUNTIME / "jobs"
            candidates = set(jobs_root.glob("*")) - before if jobs_root.exists() else set()
            if candidates:
                task.job_dir = max(candidates, key=lambda path: path.stat().st_mtime)
        task.finished = True


def _state(task: WebTask) -> dict[str, Any]:
    state: dict[str, Any] = {
        "task_id": task.id,
        "status": "QUEUED",
        "current_stage": "queued",
        "progress": STAGE_PROGRESS["queued"],
        "created_at": task.created_at,
        "dry_run": task.dry_run,
        "test_mode": task.test_mode,
        "generation_mode": (
            "dry_run" if task.dry_run else "test" if task.test_mode else "production"
        ),
        "failure": None,
        "audio_ready": False,
    }
    if task.job_dir is None:
        jobs_root = PIPELINE_RUNTIME / "jobs"
        if jobs_root.is_dir():
            created = datetime.fromisoformat(task.created_at).timestamp()
            candidates = [path for path in jobs_root.iterdir() if path.is_dir() and path.stat().st_mtime >= created - 1]
            if candidates:
                task.job_dir = max(candidates, key=lambda path: path.stat().st_mtime)
    job_file = task.job_dir / "job.json" if task.job_dir else None
    if job_file and job_file.is_file():
        try:
            pipeline_state = json.loads(job_file.read_text(encoding="utf-8"))
            stage = pipeline_state.get("current_stage", "queued")
            state.update({
                "status": pipeline_state.get("status", "RUNNING"),
                "current_stage": stage,
                "progress": STAGE_PROGRESS.get(stage, 8),
                "completed_stages": pipeline_state.get("completed_stages", []),
                "failure": pipeline_state.get("failure"),
                "audio_ready": bool(pipeline_state.get("artifacts", {}).get("final_mix")),
            })
        except (OSError, json.JSONDecodeError):
            pass
    elif task.error:
        state.update({
            "status": "FAILED", "failure": {"message": task.error},
        })
    elif task.finished:
        state.update({
            "status": "FAILED", "failure": {"message": "Task ended without a pipeline result."},
        })
    return state


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "LegaSynth Studio"}


@app.post("/api/tasks", status_code=202)
async def create_task(
    heartbeat: UploadFile = File(...),
    story: str = Form(...),
    dry_run: bool = Form(False),
    test_mode: bool = Form(False),
) -> dict[str, Any]:
    if not story.strip():
        raise HTTPException(422, "Story cannot be empty.")
    if len(story.strip()) > 8000:
        raise HTTPException(422, "Story is too long (maximum 8,000 characters).")
    filename = heartbeat.filename or "heartbeat.wav"
    if Path(filename).suffix.lower() != ".wav":
        raise HTTPException(415, "Heartbeat input must be a .wav file.")
    if dry_run and test_mode:
        raise HTTPException(422, "Dry run and test mode are mutually exclusive.")
    with tasks_lock:
        if any(_state(item)["status"] in {"QUEUED", "RUNNING"} for item in tasks.values()):
            raise HTTPException(409, "Another generation is currently running.")
        task_id = uuid4().hex[:12]
        upload_dir = UPLOAD_ROOT / task_id
        upload_dir.mkdir(parents=True, exist_ok=False)
        upload_path = upload_dir / "heartbeat.wav"
        with upload_path.open("wb") as target:
            shutil.copyfileobj(heartbeat.file, target)
        if upload_path.stat().st_size == 0:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(422, "Uploaded WAV file is empty.")
        task = WebTask(
            id=task_id,
            story=story.strip(),
            upload_path=upload_path,
            dry_run=dry_run,
            test_mode=test_mode,
        )
        tasks[task_id] = task
    threading.Thread(target=_run, args=(task,), name=f"legasynth-{task_id}", daemon=True).start()
    return _state(task)


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found.")
    return _state(task)


@app.get("/api/tasks/{task_id}/audio")
def get_audio(task_id: str) -> FileResponse:
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "Task not found.")
    state = _state(task)
    if not state["audio_ready"] or task.job_dir is None:
        raise HTTPException(409, "Audio is not ready yet.")
    job_data = json.loads((task.job_dir / "job.json").read_text(encoding="utf-8"))
    audio = Path(job_data["artifacts"]["final_mix"]).resolve()
    if task.job_dir.resolve() not in audio.parents or not audio.is_file():
        raise HTTPException(404, "Final audio artifact is unavailable.")
    return FileResponse(audio, media_type="audio/wav", filename=f"LegaSynth-{task_id}.wav")


if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str) -> FileResponse:
        del path
        return FileResponse(FRONTEND_DIST / "index.html")
