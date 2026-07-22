"""Render an immutable complete MIDI with one or more real-heartbeat packages.

The complete Stage 2 MIDI is the sole timing authority.  This module removes
its heartbeat channel only from a temporary music-render copy, renders that
copy with FluidSynth, places real S1/S2 clips at the original MIDI note-on
times, and applies an independently auditable two-stem balance.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

import numpy as np
from scipy.io import wavfile
from scipy.signal import lfilter, resample_poly

from .heartbeat_conditioning import condition_heartbeat_audio, resolve_conditioning_profile
from .renderer import Stage3RenderError, inspect_complete_midi, inspect_soundfont


VERSION = "2.1.0"
Executor = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[str]]
MODES = {"fixed", "round_robin_per_cycle", "round_robin_per_bar"}
MIX_MODES = {"manual", "event_window_relative", "perceptual_event_adaptive"}

# ITU-R BS.1770 K-weighting biquads at 48 kHz.
_K_B1 = np.array([1.53512485958697, -2.69169618940638, 1.19839281085285])
_K_A1 = np.array([1.0, -1.69065929318241, 0.73248077421585])
_K_B2 = np.array([1.0, -2.0, 1.0])
_K_A2 = np.array([1.0, -1.99004745483398, 0.99007225036621])


@dataclass(frozen=True)
class MidiEvent:
    tick: int
    event_type: str
    velocity: int
    pitch: int
    track_index: int


@dataclass(frozen=True)
class ParsedMidi:
    ppq: int
    end_tick: int
    tempos: tuple[tuple[int, int], ...]
    numerator: int
    denominator: int
    events: tuple[MidiEvent, ...]
    filtered_bytes: bytes


@dataclass(frozen=True)
class SampleClip:
    event_type: str
    cycle_index: int
    relative_path: str
    path: Path
    sha256: str
    quality_score: float
    alignment_offset_samples: int
    sample_rate: int
    signal: np.ndarray


@dataclass(frozen=True)
class PackageBank:
    package_id: str
    path: Path
    manifest_sha256: str
    samples: Mapping[str, tuple[SampleClip, ...]]


@dataclass(frozen=True)
class PackageRenderResult:
    output_dir: Path
    output_wav: Path
    assignments_csv: Path
    manifest_json: Path
    event_count: int
    duration_s: float
    sample_rate: int
    channels: int

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        return {key: str(value) if isinstance(value, Path) else value for key, value in result.items()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _vlq(value: int) -> bytes:
    if value < 0:
        raise Stage3RenderError("negative MIDI delta tick")
    output = bytearray([value & 0x7F])
    value >>= 7
    while value:
        output.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(output)


def _read_vlq(data: bytes, position: int, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise Stage3RenderError(f"truncated MIDI VLQ in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise Stage3RenderError(f"MIDI VLQ exceeds four bytes in {context}")


def _parse_and_filter_midi(path: Path, channel: int, note_map: Mapping[int, str]) -> ParsedMidi:
    data = path.read_bytes()
    if len(data) < 14 or data[:4] != b"MThd":
        raise Stage3RenderError("input is not a Standard MIDI File")
    header_size = struct.unpack_from(">I", data, 4)[0]
    midi_format, track_count, division = struct.unpack_from(">HHH", data, 8)
    if midi_format not in (0, 1) or division == 0 or division & 0x8000:
        raise Stage3RenderError("only PPQ MIDI format 0 and 1 are supported")
    header = data[: 8 + header_size]
    position = len(header)
    output_tracks: list[bytes] = []
    tempos_raw: list[tuple[int, int, int]] = []
    signatures: list[tuple[int, int, int]] = []
    heartbeat: list[MidiEvent] = []
    end_tick = 0
    for track_index in range(track_count):
        context = f"track {track_index}"
        if position + 8 > len(data) or data[position:position + 4] != b"MTrk":
            raise Stage3RenderError(f"missing MTrk chunk at {context}")
        size = struct.unpack_from(">I", data, position + 4)[0]
        chunk = data[position + 8: position + 8 + size]
        if len(chunk) != size:
            raise Stage3RenderError(f"truncated MTrk chunk at {context}")
        position += 8 + size
        cursor = 0
        absolute_tick = 0
        running: int | None = None
        pending_delta = 0
        rebuilt = bytearray()
        event_order = 0
        while cursor < len(chunk):
            delta, cursor = _read_vlq(chunk, cursor, context)
            absolute_tick += delta
            end_tick = max(end_tick, absolute_tick)
            if cursor >= len(chunk):
                raise Stage3RenderError(f"missing event in {context}")
            lead = chunk[cursor]
            explicit = lead >= 0x80
            if explicit:
                status = lead
                cursor += 1
                running = status if status < 0xF0 else None
            elif running is not None:
                status = running
            else:
                raise Stage3RenderError(f"invalid running status in {context}")
            keep = True
            if status == 0xFF:
                if cursor >= len(chunk):
                    raise Stage3RenderError(f"truncated meta event in {context}")
                kind = chunk[cursor]
                cursor += 1
                length, cursor = _read_vlq(chunk, cursor, context)
                payload = chunk[cursor:cursor + length]
                cursor += length
                if len(payload) != length:
                    raise Stage3RenderError(f"truncated meta payload in {context}")
                event_bytes = bytes((0xFF, kind)) + _vlq(length) + payload
                if kind == 0x51:
                    if length != 3:
                        raise Stage3RenderError("invalid MIDI tempo event")
                    tempos_raw.append((absolute_tick, event_order, int.from_bytes(payload, "big")))
                elif kind == 0x58:
                    if length < 2:
                        raise Stage3RenderError("invalid MIDI time signature")
                    signatures.append((absolute_tick, payload[0], 1 << payload[1]))
            elif status in (0xF0, 0xF7):
                length, cursor = _read_vlq(chunk, cursor, context)
                payload = chunk[cursor:cursor + length]
                cursor += length
                if len(payload) != length:
                    raise Stage3RenderError(f"truncated SysEx in {context}")
                event_bytes = bytes((status,)) + _vlq(length) + payload
            elif status >= 0xF0:
                raise Stage3RenderError(f"unsupported MIDI system status 0x{status:02X}")
            else:
                family, event_channel = status & 0xF0, status & 0x0F
                length = 1 if family in (0xC0, 0xD0) else 2
                payload = chunk[cursor:cursor + length]
                cursor += length
                if len(payload) != length or any(value >= 0x80 for value in payload):
                    raise Stage3RenderError(f"invalid channel event in {context}")
                event_bytes = bytes((status,)) + payload  # normalize away running status
                if event_channel == channel:
                    keep = False
                    if family == 0x90 and payload[1] > 0:
                        pitch = payload[0]
                        if pitch not in note_map:
                            raise Stage3RenderError(
                                f"heartbeat channel {channel + 1} contains unsupported note {pitch}"
                            )
                        heartbeat.append(MidiEvent(
                            absolute_tick, note_map[pitch], payload[1], pitch, track_index
                        ))
            if keep:
                rebuilt.extend(_vlq(pending_delta + delta))
                rebuilt.extend(event_bytes)
                pending_delta = 0
            else:
                pending_delta += delta
            event_order += 1
        output_tracks.append(b"MTrk" + struct.pack(">I", len(rebuilt)) + rebuilt)
    if position != len(data):
        raise Stage3RenderError("unexpected bytes after MIDI tracks")
    by_tick: dict[int, int] = {0: 500_000}
    for tick, _order, value in sorted(tempos_raw):
        by_tick[tick] = value
    effective_signatures = {(n, d) for _tick, n, d in signatures}
    if len(effective_signatures) > 1 or any(tick != 0 for tick, _n, _d in signatures):
        raise Stage3RenderError("render-packages requires one constant time signature")
    numerator, denominator = (signatures[-1][1:] if signatures else (4, 4))
    filtered = header + b"".join(output_tracks)
    return ParsedMidi(
        ppq=division,
        end_tick=end_tick,
        tempos=tuple(sorted(by_tick.items())),
        numerator=numerator,
        denominator=denominator,
        events=tuple(sorted(heartbeat, key=lambda item: (item.tick, item.track_index))),
        filtered_bytes=filtered,
    )


def _seconds_at_tick(tick: int, ppq: int, tempos: Sequence[tuple[int, int]]) -> float:
    seconds = 0.0
    previous_tick, tempo = tempos[0]
    for change_tick, next_tempo in tempos[1:]:
        if tick <= change_tick:
            break
        seconds += (change_tick - previous_tick) * tempo / (1_000_000.0 * ppq)
        previous_tick, tempo = change_tick, next_tempo
    return seconds + (tick - previous_tick) * tempo / (1_000_000.0 * ppq)


def _strict_object(value: Any, allowed: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Stage3RenderError(f"{context} must be a JSON object")
    unknown = set(value) - allowed
    if unknown:
        raise Stage3RenderError(f"unknown {context} fields: {sorted(unknown)}")
    return value


def load_render_plan(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        plan = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3RenderError(f"invalid Stage 3 render plan: {exc}") from exc
    _strict_object(plan, {
        "schema_version", "heartbeat_channel", "note_map",
        "heartbeat_conditioning_profile", "velocity_gamma",
        "default_material_rule", "section_material_rules", "mix",
    }, "render plan")
    if plan.get("schema_version") != "1.0":
        raise Stage3RenderError("render plan schema_version must be 1.0")
    channel = plan.get("heartbeat_channel", 10)
    if not isinstance(channel, int) or not 1 <= channel <= 16:
        raise Stage3RenderError("heartbeat_channel must be an integer from 1 to 16")
    raw_map = plan.get("note_map", {"36": "S1", "38": "S2"})
    if not isinstance(raw_map, dict) or not raw_map:
        raise Stage3RenderError("note_map must be a non-empty object")
    note_map: dict[int, str] = {}
    for key, value in raw_map.items():
        try:
            pitch = int(key)
        except (TypeError, ValueError) as exc:
            raise Stage3RenderError(f"invalid note_map key {key!r}") from exc
        if str(pitch) != str(key) or not 0 <= pitch <= 127 or value not in ("S1", "S2"):
            raise Stage3RenderError(f"invalid note_map entry {key!r}: {value!r}")
        note_map[pitch] = value
    profile = plan.get("heartbeat_conditioning_profile", "speaker_safe_loud_v1")
    resolve_conditioning_profile(profile)
    gamma = plan.get("velocity_gamma", 1.0)
    if not isinstance(gamma, (int, float)) or not math.isfinite(gamma) or not 0.1 <= gamma <= 4.0:
        raise Stage3RenderError("velocity_gamma must be finite and between 0.1 and 4.0")

    def validate_rule(raw: Any, context: str, section: bool) -> dict[str, Any]:
        allowed = {"mode", "package_ids", "gain_db"}
        if section:
            allowed |= {"section_id", "bar_start", "bar_end"}
        rule = _strict_object(raw, allowed, context)
        if rule.get("mode") not in MODES:
            raise Stage3RenderError(f"{context}.mode must be one of {sorted(MODES)}")
        ids = rule.get("package_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
            raise Stage3RenderError(f"{context}.package_ids must be a non-empty string list")
        if len(set(ids)) != len(ids):
            raise Stage3RenderError(f"{context}.package_ids contains duplicates")
        if rule["mode"] == "fixed" and len(ids) != 1:
            raise Stage3RenderError(f"{context} fixed mode requires exactly one package_id")
        gain = rule.get("gain_db", 0.0)
        if not isinstance(gain, (int, float)) or not math.isfinite(gain) or not -24 <= gain <= 24:
            raise Stage3RenderError(f"{context}.gain_db must be between -24 and 24")
        if section:
            if not isinstance(rule.get("section_id"), str) or not rule["section_id"]:
                raise Stage3RenderError(f"{context}.section_id is required")
            if not all(isinstance(rule.get(name), int) and rule[name] >= 1 for name in ("bar_start", "bar_end")):
                raise Stage3RenderError(f"{context} bars must be positive integers")
            if rule["bar_end"] < rule["bar_start"]:
                raise Stage3RenderError(f"{context}.bar_end cannot precede bar_start")
        return dict(rule)

    default_rule = validate_rule(plan.get("default_material_rule"), "default_material_rule", False)
    rules = [validate_rule(item, f"section_material_rules[{i}]", True)
             for i, item in enumerate(plan.get("section_material_rules", []))]
    for index, left in enumerate(rules):
        for right in rules[index + 1:]:
            if max(left["bar_start"], right["bar_start"]) <= min(left["bar_end"], right["bar_end"]):
                raise Stage3RenderError("section_material_rules may not overlap")
    mix = _strict_object(plan.get("mix", {}), {
        "mode", "music_gain_db", "heartbeat_gain_db", "heartbeat_over_music_db",
        "maximum_heartbeat_boost_db", "event_window_ms", "true_peak_ceiling_dbtp",
        "publish_stems", "target_heartbeat_over_music_db", "minimum_heartbeat_gain_db",
        "maximum_heartbeat_gain_db", "maximum_event_gain_step_db",
        "maximum_music_duck_db", "duck_attack_ms", "duck_hold_ms", "duck_release_ms",
        "velocity_gain_floor", "measurement_weighting", "final_target_lufs",
    }, "mix")
    mode = mix.get("mode", "manual")
    if mode not in MIX_MODES:
        raise Stage3RenderError(f"mix.mode must be one of {sorted(MIX_MODES)}")
    defaults = {
        "music_gain_db": 0.0, "heartbeat_gain_db": 0.0,
        "heartbeat_over_music_db": 4.0, "maximum_heartbeat_boost_db": 12.0,
        "event_window_ms": 250.0, "true_peak_ceiling_dbtp": -1.0,
        "target_heartbeat_over_music_db": 7.0,
        "minimum_heartbeat_gain_db": 0.0, "maximum_heartbeat_gain_db": 18.0,
        "maximum_event_gain_step_db": 2.0, "maximum_music_duck_db": 4.0,
        "duck_attack_ms": 10.0, "duck_hold_ms": 100.0, "duck_release_ms": 180.0,
        "velocity_gain_floor": 0.75, "final_target_lufs": -16.0,
    }
    for name, default in defaults.items():
        value = mix.get(name, default)
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise Stage3RenderError(f"mix.{name} must be finite")
        mix[name] = float(value)
    for name in (
        "music_gain_db", "heartbeat_gain_db", "heartbeat_over_music_db",
        "maximum_heartbeat_boost_db", "target_heartbeat_over_music_db",
        "minimum_heartbeat_gain_db", "maximum_heartbeat_gain_db",
        "maximum_event_gain_step_db", "maximum_music_duck_db",
    ):
        if not -24 <= mix[name] <= 24:
            raise Stage3RenderError(f"mix.{name} must be between -24 and 24 dB")
    if mix["minimum_heartbeat_gain_db"] > mix["maximum_heartbeat_gain_db"]:
        raise Stage3RenderError("minimum heartbeat gain cannot exceed maximum heartbeat gain")
    if not 0 <= mix["maximum_event_gain_step_db"] <= 12:
        raise Stage3RenderError("maximum event gain step must be between 0 and 12 dB")
    if not 0 <= mix["maximum_music_duck_db"] <= 12:
        raise Stage3RenderError("maximum music duck must be between 0 and 12 dB")
    for name in ("duck_attack_ms", "duck_hold_ms", "duck_release_ms"):
        if not 0 <= mix[name] <= 2000:
            raise Stage3RenderError(f"mix.{name} must be between 0 and 2000 ms")
    if not 0 <= mix["velocity_gain_floor"] <= 1:
        raise Stage3RenderError("mix.velocity_gain_floor must be between 0 and 1")
    if not -30 <= mix["final_target_lufs"] <= -8:
        raise Stage3RenderError("mix.final_target_lufs must be between -30 and -8")
    weighting = mix.get("measurement_weighting", "k_weighted")
    if weighting != "k_weighted":
        raise Stage3RenderError("mix.measurement_weighting must be k_weighted")
    mix["measurement_weighting"] = weighting
    if not 50 <= mix["event_window_ms"] <= 2000:
        raise Stage3RenderError("mix.event_window_ms must be between 50 and 2000")
    if not -12 <= mix["true_peak_ceiling_dbtp"] <= -0.1:
        raise Stage3RenderError("mix.true_peak_ceiling_dbtp must be between -12 and -0.1")
    if not isinstance(mix.get("publish_stems", False), bool):
        raise Stage3RenderError("mix.publish_stems must be boolean")
    mix["publish_stems"] = mix.get("publish_stems", False)
    return {
        "schema_version": "1.0", "heartbeat_channel": channel, "note_map": note_map,
        "heartbeat_conditioning_profile": profile, "velocity_gamma": float(gamma),
        "default_material_rule": default_rule, "section_material_rules": rules, "mix": mix,
        "source_path": source, "source_sha256": _sha256(source),
    }


def parse_package_binding(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise Stage3RenderError("--heartbeat-package must use ID=PATH")
    package_id, raw_path = value.split("=", 1)
    if not package_id or any(character.isspace() for character in package_id):
        raise Stage3RenderError("heartbeat package ID must be non-empty and contain no spaces")
    return package_id, Path(raw_path).expanduser().resolve()


def _read_wav(path: Path) -> tuple[int, np.ndarray]:
    try:
        rate, raw = wavfile.read(path)
    except (OSError, ValueError) as exc:
        raise Stage3RenderError(f"could not read WAV {path}: {exc}") from exc
    if raw.ndim not in (1, 2) or len(raw) == 0:
        raise Stage3RenderError(f"invalid WAV dimensions: {path}")
    if np.issubdtype(raw.dtype, np.integer):
        if np.issubdtype(raw.dtype, np.unsignedinteger):
            midpoint = float(1 << (8 * raw.dtype.itemsize - 1))
            audio = (raw.astype(np.float64) - midpoint) / midpoint
        else:
            audio = raw.astype(np.float64) / float(1 << (8 * raw.dtype.itemsize - 1))
    elif np.issubdtype(raw.dtype, np.floating):
        audio = raw.astype(np.float64)
    else:
        raise Stage3RenderError(f"unsupported WAV sample type: {raw.dtype}")
    if audio.ndim == 2:
        audio = np.mean(audio, axis=1)
    if not np.all(np.isfinite(audio)):
        raise Stage3RenderError(f"WAV contains non-finite samples: {path}")
    return int(rate), np.ascontiguousarray(audio)


def _load_package(package_id: str, path: Path) -> PackageBank:
    if not path.is_dir():
        raise Stage3RenderError(f"heartbeat package not found: {package_id}={path}")
    manifest_path = path / "heartbeat_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3RenderError(f"invalid heartbeat package {package_id}: {exc}") from exc
    if manifest.get("schema_version") != "1.0" or not isinstance(manifest.get("event_samples"), list):
        raise Stage3RenderError(f"heartbeat package {package_id} has invalid schema")
    bank: dict[str, list[SampleClip]] = {"S1": [], "S2": []}
    root = path.resolve()
    for index, item in enumerate(manifest["event_samples"]):
        if not isinstance(item, dict) or item.get("event_type") not in bank:
            raise Stage3RenderError(f"invalid {package_id} event_samples[{index}]")
        relative = str(item.get("file", ""))
        sample_path = (root / relative).resolve()
        if not sample_path.is_relative_to(root):
            raise Stage3RenderError(f"heartbeat package {package_id} contains unsafe path")
        actual_hash = _sha256(sample_path) if sample_path.is_file() else ""
        if actual_hash != str(item.get("sha256", "")):
            raise Stage3RenderError(f"heartbeat sample hash mismatch: {package_id}/{relative}")
        rate, signal = _read_wav(sample_path)
        bank[item["event_type"]].append(SampleClip(
            event_type=item["event_type"], cycle_index=int(item.get("cycle_index", index)),
            relative_path=relative.replace("\\", "/"), path=sample_path, sha256=actual_hash,
            quality_score=float(item.get("quality_score", 0.0)),
            alignment_offset_samples=int(item.get("alignment_offset_samples", 0)),
            sample_rate=rate, signal=signal,
        ))
    if not bank["S1"] and not bank["S2"]:
        raise Stage3RenderError(f"heartbeat package {package_id} has no S1/S2 samples")
    return PackageBank(package_id, root, _sha256(manifest_path), {
        key: tuple(sorted(values, key=lambda x: (x.cycle_index, -x.quality_score, x.relative_path)))
        for key, values in bank.items()
    })


def _rule_for_bar(plan: Mapping[str, Any], bar: int) -> tuple[str, Mapping[str, Any]]:
    for rule in plan["section_material_rules"]:
        if rule["bar_start"] <= bar <= rule["bar_end"]:
            return rule["section_id"], rule
    return "default", plan["default_material_rule"]


def _select_package(
    rule_id: str, rule: Mapping[str, Any], bar: int, event_type: str,
    states: dict[str, dict[str, int | None]],
) -> str:
    ids = rule["package_ids"]
    if rule["mode"] == "fixed":
        return ids[0]
    if rule["mode"] == "round_robin_per_bar":
        origin = rule.get("bar_start", 1)
        return ids[(bar - origin) % len(ids)]
    state = states.setdefault(rule_id, {"cycle": -1, "current": None})
    if event_type == "S1" or state["current"] is None:
        state["cycle"] = int(state["cycle"]) + 1
        state["current"] = int(state["cycle"]) % len(ids)
    return ids[int(state["current"])]


def _place_clip(output: np.ndarray, start: int, clip: np.ndarray) -> tuple[bool, bool]:
    source_start, destination_start = max(0, -start), max(0, start)
    used = min(len(clip) - source_start, len(output) - destination_start)
    if used <= 0:
        return source_start > 0, True
    placed = clip[source_start:source_start + used].copy()
    head, tail = source_start > 0, source_start + used < len(clip)
    if head and used > 2:
        fade = min(used // 3, 256)
        placed[:fade] *= np.sin(np.linspace(0, np.pi / 2, fade)) ** 2
    if tail and used > 2:
        fade = min(used // 3, 256)
        placed[-fade:] *= np.cos(np.linspace(0, np.pi / 2, fade)) ** 2
    output[destination_start:destination_start + used] += placed
    return head, tail


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float64) ** 2))) if audio.size else 0.0


def _k_weighted(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    values = np.asarray(audio, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if sample_rate != 48_000:
        divisor = math.gcd(sample_rate, 48_000)
        values = resample_poly(values, 48_000 // divisor, sample_rate // divisor, axis=0)
    weighted = lfilter(_K_B1, _K_A1, values, axis=0)
    return lfilter(_K_B2, _K_A2, weighted, axis=0)


def _perceptual_level_db(audio: np.ndarray, sample_rate: int) -> float:
    return _db(_rms(_k_weighted(audio, sample_rate)))


def _integrated_loudness_lufs(audio: np.ndarray, sample_rate: int) -> float:
    weighted = _k_weighted(audio, sample_rate)
    block, step = 19_200, 4_800
    if len(weighted) < block:
        weighted = np.pad(weighted, ((0, block - len(weighted)), (0, 0)))
    energies = np.asarray([
        float(np.sum(np.mean(np.square(weighted[start:start + block]), axis=0)))
        for start in range(0, len(weighted) - block + 1, step)
    ])
    block_loudness = -0.691 + 10.0 * np.log10(np.maximum(energies, 1e-300))
    absolute = energies[block_loudness >= -70.0]
    if not len(absolute):
        return float("-inf")
    relative = -0.691 + 10.0 * math.log10(float(np.mean(absolute))) - 10.0
    gated = energies[block_loudness >= max(-70.0, relative)]
    return (
        -0.691 + 10.0 * math.log10(float(np.mean(gated)))
        if len(gated) else float("-inf")
    )


def _active_bounds(signal: np.ndarray, sample_rate: int, maximum_ms: float) -> tuple[int, int]:
    """Find a stable high-energy event span without including a long silent tail."""
    values = np.abs(np.asarray(signal, dtype=np.float64))
    maximum = min(len(values), max(1, int(round(maximum_ms * sample_rate / 1000.0))))
    values = values[:maximum]
    peak = float(np.max(values)) if len(values) else 0.0
    active = np.flatnonzero(values >= peak * 10 ** (-35.0 / 20.0)) if peak > 0 else np.array([], dtype=int)
    if len(active):
        left, right = int(active[0]), int(active[-1]) + 1
    else:
        left, right = 0, maximum
    minimum = min(maximum, max(1, int(round(0.060 * sample_rate))))
    right = min(maximum, max(right, left + minimum))
    return left, right


def _duck_envelope(
    length: int,
    events: Sequence[tuple[int, float]],
    sample_rate: int,
    attack_ms: float,
    hold_ms: float,
    release_ms: float,
) -> np.ndarray:
    envelope = np.ones(length, dtype=np.float64)
    attack = int(round(attack_ms * sample_rate / 1000.0))
    hold = int(round(hold_ms * sample_rate / 1000.0))
    release = int(round(release_ms * sample_rate / 1000.0))
    for center, duck_db in events:
        if duck_db <= 0:
            continue
        low = 10 ** (-duck_db / 20.0)
        start, hold_end, end = center - attack, center + hold, center + hold + release
        left = max(0, start)
        if left < min(length, center):
            envelope[left:center] = np.minimum(
                envelope[left:center], np.linspace(1.0, low, center - left, endpoint=False)
            )
        if center < min(length, hold_end):
            envelope[center:min(length, hold_end)] = np.minimum(
                envelope[center:min(length, hold_end)], low
            )
        right_start, right_end = max(0, hold_end), min(length, end)
        if right_start < right_end:
            envelope[right_start:right_end] = np.minimum(
                envelope[right_start:right_end],
                np.linspace(low, 1.0, right_end - right_start, endpoint=False),
            )
    return envelope


def _true_peak(audio: np.ndarray) -> float:
    if not audio.size:
        return 0.0
    # Chunking bounds temporary memory while one-sample overlap protects boundaries.
    peak = 0.0
    block = 262_144
    for start in range(0, len(audio), block):
        part = audio[max(0, start - 1): min(len(audio), start + block + 1)]
        peak = max(peak, float(np.max(np.abs(resample_poly(part, 4, 1, axis=0)))))
    return peak


def _write_wav(path: Path, rate: int, audio: np.ndarray) -> None:
    encoded = np.rint(np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    wavfile.write(path, rate, encoded)


def _default_executor(command: Sequence[str], output: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    del output
    return subprocess.run(list(command), capture_output=True, text=True, timeout=timeout, check=False)


def _resolve_executable(value: str | Path) -> str:
    resolved = shutil.which(str(value))
    if resolved:
        return resolved
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    raise Stage3RenderError(f"FluidSynth executable not found: {value}")


def _publish(staging: Path, destination: Path, force: bool) -> None:
    backup: Path | None = None
    try:
        if destination.exists():
            if not force:
                raise Stage3RenderError(f"output directory already exists: {destination}")
            backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup:
            shutil.rmtree(backup)
    except Exception:
        if backup and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise


def render_with_packages(
    input_midi: str | Path,
    general_soundfont: str | Path,
    render_plan: str | Path,
    heartbeat_packages: Mapping[str, str | Path],
    output_dir: str | Path,
    *,
    fluidsynth: str | Path = "fluidsynth",
    sample_rate: int = 48_000,
    synth_gain: float = 0.5,
    timeout_seconds: int = 600,
    force: bool = False,
    executor: Executor | None = None,
) -> PackageRenderResult:
    """Render music and real heartbeat packages without changing MIDI timing."""
    if not 8_000 <= sample_rate <= 192_000:
        raise Stage3RenderError("sample_rate must be between 8000 and 192000")
    if not math.isfinite(synth_gain) or not 0 < synth_gain < 10:
        raise Stage3RenderError("synth_gain must be finite and between 0 and 10")
    if timeout_seconds < 1 or not heartbeat_packages:
        raise Stage3RenderError("timeout must be positive and at least one heartbeat package is required")
    midi_path = Path(input_midi).expanduser().resolve()
    sf2_path = Path(general_soundfont).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    plan = load_render_plan(render_plan)
    channel = plan["heartbeat_channel"] - 1
    inspection = inspect_complete_midi(
        midi_path, heartbeat_channel=channel, heartbeat_notes=tuple(plan["note_map"])
    )
    sf2_info = inspect_soundfont(sf2_path)
    source_hash = _sha256(midi_path)
    package_paths = {key: Path(value).expanduser().resolve() for key, value in heartbeat_packages.items()}
    if len(package_paths) != len(heartbeat_packages) or any(not key for key in package_paths):
        raise Stage3RenderError("heartbeat package IDs must be unique and non-empty")
    referenced_ids = set(plan["default_material_rule"]["package_ids"])
    for rule in plan["section_material_rules"]:
        referenced_ids.update(rule["package_ids"])
    missing = referenced_ids - set(package_paths)
    extra = set(package_paths) - referenced_ids
    if missing:
        raise Stage3RenderError(f"plan references unbound heartbeat packages: {sorted(missing)}")
    if extra:
        raise Stage3RenderError(f"bound heartbeat packages are unused by plan: {sorted(extra)}")
    banks = {key: _load_package(key, value) for key, value in package_paths.items()}
    parsed = _parse_and_filter_midi(midi_path, channel, plan["note_map"])
    if len(parsed.events) != inspection.heartbeat_note_on_count:
        raise Stage3RenderError("heartbeat event count changed during MIDI filtering")
    bar_ticks_value = parsed.ppq * 4 * parsed.numerator / parsed.denominator
    if not float(bar_ticks_value).is_integer():
        raise Stage3RenderError("time signature produces a non-integral MIDI bar length")
    bar_ticks = int(bar_ticks_value)
    for event in parsed.events:
        bar = event.tick // bar_ticks + 1
        _rule_id, rule = _rule_for_bar(plan, bar)
        if not any(banks[package_id].samples[event.event_type] for package_id in rule["package_ids"]):
            raise Stage3RenderError(
                f"no package selected for bar {bar} contains required {event.event_type} samples"
            )

    executable = str(fluidsynth) if executor else _resolve_executable(fluidsynth)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    runtime_midi, runtime_sf2 = staging / "music_only.mid", staging / "general.sf2"
    music_wav = staging / "music_stem.wav"
    heartbeat_wav = staging / "heartbeat_stem.wav"
    final_wav = staging / "final_mix.wav"
    csv_path = staging / "heartbeat_event_assignments.csv"
    manifest_path = staging / "stage3_render_manifest.json"
    runtime_midi.write_bytes(parsed.filtered_bytes)
    shutil.copyfile(sf2_path, runtime_sf2)
    command = [
        executable, "-ni", "-q", "-F", str(music_wav), "-T", "wav", "-O", "s16",
        "-r", str(sample_rate), "-g", format(synth_gain, ".6g"), str(runtime_sf2), str(runtime_midi),
    ]
    try:
        completed = (executor or _default_executor)(command, music_wav, timeout_seconds)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown renderer error").strip()
            raise Stage3RenderError(f"FluidSynth music render failed: {detail[:1000]}")
        renderer_log = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        if any(marker in renderer_log for marker in (
            "not a soundfont or midi file", "failed to load soundfont", "fopen() failed",
            "error occurred identifying it",
        )):
            raise Stage3RenderError("FluidSynth reported an asset load failure")
        music_rate, music = _read_wav(music_wav)
        if music_rate != sample_rate:
            raise Stage3RenderError("FluidSynth output sample rate does not match request")
        music_channels = 1 if music.ndim == 1 else music.shape[1]
        # _read_wav intentionally returns mono for package use, so recover stereo music here.
        raw_rate, raw_music = wavfile.read(music_wav)
        del raw_rate
        if np.issubdtype(raw_music.dtype, np.integer):
            music = raw_music.astype(np.float64) / float(1 << (8 * raw_music.dtype.itemsize - 1))
        else:
            music = raw_music.astype(np.float64)
        if music.ndim == 1:
            music = music[:, None]
        music_channels = music.shape[1]
        max_tail = max(
            len(clip.signal) * sample_rate / clip.sample_rate
            for bank in banks.values() for samples in bank.samples.values() for clip in samples
        )
        midi_end = int(math.ceil(_seconds_at_tick(parsed.end_tick, parsed.ppq, parsed.tempos) * sample_rate))
        length = max(len(music), midi_end + int(math.ceil(max_tail)) + 1)
        if len(music) < length:
            music = np.pad(music, ((0, length - len(music)), (0, 0)))
        mix = plan["mix"]
        music_gain_db = mix["music_gain_db"]
        music_scaled = music * 10 ** (music_gain_db / 20.0)
        adaptive = mix["mode"] == "perceptual_event_adaptive"
        heartbeat = np.zeros(length, dtype=np.float64)
        states: dict[str, dict[str, int | None]] = {}
        sample_indices: dict[tuple[str, str], int] = {}
        rows: list[dict[str, Any]] = []
        profile = plan["heartbeat_conditioning_profile"]
        conditioned_cache: dict[str, tuple[np.ndarray, int]] = {}
        previous_event_gain: dict[str, float] = {}
        duck_events: list[tuple[int, float]] = []
        event_gains: list[float] = []
        for event_index, event in enumerate(parsed.events):
            bar = event.tick // bar_ticks + 1
            rule_id, rule = _rule_for_bar(plan, bar)
            package_id = _select_package(rule_id, rule, bar, event.event_type, states)
            clips = banks[package_id].samples[event.event_type]
            if not clips:
                raise Stage3RenderError(
                    f"selected package {package_id} lacks {event.event_type} for event {event_index}"
                )
            key = (package_id, event.event_type)
            clip = clips[sample_indices.get(key, 0) % len(clips)]
            sample_indices[key] = sample_indices.get(key, 0) + 1
            if clip.sha256 not in conditioned_cache:
                conditioned, _audit = condition_heartbeat_audio(clip.signal, clip.sample_rate, profile=profile)
                if clip.sample_rate != sample_rate:
                    gcd = math.gcd(clip.sample_rate, sample_rate)
                    conditioned = resample_poly(conditioned, sample_rate // gcd, clip.sample_rate // gcd)
                offset = int(round(clip.alignment_offset_samples * sample_rate / clip.sample_rate))
                conditioned_cache[clip.sha256] = (np.asarray(conditioned, dtype=np.float64), offset)
            conditioned, offset = conditioned_cache[clip.sha256]
            event_sample = int(round(_seconds_at_tick(event.tick, parsed.ppq, parsed.tempos) * sample_rate))
            normalized_velocity = (event.velocity / 127.0) ** plan["velocity_gamma"]
            if adaptive:
                floor = mix["velocity_gain_floor"]
                velocity_gain = floor + (1.0 - floor) * normalized_velocity
            else:
                velocity_gain = normalized_velocity
            rule_gain = 10 ** (float(rule.get("gain_db", 0.0)) / 20.0)
            start = event_sample - offset
            event_signal = conditioned * velocity_gain * rule_gain
            event_gain_db = 0.0
            duck_db = 0.0
            music_level_db = None
            heartbeat_level_db = None
            predicted_balance_db = None
            if adaptive:
                active_left, active_right = _active_bounds(
                    event_signal, sample_rate, mix["event_window_ms"]
                )
                destination_left = max(0, start + active_left)
                destination_right = min(length, start + active_right)
                source_left = active_left + max(0, -(start + active_left))
                used = max(0, destination_right - destination_left)
                source_right = min(len(event_signal), source_left + used)
                used = max(0, source_right - source_left)
                destination_right = destination_left + used
                if used:
                    music_level_db = _perceptual_level_db(
                        music_scaled[destination_left:destination_right], sample_rate
                    )
                    heartbeat_level_db = _perceptual_level_db(
                        event_signal[source_left:source_right], sample_rate
                    )
                    required = (
                        music_level_db - heartbeat_level_db
                        + mix["target_heartbeat_over_music_db"]
                    )
                    event_gain_db = min(
                        mix["maximum_heartbeat_gain_db"],
                        max(mix["minimum_heartbeat_gain_db"], required),
                    )
                    prior = previous_event_gain.get(event.event_type)
                    if prior is not None:
                        step = mix["maximum_event_gain_step_db"]
                        event_gain_db = min(prior + step, max(prior - step, event_gain_db))
                    previous_event_gain[event.event_type] = event_gain_db
                    duck_db = min(
                        mix["maximum_music_duck_db"], max(0.0, required - event_gain_db)
                    )
                    predicted_balance_db = (
                        heartbeat_level_db + event_gain_db + duck_db - music_level_db
                    )
                event_gains.append(event_gain_db)
                duck_events.append((event_sample, duck_db))
                event_signal = event_signal * 10 ** (event_gain_db / 20.0)
            head, tail = _place_clip(heartbeat, start, event_signal)
            rows.append({
                "event_index": event_index, "tick": event.tick,
                "time_s": _seconds_at_tick(event.tick, parsed.ppq, parsed.tempos), "bar": bar,
                "event_type": event.event_type, "velocity": event.velocity, "pitch": event.pitch,
                "track_index": event.track_index, "rule_id": rule_id, "selection_mode": rule["mode"],
                "package_id": package_id, "source_sample": clip.relative_path,
                "source_sample_sha256": clip.sha256, "source_cycle_index": clip.cycle_index,
                "source_quality_score": clip.quality_score, "rule_gain_db": float(rule.get("gain_db", 0.0)),
                "velocity_gain": velocity_gain, "alignment_offset_output_samples": offset,
                "placement_start_sample": start, "head_truncated": head, "tail_truncated": tail,
                "perceptual_music_level_db": music_level_db,
                "perceptual_heartbeat_level_db": heartbeat_level_db,
                "adaptive_event_gain_db": event_gain_db,
                "adaptive_music_duck_db": duck_db,
                "predicted_heartbeat_over_music_db": predicted_balance_db,
            })
        heartbeat_stereo = np.repeat(heartbeat[:, None], music_channels, axis=1)
        if mix["mode"] == "manual":
            heartbeat_gain_db = mix["heartbeat_gain_db"]
            music_balanced = music_scaled
        elif mix["mode"] == "event_window_relative":
            half = max(1, int(round(mix["event_window_ms"] * sample_rate / 2000.0)))
            music_windows, heartbeat_windows = [], []
            for row in rows:
                center = int(round(row["time_s"] * sample_rate))
                left, right = max(0, center - half), min(length, center + half)
                music_windows.append(_rms(music_scaled[left:right]))
                heartbeat_windows.append(_rms(heartbeat_stereo[left:right]))
            music_local = float(np.median(music_windows))
            heartbeat_local = float(np.median(heartbeat_windows))
            required = _db(music_local / max(heartbeat_local, 1e-12)) + mix["heartbeat_over_music_db"]
            heartbeat_gain_db = min(required, mix["maximum_heartbeat_boost_db"])
            music_balanced = music_scaled
        else:
            heartbeat_gain_db = float(np.median(event_gains)) if event_gains else 0.0
            duck = _duck_envelope(
                length, duck_events, sample_rate, mix["duck_attack_ms"],
                mix["duck_hold_ms"], mix["duck_release_ms"],
            )
            music_balanced = music_scaled * duck[:, None]
        heartbeat_scaled = heartbeat_stereo * 10 ** (heartbeat_gain_db / 20.0)
        # Adaptive event gains are already baked into the heartbeat stem.
        if adaptive:
            heartbeat_scaled = heartbeat_stereo
        combined = music_balanced + heartbeat_scaled
        pre_peak = _true_peak(combined)
        ceiling = 10 ** (mix["true_peak_ceiling_dbtp"] / 20.0)
        loudness_before_mastering = _integrated_loudness_lufs(combined, sample_rate)
        if adaptive and math.isfinite(loudness_before_mastering):
            requested_master_db = mix["final_target_lufs"] - loudness_before_mastering
            peak_limited_db = _db(ceiling / max(pre_peak, 1e-12))
            master_gain_db = min(requested_master_db, peak_limited_db)
            protection_gain = 10 ** (master_gain_db / 20.0)
        else:
            protection_gain = min(1.0, ceiling / max(pre_peak, 1e-12))
            master_gain_db = _db(protection_gain)
        music_final = music_balanced * protection_gain
        heartbeat_final = heartbeat_scaled * protection_gain
        final = music_final + heartbeat_final
        _write_wav(final_wav, sample_rate, final)
        if mix["publish_stems"]:
            _write_wav(music_wav, sample_rate, music_final)
            _write_wav(heartbeat_wav, sample_rate, heartbeat_final)
        else:
            music_wav.unlink(missing_ok=True)
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        if _sha256(midi_path) != source_hash:
            raise Stage3RenderError("complete MIDI changed during Stage 3 rendering")
        manifest = {
            "schema_version": "2.0", "module_version": VERSION,
            "generated_at": datetime.now(UTC).isoformat(), "stage": "stage3",
            "operation": "complete_midi_multi_package_mix",
            "policy": {
                "complete_midi_is_timing_authority": True, "heartbeat_events_generated": False,
                "midi_timing_modified": False, "time_stretch": False, "pitch_shift": False,
                "full_song_plot_generated": False,
            },
            "input": {
                "complete_midi": str(midi_path), "complete_midi_sha256": source_hash,
                "general_soundfont": str(sf2_path), "general_soundfont_sha256": _sha256(sf2_path),
                "render_plan": str(plan["source_path"]), "render_plan_sha256": plan["source_sha256"],
                "heartbeat_packages": {
                    key: {"path": str(bank.path), "manifest_sha256": bank.manifest_sha256}
                    for key, bank in banks.items()
                },
            },
            "midi": {**inspection.as_dict(), "tempo_changes": [
                {"tick": tick, "microseconds_per_quarter": tempo} for tick, tempo in parsed.tempos
            ], "time_signature": [parsed.numerator, parsed.denominator]},
            "plan": {
                "heartbeat_channel": plan["heartbeat_channel"],
                "note_map": {str(key): value for key, value in plan["note_map"].items()},
                "heartbeat_conditioning_profile": profile, "velocity_gamma": plan["velocity_gamma"],
                "default_material_rule": plan["default_material_rule"],
                "section_material_rules": plan["section_material_rules"], "mix": mix,
            },
            "assignments": {
                "file": csv_path.name, "event_count": len(rows),
                "s1_count": sum(row["event_type"] == "S1" for row in rows),
                "s2_count": sum(row["event_type"] == "S2" for row in rows),
                "package_event_counts": {
                    key: sum(row["package_id"] == key for row in rows) for key in banks
                },
            },
            "mix": {
                "mode": mix["mode"], "requested_music_gain_db": music_gain_db,
                "effective_heartbeat_gain_db_before_peak_protection": heartbeat_gain_db,
                "adaptive_event_gain_db": ({
                    "minimum": min(event_gains), "median": float(np.median(event_gains)),
                    "maximum": max(event_gains),
                } if event_gains else None),
                "adaptive_music_duck_db": ({
                    "minimum": min((value for _sample, value in duck_events), default=0.0),
                    "median": float(np.median([value for _sample, value in duck_events])),
                    "maximum": max((value for _sample, value in duck_events), default=0.0),
                } if duck_events else None),
                "common_peak_protection_gain_db": master_gain_db,
                "final_music_gain_db": music_gain_db + master_gain_db,
                "final_heartbeat_gain_db": heartbeat_gain_db + master_gain_db,
                "pre_protection_true_peak_dbtp": _db(pre_peak),
                "post_protection_true_peak_dbtp": _db(_true_peak(final)),
                "ceiling_dbtp": mix["true_peak_ceiling_dbtp"],
                "integrated_lufs_before_mastering": loudness_before_mastering,
                "integrated_lufs_final": _integrated_loudness_lufs(final, sample_rate),
                "target_lufs": mix["final_target_lufs"] if adaptive else None,
                "music_rms_dbfs": _db(_rms(music_final)),
                "heartbeat_rms_dbfs": _db(_rms(heartbeat_final)),
            },
            "renderer": {
                "backend": "fluidsynth_cli_music_stem_plus_direct_heartbeat_samples",
                "executable": executable, "sample_rate": sample_rate, "synth_gain": synth_gain,
                "command": command, "stdout_tail": (completed.stdout or "")[-2000:],
                "stderr_tail": (completed.stderr or "")[-2000:],
            },
            "output": {
                "final_mix": final_wav.name, "final_mix_sha256": _sha256(final_wav),
                "assignments_csv": csv_path.name, "sample_rate": sample_rate,
                "channels": music_channels, "frames": len(final), "duration_s": len(final) / sample_rate,
                "published_stems": mix["publish_stems"], "plots": [],
            },
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        runtime_midi.unlink(missing_ok=True)
        runtime_sf2.unlink(missing_ok=True)
        _publish(staging, destination, force)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return PackageRenderResult(
        destination, destination / final_wav.name, destination / csv_path.name,
        destination / manifest_path.name, len(rows), len(final) / sample_rate,
        sample_rate, music_channels,
    )
