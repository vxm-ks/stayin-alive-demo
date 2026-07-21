from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.backends import FakeBackend
from stage1_story_agent.cli import main
from stage1_story_agent.tempo_normalizer import (
    TempoNormalizationError,
    bpm_to_microseconds,
    default_output_path,
    normalize_midi_tempo,
)
from stage1_story_agent.tests.fixtures import draft_data, make_request


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def _midi_with_tempo_map() -> bytes:
    header = _chunk(b"MThd", struct.pack(">HHH", 1, 2, 480))
    # Tempo changes from 120 BPM at tick 0 to 90 BPM at tick 480.
    conductor = (
        b"\x00\xff\x51\x03\x07\xa1\x20"
        b"\x83\x60\xff\x51\x03\x0a\x2c\x2b"
        b"\x00\xff\x2f\x00"
    )
    notes = (
        b"\x00\x90\x3c\x64"
        b"\x83\x60\x80\x3c\x00"
        b"\x00\xff\x2f\x00"
    )
    return header + _chunk(b"MTrk", conductor) + _chunk(b"MTrk", notes)


def _format_zero_midi_without_tempo() -> bytes:
    header = _chunk(b"MThd", struct.pack(">HHH", 0, 1, 96))
    notes = b"\x00\x90\x3c\x64\x60\x80\x3c\x00\x00\xff\x2f\x00"
    return header + _chunk(b"MTrk", notes)


class TempoNormalizerTests(unittest.TestCase):
    def test_rewrites_all_tempos_and_inserts_tick_zero_tempo(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "musecoco.mid"
            output = Path(root) / "normalized.mid"
            source.write_bytes(_midi_with_tempo_map())

            result = normalize_midi_tempo(source, output, 96)
            normalized = output.read_bytes()
            target = bpm_to_microseconds(96).to_bytes(3, "big")

            self.assertEqual(result.tempo_events_rewritten, 2)
            self.assertEqual(result.track_count, 2)
            self.assertEqual(result.ticks_per_beat, 480)
            self.assertAlmostEqual(result.encoded_bpm, 96.0)
            self.assertEqual(normalized.count(b"\xff\x51\x03" + target), 3)
            self.assertNotIn(b"\xff\x51\x03\x07\xa1\x20", normalized)
            self.assertNotIn(b"\xff\x51\x03\x0a\x2c\x2b", normalized)
            self.assertIn(b"\x00\x90\x3c\x64\x83\x60\x80\x3c\x00", normalized)

    def test_existing_output_requires_force(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            source.write_bytes(_midi_with_tempo_map())
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(TempoNormalizationError, "--force"):
                normalize_midi_tempo(source, output, 100)
            self.assertEqual(output.read_bytes(), b"keep")

            normalize_midi_tempo(source, output, 100, force=True)
            self.assertNotEqual(output.read_bytes(), b"keep")

    def test_adds_tempo_to_format_zero_file_without_tempo(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            source.write_bytes(_format_zero_midi_without_tempo())

            result = normalize_midi_tempo(source, output, 120)

            self.assertEqual(result.midi_format, 0)
            self.assertEqual(result.tempo_events_rewritten, 0)
            self.assertEqual(
                output.read_bytes().count(b"\xff\x51\x03\x07\xa1\x20"), 1
            )

    def test_rejects_invalid_bpm_and_non_midi(self):
        with self.assertRaises(TempoNormalizationError):
            bpm_to_microseconds(float("nan"))
        with self.assertRaises(TempoNormalizationError):
            bpm_to_microseconds(241)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "bad.mid"
            source.write_bytes(b"not-midi")
            with self.assertRaisesRegex(TempoNormalizationError, "Standard MIDI"):
                normalize_midi_tempo(source, Path(root) / "out.mid", 96)

    def test_default_output_is_adjacent_and_non_destructive(self):
        source = Path("motif.mid")
        self.assertEqual(default_output_path(source), Path("motif.tempo-normalized.mid"))

    def test_cli_reads_target_bpm_from_content_plan(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "motif.mid"
            output = Path(root) / "motif-fixed.mid"
            plan_path = Path(root) / "content_plan.json"
            source.write_bytes(_midi_with_tempo_map())
            backend = FakeBackend([json.dumps(draft_data(), ensure_ascii=False)])
            plan = Stage1StoryAgent(backend).plan(make_request()).content_plan
            plan_path.write_text(
                plan.model_dump_json(by_alias=True, indent=2), encoding="utf-8"
            )

            code = main(
                [
                    "normalize-tempo",
                    "--input-midi",
                    str(source),
                    "--content-plan",
                    str(plan_path),
                    "--output-midi",
                    str(output),
                ]
            )

            self.assertEqual(code, 0)
            target = bpm_to_microseconds(96).to_bytes(3, "big")
            self.assertEqual(output.read_bytes().count(b"\xff\x51\x03" + target), 3)


if __name__ == "__main__":
    unittest.main()
