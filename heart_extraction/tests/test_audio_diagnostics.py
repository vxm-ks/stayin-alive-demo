import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio_diagnostics as diagnostics


class WindowStatisticsTests(unittest.TestCase):
    def test_constant_signal_has_expected_rms(self):
        signal = np.full(1000, 0.5, dtype=np.float64)
        stats = diagnostics.compute_window_statistics(
            signal, sample_rate=1000, window_ms=100, hop_ms=50
        )
        np.testing.assert_allclose(stats.rms_fs, 0.5, atol=1e-12)
        np.testing.assert_allclose(stats.peak_abs_fs, 0.5, atol=1e-12)
        np.testing.assert_allclose(
            stats.energy_dbfs, 20 * np.log10(0.5), atol=1e-10
        )

    def test_final_partial_window_is_retained(self):
        signal = np.ones(23, dtype=np.float64)
        stats = diagnostics.compute_window_statistics(
            signal, sample_rate=1000, window_ms=10, hop_ms=10
        )
        self.assertEqual(len(stats.centers_s), 3)
        self.assertAlmostEqual(stats.ends_s[-1], 0.023)
        self.assertAlmostEqual(stats.rms_fs[-1], 1.0)


class EndToEndTests(unittest.TestCase):
    def _args(self, input_path: Path, output_dir: Path) -> argparse.Namespace:
        return argparse.Namespace(
            input=str(input_path),
            output_dir=str(output_dir),
            output_prefix=None,
            title=None,
            formats=["png", "svg"],
            window_ms=50.0,
            hop_ms=10.0,
            energy_floor_db=-60.0,
            max_waveform_bins=1000,
            channel_mode="mean",
            analysis_json=None,
            stable_start=0.2,
            stable_end=1.8,
            width=800,
            height=450,
            dpi=100,
            no_csv=False,
            no_json=False,
        )

    def test_all_artifacts_and_metadata_are_created(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sample_rate = 4000
            time = np.arange(sample_rate * 2) / sample_rate
            signal = 0.6 * np.sin(2 * np.pi * 70 * time)
            input_path = root / "synthetic.wav"
            wavfile.write(input_path, sample_rate, (signal * 32767).astype(np.int16))

            output_dir = root / "out"
            created = diagnostics.run(self._args(input_path, output_dir))
            self.assertEqual(len(created), 4)
            for path in created:
                self.assertTrue(path.is_file(), path)

            with Image.open(output_dir / "synthetic.time_energy.png") as image:
                self.assertEqual(image.size, (800, 450))
                self.assertIn("AnalysisMetadata", image.info)

            svg = (output_dir / "synthetic.time_energy.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("Short-time RMS energy", svg)
            self.assertIn("<metadata>", svg)

            metadata = json.loads(
                (output_dir / "synthetic.time_energy.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(metadata["source"]["sample_rate_hz"], sample_rate)
            self.assertEqual(metadata["annotations"]["stable_start_s"], 0.2)
            self.assertEqual(len(metadata["source"]["sha256"]), 64)
            self.assertEqual(metadata["parameters"]["window_samples"], 200)
            self.assertEqual(metadata["parameters"]["hop_samples"], 40)

    def test_annotation_json_is_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            annotation_path = root / "analysis.json"
            annotation_path.write_text(
                json.dumps(
                    {
                        "stable_region": {"start_s": 1.0, "end_s": 2.0},
                        "events": [
                            {"type": "S1", "time_s": 1.1},
                            {"type": "S2", "time_s": 1.4},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            annotations = diagnostics.read_annotations(annotation_path)
            self.assertEqual(annotations.stable_start_s, 1.0)
            self.assertEqual(annotations.events[0], ("S1", 1.1))


if __name__ == "__main__":
    unittest.main()
