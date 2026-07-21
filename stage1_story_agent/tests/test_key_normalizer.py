from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from stage1_story_agent.cli import main
from stage1_story_agent.key_normalizer import (
    KEY_SIGNATURE_SF,
    MAJOR_PROFILE,
    KeyNormalizationError,
    default_output_path,
    detect_key_from_profile,
    normalize_midi_key,
)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def _key_signature(sf: int, mode: int) -> bytes:
    return b"\x00\xff\x59\x02" + bytes((sf & 0xFF, mode))


def _midi_in_key(
    sf: int,
    mode: int,
    *,
    pitches: tuple[int, ...] = (60, 64),
    include_drums: bool = True,
) -> bytes:
    tracks = 3 if include_drums else 2
    header = _chunk(b"MThd", struct.pack(">HHH", 1, tracks, 96))
    conductor = _key_signature(sf, mode) + b"\x00\xff\x2f\x00"
    melody = bytearray()
    for pitch in pitches:
        melody.extend(bytes((0, 0x90, pitch, 100, 96, 0x80, pitch, 0)))
    melody.extend(b"\x00\xff\x2f\x00")
    chunks = [_chunk(b"MTrk", conductor), _chunk(b"MTrk", bytes(melody))]
    if include_drums:
        drums = b"\x00\x99\x24\x64\x60\x89\x24\x00\x00\xff\x2f\x00"
        chunks.append(_chunk(b"MTrk", drums))
    return header + b"".join(chunks)


class KeyNormalizerTests(unittest.TestCase):
    def test_transposes_non_drum_notes_and_rewrites_key_signature(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "c-major.mid"
            output = Path(root) / "d-major.mid"
            source.write_bytes(_midi_in_key(0, 0))

            result = normalize_midi_key(source, output, "D", "major")
            normalized = output.read_bytes()

            self.assertEqual(result.source_tonic, "C")
            self.assertEqual(result.detection_method, "key_signature")
            self.assertEqual(result.transpose_semitones, 2)
            self.assertEqual(result.melodic_note_messages_rewritten, 4)
            self.assertEqual(result.drum_note_messages_preserved, 2)
            self.assertEqual(result.melodic_pitch_range_before, (60, 64))
            self.assertEqual(result.melodic_pitch_range_after, (62, 66))
            self.assertIn(b"\x00\x90\x3e\x64\x60\x80\x3e\x00", normalized)
            self.assertIn(b"\x00\x90\x42\x64\x60\x80\x42\x00", normalized)
            self.assertIn(b"\x00\x99\x24\x64\x60\x89\x24\x00", normalized)
            d_major = bytes((KEY_SIGNATURE_SF["major"]["D"] & 0xFF, 0))
            self.assertEqual(normalized.count(b"\xff\x59\x02" + d_major), 2)
            self.assertNotEqual(result.input_sha256, result.output_sha256)

    def test_profile_detector_finds_unambiguous_c_major(self):
        detected = detect_key_from_profile(MAJOR_PROFILE)
        self.assertEqual((detected.tonic, detected.mode), ("C", "major"))
        self.assertEqual(detected.method, "pitch_profile")
        self.assertGreater(detected.profile_score or 0, 0.99)

    def test_mode_mismatch_fails_closed_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            source.write_bytes(_midi_in_key(0, 0))

            with self.assertRaisesRegex(KeyNormalizationError, "cannot correct mode"):
                normalize_midi_key(source, output, "D", "minor")
            self.assertFalse(output.exists())

    def test_out_of_range_transposition_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            source.write_bytes(_midi_in_key(5, 0, pitches=(127,), include_drums=False))

            with self.assertRaisesRegex(KeyNormalizationError, "outside MIDI"):
                normalize_midi_key(source, output, "C#", "major")
            self.assertFalse(output.exists())

    def test_cli_reads_target_key_from_content_plan(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            plan = Path(root) / "content_plan.json"
            source.write_bytes(_midi_in_key(0, 0))
            plan.write_text(
                json.dumps(
                    {"global": {"global_tonality": {"tonic": "D", "mode": "major"}}}
                ),
                encoding="utf-8",
            )

            code = main(
                [
                    "normalize-key",
                    "--input-midi",
                    str(source),
                    "--content-plan",
                    str(plan),
                    "--output-midi",
                    str(output),
                ]
            )

            self.assertEqual(code, 0)
            self.assertTrue(output.exists())
            self.assertIn(b"\x00\x90\x3e\x64", output.read_bytes())

    def test_source_override_must_be_complete(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            source.write_bytes(_midi_in_key(0, 0))
            with self.assertRaisesRegex(KeyNormalizationError, "supplied together"):
                normalize_midi_key(
                    source,
                    Path(root) / "output.mid",
                    "D",
                    "major",
                    source_tonic="C",
                )

    def test_existing_output_requires_force_and_default_name_is_adjacent(self):
        self.assertEqual(
            default_output_path(Path("motif.mid")),
            Path("motif.key-normalized.mid"),
        )
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.mid"
            output = Path(root) / "output.mid"
            source.write_bytes(_midi_in_key(0, 0))
            output.write_bytes(b"keep")
            with self.assertRaisesRegex(KeyNormalizationError, "--force"):
                normalize_midi_key(source, output, "D", "major")
            self.assertEqual(output.read_bytes(), b"keep")
            normalize_midi_key(source, output, "D", "major", force=True)
            self.assertNotEqual(output.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
