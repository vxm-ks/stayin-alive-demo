#!/usr/bin/env python3
"""Export tempo-renderer event tables as MIDI and a patient-specific SoundFont.

The module deliberately consumes the renderer's JSON/CSV audit products instead
of detecting S1/S2 again.  This keeps the score events sample-accurate with the
already approved heartbeat bars.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import scipy
from scipy.io import wavfile
from scipy.signal import resample_poly

try:
    from stage3_midi_renderer.heartbeat_conditioning import (
        HeartbeatConditioningProfile,
        condition_heartbeat_audio,
    )
except ModuleNotFoundError as exc:
    if exc.name != "stage3_midi_renderer":
        raise
    # Direct script execution puts only heartbeat_midi_exporter/ on sys.path.
    # Add the shared project root so the formal Stage 3 component is available.
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    from stage3_midi_renderer.heartbeat_conditioning import (
        HeartbeatConditioningProfile,
        condition_heartbeat_audio,
    )


VERSION = "1.2.0"
DEFAULT_METERS = ("4/4", "3/4", "2/4")
EVENT_NOTE_DEFAULTS = {"S1": 36, "S2": 38}
EVENT_VELOCITY_DEFAULTS = {"S1": 100, "S2": 85}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return value or "heartbeat"


def _normalise_wav(raw: np.ndarray) -> np.ndarray:
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        midpoint = (info.max + 1) / 2.0
        data = (raw.astype(np.float64) - midpoint) / midpoint
    elif np.issubdtype(raw.dtype, np.signedinteger):
        info = np.iinfo(raw.dtype)
        data = raw.astype(np.float64) / float(max(abs(info.min), info.max))
    elif np.issubdtype(raw.dtype, np.floating):
        data = raw.astype(np.float64)
    else:
        raise TypeError(f"Unsupported WAV dtype: {raw.dtype}")
    if data.ndim == 2:
        data = np.mean(data, axis=1)
    if data.ndim != 1 or not data.size:
        raise ValueError("Source WAV must contain at least one audio sample.")
    if not np.all(np.isfinite(data)):
        raise ValueError("Source WAV contains NaN or infinite samples.")
    return np.ascontiguousarray(data)


def load_wav(path: Path) -> tuple[int, np.ndarray]:
    sample_rate, raw = wavfile.read(path)
    if sample_rate <= 0:
        raise ValueError("Source WAV has an invalid sample rate.")
    return int(sample_rate), _normalise_wav(raw)


def write_wav(path: Path, sample_rate: int, signal: np.ndarray) -> None:
    encoded = np.rint(np.clip(signal, -1.0, 1.0) * 32767.0).astype("<i2")
    wavfile.write(path, sample_rate, encoded)


def extract_event_clip(
    signal: np.ndarray,
    sample_rate: int,
    event_time_s: float,
    event_type: str,
) -> np.ndarray:
    """Match the event window used by heart_extraction v1.3.0."""
    pre_s = 0.030
    post_s = 0.180 if event_type == "S1" else 0.150
    centre = int(round(event_time_s * sample_rate))
    start = max(0, centre - int(round(pre_s * sample_rate)))
    end = min(len(signal), centre + int(round(post_s * sample_rate)))
    if centre < 0 or centre >= len(signal) or end <= start:
        raise ValueError(f"{event_type} source event lies outside the source WAV.")
    clip = signal[start:end].copy()
    clip -= float(np.mean(clip))
    fade = min(int(round(0.006 * sample_rate)), len(clip) // 4)
    if fade > 1:
        ramp = np.sin(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
        clip[:fade] *= ramp
        clip[-fade:] *= ramp[::-1]
    return clip


def _resample(signal: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return signal.copy()
    divisor = math.gcd(source_rate, target_rate)
    return resample_poly(signal, target_rate // divisor, source_rate // divisor)


def condition_heartbeat_sample(
    signal: np.ndarray,
    sample_rate: int,
    *,
    highpass_hz: float = 35.0,
    lowpass_hz: float = 220.0,
    compressor_threshold_dbfs: float = -18.0,
    compressor_ratio: float = 4.0,
    target_peak_dbfs: float = -3.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Compatibility adapter for the formal Stage 3 conditioning component."""
    profile = HeartbeatConditioningProfile(
        name="speaker_safe_loud_v1",
        enabled=True,
        highpass_hz=highpass_hz,
        lowpass_hz=lowpass_hz,
        compressor_threshold_dbfs=compressor_threshold_dbfs,
        compressor_ratio=compressor_ratio,
        target_peak_dbfs=target_peak_dbfs,
    )
    conditioned, component_audit = condition_heartbeat_audio(
        signal,
        sample_rate,
        profile=profile,
    )
    profile_values = component_audit.profile
    audit = {
        "component": component_audit.component,
        "component_version": component_audit.component_version,
        "profile": str(profile_values["name"]),
        **{
            key: value
            for key, value in profile_values.items()
            if key not in {"name", "enabled"}
        },
        "input_peak_dbfs": component_audit.input_peak_dbfs,
        "input_rms_dbfs": component_audit.input_rms_dbfs,
        "output_peak_dbfs": component_audit.output_peak_dbfs,
        "output_rms_dbfs": component_audit.output_rms_dbfs,
        "makeup_gain_db": component_audit.makeup_gain_db,
        "input_spectral_energy_percent": component_audit.input_spectral_energy_percent,
        "output_spectral_energy_percent": component_audit.output_spectral_energy_percent,
    }
    return conditioned, audit


