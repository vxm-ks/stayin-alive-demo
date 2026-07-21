from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from stage1_story_agent.bar_normalizer import (
    BarNormalizationError,
    normalize_midi_bars,
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def _vlq(value: int) -> bytes:
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(parts))


def _midi(note_end_tick: int, end_tick: int) -> bytes:
    header = _chunk(b"MThd", struct.pack(">HHH", 0, 1, 96))
    track = (
        b"\x00\xFF\x58\x04\x04\x02\x18\x08"
        b"\x00\x90\x3C\x64"
        + _vlq(note_end_tick)
        + b"\x80\x3C\x00"
        + _vlq(end_tick - note_end_tick)
        + b"\xFF\x2F\x00"
    )
    return header + _chunk(b"MTrk", track)


def _track_end_tick(midi: bytes) -> int:
    header_length = struct.unpack(">I", midi[4:8])[0]
    position = 8 + header_length
    length = struct.unpack(">I", midi[position + 4 : position + 8])[0]
    data = midi[position + 8 : position + 8 + length]
    cursor = 0
    tick = 0
    running = None
    while cursor < len(data):
        delta = 0
        while True:
            byte = data[cursor]
            cursor += 1
            delta = (delta << 7) | (byte & 0x7F)
            if byte < 0x80:
                break
        tick += delta
        status = data[cursor]
        if status >= 0x80:
            cursor += 1
            if status < 0xF0:
                running = status
        else:
            status = running
        if status == 0xFF:
            meta_type = data[cursor]
            cursor += 1
            size = data[cursor]
            cursor += 1 + size
            if meta_type == 0x2F:
                return tick
        elif status in (0xF0, 0xF7):
            size = data[cursor]
            cursor += 1 + size
        else:
            cursor += 1 if status & 0xF0 in (0xC0, 0xD0) else 2
    raise AssertionError("missing end-of-track")


class BarNormalizerTests(unittest.TestCase):
    def test_trims_to_bar_boundary_and_closes_active_note(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "long.mid"
            output = Path(root) / "one-bar.mid"
            source.write_bytes(_midi(note_end_tick=480, end_tick=768))
            result = normalize_midi_bars(source, output, 1, "4/4")
            self.assertEqual(result.target_end_tick, 384)
            self.assertEqual(result.input_end_tick, 768)
            self.assertEqual(result.events_trimmed, 1)
            self.assertEqual(result.note_offs_inserted, 1)
            self.assertTrue(result.trimmed_to_target)
            self.assertEqual(_track_end_tick(output.read_bytes()), 384)
            self.assertIn(b"\x80\x3C\x00\x00\xFF\x2F\x00", output.read_bytes())

    def test_short_file_is_rejected_without_output(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "short.mid"
            output = Path(root) / "two-bars.mid"
            source.write_bytes(_midi(note_end_tick=96, end_tick=96))
            with self.assertRaisesRegex(BarNormalizationError, "shorter"):
                normalize_midi_bars(source, output, 2, "4/4")
            self.assertFalse(output.exists())

    def test_rejects_invalid_input_and_existing_output(self):
        with tempfile.TemporaryDirectory() as root:
            bad = Path(root) / "bad.mid"
            bad.write_bytes(b"not-midi")
            with self.assertRaisesRegex(BarNormalizationError, "Standard MIDI"):
                normalize_midi_bars(bad, Path(root) / "out.mid", 4, "4/4")

            source = Path(root) / "source.mid"
            output = Path(root) / "existing.mid"
            source.write_bytes(_midi(note_end_tick=96, end_tick=384))
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(BarNormalizationError, "--force"):
                normalize_midi_bars(source, output, 1, "4/4")
            self.assertEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
