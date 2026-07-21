import argparse
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_diagnostics as diagnostics
import heart_sound_processor as processor


def synthetic_pcg(
    sample_rate: int = 4000,
    duration_s: float = 14.0,
    period_s: float = 0.8,
    s1_s2_s: float = 0.30,
) -> np.ndarray:
    rng = np.random.default_rng(20260714)
    signal = rng.normal(0.0, 0.004, int(sample_rate * duration_s))

    def add_sound(time_s: float, amplitude: float, frequency: float, length_s: float):
        start = int(round(time_s * sample_rate))
        length = int(round(length_s * sample_rate))
        local_time = np.arange(length) / sample_rate
        pulse = amplitude * np.sin(2 * np.pi * frequency * local_time)
        pulse *= np.exp(-local_time * 27.0)
        end = min(len(signal), start + length)
        if start >= 0 and end > start:
            signal[start:end] += pulse[: end - start]

    cycle = 0
    time_s = 0.45
    while time_s + s1_s2_s < duration_s - 0.2:
        variation = 1.0 + 0.006 * math_sine(cycle)
        add_sound(time_s, 0.80 * variation, 58.0, 0.13)
        add_sound(time_s + s1_s2_s, 0.52 / variation, 76.0, 0.10)
        cycle += 1
        time_s += period_s * (1.0 + 0.008 * math_sine(cycle * 0.7))
    return signal


def math_sine(value: float) -> float:
    return float(np.sin(value))


