from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from stage3_midi_renderer.batch_validator import (
    LoudnessLimits,
    batch_validate_midis,
    integrated_loudness_lufs,
    true_peak_dbtp,
)
from stage3_midi_renderer.package_renderer import PackageRenderResult


class BatchValidatorTests(unittest.TestCase):
    def test_bs1770_measurement_tracks_gain_and_true_peak(self) -> None:
        rate = 48_000
        time = np.arange(rate * 3) / rate
        audio = np.repeat((0.1 * np.sin(2 * np.pi * 1000 * time))[:, None], 2, axis=1)
        base = integrated_loudness_lufs(audio, rate)
        louder = integrated_loudness_lufs(audio * 2.0, rate)
        self.assertAlmostEqual(louder - base, 6.0206, places=2)
        self.assertAlmostEqual(true_peak_dbtp(audio), -20.0, delta=0.1)

    def test_batch_continues_after_invalid_midi_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good, bad = root / "good.mid", root / "bad.mid"
            good.write_bytes(b"good")
            bad.write_bytes(b"bad")
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema_version": "1.0", "heartbeat_channel": 10,
                "note_map": {"36": "S1"}, "heartbeat_conditioning_profile": "none",
                "velocity_gamma": 1.0,
                "default_material_rule": {"mode": "fixed", "package_ids": ["a"], "gain_db": 0.0},
                "section_material_rules": [],
                "mix": {
                    "mode": "event_window_relative", "music_gain_db": 0.0,
                    "heartbeat_over_music_db": 4.0, "maximum_heartbeat_boost_db": 12.0,
                    "event_window_ms": 250.0, "true_peak_ceiling_dbtp": -1.0,
                    "publish_stems": False,
                },
            }), encoding="utf-8")

            def fake_renderer(midi, _sf2, _plan, _packages, output, **_kwargs):
                if Path(midi).name == "bad.mid":
                    raise ValueError("synthetic rejection")
                output = Path(output)
                output.mkdir(parents=True)
                rate = 48_000
                time = np.arange(rate * 2) / rate
                music = np.repeat((0.05 * np.sin(2 * np.pi * 440 * time))[:, None], 2, axis=1)
                heartbeat = np.zeros_like(music)
                start = rate
                heartbeat[start:start + 4_800] = 0.08 * np.exp(-20 * np.arange(4_800)[:, None] / rate)
                final = music + heartbeat
                for name, audio in (("final_mix.wav", final), ("music_stem.wav", music), ("heartbeat_stem.wav", heartbeat)):
                    wavfile.write(output / name, rate, np.rint(audio * 32767).astype("<i2"))
                assignments = output / "heartbeat_event_assignments.csv"
                with assignments.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=["time_s"])
                    writer.writeheader()
                    writer.writerow({"time_s": 1.0})
                manifest = output / "stage3_render_manifest.json"
                manifest.write_text(json.dumps({
                    "policy": {"midi_timing_modified": False},
                    "assignments": {"event_count": 1},
                }), encoding="utf-8")
                return PackageRenderResult(output, output / "final_mix.wav", assignments, manifest, 1, 2.0, rate, 2)

            result = batch_validate_midis(
                [bad, good], root / "general.sf2", plan, {"a": root / "package"},
                root / "report", renderer=fake_renderer,
                limits=LoudnessLimits(-80.0, -1.0, -0.1, -20.0, 20.0),
            )
            self.assertEqual((result.passed, result.failed, result.status), (1, 1, "FAIL"))
            report = json.loads(result.report_json.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["total"], 2)
            self.assertTrue(any("synthetic rejection" in item["failures"] for item in report["items"]))

    def test_batch_rejects_manual_mix_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            midi = root / "one.mid"
            midi.write_bytes(b"midi")
            plan = root / "plan.json"
            plan.write_text(json.dumps({
                "schema_version": "1.0", "heartbeat_channel": 10,
                "note_map": {"36": "S1"}, "heartbeat_conditioning_profile": "none",
                "velocity_gamma": 1.0,
                "default_material_rule": {"mode": "fixed", "package_ids": ["a"]},
                "section_material_rules": [], "mix": {"mode": "manual"},
            }), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "event_window_relative"):
                batch_validate_midis(
                    [midi], root / "general.sf2", plan, {"a": root / "package"}, root / "out"
                )


if __name__ == "__main__":
    unittest.main()
