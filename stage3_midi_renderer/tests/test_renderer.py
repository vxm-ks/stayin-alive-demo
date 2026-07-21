from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from stage3_midi_renderer.cli import build_parser
from stage3_midi_renderer.renderer import (
    Stage3RenderError,
    inspect_complete_midi,
    render_complete_midi,
    validate_render_inputs,
)


def vlq(value: int) -> bytes:
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(parts))


def track(events: list[tuple[int, bytes]]) -> bytes:
    payload = b"".join(vlq(delta) + event for delta, event in events)
    return b"MTrk" + struct.pack(">I", len(payload)) + payload


def midi_bytes(*, heartbeat_note: int = 36) -> bytes:
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 3, 480)
    conductor = track([
        (0, b"\xff\x51\x03\x07\xa1\x20"),
        (0, b"\xff\x58\x04\x04\x02\x18\x08"),
        (960, b"\xff\x2f\x00"),
    ])
    music = track([
        (0, b"\xc0\x00"),
        (0, b"\x90\x3c\x64"),
        (480, b"\x80\x3c\x00"),
        (480, b"\xff\x2f\x00"),
    ])
    heartbeat = track([
        (0, bytes([0x99, heartbeat_note, 100])),
        (120, bytes([0x89, heartbeat_note, 0])),
        (360, b"\x99\x26\x55"),
        (120, b"\x89\x26\x00"),
        (360, b"\xff\x2f\x00"),
    ])
    return header + conductor + music + heartbeat


def forty_eight_bar_midi_bytes() -> bytes:
    """Build a complete 4/4 MIDI spanning the Stage 1 test plan's 48 bars."""
    ppq = 480
    end_tick = 48 * 4 * ppq
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 3, ppq)
    conductor = track([
        (0, b"\xff\x51\x03\x09\x89\x68"),
        (0, b"\xff\x58\x04\x04\x02\x18\x08"),
        (end_tick, b"\xff\x2f\x00"),
    ])
    music = track([
        (0, b"\xc0\x00"),
        (0, b"\x90\x3c\x64"),
        (480, b"\x80\x3c\x00"),
        (end_tick - 480, b"\xff\x2f\x00"),
    ])
    heartbeat = track([
        (0, b"\x99\x24\x64"),
        (120, b"\x89\x24\x00"),
        (end_tick - 600, b"\x99\x26\x55"),
        (120, b"\x89\x26\x00"),
        (360, b"\xff\x2f\x00"),
    ])
    return header + conductor + music + heartbeat


def riff_chunk(tag: bytes, payload: bytes) -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) & 1 else b"")


def soundfont_bytes(presets: list[tuple[str, int, int]]) -> bytes:
    records = []
    for index, (name, program, bank) in enumerate(presets):
        encoded = name.encode("ascii")[:19] + b"\x00"
        encoded = encoded.ljust(20, b"\x00")
        records.append(struct.pack("<20sHHHIII", encoded, program, bank, index, 0, 0, 0))
    records.append(struct.pack("<20sHHHIII", b"EOP\x00".ljust(20, b"\x00"), 0, 0, len(presets), 0, 0, 0))
    pdta = b"pdta" + riff_chunk(b"phdr", b"".join(records))
    payload = b"sfbk" + riff_chunk(b"LIST", pdta)
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


