from __future__ import annotations

import csv
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

MODULE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = MODULE_DIR.parent
for path in (str(MODULE_DIR), str(WORKSPACE_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import heartbeat_stage1 as stage1
from tempo_bar_renderer import tempo_bar_renderer


def base_plan() -> dict:
    return {
        "schema_version": "1.0",
        "plan_id": "test_even",
        "meter": "4/4",
        "target_bpm": 120,
        "pattern_length_beats": 1,
        "repeat_to_fill_bar": True,
        "event_bank_mode": "paired_rotate",
        "midi_note_duration_beats": 0.5,
        "events": [
            {"type": "S1", "offset_beats": 0.0, "gain_db": 0.0},
            {"type": "S2", "offset_beats": 0.5, "gain_db": -2.0},
        ],
        "audio_policy": {
            "time_stretch": False,
            "pitch_shift": False,
            "preserve_real_timbre": True,
        },
    }


def exemplar(root: Path, event_type: str, cycle: int, amplitude: float):
    rate = 1000
    length = 170 if event_type == "S1" else 140
    time = np.arange(length) / rate
    raw = amplitude * np.sin(2 * np.pi * (65 if event_type == "S1" else 90) * time)
    raw *= np.hanning(length)
    return tempo_bar_renderer.ClipExemplar(
        event_type=event_type,
        cycle_index=cycle,
        source_path=root / "processed.wav",
        source_time_s=1.0 + cycle + (0.3 if event_type == "S2" else 0.0),
        sample_rate=rate,
        raw=raw,
        alignment_offset_samples=10,
        detected_peak_offset_samples=30,
        refined_onset_offset_samples=10,
        onset_shift_ms=-20.0,
        boundary_jump_abs=0.0,
        clip_peak=float(np.max(np.abs(raw))),
        clip_rms=float(np.sqrt(np.mean(raw * raw))),
        quality_score=0.95 - 0.01 * cycle,
        confidence=0.9,
        amplitude=amplitude,
    )


class StageOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processed = self.root / "case_regularized_events_enhanced.wav"
        wavfile.write(self.processed, 1000, np.zeros(3000, dtype=np.int16))
        self.prepared = tempo_bar_renderer.PreparedInput(
            input_path=self.processed,
            input_sha256=stage1.sha256_file(self.processed),
            prefix="case_regularized_events_enhanced",
            input_variant="regularized_events_enhanced",
            sample_rate=1000,
            duration_s=3.0,
            result=SimpleNamespace(
                bpm=120.0,
                s1_s2_s=0.25,
                confidence=0.94,
                periodicity=0.88,
            ),
            bank={
                "S1": [exemplar(self.root, "S1", 0, 0.7), exemplar(self.root, "S1", 1, 0.6)],
                "S2": [exemplar(self.root, "S2", 0, 0.5), exemplar(self.root, "S2", 1, 0.45)],
            },
            recommendation=SimpleNamespace(),
            period_correction={},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_plan_expands_even_halfbeat_pattern(self):
        plan = stage1.validate_plan(base_plan())
        events, warnings = stage1.expand_plan(plan)
        self.assertEqual([event["type"] for event in events], ["S1", "S2"] * 4)
        self.assertEqual([event["time_beats"] for event in events], [index * 0.5 for index in range(8)])
        self.assertEqual(warnings, [])
        self.assertFalse(plan["audio_policy"]["time_stretch"])
        self.assertEqual(stage1.validate_plan(plan), plan)

    def test_seconds_offset_and_arbitrary_meter_are_supported(self):
        plan = base_plan()
        plan["meter"] = {"numerator": 3, "denominator": 4}
        plan["target_bpm"] = 60
        plan["events"][1].pop("offset_beats")
        plan["events"][1]["offset_seconds"] = 0.34
        canonical = stage1.validate_plan(plan)
        events, _ = stage1.expand_plan(canonical)
        self.assertEqual(len(events), 6)
        self.assertAlmostEqual(events[1]["time_s"], 0.34)
        self.assertAlmostEqual(events[3]["time_s"], 1.34)

    def test_invalid_or_timbre_changing_plan_is_rejected(self):
        plan = base_plan()
        plan["audio_policy"]["time_stretch"] = True
        with self.assertRaisesRegex(ValueError, "time stretching"):
            stage1.validate_plan(plan)
        plan = base_plan()
        plan["events"][1]["offset_beats"] = 0.05
        with self.assertRaisesRegex(ValueError, "minimum allowed"):
            stage1.validate_plan(plan)

    @patch.object(stage1.tempo_renderer, "inspect_wav")
    def test_package_contains_real_events_midi_plots_and_audit(self, inspect_mock):
        inspect_mock.return_value = self.prepared
        before = (stage1.sha256_file(self.processed), self.processed.stat().st_mtime_ns)
        package = stage1.build_heartbeat_package(
            self.processed,
            stage1.validate_plan(base_plan()),
            self.root / "heartbeat_package",
        )
        expected = {
            "heartbeat_bar.wav",
            "heartbeat_bar.mid",
            "heartbeat_events.csv",
            "heartbeat_bar_time_energy.png",
            "heartbeat_bar_time_energy.svg",
            "rhythm_plan.json",
            "heartbeat_manifest.json",
        }
        self.assertTrue(expected.issubset({path.name for path in package.iterdir()}))
        self.assertEqual((stage1.sha256_file(self.processed), self.processed.stat().st_mtime_ns), before)
        with (package / "heartbeat_events.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 8)
        self.assertEqual([row["event_type"] for row in rows], ["S1", "S2"] * 4)
        self.assertEqual([int(row["midi_tick"]) for row in rows], [index * 240 for index in range(8)])
        midi = (package / "heartbeat_bar.mid").read_bytes()
        self.assertEqual(midi[:4], b"MThd")
        self.assertEqual(struct.unpack(">HHH", midi[8:14]), (1, 2, 480))
        self.assertEqual(midi.count(bytes([0x99, 36, 100])), 4)
        self.assertEqual(midi.count(bytes([0x99, 38, 85])), 4)
        for repetition in range(4):
            cycle_ids = {
                int(row["source_cycle_index"])
                for row in rows
                if int(row["pattern_repetition"]) == repetition
            }
            self.assertEqual(len(cycle_ids), 1)
        manifest = json.loads((package / "heartbeat_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["timing"]["event_count"], 8)
        self.assertEqual(manifest["timbre_policy"]["source_variant"], "regularized_events_enhanced")
        self.assertFalse(manifest["timbre_policy"]["time_stretch"])
        self.assertEqual(manifest["source_analysis"]["complete_pair_count"], 2)
        self.assertTrue((package / "event_samples" / "s1_cycle000.wav").is_file())
        self.assertTrue((package / "event_samples" / "s2_cycle000.wav").is_file())


if __name__ == "__main__":
    unittest.main()
