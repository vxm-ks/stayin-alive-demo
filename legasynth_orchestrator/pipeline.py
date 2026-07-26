"""One-click, single-job orchestration across LegaSynth Stages 1, 2, and 3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import sys
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .process_runner import ProcessExecutionError, ProcessRunner


SCHEMA_VERSION = "legasynth-orchestrator-job-v1"
STAGES = (
    "validating_inputs", "heartbeat_stage1", "story_and_musecoco",
    "stage2_midigpt", "stage3_render", "completed",
)


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineConfig:
    workspace: Path
    runtime_root: Path
    stage1_python: Path = Path(sys.executable)
    midigpt_python: Path = Path(sys.executable)
    midigpt_model: str = "yellow"
    stage2_repetition_mode: str = "off"
    stage2_repetition_threshold: float = 0.82
    stage2_repetition_max_occurrences: int = 2
    stage2_repetition_candidates: int = 4
    stage3_python: Path = Path(sys.executable)
    fluidsynth: Path | None = None
    general_sf2: Path | None = None
    wsl_distro: str = "Ubuntu"
    tonality_policy: str = "soft"
    heartbeat_timeout_s: int = 900
    story_musecoco_timeout_s: int = 7200
    stage2_timeout_s: int = 7200
    stage3_timeout_s: int = 1800


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _JobLock:
    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise PipelineError(f"another LegaSynth job is active: {self.path}") from exc
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(str(os.getpid()))
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback
        self.path.unlink(missing_ok=True)


def _safe_package_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise PipelineError("heartbeat package ID must use 1-64 ASCII letters, digits, _ or -")
    return value


def _require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise PipelineError(f"{label} not found: {resolved}")
    return resolved


def _validate_render_plan(path: Path, package_id: str) -> None:
    try:
        from stage3_midi_renderer.package_renderer import load_render_plan
        load_render_plan(path)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid Stage 3 render plan: {exc}") from exc
    referenced: set[str] = set()
    default = data.get("default_material_rule", {})
    referenced.update(default.get("package_ids", []))
    for rule in data.get("section_material_rules", []):
        referenced.update(rule.get("package_ids", []))
    if referenced != {package_id}:
        raise PipelineError(
            f"render plan package IDs must be exactly [{package_id!r}], got {sorted(referenced)}"
        )


def _state(
    job_dir: Path,
    job_id: str,
    dry_run: bool,
    test_mode: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "status": "RUNNING",
        "current_stage": "validating_inputs",
        "dry_run": dry_run,
        "test_mode": test_mode,
        "generation_mode": (
            "dry_run" if dry_run else "test" if test_mode else "production"
        ),
        "created_at": _now(),
        "updated_at": _now(),
        "completed_stages": [],
        "artifacts": {},
        "failure": None,
    }


def _advance(job_file: Path, state: dict[str, Any], stage: str, **artifacts: str) -> None:
    previous = state["current_stage"]
    if previous not in state["completed_stages"] and previous != "validating_inputs":
        state["completed_stages"].append(previous)
    state["current_stage"] = stage
    state["updated_at"] = _now()
    state["artifacts"].update(artifacts)
    _atomic_json(job_file, state)


def _dry_midi(path: Path) -> None:
    track = b"\x00\xff\x2f\x00"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"MThd" + struct.pack(">IHHH", 6, 1, 1, 480)
        + b"MTrk" + struct.pack(">I", len(track)) + track
    )


def _dry_heartbeat(job_dir: Path, source: Path) -> Path:
    package = job_dir / "stage1" / "heartbeat" / "heartbeat_package"
    samples = package / "event_samples"
    samples.mkdir(parents=True)
    sample = samples / "s1_cycle001.wav"
    with wave.open(str(sample), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)
    manifest = {
        "schema_version": "1.0", "dry_run": True,
        "inputs": {"raw_wav": str(source), "raw_wav_sha256": _sha256(source)},
        "event_samples": [{
            "event_type": "S1", "cycle_index": 1,
            "file": "event_samples/s1_cycle001.wav", "sha256": _sha256(sample),
            "quality_score": 1.0, "alignment_offset_samples": 0,
        }],
    }
    (package / "heartbeat_manifest.json").write_bytes(_json_bytes(manifest))
    return package


def _dry_story(job_dir: Path, output_base: Path, story_id: str) -> None:
    stage2_dir = output_base.with_name(f"{output_base.name}-stage2")
    themes = output_base.with_name(f"{output_base.name}-musecoco") / "generated_themes"
    stage2_dir.mkdir(parents=True)
    sections = []
    for index, start in enumerate((1, 17, 33), 1):
        symbol = "B" if index == 2 else "A"
        sections.append({
            "section_id": f"S{index}", "form_label": symbol,
            "theme_family_id": f"theme-{symbol}",
            "relation": "reprise" if index == 3 else "introduce",
            "source_section_id": "S1" if index == 3 else None,
            "material_source": "reuse_theme" if index == 3 else "musecoco_seed",
            "bar_start": start, "bar_end": start + 15,
            "input_motif_bars": 8, "target_section_bars": 16, "extension_bars": 8,
            "extension_method": "midigpt_extend", "tempo_bpm": 96.0,
            "source_tension": 0.8 if index == 2 else 0.2,
            "tension_level": "high" if index == 2 else "low",
            "drum_pattern": {
                "pattern": "pulse_each_beat" if index == 2 else "single_pulse_per_bar",
                "time_signature": "4/4", "kick_beats": [1.0, 2.0, 3.0, 4.0] if index == 2 else [1.0],
                "snare_beats": [], "closed_hihat_beats": [],
            },
            "midigpt_access": "extension_only",
            "protected_bar_ranges": [{"bar_start": start, "bar_end": start + 7}],
            "editable_bar_ranges": [{"bar_start": start + 8, "bar_end": start + 15}],
        })
    plan = {
        "schema_version": "0.2-draft", "story_id": story_id, "total_bars": 48,
        "form_string": "A-B-A", "time_signature": "4/4",
        "bar_numbering": "one_based_closed", "drum_track_scope": "instructions_only",
        "sections": sections,
    }
    (stage2_dir / "stage2_plan.json").write_bytes(_json_bytes(plan))
    items = []
    for symbol in ("A", "B"):
        final = themes / f"theme-{symbol}" / "final.mid"
        _dry_midi(final)
        items.append({
            "theme_family_id": f"theme-{symbol}", "base_symbol": symbol,
            "final_midi": f"theme-{symbol}/final.mid", "final_sha256": _sha256(final),
        })
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "theme_manifest.json").write_bytes(_json_bytes({
        "schema_version": "musecoco-final-themes-v1", "story_id": story_id,
        "output_motif_bars": 8, "items": items,
    }))


def _dry_stage2(
    job_dir: Path, package: Path, render_plan: Path, story_id: str,
    package_id: str = "patient",
) -> Path:
    output = job_dir / "stage2" / "final_completed.mid"
    _dry_midi(output)
    handoff = job_dir / "stage2" / "stage3_handoff.json"
    handoff.write_bytes(_json_bytes({
        "schema_version": "stage2-stage3-handoff-v1", "stage2_status": "production",
        "story_id": story_id, "form_string": "A-B-A", "total_bars": 48,
        "complete_midi": {"path": output.name, "sha256": _sha256(output)},
        "heartbeat_channel": 10, "note_map": {"36": "S1", "38": "S2"},
        "heartbeat_packages": [{
            "id": package_id, "path": os.path.relpath(package, handoff.parent),
            "manifest_sha256": _sha256(package / "heartbeat_manifest.json"),
        }],
        "render_plan": {
            "path": os.path.relpath(render_plan, handoff.parent), "sha256": _sha256(render_plan),
        },
    }))
    return handoff


def _dry_stage3(job_dir: Path) -> Path:
    output = job_dir / "stage3" / "final_mix.wav"
    output.parent.mkdir(parents=True)
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(2); handle.setsampwidth(2); handle.setframerate(48000)
        handle.writeframes(b"\x00\x00\x00\x00" * 4800)
    (output.parent / "stage3_render_manifest.json").write_bytes(_json_bytes({
        "schema_version": "dry-run", "output_sha256": _sha256(output),
    }))
    return output


def run_pipeline(
    *,
    story_text: str,
    heartbeat_wav: Path,
    rhythm_plan: Path,
    render_plan: Path,
    config: PipelineConfig,
    package_id: str = "patient",
    dry_run: bool = False,
    test_mode: bool = False,
    runner: ProcessRunner | None = None,
) -> Path:
    """Run one isolated job and return its directory."""
    workspace = config.workspace.resolve()
    runtime = config.runtime_root.resolve()
    if not story_text.strip():
        raise PipelineError("story text cannot be empty")
    heartbeat_wav = _require_file(heartbeat_wav, "heartbeat WAV")
    if heartbeat_wav.suffix.lower() != ".wav":
        raise PipelineError("heartbeat input must be a WAV file")
    rhythm_plan = _require_file(rhythm_plan, "rhythm plan")
    render_plan = _require_file(render_plan, "Stage 3 render plan")
    try:
        from heartbeat_stage1.heartbeat_stage1 import load_and_validate_plan
        load_and_validate_plan(rhythm_plan)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"invalid heartbeat rhythm plan: {exc}") from exc
    package_id = _safe_package_id(package_id)
    _validate_render_plan(render_plan, package_id)
    if not dry_run:
        if not config.midigpt_model.strip():
            raise PipelineError("MIDI-GPT model name cannot be empty")
        if config.stage2_repetition_mode not in {"off", "detect", "regenerate"}:
            raise PipelineError("invalid Stage 2 repetition mode")
        if not 0.0 < config.stage2_repetition_threshold <= 1.0:
            raise PipelineError("Stage 2 repetition threshold must be in (0, 1]")
        if config.stage2_repetition_max_occurrences < 1:
            raise PipelineError("Stage 2 maximum repetition occurrences must be positive")
        if config.stage2_repetition_candidates < 1:
            raise PipelineError("Stage 2 repetition candidates must be positive")
        if config.tonality_policy not in {"loose", "soft", "strict"}:
            raise PipelineError("tonality policy must be loose, soft, or strict")
        for path, label in (
            (config.stage1_python, "Stage 1 Python"),
            (config.midigpt_python, "MIDI-GPT Python"),
            (config.stage3_python, "Stage 3 Python"),
            (config.fluidsynth, "FluidSynth"),
            (config.general_sf2, "general SoundFont"),
        ):
            if path is None or not Path(path).expanduser().is_file():
                raise PipelineError(f"{label} is not configured: {path}")
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    job_dir = runtime / "jobs" / job_id
    inputs = job_dir / "input"
    logs = job_dir / "logs"
    inputs.mkdir(parents=True)
    logs.mkdir()
    copied_wav = inputs / "heartbeat.wav"
    copied_rhythm = inputs / "rhythm_plan.json"
    copied_render = inputs / "stage3_render_plan.json"
    shutil.copy2(heartbeat_wav, copied_wav)
    shutil.copy2(rhythm_plan, copied_rhythm)
    shutil.copy2(render_plan, copied_render)
    story_id = f"job-{job_id}"
    story_request = inputs / "story_request.json"
    story_request.write_bytes(_json_bytes({
        "story_id": story_id, "story_text": story_text, "language": "zh-CN",
        "test_mode": test_mode,
        "constraints": {"musecoco_output_bars": 8, "default_extension_bars": 8},
    }))
    job_file = job_dir / "job.json"
    state = _state(job_dir, job_id, dry_run, test_mode)
    state["inputs"] = {
        "heartbeat_wav": {"path": str(copied_wav), "sha256": _sha256(copied_wav)},
        "rhythm_plan": {"path": str(copied_rhythm), "sha256": _sha256(copied_rhythm)},
        "render_plan": {"path": str(copied_render), "sha256": _sha256(copied_render)},
        "story_request": {"path": str(story_request), "sha256": _sha256(story_request)},
    }
    _atomic_json(job_file, state)
    executor = runner or ProcessRunner()
    lock = runtime / "active_job.lock"
    try:
        with _JobLock(lock):
            _advance(job_file, state, "heartbeat_stage1")
            if dry_run:
                package = _dry_heartbeat(job_dir, copied_wav)
            else:
                heartbeat_root = job_dir / "stage1" / "heartbeat"
                executor.run("heartbeat_stage1", [
                    str(config.stage1_python), str(workspace / "heartbeat_stage1" / "heartbeat_stage1.py"),
                    str(copied_wav), str(copied_rhythm), "--output-dir", str(heartbeat_root),
                ], cwd=workspace, log_path=logs / "heartbeat_stage1.log",
                    timeout_seconds=config.heartbeat_timeout_s)
                packages = list(heartbeat_root.rglob("heartbeat_package/heartbeat_manifest.json"))
                if len(packages) != 1:
                    raise PipelineError("heartbeat Stage 1 did not publish exactly one package")
                package = packages[0].parent
            _advance(job_file, state, "story_and_musecoco", heartbeat_package=str(package))

            output_base = job_dir / "stage1" / "story"
            if dry_run:
                _dry_story(job_dir, output_base, story_id)
            else:
                stage1_command = [
                    str(config.stage1_python), "-m", "stage1_story_agent", "plan",
                    "--input", str(story_request), "--output-dir", str(output_base),
                    "--enqueue-musecoco", "--run-musecoco-queue", "--wsl-distro", config.wsl_distro,
                    "--musecoco-output-bars", "8", "--tonality-policy", config.tonality_policy,
                    "--force",
                ]
                if test_mode:
                    stage1_command.append("--test-mode")
                executor.run("story_and_musecoco", stage1_command,
                    cwd=workspace, log_path=logs / "story_and_musecoco.log",
                    timeout_seconds=config.story_musecoco_timeout_s)
            theme_manifest = output_base.with_name(f"{output_base.name}-musecoco") / "generated_themes" / "theme_manifest.json"
            stage2_plan = output_base.with_name(f"{output_base.name}-stage2") / "stage2_plan.json"
            _require_file(theme_manifest, "finalized MuseCoco theme manifest")
            _require_file(stage2_plan, "Stage 2 plan")
            _advance(job_file, state, "stage2_midigpt", theme_manifest=str(theme_manifest), stage2_plan=str(stage2_plan))

            handoff = job_dir / "stage2" / "stage3_handoff.json"
            if dry_run:
                handoff = _dry_stage2(job_dir, package, copied_render, story_id, package_id)
            else:
                stage2_output = job_dir / "stage2" / "final_completed.mid"
                executor.run("stage2_midigpt", [
                    str(config.midigpt_python), str(workspace / "stage2" / "run_pipeline_relative.py"),
                    "--stage1-output-base", str(output_base),
                    "--heartbeat-package", f"{package_id}={package}",
                    "--stage3-render-plan", str(copied_render),
                    "--model", config.midigpt_model,
                    "--repetition-mode", config.stage2_repetition_mode,
                    "--repetition-threshold", str(config.stage2_repetition_threshold),
                    "--repetition-max-occurrences", str(config.stage2_repetition_max_occurrences),
                    "--repetition-candidates", str(config.stage2_repetition_candidates),
                    "--output", str(stage2_output),
                ], cwd=workspace, log_path=logs / "stage2_midigpt.log",
                    timeout_seconds=config.stage2_timeout_s)
            _require_file(handoff, "Stage 3 handoff")
            _advance(job_file, state, "stage3_render", stage3_handoff=str(handoff))

            if dry_run:
                final_mix = _dry_stage3(job_dir)
            else:
                stage3_output = job_dir / "stage3"
                executor.run("stage3_render", [
                    str(config.stage3_python), "-m", "stage3_midi_renderer", "render-packages",
                    "--stage2-handoff", str(handoff), "--general-sf2", str(config.general_sf2),
                    "--fluidsynth", str(config.fluidsynth), "--output-dir", str(stage3_output),
                ], cwd=workspace, log_path=logs / "stage3_render.log",
                    timeout_seconds=config.stage3_timeout_s)
                final_mix = stage3_output / "final_mix.wav"
            _require_file(final_mix, "final mix")
            state["completed_stages"].append("stage3_render")
            state["current_stage"] = "completed"
            state["status"] = "COMPLETED"
            state["updated_at"] = _now()
            state["artifacts"]["final_mix"] = str(final_mix)
            state["artifacts"]["final_mix_sha256"] = _sha256(final_mix)
            _atomic_json(job_file, state)
    except (OSError, ValueError, ProcessExecutionError, PipelineError, KeyboardInterrupt) as exc:
        state["status"] = "CANCELLED" if isinstance(exc, KeyboardInterrupt) else "FAILED"
        state["failure"] = {"stage": state["current_stage"], "type": type(exc).__name__, "message": str(exc)}
        state["updated_at"] = _now()
        _atomic_json(job_file, state)
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise PipelineError(str(exc)) from exc
    return job_dir
