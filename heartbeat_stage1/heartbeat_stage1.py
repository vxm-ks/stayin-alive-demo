#!/usr/bin/env python3
"""Build a portable heartbeat package from a raw WAV and an explicit rhythm plan.

Stage one is deliberately deterministic: the user/LLM supplies event timing,
while the DSP code validates and renders it with real S1/S2 exemplars.  The
program never asks an LLM to manipulate samples and never stretches or
pitch-shifts a heartbeat waveform.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import re
import shutil
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import scipy
from scipy.signal import resample_poly

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from heart_extraction import audio_diagnostics as diagnostics
from heart_extraction import heart_sound_processor as extraction
from tempo_bar_renderer import tempo_bar_renderer as tempo_renderer


VERSION = "1.0.0"
PPQ = 480
EVENT_NOTES = {"S1": 36, "S2": 38}
EVENT_VELOCITIES = {"S1": 100, "S2": 85}
HARD_MINIMUM_EVENT_SPACING_S = 0.080
WARNING_EVENT_SPACING_S = 0.120


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return cleaned or "plan"


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, not a boolean.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field} must be a finite number.")
    return result


def _normalise_meter(raw: Any) -> tuple[int, int]:
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", raw)
        if not match:
            raise ValueError("meter string must look like 4/4, 3/4, or 6/8.")
        numerator, denominator = map(int, match.groups())
    elif isinstance(raw, dict):
        numerator = int(raw.get("numerator", 0))
        denominator = int(raw.get("denominator", 0))
    else:
        raise ValueError("meter must be a string or an object with numerator/denominator.")
    if not 1 <= numerator <= 32:
        raise ValueError("meter numerator must be between 1 and 32.")
    if denominator <= 0 or denominator & (denominator - 1):
        raise ValueError("meter denominator must be a positive power of two.")
    return numerator, denominator


def validate_plan(raw_plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a canonical plan used by both stage one and stage three."""
    if not isinstance(raw_plan, dict):
        raise ValueError("Rhythm plan root must be a JSON object.")
    unknown = set(raw_plan).difference(
        {
            "schema_version",
            "plan_id",
            "description",
            "meter",
            "target_bpm",
            "beat_unit",
            "bar_length_beats",
            "pattern_length_beats",
            "repeat_to_fill_bar",
            "events",
            "event_bank_mode",
            "midi_note_duration_beats",
            "peak_target_dbfs",
            "audio_policy",
            "validation",
        }
    )
    if unknown:
        raise ValueError(f"Unknown rhythm-plan fields: {', '.join(sorted(unknown))}")
    schema_version = str(raw_plan.get("schema_version", ""))
    if schema_version != "1.0":
        raise ValueError("schema_version must be '1.0'.")
    numerator, denominator = _normalise_meter(raw_plan.get("meter"))
    target_bpm = _finite_number(raw_plan.get("target_bpm"), "target_bpm")
    if not 20.0 <= target_bpm <= 300.0:
        raise ValueError("target_bpm must be between 20 and 300.")
    beat_s = 60.0 / target_bpm
    bar_length_beats = numerator * 4.0 / denominator
    pattern_length = _finite_number(
        raw_plan.get("pattern_length_beats"), "pattern_length_beats"
    )
    if not 0.0 < pattern_length <= bar_length_beats:
        raise ValueError("pattern_length_beats must be positive and no longer than one bar.")
    repeat_to_fill = bool(raw_plan.get("repeat_to_fill_bar", True))
    if repeat_to_fill:
        repetitions = bar_length_beats / pattern_length
        if abs(repetitions - round(repetitions)) > 1e-8:
            raise ValueError(
                "pattern_length_beats must divide the bar exactly when repeat_to_fill_bar is true."
            )

    raw_events = raw_plan.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        raise ValueError("events must be a non-empty JSON array.")
    events: list[dict[str, Any]] = []
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            raise ValueError(f"events[{index}] must be an object.")
        event_type = str(raw_event.get("type", "")).upper()
        if event_type not in EVENT_NOTES:
            raise ValueError(f"events[{index}].type must be S1 or S2.")
        has_beats = raw_event.get("offset_beats") is not None
        has_seconds = raw_event.get("offset_seconds") is not None
        if has_beats == has_seconds:
            raise ValueError(
                f"events[{index}] must contain exactly one of offset_beats or offset_seconds."
            )
        if has_beats:
            offset_beats = _finite_number(raw_event["offset_beats"], f"events[{index}].offset_beats")
            if not 0.0 <= offset_beats < pattern_length:
                raise ValueError(f"events[{index}].offset_beats lies outside the pattern.")
            offset_seconds = None
        else:
            offset_seconds = _finite_number(
                raw_event["offset_seconds"], f"events[{index}].offset_seconds"
            )
            if not 0.0 <= offset_seconds < pattern_length * beat_s:
                raise ValueError(f"events[{index}].offset_seconds lies outside the pattern.")
            offset_beats = None
        gain_db = _finite_number(raw_event.get("gain_db", 0.0), f"events[{index}].gain_db")
        if not -24.0 <= gain_db <= 12.0:
            raise ValueError(f"events[{index}].gain_db must be between -24 and +12 dB.")
        velocity = int(raw_event.get("midi_velocity", EVENT_VELOCITIES[event_type]))
        if not 1 <= velocity <= 127:
            raise ValueError(f"events[{index}].midi_velocity must be between 1 and 127.")
        events.append(
            {
                "type": event_type,
                "offset_beats": offset_beats,
                "offset_seconds": offset_seconds,
                "gain_db": gain_db,
                "midi_velocity": velocity,
            }
        )

    event_bank_mode = str(raw_plan.get("event_bank_mode", "paired_rotate"))
    if event_bank_mode not in {"best", "rotate", "paired_rotate"}:
        raise ValueError("event_bank_mode must be best, rotate, or paired_rotate.")
    note_duration = _finite_number(
        raw_plan.get("midi_note_duration_beats", 0.5), "midi_note_duration_beats"
    )
    if not 0.0 < note_duration <= bar_length_beats:
        raise ValueError("midi_note_duration_beats must be positive and no longer than the bar.")
    peak_target = _finite_number(raw_plan.get("peak_target_dbfs", -1.0), "peak_target_dbfs")
    if not -12.0 <= peak_target <= -0.1:
        raise ValueError("peak_target_dbfs must be between -12 and -0.1 dBFS.")
    audio_policy = raw_plan.get("audio_policy", {})
    if not isinstance(audio_policy, dict):
        raise ValueError("audio_policy must be an object.")
    unknown_audio_policy = set(audio_policy).difference(
        {
            "time_stretch",
            "pitch_shift",
            "preserve_real_timbre",
            "additional_denoising",
            "dynamic_compression",
        }
    )
    if unknown_audio_policy:
        raise ValueError(
            f"Unknown audio_policy fields: {', '.join(sorted(unknown_audio_policy))}"
        )
    if bool(audio_policy.get("time_stretch", False)):
        raise ValueError("Stage one forbids time stretching to preserve real heartbeat timbre.")
    if bool(audio_policy.get("pitch_shift", False)):
        raise ValueError("Stage one forbids pitch shifting to preserve real heartbeat timbre.")
    if bool(audio_policy.get("additional_denoising", False)):
        raise ValueError("Stage one forbids additional denoising after regularized enhancement.")
    if bool(audio_policy.get("dynamic_compression", False)):
        raise ValueError("Stage one forbids dynamic compression in the timbre-preserving token.")
    if audio_policy.get("preserve_real_timbre", True) is not True:
        raise ValueError("audio_policy.preserve_real_timbre must be true.")

    canonical = {
        "schema_version": "1.0",
        "plan_id": _safe_name(str(raw_plan.get("plan_id", "rhythm_plan"))),
        "description": str(raw_plan.get("description", "")),
        "meter": {"numerator": numerator, "denominator": denominator},
        "target_bpm": target_bpm,
        "beat_unit": "quarter_note",
        "bar_length_beats": bar_length_beats,
        "pattern_length_beats": pattern_length,
        "repeat_to_fill_bar": repeat_to_fill,
        "events": events,
        "event_bank_mode": event_bank_mode,
        "midi_note_duration_beats": note_duration,
        "peak_target_dbfs": peak_target,
        "audio_policy": {
            "time_stretch": False,
            "pitch_shift": False,
            "additional_denoising": False,
            "dynamic_compression": False,
            "preserve_real_timbre": True,
        },
    }
    expanded, warnings = expand_plan(canonical)
    canonical["validation"] = {
        "expanded_event_count": len(expanded),
        "minimum_onset_spacing_s": _minimum_cyclic_spacing(
            [event["time_s"] for event in expanded], bar_length_beats * beat_s
        ),
        "warnings": warnings,
    }
    return canonical


