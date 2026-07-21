"""Render a complete heartbeat track after MIDI-GPT has finished.

The final drum track is the timing authority, the Stage 1 heartbeat plan is the
section/S1/S2 authority, and heartbeat_stage1's portable package is the timbre
authority. Real clips are moved and gain-scaled but never stretched or pitched.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import os
import shutil
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import numpy as np
from scipy.signal import resample_poly

from heart_extraction import audio_diagnostics as diagnostics
from heart_extraction import heart_sound_processor as extraction
from stage1_story_agent.models import (
    HeartbeatProcessingDelivery,
    HeartbeatProcessingSection,
)


VERSION = "1.0.0"
DEFAULT_TRIGGER_CHANNEL = 9  # zero-based General MIDI drum channel
DEFAULT_TRIGGER_NOTES = (36,)
EVENT_NOTES = {"S1": 36, "S2": 38}


class HeartbeatPostRenderError(ValueError):
    """Raised when cross-stage inputs cannot be rendered safely."""


@dataclass(frozen=True)
class TempoChange:
    tick: int
    microseconds_per_quarter: int


@dataclass(frozen=True)
class TimeSignatureChange:
    tick: int
    numerator: int
    denominator: int


@dataclass(frozen=True)
class MidiTrigger:
    tick: int
    velocity: int
    pitch: int
    channel: int
    track_index: int


@dataclass(frozen=True)
class ParsedTimingMidi:
    midi_format: int
    ppq: int
    track_count: int
    end_tick: int
    tempo_changes: tuple[TempoChange, ...]
    time_signatures: tuple[TimeSignatureChange, ...]
    triggers: tuple[MidiTrigger, ...]


@dataclass(frozen=True)
class SampleClip:
    event_type: str
    cycle_index: int
    path: Path
    sha256: str
    quality_score: float
    alignment_offset_samples: int
    sample_rate: int
    signal: np.ndarray


@dataclass(frozen=True)
class ScheduledEvent:
    event_index: int
    section_id: str
    bar: int
    trigger_tick: int
    event_tick: int
    event_time_s: float
    event_type: str
    velocity: int
    trigger_pitch: int
    trigger_track_index: int
    clip: SampleClip


@dataclass(frozen=True)
class RenderResult:
    output_dir: Path
    heartbeat_wav: Path
    heartbeat_midi: Path
    events_csv: Path
    manifest_json: Path
    event_count: int
    trigger_count: int
    duration_s: float

    def as_dict(self) -> dict[str, object]:
        return {
            "output_dir": str(self.output_dir),
            "heartbeat_wav": str(self.heartbeat_wav),
            "heartbeat_midi": str(self.heartbeat_midi),
            "events_csv": str(self.events_csv),
            "manifest_json": str(self.manifest_json),
            "event_count": self.event_count,
            "trigger_count": self.trigger_count,
            "duration_s": self.duration_s,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_vlq(data: bytes, position: int, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise HeartbeatPostRenderError(f"truncated MIDI VLQ in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise HeartbeatPostRenderError(f"MIDI VLQ exceeds four bytes in {context}")


def _deduplicate_tempos(
    values: list[tuple[int, int, int, int]],
) -> tuple[TempoChange, ...]:
    by_tick: dict[int, int] = {}
    for tick, track_index, event_index, tempo in sorted(values):
        del track_index, event_index
        by_tick[tick] = tempo
    if 0 not in by_tick:
        by_tick[0] = 500_000
    return tuple(TempoChange(tick, by_tick[tick]) for tick in sorted(by_tick))


def _deduplicate_time_signatures(
    values: list[tuple[int, int, int, int, int]],
) -> tuple[TimeSignatureChange, ...]:
    by_tick: dict[int, tuple[int, int]] = {}
    for tick, track_index, event_index, numerator, denominator in sorted(values):
        del track_index, event_index
        by_tick[tick] = (numerator, denominator)
    if 0 not in by_tick:
        by_tick[0] = (4, 4)
    return tuple(
        TimeSignatureChange(tick, by_tick[tick][0], by_tick[tick][1])
        for tick in sorted(by_tick)
    )


def parse_timing_midi(
    path: str | Path,
    *,
    trigger_channel: int = DEFAULT_TRIGGER_CHANNEL,
    trigger_notes: Iterable[int] = DEFAULT_TRIGGER_NOTES,
    track_index: int | None = None,
) -> ParsedTimingMidi:
    """Parse tempo/time-signature maps and selected drum note-on triggers."""

    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise HeartbeatPostRenderError(f"could not read final MIDI: {exc}") from exc
    if len(data) < 14 or data[:4] != b"MThd":
        raise HeartbeatPostRenderError("final MIDI is not a Standard MIDI File")
    header_length = struct.unpack_from(">I", data, 4)[0]
    if header_length < 6 or 8 + header_length > len(data):
        raise HeartbeatPostRenderError("invalid or truncated MIDI header")
    midi_format, declared_tracks, division = struct.unpack_from(">HHH", data, 8)
    if midi_format not in (0, 1):
        raise HeartbeatPostRenderError("only MIDI format 0 and 1 are supported")
    if division & 0x8000 or division == 0:
        raise HeartbeatPostRenderError("SMPTE or zero MIDI division is not supported")
    if not 0 <= trigger_channel <= 15:
        raise HeartbeatPostRenderError("trigger channel must be between 0 and 15")
    notes = set(trigger_notes)
    if not notes or any(not 0 <= note <= 127 for note in notes):
        raise HeartbeatPostRenderError("trigger notes must contain MIDI pitches 0..127")
    if track_index is not None and not 0 <= track_index < declared_tracks:
        raise HeartbeatPostRenderError("selected track index is outside the MIDI file")

    position = 8 + header_length
    tempos: list[tuple[int, int, int, int]] = []
    signatures: list[tuple[int, int, int, int, int]] = []
    triggers: list[MidiTrigger] = []
    end_tick = 0
    for raw_track in range(declared_tracks):
        context = f"track {raw_track}"
        if position + 8 > len(data) or data[position : position + 4] != b"MTrk":
            raise HeartbeatPostRenderError(f"missing MTrk chunk at {context}")
        size = struct.unpack_from(">I", data, position + 4)[0]
        start, end = position + 8, position + 8 + size
        if end > len(data):
            raise HeartbeatPostRenderError(f"truncated MTrk chunk at {context}")
        chunk = data[start:end]
        position = end
        cursor = 0
        absolute_tick = 0
        running_status: int | None = None
        event_index = 0
        while cursor < len(chunk):
            delta, cursor = _read_vlq(chunk, cursor, context)
            absolute_tick += delta
            end_tick = max(end_tick, absolute_tick)
            if cursor >= len(chunk):
                raise HeartbeatPostRenderError(f"missing event in {context}")
            lead = chunk[cursor]
            if lead >= 0x80:
                status = lead
                cursor += 1
                if status < 0xF0:
                    running_status = status
                else:
                    running_status = None
            elif running_status is not None:
                status = running_status
            else:
                raise HeartbeatPostRenderError(f"running status has no prior status in {context}")

            if status == 0xFF:
                if cursor >= len(chunk):
                    raise HeartbeatPostRenderError(f"truncated meta event in {context}")
                kind = chunk[cursor]
                cursor += 1
                payload_size, cursor = _read_vlq(chunk, cursor, context)
                payload = chunk[cursor : cursor + payload_size]
                if len(payload) != payload_size:
                    raise HeartbeatPostRenderError(f"truncated meta payload in {context}")
                cursor += payload_size
                if kind == 0x51:
                    if payload_size != 3:
                        raise HeartbeatPostRenderError("Set Tempo payload must be three bytes")
                    tempo = int.from_bytes(payload, "big")
                    if tempo <= 0:
                        raise HeartbeatPostRenderError("Set Tempo value must be positive")
                    tempos.append((absolute_tick, raw_track, event_index, tempo))
                elif kind == 0x58:
                    if payload_size < 2 or payload[1] > 7:
                        raise HeartbeatPostRenderError("invalid Time Signature payload")
                    signatures.append(
                        (absolute_tick, raw_track, event_index, payload[0], 1 << payload[1])
                    )
                event_index += 1
                if kind == 0x2F:
                    break
                continue
            if status in (0xF0, 0xF7):
                payload_size, cursor = _read_vlq(chunk, cursor, context)
                cursor += payload_size
                if cursor > len(chunk):
                    raise HeartbeatPostRenderError(f"truncated SysEx in {context}")
                event_index += 1
                continue
            if status >= 0xF0:
                raise HeartbeatPostRenderError(
                    f"unsupported system status 0x{status:02X} in {context}"
                )

            family, channel = status & 0xF0, status & 0x0F
            data_size = 1 if family in (0xC0, 0xD0) else 2
            if cursor + data_size > len(chunk):
                raise HeartbeatPostRenderError(f"truncated channel event in {context}")
            first_data = chunk[cursor]
            second_data = chunk[cursor + 1] if data_size == 2 else 0
            if first_data >= 0x80 or (data_size == 2 and second_data >= 0x80):
                raise HeartbeatPostRenderError(f"invalid channel data byte in {context}")
            cursor += data_size
            if (
                family == 0x90
                and second_data > 0
                and channel == trigger_channel
                and first_data in notes
                and (track_index is None or raw_track == track_index)
            ):
                triggers.append(
                    MidiTrigger(
                        tick=absolute_tick,
                        velocity=second_data,
                        pitch=first_data,
                        channel=channel,
                        track_index=raw_track,
                    )
                )
            event_index += 1

    if position != len(data):
        raise HeartbeatPostRenderError("unexpected data after declared MIDI tracks")
    return ParsedTimingMidi(
        midi_format=midi_format,
        ppq=division,
        track_count=declared_tracks,
        end_tick=end_tick,
        tempo_changes=_deduplicate_tempos(tempos),
        time_signatures=_deduplicate_time_signatures(signatures),
        triggers=tuple(sorted(triggers, key=lambda item: (item.tick, item.track_index, item.pitch))),
    )


class TempoMap:
    def __init__(self, ppq: int, changes: tuple[TempoChange, ...]) -> None:
        self.ppq = ppq
        self.changes = changes
        self.ticks = [item.tick for item in changes]
        self.seconds: list[float] = [0.0]
        for previous, current in zip(changes, changes[1:]):
            elapsed = (
                (current.tick - previous.tick)
                * previous.microseconds_per_quarter
                / (ppq * 1_000_000.0)
            )
            self.seconds.append(self.seconds[-1] + elapsed)

    def seconds_at_tick(self, tick: int) -> float:
        if tick < 0:
            raise HeartbeatPostRenderError("MIDI tick must not be negative")
        index = bisect.bisect_right(self.ticks, tick) - 1
        change = self.changes[index]
        return self.seconds[index] + (
            (tick - change.tick)
            * change.microseconds_per_quarter
            / (self.ppq * 1_000_000.0)
        )

    def tick_at_seconds(self, seconds: float) -> int:
        if seconds < 0:
            raise HeartbeatPostRenderError("MIDI time must not be negative")
        index = bisect.bisect_right(self.seconds, seconds) - 1
        change = self.changes[index]
        delta = (
            (seconds - self.seconds[index])
            * self.ppq
            * 1_000_000.0
            / change.microseconds_per_quarter
        )
        return change.tick + int(round(delta))

    def bpm_at_tick(self, tick: int) -> float:
        index = bisect.bisect_right(self.ticks, tick) - 1
        return 60_000_000.0 / self.changes[index].microseconds_per_quarter


def _load_plan(path: Path) -> HeartbeatProcessingDelivery:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return HeartbeatProcessingDelivery.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HeartbeatPostRenderError(f"invalid heartbeat processing plan: {exc}") from exc


def _meter_and_grid(
    plan: HeartbeatProcessingDelivery, midi: ParsedTimingMidi
) -> tuple[int, int, int, int]:
    sections = plan.sections
    if not sections:
        raise HeartbeatPostRenderError("heartbeat plan has no sections")
    expected_start = 1
    meters: set[str] = set()
    for section in sections:
        if section.bar_start != expected_start:
            raise HeartbeatPostRenderError("heartbeat plan sections must be gapless from bar 1")
        expected_start = section.bar_end + 1
        meters.add(section.source_drum_pattern.time_signature)
    if len(meters) != 1:
        raise HeartbeatPostRenderError("all heartbeat plan sections must use one meter")
    numerator_text, denominator_text = next(iter(meters)).split("/", 1)
    numerator, denominator = int(numerator_text), int(denominator_text)
    raw_beat_ticks = midi.ppq * 4 / denominator
    if not raw_beat_ticks.is_integer():
        raise HeartbeatPostRenderError("MIDI PPQ cannot exactly represent the planned beat unit")
    beat_ticks = int(raw_beat_ticks)
    bar_ticks = beat_ticks * numerator
    for change in midi.time_signatures:
        if change.tick < (expected_start - 1) * bar_ticks and (
            change.numerator,
            change.denominator,
        ) != (numerator, denominator):
            raise HeartbeatPostRenderError(
                "final MIDI time signature does not match heartbeat processing plan"
            )
        if change.tick != 0 and change.tick < (expected_start - 1) * bar_ticks:
            raise HeartbeatPostRenderError("time-signature changes inside the planned song are unsupported")
    return numerator, denominator, beat_ticks, bar_ticks


def _expected_triggers(
    plan: HeartbeatProcessingDelivery,
    beat_ticks: int,
    bar_ticks: int,
) -> list[tuple[int, HeartbeatProcessingSection, int]]:
    expected: list[tuple[int, HeartbeatProcessingSection, int]] = []
    for section in plan.sections:
        for bar in range(section.bar_start, section.bar_end + 1):
            for beat in section.trigger_beats:
                tick = (bar - 1) * bar_ticks + int(round((beat - 1.0) * beat_ticks))
                expected.append((tick, section, bar))
    return sorted(expected, key=lambda item: item[0])


def _match_and_validate_triggers(
    plan: HeartbeatProcessingDelivery,
    midi: ParsedTimingMidi,
    tempo_map: TempoMap,
    beat_ticks: int,
    bar_ticks: int,
) -> list[tuple[MidiTrigger, HeartbeatProcessingSection, int]]:
    expected = _expected_triggers(plan, beat_ticks, bar_ticks)
    actual = list(midi.triggers)
    if len(actual) != len(expected):
        raise HeartbeatPostRenderError(
            f"final drum trigger count {len(actual)} does not match plan {len(expected)}"
        )
    tolerance = max(1, int(round(beat_ticks / 96)))
    matched: list[tuple[MidiTrigger, HeartbeatProcessingSection, int]] = []
    for trigger, (expected_tick, section, bar) in zip(actual, expected, strict=True):
        if abs(trigger.tick - expected_tick) > tolerance:
            raise HeartbeatPostRenderError(
                f"drum trigger at tick {trigger.tick} does not match planned tick "
                f"{expected_tick} for {section.section_id} bar {bar}"
            )
        matched.append((trigger, section, bar))

    for index, section in enumerate(plan.sections):
        section_start = (section.bar_start - 1) * bar_ticks
        section_end = section.bar_end * bar_ticks
        actual_bpm = tempo_map.bpm_at_tick(section_start)
        if abs(actual_bpm - section.tempo_bpm) > 0.25:
            raise HeartbeatPostRenderError(
                f"final MIDI BPM {actual_bpm:.3f} at {section.section_id} differs from "
                f"planned {section.tempo_bpm:.3f}"
            )
        internal_changes = [
            change.tick
            for change in midi.tempo_changes
            if section_start < change.tick < section_end
        ]
        if internal_changes:
            raise HeartbeatPostRenderError(
                f"tempo changes inside {section.section_id} are not allowed: {internal_changes}"
            )
        if index and midi.tempo_changes:
            # A boundary change is optional when adjacent sections have equal BPM.
            previous = plan.sections[index - 1]
            if abs(previous.tempo_bpm - section.tempo_bpm) > 0.25:
                if not any(change.tick == section_start for change in midi.tempo_changes):
                    raise HeartbeatPostRenderError(
                        f"missing tempo change at the start of {section.section_id}"
                    )
    return matched


def _safe_package_path(package: Path, relative: str) -> Path:
    root = package.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise HeartbeatPostRenderError("heartbeat package contains an unsafe sample path")
    return candidate


def _load_package(package: Path) -> tuple[dict[str, list[SampleClip]], float, dict[str, Any]]:
    package = package.resolve()
    manifest_path = package / "heartbeat_manifest.json"
    events_path = package / "heartbeat_events.csv"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HeartbeatPostRenderError(f"invalid heartbeat package manifest: {exc}") from exc
    if manifest.get("schema_version") != "1.0":
        raise HeartbeatPostRenderError("heartbeat package schema_version must be 1.0")
    raw_samples = manifest.get("event_samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise HeartbeatPostRenderError("heartbeat package has no event samples")
    bank: dict[str, list[SampleClip]] = {"S1": [], "S2": []}
    sample_rate: int | None = None
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict) or item.get("event_type") not in bank:
            raise HeartbeatPostRenderError(f"invalid event_samples[{index}]")
        path = _safe_package_path(package, str(item.get("file", "")))
        if not path.is_file():
            raise HeartbeatPostRenderError(f"heartbeat sample is missing: {path}")
        expected_hash = str(item.get("sha256", ""))
        actual_hash = _sha256_file(path)
        if actual_hash != expected_hash:
            raise HeartbeatPostRenderError(f"heartbeat sample hash mismatch: {path.name}")
        audio = diagnostics.load_wav(path)
        if sample_rate is None:
            sample_rate = audio.sample_rate
        elif audio.sample_rate != sample_rate:
            raise HeartbeatPostRenderError("heartbeat package samples use different sample rates")
        event_type = str(item["event_type"])
        bank[event_type].append(
            SampleClip(
                event_type=event_type,
                cycle_index=int(item["cycle_index"]),
                path=path,
                sha256=actual_hash,
                quality_score=float(item["quality_score"]),
                alignment_offset_samples=int(item["alignment_offset_samples"]),
                sample_rate=audio.sample_rate,
                signal=audio.signal,
            )
        )
    if not bank["S1"] or not bank["S2"]:
        raise HeartbeatPostRenderError("heartbeat package must contain both S1 and S2 samples")
    for values in bank.values():
        values.sort(key=lambda clip: (clip.cycle_index, -clip.quality_score))

    try:
        with events_path.open("r", encoding="utf-8", newline="") as handle:
            rows = sorted(csv.DictReader(handle), key=lambda row: float(row["time_s"]))
    except (OSError, ValueError, KeyError) as exc:
        raise HeartbeatPostRenderError(f"invalid heartbeat package events CSV: {exc}") from exc
    s2_offset_s: float | None = None
    for position, row in enumerate(rows):
        if row.get("event_type") != "S1":
            continue
        s1_time = float(row["time_s"])
        next_s2 = next(
            (item for item in rows[position + 1 :] if item.get("event_type") == "S2"),
            None,
        )
        if next_s2 is not None:
            s2_offset_s = float(next_s2["time_s"]) - s1_time
            break
    if s2_offset_s is None or not 0.08 <= s2_offset_s <= 2.0:
        raise HeartbeatPostRenderError("heartbeat package has no usable S1-to-S2 offset")
    return bank, s2_offset_s, manifest


def _schedule_events(
    matched: list[tuple[MidiTrigger, HeartbeatProcessingSection, int]],
    bank: dict[str, list[SampleClip]],
    s2_offset_s: float,
    tempo_map: TempoMap,
) -> list[ScheduledEvent]:
    pairs_by_cycle = {
        cycle: {"S1": s1, "S2": s2}
        for cycle in sorted(
            {item.cycle_index for item in bank["S1"]}
            & {item.cycle_index for item in bank["S2"]}
        )
        for s1 in bank["S1"]
        for s2 in bank["S2"]
        if s1.cycle_index == cycle and s2.cycle_index == cycle
    }
    pairs = list(pairs_by_cycle.values())
    if not pairs:
        raise HeartbeatPostRenderError("heartbeat package has no complete S1/S2 sample pair")
    scheduled: list[ScheduledEvent] = []
    single_s1_index = 0
    pair_index = 0
    for trigger, section, bar in matched:
        trigger_time = tempo_map.seconds_at_tick(trigger.tick)
        if section.heart_sounds == ["S1", "S2"]:
            selected = pairs[pair_index % len(pairs)]
            pair_index += 1
            specifications = (("S1", trigger_time), ("S2", trigger_time + s2_offset_s))
        elif section.heart_sounds == ["S1"]:
            selected = {"S1": bank["S1"][single_s1_index % len(bank["S1"])]}
            single_s1_index += 1
            specifications = (("S1", trigger_time),)
        else:
            raise HeartbeatPostRenderError(
                f"unsupported heart_sounds in {section.section_id}: {section.heart_sounds}"
            )
        for event_type, event_time in specifications:
            scheduled.append(
                ScheduledEvent(
                    event_index=len(scheduled),
                    section_id=section.section_id,
                    bar=bar,
                    trigger_tick=trigger.tick,
                    event_tick=tempo_map.tick_at_seconds(event_time),
                    event_time_s=event_time,
                    event_type=event_type,
                    velocity=trigger.velocity,
                    trigger_pitch=trigger.pitch,
                    trigger_track_index=trigger.track_index,
                    clip=selected[event_type],
                )
            )
    return scheduled


def _place_clip(output: np.ndarray, start: int, clip: np.ndarray) -> tuple[bool, bool]:
    source_start = max(0, -start)
    destination_start = max(0, start)
    used = min(len(clip) - source_start, len(output) - destination_start)
    if used <= 0:
        return source_start > 0, True
    placed = clip[source_start : source_start + used].copy()
    head_truncated = source_start > 0
    tail_truncated = source_start + used < len(clip)
    if head_truncated and used > 2:
        fade = min(used // 3, 256)
        placed[:fade] *= np.sin(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
        placed[0] = 0.0
    if tail_truncated and used > 2:
        fade = min(used // 3, 256)
        placed[-fade:] *= np.cos(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
        placed[-1] = 0.0
    output[destination_start : destination_start + used] += placed
    return head_truncated, tail_truncated


def _normalise_true_peak(signal: np.ndarray, target_dbfs: float = -1.0) -> tuple[np.ndarray, float]:
    true_peak = float(np.max(np.abs(resample_poly(signal, 4, 1))))
    if true_peak <= 1e-12:
        raise HeartbeatPostRenderError("rendered heartbeat track is silent")
    target = 10.0 ** (target_dbfs / 20.0)
    gain = target / true_peak
    return signal * gain, 20.0 * math.log10(max(gain, 1e-12))


def _vlq(value: int) -> bytes:
    if value < 0:
        raise HeartbeatPostRenderError("negative MIDI delta tick")
    buffer = value & 0x7F
    output = bytearray([buffer])
    value >>= 7
    while value:
        output.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(output)


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes((0xFF, kind)) + _vlq(len(payload)) + payload


def _midi_track(events: list[tuple[int, int, bytes]]) -> bytes:
    payload = bytearray()
    previous = 0
    for tick, order, event in sorted(events, key=lambda item: (item[0], item[1])):
        del order
        payload.extend(_vlq(tick - previous))
        payload.extend(event)
        previous = tick
    return b"MTrk" + struct.pack(">I", len(payload)) + payload


def _heartbeat_midi_bytes(
    midi: ParsedTimingMidi,
    scheduled: list[ScheduledEvent],
    end_tick: int,
) -> bytes:
    tempo_map = TempoMap(midi.ppq, midi.tempo_changes)
    meta_events: list[tuple[int, int, bytes]] = [(0, 0, _meta(0x03, b"LegaSynth timing"))]
    for item in midi.tempo_changes:
        meta_events.append(
            (item.tick, 1, _meta(0x51, item.microseconds_per_quarter.to_bytes(3, "big")))
        )
    for item in midi.time_signatures:
        meta_events.append(
            (
                item.tick,
                2,
                _meta(0x58, bytes((item.numerator, int(math.log2(item.denominator)), 24, 8))),
            )
        )
    meta_events.append((end_tick, 99, _meta(0x2F, b"")))
    note_events: list[tuple[int, int, bytes]] = [
        (0, 0, _meta(0x03, b"LegaSynth rendered heartbeat")),
        (0, 1, bytes((0xC9, 0))),
    ]
    for event in scheduled:
        note = EVENT_NOTES[event.event_type]
        clip_end_s = event.event_time_s + len(event.clip.signal) / event.clip.sample_rate
        duration = max(1, tempo_map.tick_at_seconds(clip_end_s) - event.event_tick)
        note_events.append((event.event_tick, 20, bytes((0x99, note, event.velocity))))
        note_events.append((event.event_tick + duration, 10, bytes((0x89, note, 0))))
        end_tick = max(end_tick, event.event_tick + duration)
    note_events.append((end_tick, 99, _meta(0x2F, b"")))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 2, midi.ppq)
    return header + _midi_track(meta_events) + _midi_track(note_events)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _publish_directory(staging: Path, target: Path, *, force: bool) -> None:
    backup = target.parent / f".{target.name}.backup-{uuid4().hex}"
    try:
        if target.exists():
            if not force:
                raise HeartbeatPostRenderError(
                    f"output directory already exists: {target}; use --force to replace it"
                )
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and target.exists():
            shutil.rmtree(backup, ignore_errors=True)


def render_heartbeat_track(
    final_midi: str | Path,
    heartbeat_plan: str | Path,
    heartbeat_package: str | Path,
    output_dir: str | Path,
    *,
    trigger_channel: int = DEFAULT_TRIGGER_CHANNEL,
    trigger_notes: Iterable[int] = DEFAULT_TRIGGER_NOTES,
    track_index: int | None = None,
    force: bool = False,
    render_diagnostics: bool = True,
) -> RenderResult:
    """Render and atomically publish a complete post-MIDI-GPT heartbeat track."""

    midi_path = Path(final_midi).expanduser().resolve()
    plan_path = Path(heartbeat_plan).expanduser().resolve()
    package_path = Path(heartbeat_package).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    if not midi_path.is_file():
        raise HeartbeatPostRenderError(f"final MIDI not found: {midi_path}")
    if not plan_path.is_file():
        raise HeartbeatPostRenderError(f"heartbeat plan not found: {plan_path}")
    if not package_path.is_dir():
        raise HeartbeatPostRenderError(f"heartbeat package not found: {package_path}")
    if target.exists() and not force:
        raise HeartbeatPostRenderError(
            f"output directory already exists: {target}; use --force to replace it"
        )
    if target.exists() and not target.is_dir():
        raise HeartbeatPostRenderError(f"output path is not a directory: {target}")
    if any(
        input_path.is_relative_to(target)
        for input_path in (midi_path, plan_path, package_path)
    ):
        raise HeartbeatPostRenderError("output directory must not contain any input")

    trigger_notes = tuple(trigger_notes)
    plan = _load_plan(plan_path)
    midi = parse_timing_midi(
        midi_path,
        trigger_channel=trigger_channel,
        trigger_notes=trigger_notes,
        track_index=track_index,
    )
    tempo_map = TempoMap(midi.ppq, midi.tempo_changes)
    _, _, beat_ticks, bar_ticks = _meter_and_grid(plan, midi)
    matched = _match_and_validate_triggers(plan, midi, tempo_map, beat_ticks, bar_ticks)
    bank, s2_offset_s, package_manifest = _load_package(package_path)
    scheduled = _schedule_events(matched, bank, s2_offset_s, tempo_map)
    sample_rate = bank["S1"][0].sample_rate
    planned_end_tick = plan.sections[-1].bar_end * bar_ticks
    base_duration_s = tempo_map.seconds_at_tick(max(planned_end_tick, midi.end_tick))
    max_tail_s = max(len(item.signal) / item.sample_rate for values in bank.values() for item in values)
    duration_s = base_duration_s + max_tail_s
    output = np.zeros(max(1, int(math.ceil(duration_s * sample_rate))), dtype=np.float64)
    rows: list[dict[str, object]] = []
    for event in scheduled:
        gain = event.velocity / 100.0
        clip = event.clip.signal * gain
        peak_sample = int(round(event.event_time_s * sample_rate))
        placement_start = peak_sample - event.clip.alignment_offset_samples
        head_truncated, tail_truncated = _place_clip(output, placement_start, clip)
        rows.append(
            {
                "event_index": event.event_index,
                "section_id": event.section_id,
                "bar": event.bar,
                "event_type": event.event_type,
                "trigger_tick": event.trigger_tick,
                "event_tick": event.event_tick,
                "event_time_s": event.event_time_s,
                "velocity": event.velocity,
                "linear_velocity_gain": gain,
                "trigger_pitch": event.trigger_pitch,
                "trigger_track_index": event.trigger_track_index,
                "source_cycle_index": event.clip.cycle_index,
                "source_sample": str(event.clip.path.relative_to(package_path)).replace("\\", "/"),
                "source_sample_sha256": event.clip.sha256,
                "source_quality_score": event.clip.quality_score,
                "alignment_offset_samples": event.clip.alignment_offset_samples,
                "placement_start_sample": placement_start,
                "head_truncated": head_truncated,
                "tail_truncated": tail_truncated,
            }
        )
    rendered, normalisation_gain_db = _normalise_true_peak(output)

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{uuid4().hex}"
    staging.mkdir()
    try:
        wav_path = staging / "heartbeat_track.wav"
        extraction.write_wav(wav_path, sample_rate, rendered)
        midi_output = staging / "heartbeat_track.mid"
        midi_output.write_bytes(
            _heartbeat_midi_bytes(midi, scheduled, max(planned_end_tick, midi.end_tick))
        )
        csv_path = staging / "heartbeat_events.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        diagnostics_files: dict[str, str] = {}
        if render_diagnostics:
            png, svg = diagnostics.render_time_energy_pair(
                staging / "heartbeat_track_time_energy",
                f"Post-MIDI-GPT heartbeat | {plan.story_id}",
                sample_rate,
                rendered,
                [(event.event_type, event.event_time_s) for event in scheduled],
                metadata={
                    "Story": plan.story_id,
                    "Trigger source": "final MIDI drum track",
                    "S1/S2 source": "heartbeat_stage1 heartbeat_package",
                    "Policy": "no time stretch or pitch shift",
                },
            )
            diagnostics_files = {
                "time_energy_png": png.name,
                "time_energy_svg": svg.name,
            }
        manifest = {
            "schema_version": "1.0",
            "stage": "LegaSynth post-MIDI-GPT heartbeat rendering",
            "builder": {"name": Path(__file__).name, "version": VERSION},
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "story_id": plan.story_id,
            "status": "PASS",
            "policy": {
                "timing_authority": "final_midi_selected_drum_triggers",
                "section_authority": "heartbeat_processing_plan.json",
                "timbre_authority": "heartbeat_package",
                "closed_trigger_validation": True,
                "time_stretch": False,
                "pitch_shift": False,
                "velocity_gain": "linear velocity / 100",
                "final_true_peak_target_dbfs": -1.0,
                "normalisation_gain_db": normalisation_gain_db,
            },
            "inputs": {
                "final_midi": str(midi_path),
                "final_midi_sha256": _sha256_file(midi_path),
                "heartbeat_processing_plan": str(plan_path),
                "heartbeat_processing_plan_sha256": _sha256_file(plan_path),
                "heartbeat_package": str(package_path),
                "heartbeat_package_manifest_sha256": _sha256_file(
                    package_path / "heartbeat_manifest.json"
                ),
                "heartbeat_package_builder": package_manifest.get("builder"),
            },
            "midi": {
                "format": midi.midi_format,
                "ppq": midi.ppq,
                "track_count": midi.track_count,
                "selected_track_index": track_index,
                "selected_channel_zero_based": trigger_channel,
                "selected_notes": sorted(set(trigger_notes)),
                "tempo_changes": [item.__dict__ for item in midi.tempo_changes],
                "time_signatures": [item.__dict__ for item in midi.time_signatures],
            },
            "render": {
                "sample_rate": sample_rate,
                "duration_s": len(rendered) / sample_rate,
                "trigger_count": len(matched),
                "event_count": len(scheduled),
                "s1_count": sum(item.event_type == "S1" for item in scheduled),
                "s2_count": sum(item.event_type == "S2" for item in scheduled),
                "s1_to_s2_offset_s": s2_offset_s,
            },
            "outputs": {
                "heartbeat_wav": wav_path.name,
                "heartbeat_wav_sha256": _sha256_file(wav_path),
                "heartbeat_midi": midi_output.name,
                "heartbeat_midi_sha256": _sha256_file(midi_output),
                "events_csv": csv_path.name,
                "events_csv_sha256": _sha256_file(csv_path),
                **diagnostics_files,
            },
        }
        manifest_path = staging / "heartbeat_render_manifest.json"
        _write_json(manifest_path, manifest)
        _publish_directory(staging, target, force=force)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return RenderResult(
        output_dir=target,
        heartbeat_wav=target / "heartbeat_track.wav",
        heartbeat_midi=target / "heartbeat_track.mid",
        events_csv=target / "heartbeat_events.csv",
        manifest_json=target / "heartbeat_render_manifest.json",
        event_count=len(scheduled),
        trigger_count=len(matched),
        duration_s=len(rendered) / sample_rate,
    )