def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta time cannot be negative.")
    buffer = value & 0x7F
    while value >> 7:
        value >>= 7
        buffer <<= 8
        buffer |= (value & 0x7F) | 0x80
    output = bytearray()
    while True:
        output.append(buffer & 0xFF)
        if buffer & 0x80:
            buffer >>= 8
        else:
            break
    return bytes(output)


def _meta(meta_type: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([meta_type]) + _vlq(len(payload)) + payload


def _track(events: Iterable[tuple[int, int, bytes]]) -> bytes:
    """Build an MTrk; secondary sort order puts note-offs before note-ons."""
    body = bytearray()
    previous_tick = 0
    for tick, order, payload in sorted(events, key=lambda item: (item[0], item[1])):
        del order
        body.extend(_vlq(tick - previous_tick))
        body.extend(payload)
        previous_tick = tick
    return b"MTrk" + struct.pack(">I", len(body)) + body


def build_midi(
    rows: list[dict[str, Any]],
    bpm: float,
    numerator: int,
    denominator: int,
    ppq: int,
    notes: dict[str, int],
    velocities: dict[str, int],
    note_division: int,
) -> tuple[bytes, list[dict[str, Any]]]:
    if denominator <= 0 or denominator & (denominator - 1):
        raise ValueError("MIDI time-signature denominator must be a power of two.")
    bar_ticks = int(round(numerator * ppq * 4 / denominator))
    microseconds = int(round(60_000_000 / bpm))
    note_ticks = int(round(4 * ppq / note_division))
    meta_events = [
        (0, 0, _meta(0x03, b"LegaSynth tempo and meter")),
        (0, 1, _meta(0x51, microseconds.to_bytes(3, "big"))),
        (
            0,
            2,
            _meta(
                0x58,
                bytes([numerator, int(math.log2(denominator)), 24, 8]),
            ),
        ),
        (bar_ticks, 99, _meta(0x2F, b"")),
    ]
    percussion_events: list[tuple[int, int, bytes]] = [
        (0, 0, _meta(0x03, b"LegaSynth Heartbeat")),
        (0, 1, bytes([0xC9, 0])),
    ]
    audit: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: float(item["time_s"])):
        event_type = str(row["event_type"]).upper()
        tick = int(round(float(row["time_s"]) * bpm * ppq / 60.0))
        if not 0 <= tick < bar_ticks:
            raise ValueError(f"{event_type} tick {tick} lies outside the bar.")
        note = notes[event_type]
        velocity = velocities[event_type]
        off_tick = min(tick + note_ticks, bar_ticks)
        percussion_events.append((tick, 20, bytes([0x99, note, velocity])))
        percussion_events.append((off_tick, 10, bytes([0x89, note, 0])))
        audit.append(
            {
                "event_type": event_type,
                "time_s": float(row["time_s"]),
                "tick": tick,
                "duration_ticks": off_tick - tick,
                "midi_note": note,
                "velocity": velocity,
                "source_event_time_s": float(row["source_event_time_s"]),
            }
        )
    percussion_events.append((bar_ticks, 99, _meta(0x2F, b"")))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 2, ppq)
    return header + _track(meta_events) + _track(percussion_events), audit


