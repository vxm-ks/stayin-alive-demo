from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from stage1_story_agent.examples.generate_modern_danceable import build_delivery
from stage1_story_agent.musecoco_postprocess import (
    MuseCocoPostprocessError,
    finalize_musecoco_results,
)
from stage1_story_agent.utils import json_bytes


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def _vlq(value: int) -> bytes:
    parts = [value & 0x7F]
    value >>= 7
    while value:
        parts.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(parts))


def _midi(end_tick: int) -> bytes:
    header = _chunk(b"MThd", struct.pack(">HHH", 0, 1, 96))
    track = (
        b"\x00\xFF\x58\x04\x04\x02\x18\x08"
        b"\x00\xFF\x59\x02\x00\x00"
        b"\x00\xFF\x51\x03\x07\xA1\x20"
        b"\x00\x90\x3C\x64"
        + _vlq(end_tick)
        + b"\x80\x3C\x00\x00\xFF\x2F\x00"
    )
    return header + _chunk(b"MTrk", track)


def _ambiguous_midi(end_tick: int) -> bytes:
    header = _chunk(b"MThd", struct.pack(">HHH", 0, 1, 96))
    events = bytearray(
        b"\x00\xFF\x58\x04\x04\x02\x18\x08"
        b"\x00\xFF\x51\x03\x07\xA1\x20"
    )
    elapsed = 0
    for pitch in range(60, 72):
        events.extend(b"\x00\x90" + bytes((pitch, 100)))
        events.extend(_vlq(96) + b"\x80" + bytes((pitch, 0)))
        elapsed += 96
    events.extend(_vlq(end_tick - elapsed) + b"\xFF\x2F\x00")
    return header + _chunk(b"MTrk", bytes(events))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare(root: Path, *, end_tick: int, midi_bytes: bytes | None = None) -> Path:
    musecoco = root / "musecoco"
    musecoco.mkdir()
    delivery = build_delivery()
    (musecoco / "musecoco_plan.json").write_bytes(json_bytes(delivery))
    result_dir = musecoco / "raw_results" / "task_001_theme-a"
    result_dir.mkdir(parents=True)
    (result_dir / "raw.mid").write_bytes(midi_bytes or _midi(end_tick))
    (result_dir / "result.remi.txt").write_text("sample", encoding="utf-8")
    (result_dir / "source_result_audit.json").write_bytes(
        json_bytes({"status": "success"})
    )
    files = {
        name: _sha256(result_dir / name)
        for name in ("raw.mid", "result.remi.txt", "source_result_audit.json")
    }
    manifest = {
        "schema_version": "musecoco-result-collection-v1",
        "items": [
            {
                "theme_family_id": "theme-A",
                "task_id": "task_001_theme-a",
                "files": files,
            }
        ],
    }
    (musecoco / "raw_results" / "collection_manifest.json").write_bytes(
        json_bytes(manifest)
    )
    return musecoco


class MuseCocoPostprocessTests(unittest.TestCase):
    def test_long_result_is_published_as_exact_eight_bar_final_midi(self):
        with tempfile.TemporaryDirectory() as temporary:
            musecoco = _prepare(Path(temporary), end_tick=12 * 4 * 96)
            result = finalize_musecoco_results(musecoco)
            self.assertEqual(result.theme_count, 1)
            final_midi = result.generated_themes_dir / "theme-A" / "final.mid"
            self.assertTrue(final_midi.is_file())
            audit = json.loads(
                (result.generated_themes_dir / "theme-A" / "normalization_audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(audit["generation_target_bars"], 12)
            self.assertEqual(audit["output_motif_bars"], 8)
            self.assertEqual(audit["bars"]["target_end_tick"], 8 * 4 * 96)
            self.assertTrue(audit["bars"]["trimmed_to_target"])
            self.assertEqual(audit["bars"]["input_midi"], "raw.mid")
            self.assertEqual(audit["bars"]["output_midi"], "bars-normalized.mid")
            self.assertEqual(audit["tempo"]["output_midi"], "tempo-normalized.mid")
            self.assertEqual(audit["key"]["output_midi"], "final.mid")
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["generation_target_bars"], 12)
            self.assertEqual(manifest["output_motif_bars"], 8)

    def test_loose_tonality_policy_preserves_generated_midi(self):
        with tempfile.TemporaryDirectory() as temporary:
            musecoco = _prepare(Path(temporary), end_tick=12 * 4 * 96)
            plan_path = musecoco / "musecoco_plan.json"
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["global_tonality"] = {
                "tonic": "C", "mode": "minor", "rationale": "test mismatch"
            }
            plan["theme_families"][0]["musecoco_attribute_targets"]["K1"] = "minor"
            plan_path.write_bytes(json_bytes(plan))

            result = finalize_musecoco_results(musecoco, tonality_policy="loose")
            theme = result.generated_themes_dir / "theme-A"
            audit = json.loads((theme / "normalization_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["tonality_policy"], "loose")
            self.assertFalse(audit["key"]["applied"])
            self.assertEqual(
                (theme / "tempo-normalized.mid").read_bytes(),
                (theme / "final.mid").read_bytes(),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tonality_policy"], "loose")

    def test_soft_tonality_policy_preserves_ambiguous_midi_and_publishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            end_tick = 12 * 4 * 96
            musecoco = _prepare(
                Path(temporary),
                end_tick=end_tick,
                midi_bytes=_ambiguous_midi(end_tick),
            )
            result = finalize_musecoco_results(musecoco, tonality_policy="soft")
            theme = result.generated_themes_dir / "theme-A"
            audit = json.loads(
                (theme / "normalization_audit.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["tonality_policy"], "soft")
            self.assertFalse(audit["key"]["applied"])
            self.assertEqual(
                audit["key"]["reason"],
                "key_normalization_not_reliably_applicable",
            )
            self.assertIn("ambiguous", audit["key"]["warning"])
            self.assertEqual(
                (theme / "tempo-normalized.mid").read_bytes(),
                (theme / "final.mid").read_bytes(),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tonality_policy"], "soft")

    def test_short_result_errors_without_publishing_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            musecoco = _prepare(Path(temporary), end_tick=7 * 4 * 96)
            with self.assertRaisesRegex(MuseCocoPostprocessError, "shorter"):
                finalize_musecoco_results(musecoco)
            self.assertFalse((musecoco / "generated_themes").exists())


if __name__ == "__main__":
    unittest.main()