def load_and_validate_plan(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Rhythm plan not found: {resolved}")
    with resolved.open("r", encoding="utf-8-sig") as handle:
        return validate_plan(json.load(handle))


def _minimum_cyclic_spacing(times: list[float], duration_s: float) -> float:
    ordered = sorted(times)
    if not ordered:
        return duration_s
    spacings = [b - a for a, b in zip(ordered, ordered[1:])]
    spacings.append(duration_s - ordered[-1] + ordered[0])
    return min(spacings)


def expand_plan(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Expand one pattern into a complete bar without changing event waveforms."""
    bpm = float(plan["target_bpm"])
    beat_s = 60.0 / bpm
    pattern_beats = float(plan["pattern_length_beats"])
    bar_beats = float(plan["bar_length_beats"])
    repetitions = int(round(bar_beats / pattern_beats)) if plan["repeat_to_fill_bar"] else 1
    expanded: list[dict[str, Any]] = []
    for repetition in range(repetitions):
        anchor_beats = repetition * pattern_beats
        anchor_s = anchor_beats * beat_s
        for pattern_index, event in enumerate(plan["events"]):
            if event["offset_beats"] is not None:
                time_beats = anchor_beats + float(event["offset_beats"])
                time_s = time_beats * beat_s
            else:
                time_s = anchor_s + float(event["offset_seconds"])
                time_beats = time_s / beat_s
            if time_s >= bar_beats * beat_s - 1e-10:
                raise ValueError("An expanded event reaches or exceeds the bar boundary.")
            expanded.append(
                {
                    **event,
                    "pattern_repetition": repetition,
                    "pattern_event_index": pattern_index,
                    "time_beats": time_beats,
                    "time_s": time_s,
                }
            )
    expanded.sort(key=lambda event: (event["time_s"], event["pattern_event_index"]))
    minimum = _minimum_cyclic_spacing(
        [event["time_s"] for event in expanded], bar_beats * beat_s
    )
    if minimum < HARD_MINIMUM_EVENT_SPACING_S - 1e-9:
        raise ValueError(
            f"Rhythm plan places events only {minimum:.4f}s apart; minimum allowed is "
            f"{HARD_MINIMUM_EVENT_SPACING_S:.3f}s. The program will not silently move them."
        )
    warnings = []
    if minimum < WARNING_EVENT_SPACING_S:
        warnings.append("short_inter_event_spacing_possible_tail_overlap")
    return expanded, warnings


def _place_clip(output: np.ndarray, start: int, clip: np.ndarray, sample_rate: int) -> tuple[bool, bool]:
    source_start = max(0, -start)
    destination_start = max(0, start)
    used = min(len(clip) - source_start, len(output) - destination_start)
    if used <= 0:
        return source_start > 0, True
    placed = clip[source_start : source_start + used].copy()
    head_truncated = source_start > 0
    tail_truncated = source_start + used < len(clip)
    if head_truncated and used > 2:
        fade = min(used // 3, max(2, int(round(0.005 * sample_rate))))
        placed[:fade] *= np.sin(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
        placed[0] = 0.0
    if tail_truncated and used > 2:
        fade = min(used // 3, max(2, int(round(0.006 * sample_rate))))
        placed[-fade:] *= np.cos(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
    output[destination_start : destination_start + used] += placed
    return head_truncated, tail_truncated


def _paired_cycles(prepared: tempo_renderer.PreparedInput) -> list[dict[str, tempo_renderer.ClipExemplar]]:
    by_type = {
        event_type: {item.cycle_index: item for item in exemplars}
        for event_type, exemplars in prepared.bank.items()
    }
    return [
        {"S1": by_type["S1"][cycle], "S2": by_type["S2"][cycle]}
        for cycle in sorted(set(by_type["S1"]).intersection(by_type["S2"]))
    ]


def _select_exemplar(
    prepared: tempo_renderer.PreparedInput,
    event_type: str,
    repetition: int,
    sequence_index: int,
    mode: str,
    pairs: list[dict[str, tempo_renderer.ClipExemplar]],
) -> tuple[tempo_renderer.ClipExemplar, str]:
    if mode == "best":
        return max(prepared.bank[event_type], key=lambda item: item.quality_score), "best"
    if mode == "paired_rotate" and pairs:
        return pairs[repetition % len(pairs)][event_type], "paired_rotate"
    exemplars = prepared.bank[event_type]
    fallback = "rotate_fallback_no_complete_pairs" if mode == "paired_rotate" else "rotate"
    return exemplars[sequence_index % len(exemplars)], fallback


def _true_peak(signal: np.ndarray) -> float:
    if not len(signal):
        return 0.0
    return float(np.max(np.abs(resample_poly(signal, 4, 1))))


def _normalise_true_peak(signal: np.ndarray, target_dbfs: float) -> tuple[np.ndarray, float, float]:
    peak = _true_peak(signal)
    if peak <= 1e-12:
        raise ValueError("Rendered heartbeat bar is silent.")
    target = 10.0 ** (target_dbfs / 20.0)
    gain = target / peak
    rendered = np.asarray(signal, dtype=np.float64) * gain
    return rendered, 20.0 * math.log10(max(gain, 1e-12)), _true_peak(rendered)


def _vlq(value: int) -> bytes:
    buffer = value & 0x7F
    output = bytearray([buffer])
    value >>= 7
    while value:
        buffer = (value & 0x7F) | 0x80
        output.insert(0, buffer)
        value >>= 7
    return bytes(output)


def _meta(kind: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([kind]) + _vlq(len(payload)) + payload


def _midi_track(events: Iterable[tuple[int, int, bytes]]) -> bytes:
    body = bytearray()
    previous = 0
    for tick, order, payload in sorted(events, key=lambda item: (item[0], item[1])):
        del order
        body.extend(_vlq(tick - previous))
        body.extend(payload)
        previous = tick
    return b"MTrk" + struct.pack(">I", len(body)) + body


def build_token_midi(plan: dict[str, Any], rows: list[dict[str, Any]]) -> bytes:
    numerator = int(plan["meter"]["numerator"])
    denominator = int(plan["meter"]["denominator"])
    bpm = float(plan["target_bpm"])
    bar_ticks = int(round(float(plan["bar_length_beats"]) * PPQ))
    tempo_us = int(round(60_000_000 / bpm))
    meta_events = [
        (0, 0, _meta(0x03, b"LegaSynth rhythm plan")),
        (0, 1, _meta(0x51, tempo_us.to_bytes(3, "big"))),
        (0, 2, _meta(0x58, bytes([numerator, int(math.log2(denominator)), 24, 8]))),
        (bar_ticks, 99, _meta(0x2F, b"")),
    ]
    note_events: list[tuple[int, int, bytes]] = [
        (0, 0, _meta(0x03, b"LegaSynth Heartbeat Token")),
        (0, 1, bytes([0xC9, 0])),
    ]
    duration_ticks = int(round(float(plan["midi_note_duration_beats"]) * PPQ))
    for row in rows:
        tick = int(round(float(row["time_beats"]) * PPQ))
        end_tick = min(bar_ticks, tick + duration_ticks)
        note = EVENT_NOTES[str(row["event_type"])]
        velocity = int(row["midi_velocity"])
        note_events.append((tick, 20, bytes([0x99, note, velocity])))
        note_events.append((end_tick, 10, bytes([0x89, note, 0])))
        row["midi_tick"] = tick
        row["midi_duration_ticks"] = end_tick - tick
        row["midi_note"] = note
    note_events.append((bar_ticks, 99, _meta(0x2F, b"")))
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 1, 2, PPQ)
        + _midi_track(meta_events)
        + _midi_track(note_events)
    )


def build_heartbeat_package(
    processed_wav: Path,
    plan: dict[str, Any],
    package_dir: Path,
    *,
    raw_source_path: Path | None = None,
    processing_analysis_path: Path | None = None,
) -> Path:
    """Render one plan from a regularized_events_enhanced WAV."""
    canonical = validate_plan(plan) if "validation" not in plan else plan
    expanded, plan_warnings = expand_plan(canonical)
    prepared = tempo_renderer.inspect_wav(processed_wav)
    processed_hash_before = sha256_file(processed_wav)
    package_dir.mkdir(parents=True, exist_ok=True)
    samples_dir = package_dir / "event_samples"
    samples_dir.mkdir(exist_ok=True)

    sample_rate = prepared.sample_rate
    duration_s = float(canonical["bar_length_beats"]) * 60.0 / float(canonical["target_bpm"])
    output = np.zeros(max(1, int(round(duration_s * sample_rate))), dtype=np.float64)
    pairs = _paired_cycles(prepared)
    sequence_counts = {"S1": 0, "S2": 0}
    used_samples: dict[tuple[str, int], dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    warnings = list(plan_warnings)
    for event_index, event in enumerate(expanded):
        event_type = str(event["type"])
        exemplar, effective_mode = _select_exemplar(
            prepared,
            event_type,
            int(event["pattern_repetition"]),
            sequence_counts[event_type],
            str(canonical["event_bank_mode"]),
            pairs,
        )
        sequence_counts[event_type] += 1
        if effective_mode.endswith("fallback_no_complete_pairs") and effective_mode not in warnings:
            warnings.append(effective_mode)
        clip = exemplar.raw * (10.0 ** (float(event["gain_db"]) / 20.0))
        sample_index = int(round(float(event["time_s"]) * sample_rate))
        placement_start = sample_index - exemplar.alignment_offset_samples
        head_truncated, tail_truncated = _place_clip(output, placement_start, clip, sample_rate)
        sample_key = (event_type, exemplar.cycle_index)
        if sample_key not in used_samples:
            sample_path = samples_dir / f"{event_type.lower()}_cycle{exemplar.cycle_index:03d}.wav"
            extraction.write_wav(sample_path, sample_rate, exemplar.raw)
            used_samples[sample_key] = {
                "event_type": event_type,
                "cycle_index": exemplar.cycle_index,
                "file": str(sample_path.relative_to(package_dir)).replace("\\", "/"),
                "sha256": sha256_file(sample_path),
                "source_event_time_s": exemplar.source_time_s,
                "quality_score": exemplar.quality_score,
                "alignment_offset_samples": exemplar.alignment_offset_samples,
            }
        rows.append(
            {
                "event_index": event_index,
                "pattern_repetition": int(event["pattern_repetition"]),
                "pattern_event_index": int(event["pattern_event_index"]),
                "event_type": event_type,
                "time_beats": float(event["time_beats"]),
                "time_s": float(event["time_s"]),
                "sample_index": sample_index,
                "gain_db": float(event["gain_db"]),
                "midi_velocity": int(event["midi_velocity"]),
                "source_cycle_index": exemplar.cycle_index,
                "source_event_time_s": exemplar.source_time_s,
                "source_quality_score": exemplar.quality_score,
                "selection_mode": effective_mode,
                "placement_start_sample": placement_start,
                "head_truncated_at_bar_start": head_truncated,
                "tail_truncated_at_bar_end": tail_truncated,
            }
        )

    rendered, normalisation_gain_db, measured_true_peak = _normalise_true_peak(
        output, float(canonical["peak_target_dbfs"])
    )
    token_wav = package_dir / "heartbeat_bar.wav"
    extraction.write_wav(token_wav, sample_rate, rendered)
    token_midi = package_dir / "heartbeat_bar.mid"
    token_midi.write_bytes(build_token_midi(canonical, rows))
    events_csv = package_dir / "heartbeat_events.csv"
    with events_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    annotations = [(row["event_type"], float(row["time_s"])) for row in rows]
    plot_stem = package_dir / "heartbeat_bar_time_energy"
    time_energy_png, time_energy_svg = diagnostics.render_time_energy_pair(
        plot_stem,
        (
            f"Heartbeat token | {canonical['target_bpm']:g} BPM | "
            f"{canonical['meter']['numerator']}/{canonical['meter']['denominator']}"
        ),
        sample_rate,
        rendered,
        annotations,
        metadata={
            "Plan id": canonical["plan_id"],
            "Pattern length (beats)": f"{canonical['pattern_length_beats']:g}",
            "Event bank": canonical["event_bank_mode"],
            "Method": "LLM/user timing plan; real event repositioning; no time stretch/pitch shift",
        },
    )
    plan_path = package_dir / "rhythm_plan.json"
    plan_path.write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    if sha256_file(processed_wav) != processed_hash_before:
        raise RuntimeError("Processed heartbeat WAV changed while the package was rendered.")
    raw_hash = sha256_file(raw_source_path) if raw_source_path else None
    analysis_hash = sha256_file(processing_analysis_path) if processing_analysis_path else None
    manifest = {
        "schema_version": "1.0",
        "stage": "LegaSynth heartbeat stage one",
        "builder": {"name": Path(__file__).name, "version": VERSION},
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS_WITH_WARNING" if warnings else "PASS",
        "warnings": warnings,
        "inputs": {
            "raw_wav": str(raw_source_path.resolve()) if raw_source_path else None,
            "raw_wav_sha256": raw_hash,
            "regularized_events_enhanced_wav": str(processed_wav.resolve()),
            "regularized_events_enhanced_wav_sha256": processed_hash_before,
            "heart_processing_analysis_json": (
                str(processing_analysis_path.resolve()) if processing_analysis_path else None
            ),
            "heart_processing_analysis_json_sha256": analysis_hash,
            "rhythm_plan_sha256": _json_sha256(canonical),
        },
        "timing": {
            "target_bpm": canonical["target_bpm"],
            "meter": canonical["meter"],
            "bar_length_beats": canonical["bar_length_beats"],
            "duration_s": duration_s,
            "event_count": len(rows),
            "minimum_onset_spacing_s": canonical["validation"]["minimum_onset_spacing_s"],
        },
        "timbre_policy": {
            **canonical["audio_policy"],
            "source_variant": prepared.input_variant,
            "event_bank_mode": canonical["event_bank_mode"],
            "linear_event_gain_only": True,
            "true_peak_normalisation_only": True,
            "normalisation_gain_db": normalisation_gain_db,
            "measured_final_true_peak_dbfs": 20.0 * math.log10(max(measured_true_peak, 1e-12)),
        },
        "source_analysis": {
            "detected_bpm": prepared.result.bpm,
            "detected_s1_s2_s": prepared.result.s1_s2_s,
            "confidence": prepared.result.confidence,
            "periodicity": prepared.result.periodicity,
            "exemplar_counts": {key: len(value) for key, value in prepared.bank.items()},
            "complete_pair_count": len(pairs),
        },
        "event_samples": list(used_samples.values()),
        "outputs": {
            "heartbeat_bar_wav": token_wav.name,
            "heartbeat_bar_wav_sha256": sha256_file(token_wav),
            "heartbeat_bar_midi": token_midi.name,
            "heartbeat_bar_midi_sha256": sha256_file(token_midi),
            "heartbeat_events_csv": events_csv.name,
            "heartbeat_events_csv_sha256": sha256_file(events_csv),
            "rhythm_plan_json": plan_path.name,
            "rhythm_plan_json_sha256": sha256_file(plan_path),
            "time_energy_png": time_energy_png.name,
            "time_energy_svg": time_energy_svg.name,
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "heart_sound_processor": extraction.VERSION,
            "tempo_bar_renderer": tempo_renderer.VERSION,
        },
    }
    manifest_path = package_dir / "heartbeat_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return package_dir


def _process_raw_wav(input_wav: Path, processing_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    input_wav = input_wav.expanduser().resolve()
    input_hash_before = sha256_file(input_wav)
    config = extraction.Config()
    audio = diagnostics.load_wav(input_wav)
    if audio.duration_s < config.stable_min_s:
        raise ValueError(
            f"Raw WAV duration {audio.duration_s:.3f}s is below the required {config.stable_min_s:.3f}s."
        )
    if float(np.max(np.abs(audio.signal))) < 1e-5:
        raise ValueError("Raw WAV is silent or below the usable amplitude floor.")
    result, centred, filtered, analysis_rate = extraction.analyse(
        audio.signal, audio.sample_rate, config
    )
    if audio.clipped_fraction > 0.01:
        result.warnings.append("more_than_1_percent_samples_are_clipped")
        if result.status == "PASS":
            result.status = "PASS_WITH_WARNING"
    if audio.clipped_fraction > 0.10:
        result.status = "REJECT"
    if result.status == "REJECT":
        raise ValueError(
            f"Raw WAV was rejected by heart_sound_processor (confidence={result.confidence:.3f})."
        )
    extraction.write_outputs(
        input_wav,
        processing_root,
        audio,
        result,
        centred,
        filtered,
        analysis_rate,
        config,
    )
    identity = extraction.source_identity(input_wav)
    processing_group = processing_root / identity["prefix"]
    regularized = processing_group / f"{identity['prefix']}_regularized_events_enhanced.wav"
    analysis = processing_group / f"{identity['prefix']}_analysis.json"
    if not regularized.is_file() or not analysis.is_file():
        raise RuntimeError("heart_sound_processor did not produce the required enhanced WAV/report.")
    if sha256_file(input_wav) != input_hash_before:
        raise RuntimeError("Raw input WAV changed during stage-one processing.")
    return regularized, analysis, {
        "status": result.status,
        "confidence": result.confidence,
        "bpm": result.bpm,
        "stable_start_s": result.stable_region.start_s,
        "stable_end_s": result.stable_region.end_s,
    }


def run_stage1(input_wav: Path, rhythm_plan_path: Path, output_root: Path) -> Path:
    input_wav = input_wav.expanduser().resolve()
    if not input_wav.is_file() or input_wav.suffix.lower() != ".wav":
        raise FileNotFoundError(f"Raw input WAV not found: {input_wav}")
    plan = load_and_validate_plan(rhythm_plan_path)
    identity = extraction.source_identity(input_wav)
    plan_hash = _json_sha256(plan)[:8]
    group_dir = output_root.expanduser().resolve() / (
        f"{identity['prefix']}_{plan['plan_id']}_{plan_hash}_stage1"
    )
    processing_root = group_dir / "heart_processing"
    package_dir = group_dir / "heartbeat_package"
    regularized, analysis, _ = _process_raw_wav(input_wav, processing_root)
    build_heartbeat_package(
        regularized,
        plan,
        package_dir,
        raw_source_path=input_wav,
        processing_analysis_path=analysis,
    )
    return group_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Stage one: raw WAV -> regularized_events_enhanced -> user/LLM rhythm plan "
            "-> portable heartbeat package."
        )
    )
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("rhythm_plan", type=Path)
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parent / "outputs"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        group = run_stage1(args.input_wav, args.rhythm_plan, args.output_dir)
    except (FileNotFoundError, OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Stage-one group created: {group}")
    print(f"Heartbeat package: {group / 'heartbeat_package'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