def _riff_chunk(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise ValueError("RIFF chunk tags must contain four bytes.")
    return tag + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")


def _riff_list(kind: bytes, chunks: Iterable[bytes]) -> bytes:
    return _riff_chunk(b"LIST", kind + b"".join(chunks))


def _fixed_text(value: str, length: int = 20) -> bytes:
    raw = value.encode("ascii", errors="replace")[: length - 1]
    return raw + b"\x00" * (length - len(raw))


def build_soundfont(
    samples: list[dict[str, Any]],
    bank_name: str,
    sample_rate: int,
    *,
    include_melodic_preset: bool = False,
    layer_count: int = 1,
) -> bytes:
    """Build a minimal SoundFont 2.04 heartbeat bank.

    Official Stage 3 assets are percussion-only.  This prevents a heartbeat
    bank loaded above the general SoundFont from shadowing melodic bank 0
    instruments.  ``include_melodic_preset`` remains available only for manual
    compatibility exports.
    """
    if not samples:
        raise ValueError("At least one SoundFont sample is required.")
    if not 1 <= layer_count <= 32:
        raise ValueError("SoundFont layer_count must be between 1 and 32.")
    info = _riff_list(
        b"INFO",
        [
            _riff_chunk(b"ifil", struct.pack("<HH", 2, 4)),
            _riff_chunk(b"isng", b"EMU8000\x00"),
            _riff_chunk(b"INAM", bank_name.encode("ascii", errors="replace")[:254] + b"\x00"),
            _riff_chunk(b"ICMT", b"Patient-specific S1/S2 samples; LegaSynth\x00"),
            _riff_chunk(b"ISFT", f"heartbeat_midi_exporter {VERSION}".encode("ascii") + b"\x00"),
        ],
    )

    sample_data = bytearray()
    sample_headers: list[bytes] = []
    cursor = 0
    for item in samples:
        encoded = np.rint(np.clip(item["signal"], -1.0, 1.0) * 32767.0).astype("<i2")
        start = cursor
        end = start + len(encoded)
        sample_data.extend(encoded.tobytes())
        sample_data.extend(b"\x00" * (46 * 2))
        cursor = end + 46
        sample_headers.append(
            struct.pack(
                "<20sIIIIIBbHH",
                _fixed_text(item["event_type"]),
                start,
                end,
                start,
                end,
                sample_rate,
                int(item["note"]),
                0,
                0,
                1,
            )
        )
    sdta = _riff_list(b"sdta", [_riff_chunk(b"smpl", bytes(sample_data))])

    presets = [("Heartbeat Perc", 0, 128)]
    if include_melodic_preset:
        presets.insert(0, ("Heartbeat Keys", 0, 0))
    phdr = b"".join(
        struct.pack("<20sHHHIII", _fixed_text(name), program, bank, index, 0, 0, 0)
        for index, (name, program, bank) in enumerate(presets)
    ) + struct.pack(
        "<20sHHHIII", _fixed_text("EOP"), 0, 0, len(presets), 0, 0, 0
    )
    pbag = b"".join(
        struct.pack("<HH", index, 0) for index in range(len(presets) + 1)
    )
    pmod = b"\x00" * 10
    pgen = b"".join(struct.pack("<HH", 41, 0) for _ in presets) + b"\x00" * 4
    zone_sample_indexes = [
        sample_index
        for sample_index in range(len(samples))
        for _ in range(layer_count)
    ]
    inst = b"".join(
        [
            struct.pack("<20sH", _fixed_text("Heartbeat"), 0),
            struct.pack("<20sH", _fixed_text("EOI"), len(zone_sample_indexes)),
        ]
    )
    ibag = b"".join(
        struct.pack("<HH", index * 4, 0)
        for index in range(len(zone_sample_indexes) + 1)
    )
    imod = b"\x00" * 10
    igen_parts: list[bytes] = []
    for sample_index in zone_sample_indexes:
        item = samples[sample_index]
        key = int(item["note"])
        igen_parts.extend(
            [
                struct.pack("<HH", 43, key | (key << 8)),  # keyRange
                struct.pack("<HH", 58, key),  # overridingRootKey
                struct.pack("<HH", 54, 0),  # unlooped sample
                struct.pack("<HH", 53, sample_index),  # sampleID must be last
            ]
        )
    igen = b"".join(igen_parts) + b"\x00" * 4
    eos_start = cursor
    shdr = b"".join(sample_headers) + struct.pack(
        "<20sIIIIIBbHH",
        _fixed_text("EOS"),
        eos_start,
        eos_start,
        eos_start,
        eos_start,
        sample_rate,
        0,
        0,
        0,
        1,
    )
    pdta = _riff_list(
        b"pdta",
        [
            _riff_chunk(b"phdr", phdr),
            _riff_chunk(b"pbag", pbag),
            _riff_chunk(b"pmod", pmod),
            _riff_chunk(b"pgen", pgen),
            _riff_chunk(b"inst", inst),
            _riff_chunk(b"ibag", ibag),
            _riff_chunk(b"imod", imod),
            _riff_chunk(b"igen", igen),
            _riff_chunk(b"shdr", shdr),
        ],
    )
    payload = b"sfbk" + info + sdta + pdta
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def _read_rows(events_path: Path) -> list[dict[str, Any]]:
    with events_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "meter",
        "event_type",
        "time_s",
        "source_event_time_s",
        "source_amplitude",
        "exemplar_quality_score",
    }
    if not rows:
        raise ValueError("Event CSV is empty.")
    missing = required.difference(rows[0])
    if missing:
        raise ValueError(f"Event CSV lacks columns: {', '.join(sorted(missing))}")
    for row in rows:
        event_type = str(row["event_type"]).upper()
        if event_type not in EVENT_NOTE_DEFAULTS:
            raise ValueError(f"Unsupported event type: {event_type}")
        row["event_type"] = event_type
        for field in ("time_s", "source_event_time_s", "source_amplitude", "exemplar_quality_score"):
            row[field] = float(row[field])
            if not math.isfinite(row[field]):
                raise ValueError(f"Non-finite {field} in event CSV.")
    return rows


