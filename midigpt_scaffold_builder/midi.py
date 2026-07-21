"""Small Standard MIDI File reader/writer used by the offline compiler.

The project intentionally avoids importing MIDI-GPT or a model checkpoint.  The
reader supports the channel messages needed for generated motif assets and
keeps note timing in integer ticks, which makes audit comparisons deterministic.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MidiNote:
    start_tick: int
    duration_ticks: int
    pitch: int
    velocity: int
    channel: int = 0


@dataclass
class MidiTrack:
    name: str
    program: int
    track_type: str
    notes: list[MidiNote] = field(default_factory=list)


@dataclass
class ParsedMidi:
    resolution: int
    tempo: int
    numerator: int
    denominator: int
    tracks: list[MidiTrack]


def _read_vlq(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise ValueError("Truncated MIDI variable-length quantity")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, position
    raise ValueError("MIDI variable-length quantity exceeds four bytes")


def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta tick cannot be negative")
    buffer = value & 0x7F
    while value >> 7:
        value >>= 7
        buffer = ((buffer << 8) | ((value & 0x7F) | 0x80))
    output = bytearray()
    while True:
        output.append(buffer & 0xFF)
        if buffer & 0x80:
            buffer >>= 8
        else:
            return bytes(output)


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(payload)) + payload


def parse_midi(path: str | Path) -> ParsedMidi:
    source = Path(path)
    data = source.read_bytes()
    if len(data) < 14 or data[:4] != b"MThd":
        raise ValueError(f"Not a Standard MIDI File: {source}")
    header_length = struct.unpack_from(">I", data, 4)[0]
    if header_length < 6 or 8 + header_length > len(data):
        raise ValueError("Invalid MIDI header length")
    _format, track_count, division = struct.unpack_from(">HHH", data, 8)
    if division & 0x8000:
        raise ValueError("SMPTE MIDI divisions are not supported")
    if division <= 0:
        raise ValueError("MIDI PPQ must be positive")

    position = 8 + header_length
    tempo = 500_000
    numerator, denominator = 4, 4
    grouped: list[MidiTrack] = []
    for raw_index in range(track_count):
        if position + 8 > len(data) or data[position : position + 4] != b"MTrk":
            raise ValueError(f"Missing MTrk chunk at index {raw_index}")
        length = struct.unpack_from(">I", data, position + 4)[0]
        start, end = position + 8, position + 8 + length
        if end > len(data):
            raise ValueError(f"Truncated MTrk chunk at index {raw_index}")
        chunk = data[start:end]
        position = end

        cursor = 0
        absolute = 0
        running_status: int | None = None
        name = f"Track {raw_index}"
        programs: dict[int, int] = {}
        active: dict[tuple[int, int], list[tuple[int, int]]] = {}
        notes: dict[int, list[MidiNote]] = {}
        last_tick = 0
        while cursor < len(chunk):
            delta, cursor = _read_vlq(chunk, cursor)
            absolute += delta
            last_tick = max(last_tick, absolute)
            if cursor >= len(chunk):
                raise ValueError(f"Truncated event in track {raw_index}")
            first = chunk[cursor]
            if first & 0x80:
                status = first
                cursor += 1
                if status < 0xF0:
                    running_status = status
            elif running_status is not None:
                status = running_status
            else:
                raise ValueError(f"Running status without prior status in track {raw_index}")

            if status == 0xFF:
                running_status = None
                if cursor >= len(chunk):
                    raise ValueError("Truncated MIDI meta event")
                kind = chunk[cursor]
                cursor += 1
                size, cursor = _read_vlq(chunk, cursor)
                payload = chunk[cursor : cursor + size]
                if len(payload) != size:
                    raise ValueError("Truncated MIDI meta payload")
                cursor += size
                if kind == 0x03:
                    name = payload.decode("utf-8", errors="replace") or name
                elif kind == 0x51 and size == 3:
                    tempo = int.from_bytes(payload, "big")
                elif kind == 0x58 and size >= 2:
                    numerator = int(payload[0])
                    denominator = 1 << int(payload[1])
                elif kind == 0x2F:
                    break
                continue
            if status in {0xF0, 0xF7}:
                running_status = None
                size, cursor = _read_vlq(chunk, cursor)
                cursor += size
                if cursor > len(chunk):
                    raise ValueError("Truncated MIDI SysEx event")
                continue
            if status >= 0xF0:
                raise ValueError(f"Unsupported MIDI system event 0x{status:02x}")

            family, channel = status & 0xF0, status & 0x0F
            data_size = 1 if family in {0xC0, 0xD0} else 2
            if cursor + data_size > len(chunk):
                raise ValueError("Truncated MIDI channel event")
            first_data = chunk[cursor]
            second_data = chunk[cursor + 1] if data_size == 2 else 0
            cursor += data_size
            if family == 0xC0:
                programs[channel] = int(first_data)
            elif family == 0x90 and second_data > 0:
                active.setdefault((channel, int(first_data)), []).append(
                    (absolute, int(second_data))
                )
            elif family == 0x80 or (family == 0x90 and second_data == 0):
                key = (channel, int(first_data))
                pending = active.get(key)
                if pending:
                    note_start, velocity = pending.pop(0)
                    notes.setdefault(channel, []).append(
                        MidiNote(
                            start_tick=note_start,
                            duration_ticks=max(1, absolute - note_start),
                            pitch=int(first_data),
                            velocity=velocity,
                            channel=channel,
                        )
                    )

        for (channel, pitch), pending in active.items():
            for note_start, velocity in pending:
                notes.setdefault(channel, []).append(
                    MidiNote(
                        start_tick=note_start,
                        duration_ticks=max(1, last_tick - note_start),
                        pitch=pitch,
                        velocity=velocity,
                        channel=channel,
                    )
                )
        for channel in sorted(notes):
            grouped.append(
                MidiTrack(
                    name=name,
                    program=programs.get(channel, 0),
                    track_type="drum" if channel == 9 else "melodic",
                    notes=sorted(
                        notes[channel],
                        key=lambda item: (item.start_tick, item.pitch, item.duration_ticks),
                    ),
                )
            )

    if not grouped:
        raise ValueError(f"MIDI contains no note events: {source}")
    return ParsedMidi(
        resolution=division,
        tempo=tempo,
        numerator=numerator,
        denominator=denominator,
        tracks=grouped,
    )


def _track_chunk(events: list[tuple[int, int, bytes]]) -> bytes:
    payload = bytearray()
    previous = 0
    for tick, order, event in sorted(events, key=lambda item: (item[0], item[1])):
        del order
        if tick < previous:
            raise ValueError("MIDI events are not monotonic")
        payload.extend(_vlq(tick - previous))
        payload.extend(event)
        previous = tick
    return b"MTrk" + struct.pack(">I", len(payload)) + payload


def midi_bytes(
    *,
    resolution: int,
    tempo: int,
    numerator: int,
    denominator: int,
    tracks: list[MidiTrack],
    end_tick: int,
) -> bytes:
    if resolution <= 0 or tempo <= 0 or numerator <= 0 or end_tick <= 0:
        raise ValueError("MIDI timing values must be positive")
    if denominator <= 0 or denominator & (denominator - 1):
        raise ValueError("MIDI denominator must be a power of two")
    if tempo > 0xFFFFFF:
        raise ValueError("MIDI tempo exceeds the three-byte representation")

    # Never shorten a protected note merely because its note-off lies beyond
    # the nominal last bar.  Keep the musical bar grid unchanged and move the
    # End-of-Track marker far enough to preserve every complete event.
    file_end_tick = max(
        [end_tick]
        + [
            note.start_tick + note.duration_ticks
            for track in tracks
            for note in track.notes
        ]
    )

    meta_events = [
        (0, 0, _meta(0x03, b"LegaSynth scaffold timing")),
        (0, 1, _meta(0x51, tempo.to_bytes(3, "big"))),
        (
            0,
            2,
            _meta(0x58, bytes([numerator, int(math.log2(denominator)), 24, 8])),
        ),
        (file_end_tick, 99, _meta(0x2F, b"")),
    ]
    chunks = [_track_chunk(meta_events)]
    melodic_channels = [value for value in range(16) if value != 9]
    melodic_position = 0
    for track in tracks:
        if track.track_type == "drum":
            channel = 9
        else:
            if melodic_position >= len(melodic_channels):
                raise ValueError("At most 15 melodic MIDI tracks can be written")
            channel = melodic_channels[melodic_position]
            melodic_position += 1
        name = track.name.encode("utf-8")[:127]
        events: list[tuple[int, int, bytes]] = [
            (0, 0, _meta(0x03, name)),
            (0, 1, bytes([0xC0 | channel, track.program])),
        ]
        for note in track.notes:
            if not (0 <= note.pitch <= 127 and 1 <= note.velocity <= 127):
                raise ValueError("MIDI note pitch/velocity is out of range")
            if note.start_tick < 0 or note.duration_ticks <= 0:
                raise ValueError("MIDI note timing is invalid")
            stop = note.start_tick + note.duration_ticks
            events.append(
                (
                    note.start_tick,
                    20,
                    bytes([0x90 | channel, note.pitch, note.velocity]),
                )
            )
            events.append((stop, 10, bytes([0x80 | channel, note.pitch, 0])))
        events.append((file_end_tick, 99, _meta(0x2F, b"")))
        chunks.append(_track_chunk(events))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), resolution)
    return header + b"".join(chunks)


def write_midi(path: str | Path, **kwargs: object) -> None:
    Path(path).write_bytes(midi_bytes(**kwargs))
