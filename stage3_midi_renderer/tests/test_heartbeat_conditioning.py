from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from stage3_midi_renderer.cli import main
from stage3_midi_renderer.heartbeat_conditioning import (
    HeartbeatConditioningError,
    HeartbeatConditioningProfile,
    available_conditioning_profiles,
    condition_heartbeat_audio,
    condition_heartbeat_wav,
)


class HeartbeatConditioningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.rate = 44_100
        time = np.arange(self.rate // 2, dtype=np.float64) / self.rate
        self.audio = (
            0.55 * np.sin(2 * np.pi * 60 * time)
            + 0.12 * np.sin(2 * np.pi * 5_000 * time)
            + 0.08 * np.sin(2 * np.pi * 18 * time)
        )
        self.input_wav = self.root / "patient_s1.wav"
        wavfile.write(
            self.input_wav,
            self.rate,
            np.rint(self.audio * 32767.0).astype("<i2"),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_registered_profile_reduces_out_of_band_energy_and_audits_parameters(self):
        conditioned, audit = condition_heartbeat_audio(
            self.audio,
            self.rate,
            profile="speaker_safe_loud_v1",
        )
        self.assertEqual(conditioned.shape, self.audio.shape)
        self.assertEqual(audit.profile["name"], "speaker_safe_loud_v1")
        self.assertEqual(audit.profile["highpass_hz"], 35.0)
        self.assertEqual(audit.profile["lowpass_hz"], 220.0)
        self.assertLess(
            audit.output_spectral_energy_percent["above_1000_hz"],
            audit.input_spectral_energy_percent["above_1000_hz"] * 0.001,
        )
        self.assertLess(
            audit.output_spectral_energy_percent["below_30_hz"],
            audit.input_spectral_energy_percent["below_30_hz"],
        )
        self.assertLessEqual(float(np.max(np.abs(conditioned))), 10 ** (-2.9 / 20.0))

    def test_none_profile_is_an_audited_bypass(self):
        output, audit = condition_heartbeat_audio(self.audio, self.rate, profile="none")
        np.testing.assert_array_equal(output, self.audio)
        self.assertFalse(audit.profile["enabled"])
        self.assertEqual(audit.makeup_gain_db, 0.0)

    def test_explicit_profile_is_supported_and_validated(self):
        custom = HeartbeatConditioningProfile(
            name="venue_subwoofer_v1",
            enabled=True,
            highpass_hz=25.0,
            lowpass_hz=300.0,
            compressor_ratio=2.0,
        )
        _output, audit = condition_heartbeat_audio(self.audio, self.rate, profile=custom)
        self.assertEqual(audit.profile["name"], "venue_subwoofer_v1")
        with self.assertRaisesRegex(HeartbeatConditioningError, "highpass"):
            condition_heartbeat_audio(
                self.audio,
                self.rate,
                profile=HeartbeatConditioningProfile(
                    name="invalid",
                    enabled=True,
                    highpass_hz=500.0,
                    lowpass_hz=200.0,
                ),
            )

    def test_wav_component_publishes_audio_and_machine_readable_audit(self):
        before = self.input_wav.read_bytes()
        result = condition_heartbeat_wav(
            self.input_wav,
            self.root / "conditioned",
            profile="speaker_safe_loud_v1",
        )
        self.assertEqual(self.input_wav.read_bytes(), before)
        self.assertEqual(
            sorted(path.name for path in result.output_dir.iterdir()),
            ["heartbeat_conditioned.wav", "heartbeat_conditioning_manifest.json"],
        )
        manifest = json.loads(result.manifest_json.read_text(encoding="utf-8"))
        self.assertEqual(manifest["stage"], "stage3")
        self.assertEqual(manifest["operation"], "heartbeat_timbre_conditioning")
        self.assertFalse(manifest["complete_mix_filtered"])
        self.assertFalse(manifest["midi_events_modified"])
        self.assertEqual(
            manifest["processing"]["profile"]["name"],
            "speaker_safe_loud_v1",
        )

    def test_cli_exposes_component_and_registered_profiles(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["conditioning-profiles"])
        self.assertEqual(status, 0)
        profiles = json.loads(stdout.getvalue())
        self.assertEqual(
            tuple(profiles["profiles"]),
            available_conditioning_profiles(),
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main([
                "condition-heartbeat",
                "--input-wav", str(self.input_wav),
                "--output-dir", str(self.root / "cli-output"),
            ])
        self.assertEqual(status, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["profile"], "speaker_safe_loud_v1")
        self.assertTrue(Path(result["output_wav"]).is_file())


if __name__ == "__main__":
    unittest.main()
