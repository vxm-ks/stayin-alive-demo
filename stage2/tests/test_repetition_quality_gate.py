from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

from stage2.repetition_quality_gate import (
    compare_windows,
    detect_repetitions,
    run_repetition_quality_gate,
)


class RepetitionQualityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_repeated_midi(self) -> tuple[Path, Path]:
        midi = MidiFile(type=1, ticks_per_beat=480)
        music = MidiTrack()
        ticks_per_bar = 1920
        patterns = ((60, 64, 67), (62, 65, 69)) * 3
        pending = 0
        for pitches in patterns:
            for index, pitch in enumerate(pitches):
                music.append(Message("note_on", channel=0, note=pitch, velocity=80, time=pending if index == 0 else 0))
                pending = 0
            for index, pitch in enumerate(pitches):
                music.append(Message("note_off", channel=0, note=pitch, velocity=0, time=240 if index == 0 else 0))
            pending = ticks_per_bar - 240
        music.append(MetaMessage("end_of_track", time=pending))
        drums = MidiTrack()
        for _ in range(6):
            drums.append(Message("note_on", channel=9, note=36, velocity=100, time=0))
            drums.append(Message("note_off", channel=9, note=36, velocity=0, time=120))
            drums.append(MetaMessage("marker", text="padding", time=ticks_per_bar - 120))
        drums.append(MetaMessage("end_of_track", time=0))
        midi.tracks.extend([music, drums])
        midi_path = self.root / "repeated.mid"
        midi.save(midi_path)
        plan_path = self.root / "stage2_plan.json"
        plan_path.write_text(json.dumps({
            "sections": [{
                "section_id": "S1", "bar_start": 1, "bar_end": 6,
                "editable_bar_ranges": [{"bar_start": 3, "bar_end": 6}],
            }]
        }), encoding="utf-8")
        return midi_path, plan_path

    def test_third_two_bar_occurrence_is_detected_without_track_classification(self) -> None:
        midi, plan = self._write_repeated_midi()
        issues, _ = detect_repetitions(midi, stage2_plan=plan)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["repeated_bars"], [5, 6])
        self.assertEqual(issues[0]["occurrence"], 3)
        self.assertTrue(issues[0]["editable"])

    def test_rhythm_is_not_an_independent_similarity_component(self) -> None:
        left = [{60, 64, 67}, {62, 65, 69}]
        unrelated_same_event_count = [{61, 66, 70}, {63, 68, 71}]
        result = compare_windows(left, unrelated_same_event_count)
        self.assertLess(result["absolute_similarity"], 0.5)

    def test_transposed_whole_texture_is_detected(self) -> None:
        left = [{60, 64, 67}, {62, 65, 69}]
        right = [{62, 66, 69}, {64, 67, 71}]
        result = compare_windows(left, right)
        self.assertEqual(result["transposition_invariant_similarity"], 1.0)
        self.assertEqual(result["best_transposition_semitones"], -2)

    def test_detect_mode_writes_audit_without_changing_midi(self) -> None:
        midi, plan = self._write_repeated_midi()
        before = midi.read_bytes()
        report_path = self.root / "report.json"
        report = run_repetition_quality_gate(
            midi, report_path, mode="detect", stage2_plan=plan,
            model_name="yellow", seed=42,
        )
        self.assertEqual(before, midi.read_bytes())
        self.assertEqual(report["initial_issue_count"], 1)
        self.assertFalse(report["rhythm_scored"])
        self.assertTrue(report_path.is_file())

    def test_regenerate_requires_explicit_editable_plan(self) -> None:
        midi, _ = self._write_repeated_midi()
        with self.assertRaisesRegex(ValueError, "requires stage2_plan"):
            run_repetition_quality_gate(
                midi, self.root / "report.json", mode="regenerate", stage2_plan=None,
                model_name="yellow", seed=42,
            )


if __name__ == "__main__":
    unittest.main()
