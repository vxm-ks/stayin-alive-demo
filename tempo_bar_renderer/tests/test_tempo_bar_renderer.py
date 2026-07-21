import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from heart_extraction import audio_diagnostics as diagnostics
from heart_extraction import heart_sound_processor as extraction
from tempo_bar_renderer import tempo_bar_renderer as renderer


def synthetic_processed_pcg(
    sample_rate: int = 4000,
    duration_s: float = 14.0,
    period_s: float = 0.8,
    s1_s2_s: float = 0.30,
) -> np.ndarray:
    rng = np.random.default_rng(20260714)
    signal = rng.normal(0.0, 0.0025, int(sample_rate * duration_s))

    def add_event(time_s: float, amplitude: float, frequency: float) -> None:
        start = int(round(time_s * sample_rate))
        length = int(round(0.16 * sample_rate))
        local = np.arange(length) / sample_rate
        pulse = amplitude * np.sin(2.0 * np.pi * frequency * local)
        pulse *= np.exp(-local * 27.0)
        end = min(len(signal), start + length)
        signal[start:end] += pulse[: end - start]

    cycle = 0
    time_s = 0.45
    while time_s + s1_s2_s < duration_s - 0.2:
        variation = 1.0 + 0.006 * math.sin(cycle * 0.8)
        add_event(time_s, 0.62, 72.0)
        add_event(time_s + s1_s2_s * variation, 0.42, 96.0)
        time_s += period_s * variation
        cycle += 1
    return signal


class TempoBarRendererTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, str]:
        prefix = "1985_20260714T130032_regularized_events_clean"
        path = root / f"{prefix}.wav"
        extraction.write_wav(path, 4000, synthetic_processed_pcg())
        return path, prefix

    def test_one_wav_is_inspected_read_only_and_renders_all_bars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_wav, prefix = self._fixture(root)
            source_hash = diagnostics.file_sha256(input_wav)
            source_mtime = input_wav.stat().st_mtime_ns

            prepared = renderer.inspect_wav(input_wav)
            recommendation = prepared.recommendation
            self.assertGreater(recommendation.detected_bpm, 70.0)
            self.assertLess(recommendation.detected_bpm, 80.0)
            self.assertAlmostEqual(
                recommendation.detected_bpm,
                prepared.result.period_candidates[0]["bpm"],
                delta=3.0,
            )
            self.assertLessEqual(
                recommendation.recommended_min_bpm, recommendation.detected_bpm
            )
            self.assertGreaterEqual(
                recommendation.recommended_max_bpm, recommendation.detected_bpm
            )

            output_group, created = renderer.render_prepared(
                prepared, 120.0, root / "bar_outputs"
            )
            self.assertEqual(diagnostics.file_sha256(input_wav), source_hash)
            self.assertEqual(input_wav.stat().st_mtime_ns, source_mtime)
            self.assertEqual(len(list(output_group.glob("*.wav"))), 3)
            self.assertEqual(len(list(output_group.glob("*_time_energy.png"))), 3)
            self.assertEqual(len(list(output_group.glob("*_time_energy.svg"))), 3)
            expected_samples = {"4-4": 8000, "3-4": 6000, "2-4": 4000}
            for meter, count in expected_samples.items():
                rate, audio = wavfile.read(
                    output_group / f"{prefix}_bpm120_rotate_{meter}.wav"
                )
                self.assertEqual(rate, 4000)
                self.assertEqual(len(audio), count)
            report = json.loads(
                (output_group / f"{prefix}_bpm120_rotate_bar_analysis.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["source"]["selected_wav"], str(input_wav))
            self.assertEqual(report["source"]["selected_wav_sha256"], source_hash)
            self.assertTrue(report["source"]["read_only_input_policy"])
            self.assertFalse(report["timing"]["time_stretching_applied"])
            self.assertEqual(
                report["processing"]["input_variant_inherited"],
                "regularized_events_clean",
            )
            self.assertFalse(report["processing"]["additional_denoising_applied"])
            self.assertFalse(
                report["processing"]["additional_dynamic_compression_applied"]
            )
            self.assertEqual(set(report["meters"]), {"4/4", "3/4", "2/4"})
            self.assertEqual(report["processing"]["event_bank_mode"], "rotate")
            self.assertEqual(len(created), 11)

    def test_cli_prints_recommendation_then_accepts_user_bpm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_wav, _ = self._fixture(root)
            stdout = io.StringIO()
            with patch(
                "builtins.input", side_effect=["s2", "100"]
            ), contextlib.redirect_stdout(stdout):
                status = renderer.main(
                    [
                        str(input_wav),
                        "--output-dir",
                        str(root / "interactive_outputs"),
                    ]
                )
            output = stdout.getvalue()
            self.assertEqual(status, 0)
            self.assertIn("status=READY", output)
            self.assertIn("beat_peak_mode=s2", output)
            self.assertIn("detected_bpm=", output)
            self.assertIn("recommended_bpm_range=", output)
            self.assertIn("status=PASS", output)
            self.assertEqual(
                len(list((root / "interactive_outputs").rglob("*.wav"))), 3
            )
            report_path = next(
                (root / "interactive_outputs").rglob("*_s2_best_bar_analysis.json")
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["timing"]["beat_peak"], "s2")
            event_rows = next(
                (root / "interactive_outputs").rglob("*_s2_best_bar_events.csv")
            ).read_text(encoding="utf-8")
            self.assertIn("beat_peak_mode", event_rows)
            self.assertNotIn(",S1,", event_rows)

    def test_single_peak_modes_place_only_the_selected_peak_on_beats(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_wav, _ = self._fixture(root)
            prepared = renderer.inspect_wav(input_wav)
            phase = prepared.result.s1_s2_s / prepared.result.period_s
            for mode, expected_type in (("s1", "S1"), ("s2", "S2")):
                signal, rows, _ = renderer._render_bar(
                    prepared.bank,
                    prepared.sample_rate,
                    4,
                    0.6,
                    phase,
                    "4-4",
                    mode,
                )
                self.assertEqual(len(signal), int(round(2.4 * prepared.sample_rate)))
                self.assertEqual(len(rows), 4)
                self.assertEqual({row["event_type"] for row in rows}, {expected_type})
                self.assertEqual({row["event_bank_mode"] for row in rows}, {"best"})
                self.assertEqual(len({row["source_cycle_index"] for row in rows}), 1)
                np.testing.assert_allclose(
                    [row["time_s"] for row in rows], [0.0, 0.6, 1.2, 1.8]
                )
                boundary = renderer._measure_boundary_quality(signal, rows)
                self.assertEqual(boundary["status"], "PASS")
                self.assertLessEqual(
                    boundary["maximum_checked_boundary_jump_abs"], 0.03
                )

    def test_single_peak_mode_has_no_s1_s2_spacing_rejection_at_300_bpm(self):
        quality = renderer._quality_policy(300.0, 0.20, "s1")
        self.assertEqual(quality["beat_peak"], "s1")
        self.assertAlmostEqual(quality["minimum_onset_spacing_s"], 0.2)

    def test_even_pair_rhythm_places_s2_at_half_beat_without_stretching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_wav, _ = self._fixture(root)
            prepared = renderer.inspect_wav(input_wav)
            source_phase = prepared.result.s1_s2_s / prepared.result.period_s
            bank_before = {
                event_type: [item.raw.copy() for item in items]
                for event_type, items in prepared.bank.items()
            }

            signal, rows, _ = renderer._render_bar(
                prepared.bank,
                prepared.sample_rate,
                4,
                0.8,
                source_phase,
                "4-4",
                "both",
                "rotate",
                "even",
            )

            self.assertEqual(len(signal), int(round(3.2 * prepared.sample_rate)))
            self.assertEqual({row["pair_rhythm"] for row in rows}, {"even"})
            s1_times = [row["time_s"] for row in rows if row["event_type"] == "S1"]
            s2_times = [row["time_s"] for row in rows if row["event_type"] == "S2"]
            np.testing.assert_allclose(s1_times, [0.0, 0.8, 1.6, 2.4])
            np.testing.assert_allclose(s2_times, [0.4, 1.2, 2.0, 2.8])
            ordered_times = sorted(row["time_s"] for row in rows)
            np.testing.assert_allclose(np.diff(ordered_times), [0.4] * 7)
            for event_type, items in prepared.bank.items():
                for before, after in zip(bank_before[event_type], items):
                    np.testing.assert_array_equal(before, after.raw)

            quality = renderer._quality_policy(300.0, source_phase, "both", "even")
            self.assertAlmostEqual(quality["s1_to_s2_s"], 0.1)
            self.assertAlmostEqual(quality["s2_to_next_s1_s"], 0.1)
            self.assertTrue(quality["equal_inter_onset_spacing"])

    def test_even_pair_rhythm_is_named_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_wav, prefix = self._fixture(root)
            prepared = renderer.inspect_wav(input_wav)
            source_hash = diagnostics.file_sha256(input_wav)
            output_group, _ = renderer.render_prepared(
                prepared,
                120.0,
                root / "bar_outputs",
                "both",
                "rotate",
                "safe",
                "even",
            )

            self.assertIn("_bpm120_even_rotate_bars", output_group.name)
            report_path = (
                output_group
                / f"{prefix}_bpm120_even_rotate_bar_analysis.json"
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["timing"]["pair_rhythm"], "even")
            self.assertEqual(report["timing"]["rendered_s2_phase_ratio"], 0.5)
            self.assertFalse(report["timing"]["source_s2_phase_preserved"])
            self.assertTrue(report["timing"]["equal_inter_onset_spacing"])
            self.assertFalse(report["timing"]["time_stretching_applied"])
            self.assertFalse(
                report["processing"]["event_waveform_duration_adjustment_applied"]
            )
            self.assertEqual(diagnostics.file_sha256(input_wav), source_hash)

    def test_even_pair_rhythm_rejects_single_peak_mode(self):
        with self.assertRaisesRegex(ValueError, "requires beat_peak=both"):
            renderer._quality_policy(100.0, 0.35, "s1", "even")

    def test_loudness_profiles_are_derived_and_true_peak_controlled(self):
        sample_rate = 4000
        raw = synthetic_processed_pcg(
            sample_rate=sample_rate, duration_s=2.4, period_s=0.6
        )
        safe, safe_report = renderer._apply_loudness_mode(
            raw, sample_rate, "s1", "safe"
        )
        loud, loud_report = renderer._apply_loudness_mode(
            raw, sample_rate, "s1", "loud"
        )
        cinematic, cinematic_report = renderer._apply_loudness_mode(
            raw, sample_rate, "s1", "cinematic"
        )

        self.assertAlmostEqual(
            safe_report["metrics_after_mode"]["sample_peak_dbfs"], -3.0, delta=0.02
        )
        for signal, report in ((loud, loud_report), (cinematic, cinematic_report)):
            self.assertTrue(np.all(np.isfinite(signal)))
            self.assertTrue(report["dynamic_compression_applied"])
            self.assertAlmostEqual(
                report["metrics_after_mode"]["estimated_true_peak_dbtp"],
                -1.0,
                delta=0.02,
            )
            self.assertGreater(
                report["metrics_after_mode"]["active_rms_dbfs"],
                safe_report["metrics_after_mode"]["active_rms_dbfs"],
            )
        self.assertFalse(loud_report["cinematic_layers_applied"])
        self.assertTrue(cinematic_report["cinematic_layers_applied"])
        self.assertEqual(cinematic_report["layers"]["body_band_hz"], [45.0, 120.0])
        self.assertEqual(
            cinematic_report["source_derivation_policy"],
            "all layers derived only from the selected heartbeat",
        )

    def test_rejects_unknown_loudness_profile(self):
        with self.assertRaisesRegex(ValueError, "loudness_mode"):
            renderer._apply_loudness_mode(
                synthetic_processed_pcg(duration_s=2.0), 4000, "s1", "extreme"
            )

    def test_rejects_bpm_that_collapses_event_spacing(self):
        with self.assertRaisesRegex(ValueError, "onset spacing"):
            renderer._quality_policy(300.0, 0.375)

    def test_rejects_a_wav_outside_the_nine_supported_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "original_recording.wav"
            extraction.write_wav(path, 4000, synthetic_processed_pcg())
            with self.assertRaisesRegex(ValueError, "nine supported"):
                renderer.inspect_wav(path)


if __name__ == "__main__":
    unittest.main()
