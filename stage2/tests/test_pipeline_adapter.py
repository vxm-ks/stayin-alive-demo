from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mido import MidiFile

from stage2.combine_midi_with_drums import build_heartbeat_track_from_plan
from stage2.run_pipeline_relative import load_stage2_plan


def make_plan(bpm: float = 96.0) -> dict:
    gap = 28 if bpm > 100 else 12
    sections = []
    start = 1
    for index in range(3):
        end = start + 4 + gap - 1
        sections.append({
            "section_id": f"S{index + 1}", "bar_start": start, "bar_end": end,
            "input_motif_bars": 4, "target_section_bars": 4 + gap,
            "extension_bars": gap, "extension_method": "midigpt_extend",
            "tempo_bpm": bpm, "source_tension": 0.8 if index == 1 else 0.3,
            "tension_level": "high" if index == 1 else "low",
            "drum_pattern": {"pattern": "single_pulse_per_bar", "time_signature": "4/4", "kick_beats": [1.0], "snare_beats": [], "closed_hihat_beats": []},
            "midigpt_access": "extension_only",
            "protected_bar_ranges": [{"bar_start": start, "bar_end": start + 3}],
            "editable_bar_ranges": [{"bar_start": start + 4, "bar_end": end}],
        })
        start = end + 1
    return {"schema_version": "0.2-draft", "story_id": "story-1", "total_bars": start - 1, "form_string": "A-B-A", "time_signature": "4/4", "bar_numbering": "one_based_closed", "drum_track_scope": "instructions_only", "sections": sections}


class PipelineAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_plan(self, data: dict) -> Path:
        path = self.root / "stage2_plan.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_plan_maps_to_existing_slow_tempo_layout(self) -> None:
        settings = load_stage2_plan(self.write_plan(make_plan()), self.root)
        self.assertEqual(settings.gap_bars, 12)
        self.assertEqual(settings.total_bars, 48)

    def test_plan_with_unprotected_motif_is_rejected(self) -> None:
        data = make_plan()
        data["sections"][0]["protected_bar_ranges"] = []
        with self.assertRaisesRegex(ValueError, "protect"):
            load_stage2_plan(self.write_plan(data), self.root)

    def test_low_tension_has_s1_s2_and_high_tension_has_s1(self) -> None:
        path = self.write_plan(make_plan())
        track = build_heartbeat_track_from_plan(path, 48, 4, 4, 480)
        midi = MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(track)
        output = self.root / "heartbeat.mid"
        midi.save(output)
        notes = [message.note for message in MidiFile(output).tracks[0] if message.type == "note_on" and message.velocity]
        self.assertIn(36, notes)
        self.assertIn(38, notes)
        self.assertEqual(notes[:2], [36, 38])


if __name__ == "__main__":
    unittest.main()