class Stage3RendererTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.midi = self.root / "complete.mid"
        self.general = self.root / "general.sf2"
        self.heartbeat = self.root / "heartbeat.sf2"
        self.midi.write_bytes(midi_bytes())
        self.general.write_bytes(soundfont_bytes([("Piano", 0, 0), ("GM Drums", 0, 128)]))
        self.heartbeat.write_bytes(soundfont_bytes([("Heartbeat Perc", 0, 128)]))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_midi_requires_music_and_existing_heartbeat(self):
        result = inspect_complete_midi(self.midi)
        self.assertEqual(result.track_count, 3)
        self.assertEqual(result.music_note_on_count, 1)
        self.assertEqual(result.heartbeat_note_on_count, 2)
        self.assertEqual(result.heartbeat_notes, (36, 38))

    def test_accepts_complete_forty_eight_bar_stage1_test_form(self):
        self.midi.write_bytes(forty_eight_bar_midi_bytes())
        result = inspect_complete_midi(self.midi)
        self.assertEqual(result.end_tick, 48 * 4 * 480)
        self.assertEqual(result.music_note_on_count, 1)
        self.assertEqual(result.heartbeat_note_on_count, 2)
        self.assertEqual(result.heartbeat_notes, (36, 38))

    def test_heartbeat_channel_rejects_non_heartbeat_note(self):
        self.midi.write_bytes(midi_bytes(heartbeat_note=42))
        with self.assertRaisesRegex(Stage3RenderError, "unsupported note 42"):
            inspect_complete_midi(self.midi)

    def test_heartbeat_soundfont_must_be_percussion_only(self):
        self.heartbeat.write_bytes(
            soundfont_bytes([("Heartbeat Keys", 0, 0), ("Heartbeat Perc", 0, 128)])
        )
        with self.assertRaisesRegex(Stage3RenderError, "only percussion"):
            validate_render_inputs(self.midi, self.general, self.heartbeat)

    def test_render_is_one_whole_midi_pass_and_audited(self):
        commands: list[list[str]] = []

        def fake_executor(command, output_path, timeout_seconds):
            commands.append(list(command))
            self.assertEqual(timeout_seconds, 600)
            with wave.open(str(output_path), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(48_000)
                handle.writeframes(b"\x00\x00\x00\x00" * 480)
            return subprocess.CompletedProcess(command, 0, "rendered", "")

        before = self.midi.read_bytes()
        result = render_complete_midi(
            self.midi,
            self.general,
            self.heartbeat,
            self.root / "output",
            fluidsynth="fake-fluidsynth",
            executor=fake_executor,
        )
        self.assertEqual(self.midi.read_bytes(), before)
        self.assertEqual(len(commands), 1)
        general_index = next(index for index, value in enumerate(commands[0]) if value.endswith("general.sf2"))
        heartbeat_index = next(index for index, value in enumerate(commands[0]) if value.endswith("heartbeat.sf2"))
        self.assertLess(general_index, heartbeat_index)
        self.assertTrue(commands[0][-1].endswith("complete.mid"))
        self.assertTrue(result.output_wav.is_file())
        files = sorted(path.name for path in result.output_dir.iterdir())
        self.assertEqual(files, ["final_mix.wav", "stage3_render_manifest.json"])
        manifest = json.loads(result.manifest_json.read_text(encoding="utf-8"))
        self.assertEqual(manifest["operation"], "unified_whole_midi_render")
        self.assertFalse(manifest["heartbeat_events_generated"])
        self.assertFalse(manifest["separate_heartbeat_wav_mixed"])
        self.assertEqual(manifest["soundfonts"]["load_order"], ["general", "heartbeat"])

    def test_cli_contract_uses_complete_midi_and_two_soundfonts(self):
        args = build_parser().parse_args([
            "render",
            "--input-midi", "complete.mid",
            "--general-sf2", "general.sf2",
            "--heartbeat-sf2", "heartbeat.sf2",
        ])
        self.assertEqual(args.command, "render")
        self.assertEqual(args.heartbeat_channel, 10)
        self.assertEqual(args.sample_rate, 48_000)

        conditioning = build_parser().parse_args([
            "condition-heartbeat",
            "--input-wav", "s1.wav",
        ])
        self.assertEqual(conditioning.profile, "speaker_safe_loud_v1")
        self.assertIsNone(conditioning.output_dir)

    def test_fluidsynth_soft_asset_failure_is_rejected(self):
        def soft_failure(command, output_path, timeout_seconds):
            del timeout_seconds
            with wave.open(str(output_path), "wb") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(48_000)
                handle.writeframes(b"\x00\x00\x00\x00" * 10)
            return subprocess.CompletedProcess(
                command,
                0,
                "",
                "Parameter 'heartbeat.sf2' not a SoundFont or MIDI file or error occurred identifying it.",
            )

        with self.assertRaisesRegex(Stage3RenderError, "exit code 0"):
            render_complete_midi(
                self.midi,
                self.general,
                self.heartbeat,
                self.root / "soft-failure",
                fluidsynth="fake-fluidsynth",
                executor=soft_failure,
            )


if __name__ == "__main__":
    unittest.main()
