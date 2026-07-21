"""Trim or pad a Standard MIDI File to an exact musical bar count."""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path


class BarNormalizationError(ValueError):
    """Raised when a MIDI file cannot be normalized safely."""


@dataclass(frozen=True)
class BarNormalizationResult:
    input_midi: Path
    output_midi: Path
    midi_format: int
    track_count: int
    ticks_per_beat: int
    time_signature: str
    target_bars: int
    target_end_tick: int
    input_end_tick: int
    events_trimmed: int
    note_offs_inserted: int
    trimmed_to_target: bool
    input_sha256: str
    output_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "input_midi": str(self.input_midi.resolve()),
            "output_midi": str(self.output_midi.resolve()),
            "midi_format": self.midi_format,
            "track_count": self.track_count,
            "ticks_per_beat": self.ticks_per_beat,
            "time_signature": self.time_signature,
            "target_bars": self.target_bars,
            "target_end_tick": self.target_end_tick,
            "input_end_tick": self.input_end_tick,
            "events_trimmed": self.events_trimmed,
            "note_offs_inserted": self.note_offs_inserted,
            "short_input_policy": "error",
            "trimmed_to_target": self.trimmed_to_target,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
        }


@dataclass(frozen=True)
class _Event:
    tick: int
    encoded: bytes
    status: int
    data: bytes
    meta_type: int | None = None


def _read_vlq(data: bytes, position: int, *, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise BarNormalizationError(f"truncated variable-length value in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise BarNormalizationError(f"variable-length value exceeds four bytes in {context}")


def _write_vlq(value: int) -> bytes:
    if not 0 <= value <= 0x0FFFFFFF:
        raise BarNormalizationError(f"delta time is outside MIDI VLQ range: {value}")
    buffer = [value & 0x7F]
    value >>= 7
    while value:
        buffer.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buffer))


def _parse_track(data: bytes, track_index: int) -> list[_Event]:
    position = 0
    tick = 0
    running_status: int | None = None
    events: list[_Event] = []
    context = f"track {track_index}"
    while position < len(data):
        delta, position = _read_vlq(data, position, context=context)
        tick += delta
        if position >= len(data):
            raise BarNormalizationError(f"missing event after delta time in {context}")
        lead = data[position]
        if lead >= 0x80:
            status = lead
            position += 1
            if 0x80 <= status <= 0xEF:
                running_status = status
            elif status not in (0xF0, 0xF7, 0xFF):
                raise BarNormalizationError(
                    f"unsupported status byte 0x{status:02X} in {context}"
                )
        else:
            if running_status is None:
                raise BarNormalizationError(f"running status has no prior status in {context}")
            status = running_status

        if 0x80 <= status <= 0xEF:
            kind = status & 0xF0
            length = 1 if kind in (0xC0, 0xD0) else 2
            end = position + length
            if end > len(data) or any(byte >= 0x80 for byte in data[position:end]):
                raise BarNormalizationError(f"invalid channel event in {context}")
            payload = data[position:end]
            position = end
            events.append(_Event(tick, bytes([status]) + payload, status, payload))
            continue

        if status in (0xF0, 0xF7):
            length, payload_start = _read_vlq(data, position, context=context)
            payload_end = payload_start + length
            if payload_end > len(data):
                raise BarNormalizationError(f"truncated SysEx payload in {context}")
            payload = data[payload_start:payload_end]
            position = payload_end
            events.append(
                _Event(tick, bytes([status]) + _write_vlq(length) + payload, status, payload)
            )
            continue

        if position >= len(data):
            raise BarNormalizationError(f"missing meta-event type in {context}")
        meta_type = data[position]
        position += 1
        length, payload_start = _read_vlq(data, position, context=context)
        payload_end = payload_start + length
        if payload_end > len(data):
            raise BarNormalizationError(f"truncated meta-event payload in {context}")
        payload = data[payload_start:payload_end]
        position = payload_end
        events.append(
            _Event(
                tick,
                b"\xFF" + bytes([meta_type]) + _write_vlq(length) + payload,
                status,
                payload,
                meta_type,
            )
        )
    return events


def _update_active(active: dict[tuple[int, int], int], event: _Event) -> None:
    if not 0x80 <= event.status <= 0xEF or len(event.data) != 2:
        return
    kind = event.status & 0xF0
    if kind not in (0x80, 0x90):
        return
    key = (event.status & 0x0F, event.data[0])
    if kind == 0x90 and event.data[1] > 0:
        active[key] = active.get(key, 0) + 1
    elif active.get(key, 0) > 1:
        active[key] -= 1
    else:
        active.pop(key, None)


def _normalize_track(
    data: bytes, track_index: int, target_tick: int
) -> tuple[bytes, int, int, int]:
    events = _parse_track(data, track_index)
    input_end = max((event.tick for event in events), default=0)
    retained: list[_Event] = []
    active: dict[tuple[int, int], int] = {}
    trimmed = 0
    for event in events:
        if event.meta_type == 0x2F:
            continue
        is_note_on = (
            0x80 <= event.status <= 0xEF
            and (event.status & 0xF0) == 0x90
            and len(event.data) == 2
            and event.data[1] > 0
        )
        if event.tick > target_tick or (event.tick == target_tick and is_note_on):
            trimmed += 1
            continue
        retained.append(event)
        _update_active(active, event)

    note_offs = 0
    for (channel, pitch), count in sorted(active.items()):
        for _ in range(count):
            status = 0x80 | channel
            retained.append(_Event(target_tick, bytes([status, pitch, 0]), status, bytes([pitch, 0])))
            note_offs += 1

    output = bytearray()
    last_tick = 0
    for event in retained:
        if event.tick < last_tick:
            raise BarNormalizationError("MIDI events are not in chronological order")
        output.extend(_write_vlq(event.tick - last_tick))
        output.extend(event.encoded)
        last_tick = event.tick
    output.extend(_write_vlq(target_tick - last_tick))
    output.extend(b"\xFF\x2F\x00")
    return bytes(output), input_end, trimmed, note_offs


