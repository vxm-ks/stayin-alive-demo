import contextlib
import io
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wav_track_mixer import wav_track_mixer as mixer


def write_tone(
    path: Path,
    sample_rate: int,
    duration_s: float,
    amplitude: float,
    channels: int = 1,
) -> None:
    times = np.arange(int(round(sample_rate * duration_s))) / sample_rate
    tone = amplitude * np.sin(2.0 * np.pi * 220.0 * times)
    if channels > 1:
        tone = np.repeat(tone[:, np.newaxis], channels, axis=1)
    wavfile.write(path, sample_rate, np.round(tone * 32767.0).astype(np.int16))


class WavTrackMixerTests(unittest.TestCase):
    def test_mixes_two_tracks_and_keeps_longer_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            output = root / "mixed.wav"
            write_tone(first, 8000, 0.20, 0.10)
            write_tone(second, 8000, 0.35, 0.20)

            result = mixer.mix_wav_tracks(first, second, output)
            rate, audio = wavfile.read(output)

            self.assertEqual(rate, 8000)
            self.assertEqual(audio.ndim, 1)
            self.assertEqual(len(audio), 2800)
            self.assertAlmostEqual(result.duration_s, 0.35)
            self.assertAlmostEqual(result.peak_gain_db, 0.0)

    def test_resamples_expands_mono_and_offsets_second_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "mono_8k.wav"
            second = root / "stereo_16k.wav"
            output = root / "mixed.wav"
            write_tone(first, 8000, 0.10, 0.10)
            write_tone(second, 16000, 0.20, 0.10, channels=2)

            result = mixer.mix_wav_tracks(first, second, output, offset_b_s=0.05)
            rate, audio = wavfile.read(output)

            self.assertEqual(rate, 16000)
            self.assertEqual(audio.shape, (4000, 2))
            self.assertEqual(result.channels, 2)
            np.testing.assert_array_equal(audio[:, 0], audio[:, 1])

    def test_peak_protection_prevents_clipping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            output = root / "mixed.wav"
            write_tone(first, 12000, 0.20, 0.80)
            write_tone(second, 12000, 0.20, 0.80)

            result = mixer.mix_wav_tracks(first, second, output)
            _, audio = wavfile.read(output)
            peak = np.max(np.abs(audio.astype(np.float64))) / 32768.0

            self.assertGreater(result.source_peak, 1.0)
            self.assertLess(result.peak_gain_db, 0.0)
            self.assertAlmostEqual(peak, 10.0 ** (-1.0 / 20.0), delta=2e-4)

    def test_shorter_track_repeats_to_the_end_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            short = root / "short.wav"
            long = root / "long.wav"
            output = root / "mixed.wav"
            write_tone(short, 8000, 0.10, 0.10)
            write_tone(long, 8000, 0.35, 0.10)

            result = mixer.mix_wav_tracks(short, long, output)
            _, mixed = wavfile.read(output)
            _, long_audio = wavfile.read(long)

            self.assertEqual(result.repeats_a, 4)
            self.assertEqual(result.repeats_b, 1)
            self.assertGreater(
                np.max(np.abs(mixed[-400:].astype(np.int32) - long_audio[-400:])),
                100,
            )

    def test_looping_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            short = root / "short.wav"
            long = root / "long.wav"
            output = root / "mixed.wav"
            write_tone(short, 8000, 0.10, 0.10)
            write_tone(long, 8000, 0.35, 0.10)

            result = mixer.mix_wav_tracks(
                short, long, output, loop_shorter=False
            )
            _, mixed = wavfile.read(output)
            _, long_audio = wavfile.read(long)

            self.assertEqual(result.repeats_a, 1)
            np.testing.assert_array_equal(mixed[-400:], long_audio[-400:])

    def test_cli_reports_output_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            output = root / "mixed.wav"
            write_tone(first, 8000, 0.10, 0.10)
            write_tone(second, 8000, 0.10, 0.10)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = mixer.main([str(first), str(second), str(output)])

            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertIn("status=PASS", stdout.getvalue())
            self.assertIn("sample_rate=8000", stdout.getvalue())

    def test_refuses_to_overwrite_an_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.wav"
            other = Path(temporary) / "other.wav"
            write_tone(source, 8000, 0.10, 0.10)
            write_tone(other, 8000, 0.10, 0.10)

            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                mixer.mix_wav_tracks(source, other, source)


if __name__ == "__main__":
    unittest.main()
