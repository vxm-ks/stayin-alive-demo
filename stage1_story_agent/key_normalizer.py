"""Detect and force a MuseCoco MIDI file to the planned global key.

Pure transposition can correct a tonic while preserving mode; it cannot turn a
major composition into a minor composition (or vice versa).  This module
therefore fails closed on mode mismatch, transposes every non-drum note event,
leaves General MIDI channel 10 untouched, and publishes a target Key Signature
meta event with an auditable result.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence


Mode = Literal["major", "minor"]

TONIC_TO_PC = {
    "C": 0,
    "C#": 1,
    "D": 2,
    "Eb": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "G": 7,
    "Ab": 8,
    "A": 9,
    "Bb": 10,
    "B": 11,
}
PC_TO_TONIC = {value: key for key, value in TONIC_TO_PC.items()}

# MIDI Key Signature ``sf`` values chosen to match the canonical spellings
# used by Stage 1's Tonic literal.
KEY_SIGNATURE_SF = {
    "major": {
        "C": 0, "C#": 7, "D": 2, "Eb": -3, "E": 4, "F": -1,
        "F#": 6, "G": 1, "Ab": -4, "A": 3, "Bb": -2, "B": 5,
    },
    "minor": {
        "C": -3, "C#": 4, "D": -1, "Eb": -6, "E": 1, "F": -4,
        "F#": 3, "G": -2, "Ab": -7, "A": 0, "Bb": -5, "B": 2,
    },
}
SF_TO_KEY = {
    **{(sf, 0): (PC_TO_TONIC[(7 * sf) % 12], "major") for sf in range(-7, 8)},
    **{(sf, 1): (PC_TO_TONIC[(9 + 7 * sf) % 12], "minor") for sf in range(-7, 8)},
}

# Krumhansl-Kessler duration profiles, indexed relative to the tonic.
MAJOR_PROFILE = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR_PROFILE = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)


class KeyNormalizationError(ValueError):
    """Raised when key detection or lossless transposition cannot be completed."""


@dataclass(frozen=True)
class DetectedKey:
    tonic: str
    mode: Mode
    method: Literal["key_signature", "pitch_profile", "override"]
    profile_score: float | None = None
    profile_margin: float | None = None


@dataclass(frozen=True)
class KeyNormalizationResult:
    input_midi: Path
    output_midi: Path
    midi_format: int
    track_count: int
    ticks_per_beat: int
    source_tonic: str
    source_mode: Mode
    target_tonic: str
    target_mode: Mode
    detection_method: str
    profile_score: float | None
    profile_margin: float | None
    transpose_semitones: int
    melodic_note_messages_rewritten: int
    drum_note_messages_preserved: int
    key_signature_events_rewritten: int
    key_signature_inserted_at_tick_zero: bool
    melodic_pitch_range_before: tuple[int, int]
    melodic_pitch_range_after: tuple[int, int]
    input_sha256: str
    output_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "input_midi": str(self.input_midi.resolve()),
            "output_midi": str(self.output_midi.resolve()),
            "midi_format": self.midi_format,
            "track_count": self.track_count,
            "ticks_per_beat": self.ticks_per_beat,
            "source_key": {"tonic": self.source_tonic, "mode": self.source_mode},
            "target_key": {"tonic": self.target_tonic, "mode": self.target_mode},
            "detection_method": self.detection_method,
            "profile_score": self.profile_score,
            "profile_margin": self.profile_margin,
            "transpose_semitones": self.transpose_semitones,
            "melodic_note_messages_rewritten": self.melodic_note_messages_rewritten,
            "drum_note_messages_preserved": self.drum_note_messages_preserved,
            "key_signature_events_rewritten": self.key_signature_events_rewritten,
            "key_signature_inserted_at_tick_zero": self.key_signature_inserted_at_tick_zero,
            "melodic_pitch_range_before": list(self.melodic_pitch_range_before),
            "melodic_pitch_range_after": list(self.melodic_pitch_range_after),
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
        }


@dataclass
class _MidiScan:
    midi_format: int
    track_count: int
    ticks_per_beat: int
    chunks: list[tuple[bytes, bytes]]
    pitch_weights: list[float]
    key_signatures: list[tuple[int, int]]
    melodic_pitches: list[int]


def _read_vlq(data: bytes, position: int, *, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise KeyNormalizationError(f"truncated variable-length value in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise KeyNormalizationError(f"variable-length value exceeds four bytes in {context}")


def _require_data_bytes(data: bytes, start: int, length: int, *, context: str) -> int:
    end = start + length
    if end > len(data):
        raise KeyNormalizationError(f"truncated MIDI event in {context}")
    if any(byte >= 0x80 for byte in data[start:end]):
        raise KeyNormalizationError(f"invalid channel data byte in {context}")
    return end


def _scan_track(data: bytes, track_index: int) -> tuple[list[float], list[tuple[int, int]], list[int]]:
    position = 0
    running_status: int | None = None
    absolute_tick = 0
    active: dict[tuple[int, int], list[tuple[int, int]]] = {}
    pitch_weights = [0.0] * 12
    signatures: list[tuple[int, int]] = []
    pitches: list[int] = []
    context = f"track {track_index}"

    while position < len(data):
        delta, position = _read_vlq(data, position, context=context)
        absolute_tick += delta
        if position >= len(data):
            raise KeyNormalizationError(f"missing event after delta time in {context}")

        lead = data[position]
        if lead >= 0x80:
            status = lead
            position += 1
            if 0x80 <= status <= 0xEF:
                running_status = status
            elif status not in (0xF0, 0xF7, 0xFF):
                raise KeyNormalizationError(f"unsupported status byte 0x{status:02X} in {context}")
        else:
            if running_status is None:
                raise KeyNormalizationError(f"running status has no prior status in {context}")
            status = running_status

        if 0x80 <= status <= 0xEF:
            kind = status & 0xF0
            channel = status & 0x0F
            length = 1 if kind in (0xC0, 0xD0) else 2
            data_start = position
            position = _require_data_bytes(data, position, length, context=context)
            if kind in (0x80, 0x90) and channel != 9:
                pitch = data[data_start]
                velocity = data[data_start + 1]
                key = (channel, pitch)
                if kind == 0x90 and velocity > 0:
                    active.setdefault(key, []).append((absolute_tick, velocity))
                    pitches.append(pitch)
                else:
                    starts = active.get(key)
                    if starts:
                        start_tick, _ = starts.pop(0)
                        if not starts:
                            del active[key]
                        pitch_weights[pitch % 12] += max(1, absolute_tick - start_tick)
            continue

        if status in (0xF0, 0xF7):
            payload_length, position = _read_vlq(data, position, context=context)
            position += payload_length
            if position > len(data):
                raise KeyNormalizationError(f"truncated SysEx payload in {context}")
            continue

        if position >= len(data):
            raise KeyNormalizationError(f"missing meta-event type in {context}")
        meta_type = data[position]
        position += 1
        payload_length, position = _read_vlq(data, position, context=context)
        payload_start = position
        position += payload_length
        if position > len(data):
            raise KeyNormalizationError(f"truncated meta-event payload in {context}")
        if meta_type == 0x59:
            if payload_length != 2:
                raise KeyNormalizationError(f"Key Signature meta event must contain two bytes in {context}")
            sf_raw, mi = data[payload_start : payload_start + 2]
            sf = sf_raw if sf_raw < 128 else sf_raw - 256
            if not -7 <= sf <= 7 or mi not in (0, 1):
                raise KeyNormalizationError(f"invalid Key Signature payload in {context}")
            signatures.append((sf, mi))

    for (_, pitch), starts in active.items():
        for start_tick, _ in starts:
            pitch_weights[pitch % 12] += max(1, absolute_tick - start_tick)
    return pitch_weights, signatures, pitches


def _scan_midi(data: bytes) -> _MidiScan:
    if len(data) < 14 or data[:4] != b"MThd":
        raise KeyNormalizationError("input is not a Standard MIDI File")
    header_length = struct.unpack(">I", data[4:8])[0]
    if header_length < 6 or 8 + header_length > len(data):
        raise KeyNormalizationError("invalid or truncated MIDI header")
    midi_format, declared_tracks, division = struct.unpack(">HHH", data[8:14])
    if midi_format not in (0, 1):
        raise KeyNormalizationError("only MIDI format 0 and format 1 are supported")
    if midi_format == 0 and declared_tracks != 1:
        raise KeyNormalizationError("MIDI format 0 must declare exactly one track")
    if declared_tracks < 1:
        raise KeyNormalizationError("MIDI file must declare at least one track")
    if division & 0x8000 or division == 0:
        raise KeyNormalizationError("only non-zero ticks-per-beat time division is supported")

    position = 8 + header_length
    chunks: list[tuple[bytes, bytes]] = []
    weights = [0.0] * 12
    signatures: list[tuple[int, int]] = []
    pitches: list[int] = []
    track_count = 0
    while position < len(data):
        if position + 8 > len(data):
            raise KeyNormalizationError("truncated MIDI chunk header")
        chunk_type = data[position : position + 4]
        chunk_length = struct.unpack(">I", data[position + 4 : position + 8])[0]
        payload_start = position + 8
        payload_end = payload_start + chunk_length
        if payload_end > len(data):
            raise KeyNormalizationError("truncated MIDI chunk payload")
        payload = data[payload_start:payload_end]
        position = payload_end
        chunks.append((chunk_type, payload))
        if chunk_type == b"MTrk":
            track_weights, track_signatures, track_pitches = _scan_track(payload, track_count)
            weights = [left + right for left, right in zip(weights, track_weights, strict=True)]
            signatures.extend(track_signatures)
            pitches.extend(track_pitches)
            track_count += 1

    if track_count != declared_tracks:
        raise KeyNormalizationError(
            f"MIDI header declares {declared_tracks} tracks but file contains {track_count}"
        )
    if not pitches:
        raise KeyNormalizationError("MIDI contains no non-drum note-on events")
    return _MidiScan(midi_format, track_count, division, chunks, weights, signatures, pitches)


def _correlation(observed: Sequence[float], expected: Sequence[float]) -> float:
    observed_mean = sum(observed) / len(observed)
    expected_mean = sum(expected) / len(expected)
    numerator = sum((a - observed_mean) * (b - expected_mean) for a, b in zip(observed, expected, strict=True))
    left = math.sqrt(sum((value - observed_mean) ** 2 for value in observed))
    right = math.sqrt(sum((value - expected_mean) ** 2 for value in expected))
    if left == 0 or right == 0:
        return 0.0
    return numerator / (left * right)


def detect_key_from_profile(pitch_weights: Sequence[float]) -> DetectedKey:
    """Return the strongest of 24 Krumhansl-Kessler profile correlations."""

    if len(pitch_weights) != 12 or not any(value > 0 for value in pitch_weights):
        raise KeyNormalizationError("key detection requires a non-empty 12-bin pitch profile")
    candidates: list[tuple[float, int, Mode]] = []
    for tonic_pc in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            rotated = [profile[(pitch_pc - tonic_pc) % 12] for pitch_pc in range(12)]
            candidates.append((_correlation(pitch_weights, rotated), tonic_pc, mode))
    candidates.sort(reverse=True, key=lambda item: item[0])
    best, second = candidates[0], candidates[1]
    if best[0] < 0.10 or best[0] - second[0] < 0.01:
        raise KeyNormalizationError(
            "automatic key detection is ambiguous; provide --source-tonic and --source-mode"
        )
    return DetectedKey(
        tonic=PC_TO_TONIC[best[1]],
        mode=best[2],
        method="pitch_profile",
        profile_score=best[0],
        profile_margin=best[0] - second[0],
    )


def _detect_source_key(
    scan: _MidiScan,
    source_tonic: str | None,
    source_mode: str | None,
) -> DetectedKey:
    if (source_tonic is None) != (source_mode is None):
        raise KeyNormalizationError("source tonic and mode must be supplied together")
    if source_tonic is not None:
        tonic, mode = _validate_key(source_tonic, source_mode)
        return DetectedKey(tonic, mode, "override")

    unique_signatures = set(scan.key_signatures)
    if len(unique_signatures) > 1:
        raise KeyNormalizationError(
            "multiple source key signatures imply modulation; provide an explicit source key only if a global transposition is intended"
        )
    if unique_signatures:
        signature = next(iter(unique_signatures))
        key = SF_TO_KEY.get(signature)
        if key is None:
            raise KeyNormalizationError("source Key Signature cannot be represented by the Stage 1 tonic vocabulary")
        return DetectedKey(key[0], key[1], "key_signature")
    return detect_key_from_profile(scan.pitch_weights)


def _validate_key(tonic: str, mode: str | None) -> tuple[str, Mode]:
    if tonic not in TONIC_TO_PC:
        raise KeyNormalizationError(f"unsupported tonic {tonic!r}; use one of {', '.join(TONIC_TO_PC)}")
    if mode not in ("major", "minor"):
        raise KeyNormalizationError("mode must be 'major' or 'minor'")
    return tonic, mode


def _minimal_shift(source_tonic: str, target_tonic: str) -> int:
    shift = (TONIC_TO_PC[target_tonic] - TONIC_TO_PC[source_tonic]) % 12
    return shift - 12 if shift > 6 else shift


def _target_key_payload(tonic: str, mode: Mode) -> bytes:
    sf = KEY_SIGNATURE_SF[mode][tonic]
    return bytes((sf & 0xFF, 0 if mode == "major" else 1))


def _transpose_track(
    data: bytes,
    shift: int,
    target_payload: bytes,
    track_index: int,
) -> tuple[bytes, int, int, int]:
    position = 0
    running_status: int | None = None
    output = bytearray()
    melodic_messages = 0
    drum_messages = 0
    key_signatures = 0
    context = f"track {track_index}"

    while position < len(data):
        event_start = position
        _, position = _read_vlq(data, position, context=context)
        if position >= len(data):
            raise KeyNormalizationError(f"missing event after delta time in {context}")
        lead = data[position]
        if lead >= 0x80:
            status = lead
            position += 1
            if 0x80 <= status <= 0xEF:
                running_status = status
            elif status not in (0xF0, 0xF7, 0xFF):
                raise KeyNormalizationError(f"unsupported status byte 0x{status:02X} in {context}")
        else:
            if running_status is None:
                raise KeyNormalizationError(f"running status has no prior status in {context}")
            status = running_status

        if 0x80 <= status <= 0xEF:
            kind = status & 0xF0
            channel = status & 0x0F
            length = 1 if kind in (0xC0, 0xD0) else 2
            data_start = position
            position = _require_data_bytes(data, position, length, context=context)
            if kind in (0x80, 0x90):
                if channel == 9:
                    drum_messages += 1
                else:
                    pitch = data[data_start]
                    transposed = pitch + shift
                    if not 0 <= transposed <= 127:
                        raise KeyNormalizationError(
                            f"transposition would move MIDI pitch {pitch} outside 0..127 in {context}"
                        )
                    output.extend(data[event_start:data_start])
                    output.append(transposed)
                    output.extend(data[data_start + 1 : position])
                    melodic_messages += 1
                    continue
            output.extend(data[event_start:position])
            continue

        if status in (0xF0, 0xF7):
            payload_length, position = _read_vlq(data, position, context=context)
            position += payload_length
            if position > len(data):
                raise KeyNormalizationError(f"truncated SysEx payload in {context}")
            output.extend(data[event_start:position])
            continue

        if position >= len(data):
            raise KeyNormalizationError(f"missing meta-event type in {context}")
        meta_type = data[position]
        position += 1
        payload_length, position = _read_vlq(data, position, context=context)
        payload_start = position
        position += payload_length
        if position > len(data):
            raise KeyNormalizationError(f"truncated meta-event payload in {context}")
        if meta_type == 0x59:
            if payload_length != 2:
                raise KeyNormalizationError(f"Key Signature meta event must contain two bytes in {context}")
            output.extend(data[event_start:payload_start])
            output.extend(target_payload)
            key_signatures += 1
        else:
            output.extend(data[event_start:position])
    return bytes(output), melodic_messages, drum_messages, key_signatures


def _render_transposed(data: bytes, scan: _MidiScan, shift: int, tonic: str, mode: Mode) -> tuple[bytes, int, int, int]:
    header_length = struct.unpack(">I", data[4:8])[0]
    output = bytearray(data[: 8 + header_length])
    target_payload = _target_key_payload(tonic, mode)
    track_index = 0
    melodic_messages = 0
    drum_messages = 0
    key_signatures = 0
    for chunk_type, payload in scan.chunks:
        if chunk_type == b"MTrk":
            rewritten, melodic, drums, signatures = _transpose_track(
                payload, shift, target_payload, track_index
            )
            if track_index == 0:
                rewritten = b"\x00\xff\x59\x02" + target_payload + rewritten
            payload = rewritten
            track_index += 1
            melodic_messages += melodic
            drum_messages += drums
            key_signatures += signatures
        output.extend(chunk_type)
        output.extend(struct.pack(">I", len(payload)))
        output.extend(payload)
    return bytes(output), melodic_messages, drum_messages, key_signatures


def default_output_path(input_midi: Path) -> Path:
    suffix = input_midi.suffix if input_midi.suffix.lower() in (".mid", ".midi") else ".mid"
    return input_midi.with_name(f"{input_midi.stem}.key-normalized{suffix}")


def normalize_midi_key(
    input_midi: Path,
    output_midi: Path,
    target_tonic: str,
    target_mode: str,
    *,
    source_tonic: str | None = None,
    source_mode: str | None = None,
    force: bool = False,
) -> KeyNormalizationResult:
    """Atomically publish a globally transposed copy in the planned key."""

    input_path = Path(input_midi)
    output_path = Path(output_midi)
    target_tonic, target_mode = _validate_key(target_tonic, target_mode)
    try:
        if input_path.resolve() == output_path.resolve():
            raise KeyNormalizationError("input and output MIDI paths must differ")
        source = input_path.read_bytes()
    except KeyNormalizationError:
        raise
    except OSError as exc:
        raise KeyNormalizationError(f"could not read input MIDI: {exc}") from exc

    scan = _scan_midi(source)
    detected = _detect_source_key(scan, source_tonic, source_mode)
    if detected.mode != target_mode:
        raise KeyNormalizationError(
            f"source mode is {detected.mode} but target mode is {target_mode}; pure transposition cannot correct mode"
        )
    shift = _minimal_shift(detected.tonic, target_tonic)
    before = (min(scan.melodic_pitches), max(scan.melodic_pitches))
    after = (before[0] + shift, before[1] + shift)
    if not 0 <= after[0] <= after[1] <= 127:
        raise KeyNormalizationError(
            f"transposition by {shift:+d} semitones would move melodic range {before} outside MIDI 0..127"
        )
    normalized, melodic, drums, signatures = _render_transposed(
        source, scan, shift, target_tonic, target_mode
    )
    if output_path.exists() and not force:
        raise KeyNormalizationError(
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
        except FileExistsError as exc:
            raise KeyNormalizationError(
                f"output MIDI already exists: {output_path}; use --force to replace it"
            ) from exc
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
    except KeyNormalizationError:
        raise
    except OSError as exc:
        raise KeyNormalizationError(f"could not write output MIDI: {exc}") from exc

    return KeyNormalizationResult(
        input_midi=input_path,
        output_midi=output_path,
        midi_format=scan.midi_format,
        track_count=scan.track_count,
        ticks_per_beat=scan.ticks_per_beat,
        source_tonic=detected.tonic,
        source_mode=detected.mode,
        target_tonic=target_tonic,
        target_mode=target_mode,
        detection_method=detected.method,
        profile_score=detected.profile_score,
        profile_margin=detected.profile_margin,
        transpose_semitones=shift,
        melodic_note_messages_rewritten=melodic,
        drum_note_messages_preserved=drums,
        key_signature_events_rewritten=signatures,
        key_signature_inserted_at_tick_zero=True,
        melodic_pitch_range_before=before,
        melodic_pitch_range_after=after,
        input_sha256=hashlib.sha256(source).hexdigest(),
        output_sha256=hashlib.sha256(normalized).hexdigest(),
    )
