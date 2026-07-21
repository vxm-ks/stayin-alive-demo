"""Batch acceptance testing for Stage-2-compliant complete MIDI files.

The loudness measurement follows ITU-R BS.1770 / EBU R128 integrated gating.
Audio is resampled to 48 kHz for the published BS.1770 K-weighting biquads.
This module only evaluates Stage 3 output; it never creates or retimes MIDI events.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
from scipy.io import wavfile
from scipy.signal import lfilter, resample_poly

from .package_renderer import PackageRenderResult, Stage3RenderError, load_render_plan, render_with_packages


# BS.1770 K-weighting coefficients at 48 kHz (pre-filter, then RLB high-pass).
_K_B1 = np.array([1.53512485958697, -2.69169618940638, 1.19839281085285])
_K_A1 = np.array([1.0, -1.69065929318241, 0.73248077421585])
_K_B2 = np.array([1.0, -2.0, 1.0])
_K_A2 = np.array([1.0, -1.99004745483398, 0.99007225036621])


@dataclass(frozen=True)
class LoudnessLimits:
    min_integrated_lufs: float = -18.0
    max_integrated_lufs: float = -12.0
    max_true_peak_dbtp: float = -1.0
    min_heartbeat_over_music_db: float = 2.0
    max_heartbeat_over_music_db: float = 8.0

    def validate(self) -> None:
        values = tuple(self.__dict__.values())
        if not all(math.isfinite(value) for value in values):
            raise Stage3RenderError("batch loudness limits must be finite")
        if self.min_integrated_lufs >= self.max_integrated_lufs:
            raise Stage3RenderError("minimum integrated LUFS must be below maximum")
        if self.min_heartbeat_over_music_db >= self.max_heartbeat_over_music_db:
            raise Stage3RenderError("minimum heartbeat balance must be below maximum")
        if not -12.0 <= self.max_true_peak_dbtp <= -0.1:
            raise Stage3RenderError("maximum true peak must be between -12 and -0.1 dBTP")


@dataclass(frozen=True)
class BatchValidationResult:
    output_dir: Path
    report_json: Path
    report_csv: Path
    passed: int
    failed: int

    @property
    def status(self) -> str:
        return "PASS" if self.failed == 0 else "FAIL"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "output_dir": str(self.output_dir),
            "report_json": str(self.report_json),
            "report_csv": str(self.report_csv),
            "passed": self.passed,
            "failed": self.failed,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_audio(path: Path) -> tuple[int, np.ndarray]:
    try:
        rate, raw = wavfile.read(path)
    except (OSError, ValueError) as exc:
        raise Stage3RenderError(f"could not read rendered WAV {path}: {exc}") from exc
    if raw.ndim == 1:
        raw = raw[:, None]
    if raw.ndim != 2 or not len(raw) or raw.shape[1] not in (1, 2):
        raise Stage3RenderError(f"batch validator requires mono or stereo WAV: {path}")
    if np.issubdtype(raw.dtype, np.integer):
        scale = float(1 << (8 * raw.dtype.itemsize - 1))
        if np.issubdtype(raw.dtype, np.unsignedinteger):
            audio = (raw.astype(np.float64) - scale) / scale
        else:
            audio = raw.astype(np.float64) / scale
    elif np.issubdtype(raw.dtype, np.floating):
        audio = raw.astype(np.float64)
    else:
        raise Stage3RenderError(f"unsupported rendered WAV type: {raw.dtype}")
    if not np.all(np.isfinite(audio)):
        raise Stage3RenderError(f"rendered WAV contains non-finite samples: {path}")
    return int(rate), np.ascontiguousarray(audio)


def integrated_loudness_lufs(audio: np.ndarray, sample_rate: int) -> float:
    """Measure mono/stereo integrated loudness using BS.1770 gated blocks."""
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.ndim != 2 or audio.shape[1] not in (1, 2) or sample_rate <= 0:
        raise Stage3RenderError("integrated loudness requires finite mono/stereo audio")
    if sample_rate != 48_000:
        divisor = math.gcd(sample_rate, 48_000)
        audio = resample_poly(audio, 48_000 // divisor, sample_rate // divisor, axis=0)
    weighted = lfilter(_K_B1, _K_A1, audio, axis=0)
    weighted = lfilter(_K_B2, _K_A2, weighted, axis=0)
    block, step = 19_200, 4_800  # 400 ms blocks with 75% overlap.
    if len(weighted) < block:
        weighted = np.pad(weighted, ((0, block - len(weighted)), (0, 0)))
    energies = []
    for start in range(0, len(weighted) - block + 1, step):
        energies.append(float(np.sum(np.mean(np.square(weighted[start:start + block]), axis=0))))
    values = np.asarray(energies, dtype=np.float64)
    block_loudness = -0.691 + 10.0 * np.log10(np.maximum(values, 1e-300))
    absolute = values[block_loudness >= -70.0]
    if not len(absolute):
        return float("-inf")
    relative_threshold = -0.691 + 10.0 * math.log10(float(np.mean(absolute))) - 10.0
    gated = values[block_loudness >= max(-70.0, relative_threshold)]
    if not len(gated):
        return float("-inf")
    return -0.691 + 10.0 * math.log10(float(np.mean(gated)))


def true_peak_dbtp(audio: np.ndarray) -> float:
    if audio.ndim == 1:
        audio = audio[:, None]
    peak = float(np.max(np.abs(resample_poly(audio, 4, 1, axis=0)))) if len(audio) else 0.0
    return 20.0 * math.log10(max(peak, 1e-300))


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def _local_balance_db(
    music: np.ndarray,
    heartbeat: np.ndarray,
    sample_rate: int,
    assignments: Path,
    window_ms: float,
) -> float:
    music_values, heartbeat_values = [], []
    half = max(1, int(round(window_ms * sample_rate / 2000.0)))
    with assignments.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise Stage3RenderError("render produced no heartbeat event assignments")
    length = min(len(music), len(heartbeat))
    for row in rows:
        center = int(round(float(row["time_s"]) * sample_rate))
        left, right = max(0, center - half), min(length, center + half)
        music_values.append(_rms(music[left:right]))
        heartbeat_values.append(_rms(heartbeat[left:right]))
    music_median = float(np.median(music_values))
    heartbeat_median = float(np.median(heartbeat_values))
    return 20.0 * math.log10(max(heartbeat_median, 1e-300) / max(music_median, 1e-300))


def _diagnostic_plan(source: Path, destination: Path) -> dict[str, object]:
    raw = json.loads(source.read_text(encoding="utf-8"))
    raw.setdefault("mix", {})["publish_stems"] = True
    destination.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return load_render_plan(destination)


def batch_validate_midis(
    input_midis: Sequence[str | Path],
    general_soundfont: str | Path,
    render_plan: str | Path,
    heartbeat_packages: Mapping[str, str | Path],
    output_dir: str | Path,
    *,
    fluidsynth: str | Path = "fluidsynth",
    sample_rate: int = 48_000,
    synth_gain: float = 0.5,
    timeout_seconds: int = 600,
    limits: LoudnessLimits = LoudnessLimits(),
    force: bool = False,
    renderer: Callable[..., PackageRenderResult] = render_with_packages,
) -> BatchValidationResult:
    """Render every complete MIDI, continue after per-file failures, and report acceptance."""
    limits.validate()
    midis = sorted({Path(item).expanduser().resolve() for item in input_midis}, key=str)
    if not midis:
        raise Stage3RenderError("batch validation requires at least one MIDI")
    missing = [str(path) for path in midis if not path.is_file()]
    if missing:
        raise Stage3RenderError(f"batch MIDI files not found: {missing}")
    destination = Path(output_dir).expanduser().resolve()
    if destination.exists():
        if not force:
            raise Stage3RenderError(f"batch output already exists: {destination}; use --force")
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    rows: list[dict[str, object]] = []
    try:
        diagnostic_plan_path = staging / "diagnostic_render_plan.json"
        plan = _diagnostic_plan(Path(render_plan).expanduser().resolve(), diagnostic_plan_path)
        if plan["mix"]["mode"] != "event_window_relative":
            raise Stage3RenderError("batch adaptation test requires mix.mode=event_window_relative")
        for index, midi in enumerate(midis, 1):
            identity = f"{midi.stem}-{_sha256(midi)[:10]}"
            item_dir = staging / "renders" / identity
            row: dict[str, object] = {
                "index": index, "input_midi": str(midi), "input_sha256": _sha256(midi),
                "render_dir": str(Path("renders") / identity), "status": "FAIL", "failures": "",
            }
            try:
                result = renderer(
                    midi, general_soundfont, diagnostic_plan_path, heartbeat_packages, item_dir,
                    fluidsynth=fluidsynth, sample_rate=sample_rate, synth_gain=synth_gain,
                    timeout_seconds=timeout_seconds, force=False,
                )
                manifest = json.loads(result.manifest_json.read_text(encoding="utf-8"))
                final_rate, final = _read_audio(result.output_wav)
                music_rate, music = _read_audio(item_dir / "music_stem.wav")
                heartbeat_rate, heartbeat = _read_audio(item_dir / "heartbeat_stem.wav")
                if len({final_rate, music_rate, heartbeat_rate}) != 1:
                    raise Stage3RenderError("rendered stems have inconsistent sample rates")
                lufs = integrated_loudness_lufs(final, final_rate)
                peak = true_peak_dbtp(final)
                balance = _local_balance_db(
                    music, heartbeat, final_rate, result.assignments_csv,
                    float(plan["mix"]["event_window_ms"]),
                )
                failures = []
                if not limits.min_integrated_lufs <= lufs <= limits.max_integrated_lufs:
                    failures.append("integrated_lufs_out_of_range")
                if peak > limits.max_true_peak_dbtp + 0.1:
                    failures.append("true_peak_above_limit")
                if not limits.min_heartbeat_over_music_db <= balance <= limits.max_heartbeat_over_music_db:
                    failures.append("heartbeat_balance_out_of_range")
                if manifest.get("policy", {}).get("midi_timing_modified") is not False:
                    failures.append("midi_timing_policy_failed")
                row.update({
                    "integrated_lufs": round(lufs, 3), "true_peak_dbtp": round(peak, 3),
                    "heartbeat_over_music_db": round(balance, 3),
                    "heartbeat_event_count": manifest.get("assignments", {}).get("event_count", 0),
                    "status": "PASS" if not failures else "FAIL", "failures": ";".join(failures),
                })
            except Exception as exc:  # Each MIDI must receive an auditable result.
                row["failures"] = f"render_error:{type(exc).__name__}:{exc}"
            rows.append(row)
        fieldnames = [
            "index", "input_midi", "input_sha256", "render_dir", "status", "failures",
            "integrated_lufs", "true_peak_dbtp", "heartbeat_over_music_db", "heartbeat_event_count",
        ]
        report_csv = staging / "batch_validation_report.csv"
        with report_csv.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        passed = sum(row["status"] == "PASS" for row in rows)
        report = {
            "schema_version": "1.0", "generated_at": datetime.now(UTC).isoformat(),
            "standard": {
                "integrated_loudness": "ITU-R BS.1770-5 / EBU R128 gated integrated loudness",
                "true_peak": "4x oversampled estimate",
            },
            "policy": {
                "stage2_complete_midi_required": True, "midi_events_modified": False,
                "diagnostic_stems_forced": True,
            },
            "limits": limits.__dict__, "summary": {
                "status": "PASS" if passed == len(rows) else "FAIL",
                "total": len(rows), "passed": passed, "failed": len(rows) - passed,
            },
            "items": rows,
        }
        report_json = staging / "batch_validation_report.json"
        report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        diagnostic_plan_path.unlink(missing_ok=True)
        staging.replace(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BatchValidationResult(
        destination, destination / "batch_validation_report.json",
        destination / "batch_validation_report.csv", passed, len(rows) - passed,
    )