def _target_tick(target_bars: int, time_signature: str, ticks_per_beat: int) -> int:
    if target_bars < 1:
        raise BarNormalizationError("target_bars must be positive")
    try:
        numerator_text, denominator_text = time_signature.split("/", 1)
        numerator, denominator = int(numerator_text), int(denominator_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BarNormalizationError(f"invalid time signature: {time_signature!r}") from exc
    if numerator < 1 or denominator < 1 or denominator & (denominator - 1):
        raise BarNormalizationError("time-signature denominator must be a positive power of two")
    numerator_ticks = target_bars * numerator * 4 * ticks_per_beat
    if numerator_ticks % denominator:
        raise BarNormalizationError("target bar boundary is not representable in integer MIDI ticks")
    return numerator_ticks // denominator


def _parse_and_normalize(
    source: bytes, target_bars: int, time_signature: str
) -> tuple[bytes, int, int, int, int, int, int, int, bool]:
    if len(source) < 14 or source[:4] != b"MThd":
        raise BarNormalizationError("input is not a Standard MIDI File")
    header_length = struct.unpack(">I", source[4:8])[0]
    if header_length < 6 or 8 + header_length > len(source):
        raise BarNormalizationError("invalid or truncated MIDI header")
    midi_format, declared_tracks, division = struct.unpack(">HHH", source[8:14])
    if midi_format not in (0, 1) or declared_tracks < 1:
        raise BarNormalizationError("only valid MIDI format 0 and format 1 files are supported")
    if division == 0 or division & 0x8000:
        raise BarNormalizationError("only non-zero ticks-per-beat division is supported")
    target_tick = _target_tick(target_bars, time_signature, division)

    position = 8 + header_length
    output_chunks: list[tuple[bytes, bytes]] = []
    track_count = 0
    input_end = 0
    trimmed = 0
    note_offs = 0
    while position < len(source):
        if position + 8 > len(source):
            raise BarNormalizationError("truncated MIDI chunk header")
        kind = source[position : position + 4]
        length = struct.unpack(">I", source[position + 4 : position + 8])[0]
        payload_start = position + 8
        payload_end = payload_start + length
        if payload_end > len(source):
            raise BarNormalizationError("truncated MIDI chunk payload")
        payload = source[payload_start:payload_end]
        position = payload_end
        if kind == b"MTrk":
            normalized, track_end, track_trimmed, track_note_offs = _normalize_track(
                payload, track_count, target_tick
            )
            output_chunks.append((kind, normalized))
            input_end = max(input_end, track_end)
            trimmed += track_trimmed
            note_offs += track_note_offs
            track_count += 1
        else:
            output_chunks.append((kind, payload))
    if track_count != declared_tracks:
        raise BarNormalizationError(
            f"MIDI header declares {declared_tracks} tracks but contains {track_count}"
        )

    output = bytearray(source[: 8 + header_length])
    for kind, payload in output_chunks:
        output.extend(kind)
        output.extend(struct.pack(">I", len(payload)))
        output.extend(payload)
    return (
        bytes(output), midi_format, track_count, division, target_tick,
        input_end, trimmed, note_offs, input_end > target_tick,
    )


def normalize_midi_bars(
    input_midi: Path,
    output_midi: Path,
    target_bars: int,
    time_signature: str,
    *,
    force: bool = False,
) -> BarNormalizationResult:
    input_path = Path(input_midi)
    output_path = Path(output_midi)
    try:
        if input_path.resolve() == output_path.resolve():
            raise BarNormalizationError("input and output MIDI paths must differ")
        source = input_path.read_bytes()
    except BarNormalizationError:
        raise
    except OSError as exc:
        raise BarNormalizationError(f"could not read input MIDI: {exc}") from exc
    (
        normalized, midi_format, track_count, division, target_tick,
        input_end, trimmed, note_offs, was_longer,
    ) = _parse_and_normalize(source, target_bars, time_signature)
    if input_end < target_tick:
        raise BarNormalizationError(
            "MuseCoco MIDI is shorter than the requested output: "
            f"end tick {input_end}, required {target_tick} for {target_bars} bars"
        )
    if output_path.exists() and not force:
        raise BarNormalizationError(
            f"output MIDI already exists: {output_path}; use --force to replace it"
        )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(normalized)
                handle.flush()
                os.fsync(handle.fileno())
            if force:
                os.replace(temporary_name, output_path)
            else:
                os.link(temporary_name, output_path)
                os.unlink(temporary_name)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
    except FileExistsError as exc:
        raise BarNormalizationError(
            f"output MIDI already exists: {output_path}; use --force to replace it"
        ) from exc
    except OSError as exc:
        raise BarNormalizationError(f"could not write output MIDI: {exc}") from exc
    return BarNormalizationResult(
        input_midi=input_path,
        output_midi=output_path,
        midi_format=midi_format,
        track_count=track_count,
        ticks_per_beat=division,
        time_signature=time_signature,
        target_bars=target_bars,
        target_end_tick=target_tick,
        input_end_tick=input_end,
        events_trimmed=trimmed,
        note_offs_inserted=note_offs,
        trimmed_to_target=was_longer,
        input_sha256=hashlib.sha256(source).hexdigest(),
        output_sha256=hashlib.sha256(normalized).hexdigest(),
    )
