"""Validate and render one complete Stage 2 MIDI in a single synth pass.

Stage 3 never creates, moves, or re-times heartbeat events.  The complete MIDI
is immutable input.  A general SoundFont is loaded first and a percussion-only
heartbeat SoundFont is loaded second so that bank 128/program 0 resolves to the
patient heartbeat timbre without shadowing melodic bank 0 programs.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence
from uuid import uuid4


VERSION = "1.1.0"
DEFAULT_HEARTBEAT_CHANNEL = 9  # zero-based MIDI channel 10
DEFAULT_HEARTBEAT_NOTES = (36, 38)


class Stage3RenderError(ValueError):
    """Raised when a Stage 3 input or render result violates the contract."""


@dataclass(frozen=True)
class MidiInspection:
    midi_format: int
    track_count: int
    ppq: int
    end_tick: int
    note_on_count: int
    music_note_on_count: int
    heartbeat_note_on_count: int
    heartbeat_notes: tuple[int, ...]
    active_channels: tuple[int, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "midi_format": self.midi_format,
            "track_count": self.track_count,
            "ppq": self.ppq,
            "end_tick": self.end_tick,
            "note_on_count": self.note_on_count,
            "music_note_on_count": self.music_note_on_count,
            "heartbeat_note_on_count": self.heartbeat_note_on_count,
            "heartbeat_notes": list(self.heartbeat_notes),
            "active_channels_human": [channel + 1 for channel in self.active_channels],
        }


@dataclass(frozen=True)
class SoundFontInspection:
    preset_count: int
    presets: tuple[tuple[int, int, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "preset_count": self.preset_count,
            "presets": [
                {"bank": bank, "program": program, "name": name}
                for bank, program, name in self.presets
            ],
        }


@dataclass(frozen=True)
class RenderResult:
    output_dir: Path
    output_wav: Path
    manifest_json: Path
    duration_s: float
    sample_rate: int
    channels: int

    def as_dict(self) -> dict[str, object]:
        return {
            "output_dir": str(self.output_dir),
            "output_wav": str(self.output_wav),
            "manifest_json": str(self.manifest_json),
            "duration_s": self.duration_s,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


Executor = Callable[[Sequence[str], Path, int], subprocess.CompletedProcess[str]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_vlq(data: bytes, position: int, context: str) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if position >= len(data):
            raise Stage3RenderError(f"truncated MIDI variable-length value in {context}")
        byte = data[position]
        position += 1
        value = (value << 7) | (byte & 0x7F)
        if byte < 0x80:
            return value, position
    raise Stage3RenderError(f"MIDI variable-length value exceeds four bytes in {context}")


def inspect_complete_midi(
    path: str | Path,
    *,
    heartbeat_channel: int = DEFAULT_HEARTBEAT_CHANNEL,
    heartbeat_notes: Sequence[int] = DEFAULT_HEARTBEAT_NOTES,
) -> MidiInspection:
    """Inspect a Standard MIDI File and enforce the Stage 3 whole-song contract."""

    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise Stage3RenderError(f"could not read complete MIDI: {exc}") from exc
    if len(data) < 14 or data[:4] != b"MThd":
        raise Stage3RenderError("input is not a Standard MIDI File")
    header_size = struct.unpack_from(">I", data, 4)[0]
    if header_size < 6 or 8 + header_size > len(data):
        raise Stage3RenderError("invalid or truncated MIDI header")
    midi_format, declared_tracks, division = struct.unpack_from(">HHH", data, 8)
    if midi_format not in (0, 1):
        raise Stage3RenderError("only MIDI format 0 and 1 are supported")
    if declared_tracks < 1:
        raise Stage3RenderError("complete MIDI has no tracks")
    if division == 0 or division & 0x8000:
        raise Stage3RenderError("SMPTE or zero MIDI division is not supported")
    if not 0 <= heartbeat_channel <= 15:
        raise Stage3RenderError("heartbeat channel must be between 0 and 15")
    allowed = set(heartbeat_notes)
    if not allowed or any(not 0 <= note <= 127 for note in allowed):
        raise Stage3RenderError("heartbeat notes must be MIDI pitches 0..127")

    position = 8 + header_size
    note_on_count = 0
    heartbeat_count = 0
    music_count = 0
    observed_heartbeat_notes: set[int] = set()
    active_channels: set[int] = set()
    end_tick = 0

    for track_index in range(declared_tracks):
        context = f"track {track_index}"
        if position + 8 > len(data) or data[position : position + 4] != b"MTrk":
            raise Stage3RenderError(f"missing MTrk chunk at {context}")
        size = struct.unpack_from(">I", data, position + 4)[0]
        start, end = position + 8, position + 8 + size
        if end > len(data):
            raise Stage3RenderError(f"truncated MTrk chunk at {context}")
        chunk = data[start:end]
        position = end
        cursor = 0
        absolute_tick = 0
        running_status: int | None = None
        while cursor < len(chunk):
            delta, cursor = _read_vlq(chunk, cursor, context)
            absolute_tick += delta
            end_tick = max(end_tick, absolute_tick)
            if cursor >= len(chunk):
                raise Stage3RenderError(f"missing MIDI event in {context}")
            lead = chunk[cursor]
            if lead >= 0x80:
                status = lead
                cursor += 1
                running_status = status if status < 0xF0 else None
            elif running_status is not None:
                status = running_status
            else:
                raise Stage3RenderError(f"running status has no prior status in {context}")

            if status == 0xFF:
                if cursor >= len(chunk):
                    raise Stage3RenderError(f"truncated meta event in {context}")
                kind = chunk[cursor]
                cursor += 1
                payload_size, cursor = _read_vlq(chunk, cursor, context)
                cursor += payload_size
                if cursor > len(chunk):
                    raise Stage3RenderError(f"truncated meta payload in {context}")
                if kind == 0x2F:
                    break
                continue
            if status in (0xF0, 0xF7):
                payload_size, cursor = _read_vlq(chunk, cursor, context)
                cursor += payload_size
                if cursor > len(chunk):
                    raise Stage3RenderError(f"truncated SysEx event in {context}")
                continue
            if status >= 0xF0:
                raise Stage3RenderError(f"unsupported MIDI system status 0x{status:02X}")

            family, channel = status & 0xF0, status & 0x0F
            data_size = 1 if family in (0xC0, 0xD0) else 2
            if cursor + data_size > len(chunk):
                raise Stage3RenderError(f"truncated channel event in {context}")
            first = chunk[cursor]
            second = chunk[cursor + 1] if data_size == 2 else 0
            if first >= 0x80 or (data_size == 2 and second >= 0x80):
                raise Stage3RenderError(f"invalid MIDI channel data in {context}")
            cursor += data_size
            if family == 0x90 and second > 0:
                note_on_count += 1
                active_channels.add(channel)
                if channel == heartbeat_channel:
                    heartbeat_count += 1
                    observed_heartbeat_notes.add(first)
                    if first not in allowed:
                        raise Stage3RenderError(
                            f"heartbeat channel {channel + 1} contains unsupported note {first}; "
                            f"allowed notes are {sorted(allowed)}"
                        )
                else:
                    music_count += 1

    if position != len(data):
        raise Stage3RenderError("unexpected bytes after declared MIDI tracks")
    if heartbeat_count == 0:
        raise Stage3RenderError(
            f"complete MIDI has no heartbeat events on channel {heartbeat_channel + 1}"
        )
    if music_count == 0:
        raise Stage3RenderError("complete MIDI has no non-heartbeat music events")
    return MidiInspection(
        midi_format=midi_format,
        track_count=declared_tracks,
        ppq=division,
        end_tick=end_tick,
        note_on_count=note_on_count,
        music_note_on_count=music_count,
        heartbeat_note_on_count=heartbeat_count,
        heartbeat_notes=tuple(sorted(observed_heartbeat_notes)),
        active_channels=tuple(sorted(active_channels)),
    )


def _chunks(data: bytes, start: int, end: int):
    position = start
    while position + 8 <= end:
        tag = data[position : position + 4]
        size = struct.unpack_from("<I", data, position + 4)[0]
        payload_start = position + 8
        payload_end = payload_start + size
        if payload_end > end:
            raise Stage3RenderError("truncated SoundFont RIFF chunk")
        yield tag, data[payload_start:payload_end]
        position = payload_end + (size & 1)
    if position != end:
        raise Stage3RenderError("invalid SoundFont RIFF padding")


def inspect_soundfont(path: str | Path) -> SoundFontInspection:
    source = Path(path)
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise Stage3RenderError(f"could not read SoundFont {source}: {exc}") from exc
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"sfbk":
        raise Stage3RenderError(f"not a SoundFont 2 RIFF file: {source}")
    declared_size = struct.unpack_from("<I", data, 4)[0] + 8
    if declared_size != len(data):
        raise Stage3RenderError(f"SoundFont RIFF size mismatch: {source}")
    phdr: bytes | None = None
    for tag, payload in _chunks(data, 12, len(data)):
        if tag == b"LIST" and payload[:4] == b"pdta":
            for child_tag, child in _chunks(payload, 4, len(payload)):
                if child_tag == b"phdr":
                    phdr = child
                    break
    if phdr is None or len(phdr) < 76 or len(phdr) % 38:
        raise Stage3RenderError(f"SoundFont has no valid phdr table: {source}")
    presets: list[tuple[int, int, str]] = []
    records = len(phdr) // 38
    for index in range(records - 1):
        name_raw, program, bank, _bag, _lib, _genre, _morph = struct.unpack_from(
            "<20sHHHIII", phdr, index * 38
        )
        name = name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
        presets.append((bank, program, name))
    return SoundFontInspection(preset_count=len(presets), presets=tuple(presets))


def validate_render_inputs(
    input_midi: str | Path,
    general_soundfont: str | Path,
    heartbeat_soundfont: str | Path,
    *,
    heartbeat_channel: int = DEFAULT_HEARTBEAT_CHANNEL,
    heartbeat_notes: Sequence[int] = DEFAULT_HEARTBEAT_NOTES,
) -> tuple[MidiInspection, SoundFontInspection, SoundFontInspection]:
    midi = inspect_complete_midi(
        input_midi,
        heartbeat_channel=heartbeat_channel,
        heartbeat_notes=heartbeat_notes,
    )
    general = inspect_soundfont(general_soundfont)
    heartbeat = inspect_soundfont(heartbeat_soundfont)
    heartbeat_keys = {(bank, program) for bank, program, _name in heartbeat.presets}
    if (128, 0) not in heartbeat_keys:
        raise Stage3RenderError(
            "heartbeat SoundFont must provide percussion bank 128/program 0"
        )
    unexpected_presets = sorted(key for key in heartbeat_keys if key != (128, 0))
    if unexpected_presets:
        raise Stage3RenderError(
            "heartbeat SoundFont must contain only percussion bank 128/program 0 so it "
            f"cannot shadow other instruments; found extra presets {unexpected_presets}"
        )
    return midi, general, heartbeat


def _default_executor(
    command: Sequence[str], output_path: Path, timeout_seconds: int
) -> subprocess.CompletedProcess[str]:
    del output_path
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _resolve_executable(value: str | Path) -> str:
    candidate = str(value)
    resolved = shutil.which(candidate)
    if resolved:
        return resolved
    path = Path(candidate).expanduser()
    if path.is_file():
        return str(path.resolve())
    raise Stage3RenderError(
        f"FluidSynth executable not found: {candidate}. Install FluidSynth or pass --fluidsynth."
    )


def _inspect_wav(path: Path) -> tuple[int, int, int, float]:
    if not path.is_file():
        raise Stage3RenderError("FluidSynth completed without producing final_mix.wav")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            sample_width = handle.getsampwidth()
    except (OSError, wave.Error) as exc:
        raise Stage3RenderError(f"rendered output is not a valid WAV: {exc}") from exc
    if channels < 1 or sample_rate < 8000 or frames < 1 or sample_width < 2:
        raise Stage3RenderError("rendered WAV has invalid audio dimensions")
    return sample_rate, channels, frames, frames / sample_rate


def _publish(staging: Path, destination: Path, force: bool) -> None:
    backup: Path | None = None
    try:
        if destination.exists():
            if not force:
                raise Stage3RenderError(f"output directory already exists: {destination}")
            backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if not destination.exists() and backup is not None and backup.exists():
            os.replace(backup, destination)
        raise


def render_complete_midi(
    input_midi: str | Path,
    general_soundfont: str | Path,
    heartbeat_soundfont: str | Path,
    output_dir: str | Path,
    *,
    fluidsynth: str | Path = "fluidsynth",
    heartbeat_channel: int = DEFAULT_HEARTBEAT_CHANNEL,
    heartbeat_notes: Sequence[int] = DEFAULT_HEARTBEAT_NOTES,
    sample_rate: int = 48_000,
    gain: float = 0.5,
    timeout_seconds: int = 600,
    force: bool = False,
    executor: Executor | None = None,
) -> RenderResult:
    """Render the immutable complete MIDI to one final WAV in one FluidSynth run."""

    if not 8_000 <= sample_rate <= 192_000:
        raise Stage3RenderError("sample_rate must be between 8000 and 192000")
    if not math.isfinite(gain) or not 0 < gain < 10:
        raise Stage3RenderError("gain must be finite and between 0 and 10")
    if timeout_seconds < 1:
        raise Stage3RenderError("timeout_seconds must be positive")

    midi_path = Path(input_midi).expanduser().resolve()
    general_path = Path(general_soundfont).expanduser().resolve()
    heartbeat_path = Path(heartbeat_soundfont).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    inspection, general_info, heartbeat_info = validate_render_inputs(
        midi_path,
        general_path,
        heartbeat_path,
        heartbeat_channel=heartbeat_channel,
        heartbeat_notes=heartbeat_notes,
    )
    input_hash_before = _sha256(midi_path)
    executable = str(fluidsynth) if executor is not None else _resolve_executable(fluidsynth)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    wav_path = staging / "final_mix.wav"
    manifest_path = staging / "stage3_render_manifest.json"
    # FluidSynth's Windows build can fail to open otherwise valid assets whose
    # absolute paths exceed the legacy path limit.  Fixed short runtime names
    # also make the backend call independent of user filenames.  These copies
    # are removed before publication; source hashes remain the audit authority.
    runtime_midi = staging / "complete.mid"
    runtime_general = staging / "general.sf2"
    runtime_heartbeat = staging / "heartbeat.sf2"
    shutil.copyfile(midi_path, runtime_midi)
    shutil.copyfile(general_path, runtime_general)
    shutil.copyfile(heartbeat_path, runtime_heartbeat)
    command = [
        executable,
        "-ni",
        "-q",
        "-F",
        str(wav_path),
        "-T",
        "wav",
        "-O",
        "s16",
        "-r",
        str(sample_rate),
        "-g",
        format(gain, ".6g"),
        str(runtime_general),
        str(runtime_heartbeat),
        str(runtime_midi),
    ]
    try:
        completed = (executor or _default_executor)(command, wav_path, timeout_seconds)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown renderer error").strip()
            raise Stage3RenderError(f"FluidSynth render failed: {detail[:1000]}")
        renderer_log = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        fatal_log_markers = (
            "not a soundfont or midi file",
            "fluid_is_soundfont(): fopen() failed",
            "failed to load soundfont",
            "error occurred identifying it",
        )
        if any(marker in renderer_log for marker in fatal_log_markers):
            detail = (completed.stderr or completed.stdout or "renderer asset load failure").strip()
            raise Stage3RenderError(
                "FluidSynth reported an asset load failure despite exit code 0: "
                f"{detail[:1000]}"
            )
        actual_rate, channels, frames, duration = _inspect_wav(wav_path)
        if actual_rate != sample_rate:
            raise Stage3RenderError(
                f"rendered WAV sample rate {actual_rate} does not match requested {sample_rate}"
            )
        input_hash_after = _sha256(midi_path)
        if input_hash_after != input_hash_before:
            raise Stage3RenderError("complete MIDI changed during Stage 3 rendering")
        manifest = {
            "schema_version": "1.0",
            "module_version": VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "stage": "stage3",
            "operation": "unified_whole_midi_render",
            "heartbeat_events_generated": False,
            "separate_heartbeat_wav_mixed": False,
            "input": {
                "complete_midi": str(midi_path),
                "complete_midi_sha256": input_hash_before,
                "general_soundfont": str(general_path),
                "general_soundfont_sha256": _sha256(general_path),
                "heartbeat_soundfont": str(heartbeat_path),
                "heartbeat_soundfont_sha256": _sha256(heartbeat_path),
            },
            "midi_inspection": inspection.as_dict(),
            "soundfonts": {
                "load_order": ["general", "heartbeat"],
                "general": general_info.as_dict(),
                "heartbeat": heartbeat_info.as_dict(),
                "heartbeat_channel_human": heartbeat_channel + 1,
                "heartbeat_allowed_notes": list(heartbeat_notes),
            },
            "renderer": {
                "backend": "fluidsynth_cli",
                "executable": executable,
                "sample_rate": sample_rate,
                "gain": gain,
                "single_process": True,
                "command": command,
                "stdout_tail": (completed.stdout or "")[-2000:],
                "stderr_tail": (completed.stderr or "")[-2000:],
            },
            "output": {
                "file": wav_path.name,
                "sha256": _sha256(wav_path),
                "sample_rate": actual_rate,
                "channels": channels,
                "frames": frames,
                "duration_s": duration,
            },
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        runtime_midi.unlink()
        runtime_general.unlink()
        runtime_heartbeat.unlink()
        _publish(staging, destination, force)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return RenderResult(
        output_dir=destination,
        output_wav=destination / wav_path.name,
        manifest_json=destination / manifest_path.name,
        duration_s=duration,
        sample_rate=actual_rate,
        channels=channels,
    )