class AutomaticProcessingTests(unittest.TestCase):
    def test_detects_period_and_selects_stable_region(self):
        sample_rate = 4000
        signal = synthetic_pcg(sample_rate=sample_rate)
        config = processor.Config(
            stable_min_s=6.0,
            stable_target_s=8.0,
            stable_max_s=10.0,
            stable_min_cycles=7,
            loop_duration_s=5.0,
        )
        result, _, _, _ = processor.analyse(signal, sample_rate, config)
        self.assertIn(result.status, {"PASS", "PASS_WITH_WARNING"})
        self.assertAlmostEqual(result.bpm, 75.0, delta=3.0)
        self.assertAlmostEqual(result.s1_s2_s, 0.30, delta=0.06)
        self.assertGreaterEqual(result.stable_region.duration_s, 6.0)
        self.assertLessEqual(result.stable_region.duration_s, 10.0)
        self.assertGreaterEqual(result.stable_region.valid_fraction, 0.8)

    def test_end_to_end_outputs_include_audio_and_analysis(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample_rate = 4000
            signal = synthetic_pcg(sample_rate=sample_rate)
            input_path = root / "synthetic_pcg.wav"
            wavfile.write(input_path, sample_rate, (signal * 32767).astype(np.int16))
            audio = diagnostics.load_wav(input_path)
            config = processor.Config(
                stable_min_s=6.0,
                stable_target_s=8.0,
                stable_max_s=10.0,
                stable_min_cycles=7,
                loop_duration_s=5.0,
            )
            result, centred, filtered, analysis_rate = processor.analyse(
                audio.signal, audio.sample_rate, config
            )
            output_dir = root / "out"
            created = processor.write_outputs(
                input_path,
                output_dir,
                audio,
                result,
                centred,
                filtered,
                analysis_rate,
                config,
            )
            self.assertTrue(created)
            identity = processor.source_identity(input_path)
            prefix = identity["prefix"]
            group_dir = output_dir / prefix
            required_suffixes = {
                "analysis.json",
                "cycles.csv",
                "heartbeat_events.csv",
                "window_energy.csv",
                "loop_event_energy.csv",
                "stable_segment_raw.wav",
                "stable_segment_filtered.wav",
                "stable_segment_clean.wav",
                "stable_segment_enhanced.wav",
                "stable_segment_event_repaired.wav",
                "authentic_loop.wav",
                "authentic_loop_clean.wav",
                "authentic_loop_enhanced.wav",
                "regularized_events.wav",
                "regularized_events_clean.wav",
                "regularized_events_enhanced.wav",
                "events_only_monitor.wav",
                "background_only_monitor.wav",
                "removed_noise_monitor.wav",
                "diagnostic.png",
                "diagnostic.svg",
                "stable_comparison.png",
                "stable_comparison.svg",
                "authentic_loop_comparison.png",
                "authentic_loop_comparison.svg",
                "regularized_comparison.png",
                "regularized_comparison.svg",
                "noise_diagnostics.png",
                "noise_diagnostics.svg",
                "stable_segment_raw_time_energy.png",
                "stable_segment_raw_time_energy.svg",
                "stable_segment_clean_time_energy.png",
                "stable_segment_clean_time_energy.svg",
                "stable_segment_enhanced_time_energy.png",
                "stable_segment_enhanced_time_energy.svg",
                "authentic_loop_time_energy.png",
                "authentic_loop_time_energy.svg",
                "authentic_loop_clean_time_energy.png",
                "authentic_loop_clean_time_energy.svg",
                "authentic_loop_enhanced_time_energy.png",
                "authentic_loop_enhanced_time_energy.svg",
                "regularized_events_time_energy.png",
                "regularized_events_time_energy.svg",
                "regularized_events_clean_time_energy.png",
                "regularized_events_clean_time_energy.svg",
                "regularized_events_enhanced_time_energy.png",
                "regularized_events_enhanced_time_energy.svg",
            }
            required = {f"{prefix}_{suffix}" for suffix in required_suffixes}
            self.assertTrue(required.issubset({path.name for path in created}))
            samples_dir = group_dir / f"{prefix}_event_samples"
            self.assertTrue(any(samples_dir.glob(f"{prefix}_s1_*.wav")))
            self.assertTrue(any(samples_dir.glob(f"{prefix}_s2_*.wav")))
            analysis_path = group_dir / f"{prefix}_analysis.json"
            payload = json.loads(analysis_path.read_text("utf-8"))
            self.assertIn("stable_region", payload)
            self.assertIn("audio_crop", payload)
            self.assertIn("period_candidates", payload)
            self.assertIn("method_references", payload)
            self.assertIn("event_preservation", payload)
            self.assertEqual(len(payload["outputs"]["time_energy_figures"]), 9)
            self.assertLessEqual(
                payload["event_preservation"]["authentic_rhythm"]["period_cv"],
                0.061,
            )
            self.assertAlmostEqual(
                payload["event_preservation"]["regularized_rhythm"]["period_cv"],
                0.0,
                places=8,
            )
            self.assertEqual(
                payload["outputs"]["stable_comparison_png"],
                f"{prefix}_stable_comparison.png",
            )
            self.assertEqual(payload["source"]["analysis_group_directory"], prefix)
            with (group_dir / f"{prefix}_loop_event_energy.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                energy_rows = list(csv.DictReader(stream))
            self.assertTrue(energy_rows)
            self.assertEqual(
                {row["preservation_status"] for row in energy_rows} - {"OK", "WEAK"},
                set(),
            )

            loop_rate, loop = wavfile.read(group_dir / f"{prefix}_authentic_loop.wav")
            self.assertEqual(loop_rate, sample_rate)
            self.assertEqual(len(loop), sample_rate * 5)

            _, raw_stable = wavfile.read(group_dir / f"{prefix}_stable_segment_raw.wav")
            _, loud_stable = wavfile.read(
                group_dir / f"{prefix}_stable_segment_enhanced.wav"
            )
            raw_rms = np.sqrt(np.mean((raw_stable.astype(float) / 32768.0) ** 2))
            loud_rms = np.sqrt(np.mean((loud_stable.astype(float) / 32768.0) ** 2))
            self.assertGreater(loud_rms, raw_rms)
            self.assertLessEqual(np.max(np.abs(loud_stable)), 32767)

    def test_equal_phase_boundary_preserves_duration(self):
        sample_rate = 1000
        signal = np.ones(5000, dtype=np.float64)
        signal[950:960] = 0.0
        signal[2950:2960] = 0.0
        start, end, offset = processor.choose_equal_phase_loop_boundaries(
            signal, sample_rate, 1.0, 3.0
        )
        self.assertEqual(end - start, 2000)
        self.assertLessEqual(offset, 0.0)

    def test_recorder_filename_is_split_into_sample_id_and_time(self):
        identity = processor.source_identity(Path("202607071630425298.wav"))
        self.assertEqual(identity["sample_id"], "5298")
        self.assertEqual(identity["acquisition_time"], "20260707T163042")
        self.assertEqual(identity["prefix"], "5298_20260707T163042")
        self.assertEqual(identity["time_source"], "input_filename_YYYYMMDDhhmmss")

    def test_silence_is_rejected(self):
        config = processor.Config(
            stable_min_s=2.0,
            stable_target_s=3.0,
            stable_max_s=4.0,
            stable_min_cycles=3,
        )
        with self.assertRaises(ValueError):
            processor.analyse(np.zeros(16000), 4000, config)

    def test_cli_writes_rejection_report_for_silence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "silence.wav"
            wavfile.write(input_path, 4000, np.zeros(16000, dtype=np.int16))
            output_dir = root / "rejected"
            exit_code = processor.main(
                [
                    str(input_path),
                    "--output-dir",
                    str(output_dir),
                    "--stable-min-s",
                    "2",
                    "--stable-target-s",
                    "3",
                    "--stable-max-s",
                    "4",
                    "--stable-min-cycles",
                    "3",
                ]
            )
            self.assertEqual(exit_code, 2)
            prefix = processor.source_identity(input_path)["prefix"]
            report_path = output_dir / prefix / f"{prefix}_analysis.json"
            payload = json.loads(report_path.read_text("utf-8"))
            self.assertEqual(payload["status"], "REJECT")
            self.assertTrue(payload["rejection_reasons"])


if __name__ == "__main__":
    unittest.main()