def _velocity_map(
    rows: list[dict[str, Any]],
    mode: str,
    s1_velocity: int,
    s2_velocity: int,
) -> dict[str, int]:
    if mode == "uniform":
        return {"S1": s1_velocity, "S2": s2_velocity}
    medians: dict[str, float] = {}
    for event_type in EVENT_NOTE_DEFAULTS:
        values = [max(0.0, float(row["source_amplitude"])) for row in rows if row["event_type"] == event_type]
        if values:
            medians[event_type] = float(np.median(values))
    peak = max(medians.values(), default=1.0)
    return {
        event_type: int(round(np.clip(45 + 70 * amplitude / max(peak, 1e-12), 45, 115)))
        for event_type, amplitude in medians.items()
    }


def _best_exemplars(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for event_type in EVENT_NOTE_DEFAULTS:
        candidates = [row for row in rows if row["event_type"] == event_type]
        if candidates:
            selected[event_type] = max(
                candidates,
                key=lambda row: (float(row["exemplar_quality_score"]), float(row["source_amplitude"])),
            )
    return selected


def export_bundle(
    report_path: Path,
    events_path: Path | None = None,
    output_dir: Path | None = None,
    *,
    s1_note: int = 36,
    s2_note: int = 38,
    s1_velocity: int = 100,
    s2_velocity: int = 85,
    velocity_mode: str = "uniform",
    ppq: int = 480,
    note_division: int = 8,
    soundfont_rate: int = 44100,
    event_mode: str = "both",
    selected_meters: Sequence[str] = DEFAULT_METERS,
    midi_only: bool = False,
    soundfont_layers: int = 1,
    soundfont_conditioning: str = "none",
) -> Path:
    report_path = report_path.expanduser().resolve()
    if not report_path.is_file():
        raise FileNotFoundError(f"Analysis JSON not found: {report_path}")
    with report_path.open("r", encoding="utf-8-sig") as handle:
        report = json.load(handle)
    if events_path is None:
        event_name = report.get("event_table")
        if not event_name:
            raise ValueError("Analysis JSON does not identify its event_table.")
        events_path = report_path.parent / str(event_name)
    events_path = events_path.expanduser().resolve()
    if not events_path.is_file():
        raise FileNotFoundError(f"Event CSV not found: {events_path}")
    source_path = Path(str(report.get("source", {}).get("selected_wav", ""))).expanduser()
    if not source_path.is_absolute():
        source_path = report_path.parent / source_path
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"Source WAV recorded by the report is missing: {source_path}")

    expected_source_hash = str(report.get("source", {}).get("selected_wav_sha256", ""))
    source_hash_before = sha256_file(source_path)
    if expected_source_hash and source_hash_before.lower() != expected_source_hash.lower():
        raise ValueError("Source WAV SHA-256 differs from the renderer report; export aborted.")
    report_hash_before = sha256_file(report_path)
    events_hash_before = sha256_file(events_path)

    timing = report.get("timing", {})
    bpm = float(timing.get("target_bpm", 0.0))
    if not math.isfinite(bpm) or bpm <= 0:
        raise ValueError("Analysis JSON has no valid timing.target_bpm.")
    if event_mode not in {"both", "s1", "s2"}:
        raise ValueError("event_mode must be one of: both, s1, s2.")
    enabled_types = {"S1", "S2"} if event_mode == "both" else {event_mode.upper()}
    selected_meters = tuple(dict.fromkeys(selected_meters))
    if not selected_meters:
        raise ValueError("At least one meter must be selected.")
    unknown_meters = set(selected_meters).difference(DEFAULT_METERS)
    if unknown_meters:
        raise ValueError(f"Unsupported meters: {', '.join(sorted(unknown_meters))}")

    meters = report.get("meters", {})
    missing_meters = set(selected_meters).difference(meters)
    if missing_meters:
        raise ValueError(f"Analysis JSON lacks meters: {', '.join(sorted(missing_meters))}")
    rows = _read_rows(events_path)
    rows = [row for row in rows if row["event_type"] in enabled_types]
    for meter in selected_meters:
        if not any(row["meter"] == meter for row in rows):
            raise ValueError(f"Event CSV contains no selected {event_mode} events for {meter}.")

    if ppq <= 0 or note_division <= 0 or soundfont_rate < 8000:
        raise ValueError("PPQ/note division must be positive and SoundFont rate must be >= 8000 Hz.")
    if not 1 <= soundfont_layers <= 32:
        raise ValueError("soundfont_layers must be between 1 and 32.")
    if soundfont_conditioning not in {"none", "speaker_safe_loud_v1"}:
        raise ValueError("soundfont_conditioning must be none or speaker_safe_loud_v1.")
    notes = {"S1": s1_note, "S2": s2_note}
    for name, value in {**notes, "S1 velocity": s1_velocity, "S2 velocity": s2_velocity}.items():
        if not 0 <= value <= 127:
            raise ValueError(f"{name} must be in the MIDI range 0..127.")
    velocities = _velocity_map(rows, velocity_mode, s1_velocity, s2_velocity)

    prefix = _safe_name(report_path.stem.removesuffix("_bar_analysis"))
    division_name = {1: "whole", 2: "half", 4: "quarter", 8: "eighth", 16: "sixteenth"}.get(
        note_division, f"1-{note_division}"
    )
    variant_parts: list[str] = []
    if event_mode != "both":
        variant_parts.append(event_mode)
    if note_division != 8:
        variant_parts.append(division_name)
    if selected_meters != DEFAULT_METERS:
        variant_parts.append("meters-" + "_".join(meter.replace("/", "-") for meter in selected_meters))
    if soundfont_layers != 1:
        variant_parts.append(f"layers-{soundfont_layers}")
    if soundfont_conditioning != "none":
        variant_parts.append(soundfont_conditioning)
    variant = "_".join(variant_parts)
    base_output = output_dir or (Path(__file__).resolve().parent / "outputs")
    group_name = f"{prefix}_{variant}_midi_bundle" if variant else f"{prefix}_midi_bundle"
    group_dir = base_output.expanduser().resolve() / group_name
    group_dir.mkdir(parents=True, exist_ok=True)

    midi_outputs: dict[str, Any] = {}
    for meter in selected_meters:
        meter_report = meters[meter]
        numerator = int(meter_report["numerator"])
        denominator = int(meter_report["denominator"])
        meter_rows = [row for row in rows if row["meter"] == meter]
        midi, event_audit = build_midi(
            meter_rows,
            bpm,
            numerator,
            denominator,
            ppq,
            notes,
            velocities,
            note_division,
        )
        midi_variant = f"_{event_mode}_{division_name}" if variant else ""
        midi_path = group_dir / f"{prefix}_heartbeat{midi_variant}_{numerator}-{denominator}.mid"
        midi_path.write_bytes(midi)
        midi_outputs[meter] = {
            "file": midi_path.name,
            "sha256": sha256_file(midi_path),
            "events": event_audit,
        }

    soundfont_manifest: dict[str, Any] = {"generated": False}
    if not midi_only:
        source_rate, source_signal = load_wav(source_path)
        exemplars = _best_exemplars(rows)
        sf_samples: list[dict[str, Any]] = []
        raw_clips: dict[str, np.ndarray] = {}
        for event_type, row in exemplars.items():
            raw_clips[event_type] = extract_event_clip(
                source_signal, source_rate, float(row["source_event_time_s"]), event_type
            )
        common_peak = max((float(np.max(np.abs(clip))) for clip in raw_clips.values()), default=0.0)
        gain = (10.0 ** (-3.0 / 20.0)) / common_peak if common_peak > 1e-12 else 1.0
        sample_outputs: dict[str, Any] = {}
        selected_event_types = {
            "both": ("S1", "S2"),
            "s1": ("S1",),
            "s2": ("S2",),
        }[event_mode]
        for event_type in selected_event_types:
            if event_type not in raw_clips:
                continue
            rendered = _resample(raw_clips[event_type] * gain, source_rate, soundfont_rate)
            conditioning_audit = None
            if soundfont_conditioning == "speaker_safe_loud_v1":
                rendered, conditioning_audit = condition_heartbeat_sample(rendered, soundfont_rate)
            wav_path = group_dir / f"{prefix}_soundfont_{event_type.lower()}.wav"
            write_wav(wav_path, soundfont_rate, rendered)
            row = exemplars[event_type]
            sf_samples.append({"event_type": event_type, "note": notes[event_type], "signal": rendered})
            sample_outputs[event_type] = {
                "file": wav_path.name,
                "sha256": sha256_file(wav_path),
                "midi_note": notes[event_type],
                "source_event_time_s": float(row["source_event_time_s"]),
                "source_quality_score": float(row["exemplar_quality_score"]),
                "source_amplitude": float(row["source_amplitude"]),
                "gain_db": float(20.0 * math.log10(max(gain, 1e-12))),
                "conditioning": conditioning_audit,
            }
        sf2_path = group_dir / f"{prefix}_heartbeat.sf2"
        sf2_path.write_bytes(
            build_soundfont(
                sf_samples,
                f"LegaSynth {prefix}",
                soundfont_rate,
                layer_count=soundfont_layers,
            )
        )
        soundfont_manifest = {
            "generated": True,
            "format": "SoundFont 2.04",
            "file": sf2_path.name,
            "sha256": sha256_file(sf2_path),
            "sample_rate_hz": soundfont_rate,
            "normalisation": "common gain to -3 dBFS; relative S1/S2 level preserved",
            "presets": ["bank 128 program 0: Heartbeat Perc"],
            "layers_per_sample": soundfont_layers,
            "conditioning_profile": soundfont_conditioning,
            "samples": sample_outputs,
        }

    if (
        sha256_file(report_path) != report_hash_before
        or sha256_file(events_path) != events_hash_before
        or sha256_file(source_path) != source_hash_before
    ):
        raise RuntimeError("An input changed during export; outputs cannot be trusted.")

    manifest = {
        "exporter": {"name": Path(__file__).name, "version": VERSION},
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "bar_analysis_json": str(report_path),
            "bar_analysis_json_sha256": report_hash_before,
            "bar_events_csv": str(events_path),
            "bar_events_csv_sha256": events_hash_before,
            "selected_wav": str(source_path),
            "selected_wav_sha256": source_hash_before,
            "selected_wav_hash_matches_report": not expected_source_hash or source_hash_before.lower() == expected_source_hash.lower(),
        },
        "midi": {
            "format": 1,
            "channel_human_number": 10,
            "channel_zero_based": 9,
            "target_bpm": bpm,
            "ppq": ppq,
            "equal_note_value": f"1/{note_division}",
            "event_mode": event_mode,
            "selected_meters": list(selected_meters),
            "velocity_mode": velocity_mode,
            "mapping": {
                event_type: {"note": note, "velocity": velocities.get(event_type)}
                for event_type, note in notes.items()
                if event_type in enabled_types
            },
            "outputs": midi_outputs,
        },
        "soundfont": soundfont_manifest,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "standards_and_software": [
            {
                "name": "Standard MIDI Files",
                "url": "https://midi.org/standard-midi-files",
                "use": "MIDI container, tracks, tempo, time signature and note events",
            },
            {
                "name": "SoundFont 2.04 Technical Specification",
                "url": "https://www.synthfont.com/sfspec24.pdf",
                "use": "SF2 RIFF structure, preset/instrument/sample tables",
            },
            {
                "name": "NumPy",
                "version": np.__version__,
                "url": "https://numpy.org/",
                "use": "sample-domain numerical processing",
            },
            {
                "name": "SciPy",
                "version": scipy.__version__,
                "url": "https://scipy.org/",
                "use": "WAV I/O and polyphase sample-rate conversion",
            },
        ],
    }
    manifest_path = group_dir / f"{prefix}_midi_mapping.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return group_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a tempo_bar_renderer analysis JSON and event CSV into MIDI + SF2."
    )
    parser.add_argument("analysis_json", type=Path, help="*_bar_analysis.json from tempo_bar_renderer")
    parser.add_argument("--events-csv", type=Path, help="Override the report's event_table CSV")
    parser.add_argument("--output-dir", type=Path, help="Parent output directory")
    parser.add_argument("--s1-note", type=int, default=36)
    parser.add_argument("--s2-note", type=int, default=38)
    parser.add_argument("--s1-velocity", type=int, default=100)
    parser.add_argument("--s2-velocity", type=int, default=85)
    parser.add_argument("--velocity-mode", choices=("uniform", "source"), default="uniform")
    parser.add_argument("--ppq", type=int, default=480)
    parser.add_argument("--note-division", type=int, default=8, help="8 means equal eighth notes")
    parser.add_argument("--event-mode", choices=("both", "s1", "s2"), default="both")
    parser.add_argument("--meters", nargs="+", choices=DEFAULT_METERS, default=list(DEFAULT_METERS))
    parser.add_argument("--midi-only", action="store_true", help="Do not create SF2/sample WAV files")
    parser.add_argument("--soundfont-rate", type=int, default=44100)
    parser.add_argument(
        "--soundfont-layers", type=int, default=1,
        help="stack 1-32 identical heartbeat sample layers for additional level",
    )
    parser.add_argument(
        "--soundfont-conditioning",
        choices=("none", "speaker_safe_loud_v1"),
        default="none",
        help="optionally filter/compress heartbeat samples before SoundFont creation",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = export_bundle(
            args.analysis_json,
            args.events_csv,
            args.output_dir,
            s1_note=args.s1_note,
            s2_note=args.s2_note,
            s1_velocity=args.s1_velocity,
            s2_velocity=args.s2_velocity,
            velocity_mode=args.velocity_mode,
            ppq=args.ppq,
            note_division=args.note_division,
            soundfont_rate=args.soundfont_rate,
            event_mode=args.event_mode,
            selected_meters=args.meters,
            midi_only=args.midi_only,
            soundfont_layers=args.soundfont_layers,
            soundfont_conditioning=args.soundfont_conditioning,
        )
    except (FileNotFoundError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"MIDI export bundle created: {output}")
    if not args.midi_only:
        print("MuseScore: File > Open a .mid, then select the generated .sf2 sound in Mixer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
