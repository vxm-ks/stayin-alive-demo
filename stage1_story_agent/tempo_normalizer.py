"""Deterministically normalize Standard MIDI File tempo metadata.

MuseCoco can emit a tempo map that differs from the Stage 1 plan.  This
module rewrites every Set Tempo meta event and inserts the requested tempo at
tick zero.  Note positions remain on their original MIDI tick grid.
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


MIN_BPM = Decimal("30")
MAX_BPM = Decimal("240")
_TEMPO_PREFIX = b"\xff\x51\x03"


class TempoNormalizationError(ValueError):
    """Raised when a MIDI file or requested normalization is invalid."""


@dataclass(frozen=True)
class TempoNormalizationResult:
    input_midi: Path
    output_midi: Path
    midi_format: int
    track_count: int
    ticks_per_beat: int
    target_bpm: float
    encoded_bpm: float
    microseconds_per_quarter: int
    tempo_events_rewritten: int
    tempo_event_inserted_at_tick_zero: bool
    input_sha256: str
    output_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "input_midi": str(self.input_midi.resolve()),
            "output_midi": str(self.output_midi.resolve()),
            "midi_format": self.midi_format,
            "track_count": self.track_count,
            "ticks_per_beat": self.ticks_per_beat,
            "target_bpm": self.target_bpm,
            "encoded_bpm": self.encoded_bpm,
            "microseconds_per_quarter": self.microseconds_per_quarter,
            "tempo_events_rewritten": self.tempo_events_rewritten,
            "tempo_event_inserted_at_tick_zero": self.tempo_event_inserted_at_tick_zero,
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
        }


def bpm_to_microseconds(bpm: float | Decimal | str) -> int:
    """Convert BPM to the 24-bit MIDI Set Tempo representation."""

    try:
        value = Decimal(str(bpm))
    except InvalidOperation as exc:
        raise TempoNormalizationError("target BPM must be a finite number") from exc
    if not value.is_finite() or not MIN_BPM <= value <= MAX_BPM:
        raise TempoNormalizationError(
            f"target BPM must be between {MIN_BPM} and {MAX_BPM}"
        )
    microseconds = int(
        (Decimal("60000000") / value).to_integral_value(rounding=ROUND_HALF_UP)
    )
    if not 1 <= microseconds <= 0xFFFFFF:
        raise TempoNormalizationError("target BPM cannot be represented by MIDI Set Tempo")
    return microseconds


def _read_vlq(data: bytes, position: int, *, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise TempoNormalizationError(f"truncated variable-length value in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise TempoNormalizationError(f"variable-length value exceeds four bytes in {context}")


def _require_data_bytes(data: bytes, start: int, length: int, *, context: str) -> int:
    end = start + length
    if end > len(data):
        raise TempoNormalizationError(f"truncated MIDI event in {context}")
    if any(byte >= 0x80 for byte in data[start:end]):
        raise TempoNormalizationError(f"invalid channel data byte in {context}")
    return end


def _normalize_track(data: bytes, tempo_bytes: bytes, track_index: int) -> tuple[bytes, int]:
    position = 0
    running_status: int | None = None
    rewritten = 0
    output = bytearray()
    context = f"track {track_index}"

    while position < len(data):
        event_start = position
        _, position = _read_vlq(data, position, context=context)
        if position >= len(data):
            raise TempoNormalizationError(f"missing event after delta time in {context}")

        lead = data[position]
        if lead >= 0x80:
            status = lead
            position += 1
            if 0x80 <= status <= 0xEF:
                running_status = status
            elif status not in (0xF0, 0xF7, 0xFF):
                raise TempoNormalizationError(
                    f"unsupported status byte 0x{status:02X} in {context}"
                )
        else:
            if running_status is None:
                raise TempoNormalizationError(f"running status has no prior status in {context}")
            status = running_status

        if 0x80 <= status <= 0xEF:
            data_length = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
            position = _require_data_bytes(
                data,
                position,
                data_length,
                context=context,
            )
            output.extend(data[event_start:position])
            continue

        if status in (0xF0, 0xF7):
            payload_length, position = _read_vlq(data, position, context=context)
            payload_end = position + payload_length
            if payload_end > len(data):
                raise TempoNormalizationError(f"truncated SysEx payload in {context}")
            position = payload_end
            output.extend(data[event_start:position])
            continue

        # Meta event (0xFF).
        if position >= len(data):
            raise TempoNormalizationError(f"missing meta-event type in {context}")
        meta_type = data[position]
        position += 1
        payload_length, position = _read_vlq(data, position, context=context)
        payload_start = position
        payload_end = payload_start + payload_length
        if payload_end > len(data):
            raise TempoNormalizationError(f"truncated meta-event payload in {context}")
        position = payload_end

        if meta_type == 0x51:
            if payload_length != 3:
                raise TempoNormalizationError(
                    f"Set Tempo meta event must contain exactly three bytes in {context}"
                )
            output.extend(data[event_start:payload_start])
            output.extend(tempo_bytes)
            rewritten += 1
        else:
            output.extend(data[event_start:position])

    return bytes(output), rewritten


def _parse_and_normalize(data: bytes, microseconds: int) -> tuple[bytes, int, int, int, int]:
    if len(data) < 14 or data[:4] != b"MThd":
        raise TempoNormalizationError("input is not a Standard MIDI File")
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length < 6 or 8 + header_length > len(data):
        raise TempoNormalizationError("invalid or truncated MIDI header")

    midi_format, declared_tracks, division = struct.unpack(">HHH", data[8:14])
    if midi_format not in (0, 1):
        raise TempoNormalizationError("only MIDI format 0 and format 1 are supported")
    if midi_format == 0 and declared_tracks != 1:
        raise TempoNormalizationError("MIDI format 0 must declare exactly one track")
    if declared_tracks < 1:
        raise TempoNormalizationError("MIDI file must declare at least one track")
    if division & 0x8000:
        raise TempoNormalizationError("SMPTE time division is not supported")
    if division == 0:
        raise TempoNormalizationError("ticks per beat must be non-zero")

    position = 8 + header_length
    chunks: list[tuple[bytes, bytes]] = []
    track_count = 0
    rewritten = 0
    tempo_bytes = microseconds.to_bytes(3, "big")

    while position < len(data):
        if position + 8 > len(data):
            raise TempoNormalizationError("truncated MIDI chunk header")
        chunk_type = data[position : position + 4]
        chunk_length = struct.unpack(">I", data[position + 4 : position + 8])[0]
        payload_start = position + 8
        payload_end = payload_start + chunk_length
        if payload_end > len(data):
            raise TempoNormalizationError("truncated MIDI chunk payload")
        payload = data[payload_start:payload_end]
        position = payload_end

        if chunk_type == b"MTrk":
            normalized, count = _normalize_track(payload, tempo_bytes, track_count)
            if track_count == 0:
                normalized = b"\x00" + _TEMPO_PREFIX + tempo_bytes + normalized
            chunks.append((chunk_type, normalized))
            track_count += 1
            rewritten += count
        else:
            chunks.append((chunk_type, payload))

    if track_count != declared_tracks:
        raise TempoNormalizationError(
            f"MIDI header declares {declared_tracks} tracks but file contains {track_count}"
        )

    output = bytearray(data[: 8 + header_length])
    for chunk_type, payload in chunks:
        output.extend(chunk_type)
        output.extend(struct.pack(">I", len(payload)))
        output.extend(payload)
    return bytes(output), midi_format, track_count, division, rewritten


def default_output_path(input_midi: Path) -> Path:
    suffix = input_midi.suffix if input_midi.suffix.lower() in (".mid", ".midi") else ".mid"
    return input_midi.with_name(f"{input_midi.stem}.tempo-normalized{suffix}")


def normalize_midi_tempo(
    input_midi: Path,
    output_midi: Path,
    target_bpm: float | Decimal | str,
    *,
    force: bool = False,
) -> TempoNormalizationResult:
    """Write an atomically published constant-tempo copy of a MIDI file."""

    input_path = Path(input_midi)
    output_path = Path(output_midi)
    try:
        if input_path.resolve() == output_path.resolve():
            raise TempoNormalizationError("input and output MIDI paths must differ")
        source = input_path.read_bytes()
    except TempoNormalizationError:
        raise
    except OSError as exc:
        raise TempoNormalizationError(f"could not read input MIDI: {exc}") from exc

    microseconds = bpm_to_microseconds(target_bpm)
    normalized, midi_format, tracks, division, rewritten = _parse_and_normalize(
        source, microseconds
    )
    if output_path.exists() and not force:
        raise TempoNormalizationError(
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
                # A same-directory hard link publishes the completed temporary
                # file atomically and fails if another writer created output.
                os.link(temporary_name, output_path)
                os.unlink(temporary_name)
        except FileExistsError as exc:
            raise TempoNormalizationError(
                f"output MIDI already exists: {output_path}; use --force to replace it"
            ) from exc
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
    except TempoNormalizationError:
        raise
    except OSError as exc:
        raise TempoNormalizationError(f"could not write output MIDI: {exc}") from exc

    encoded_bpm = 60_000_000 / microseconds
    return TempoNormalizationResult(
        input_midi=input_path,
        output_midi=output_path,
        midi_format=midi_format,
        track_count=tracks,
        ticks_per_beat=division,
        target_bpm=float(Decimal(str(target_bpm))),
        encoded_bpm=encoded_bpm,
        microseconds_per_quarter=microseconds,
        tempo_events_rewritten=rewritten,
        tempo_event_inserted_at_tick_zero=True,
        input_sha256=hashlib.sha256(source).hexdigest(),
        output_sha256=hashlib.sha256(normalized).hexdigest(),
    )
