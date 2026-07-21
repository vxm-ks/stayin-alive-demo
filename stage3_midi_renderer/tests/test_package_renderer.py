from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from stage3_midi_renderer.cli import build_parser
from stage3_midi_renderer.package_renderer import load_render_plan, render_with_packages
from stage3_midi_renderer.renderer import Stage3RenderError
from stage3_midi_renderer.tests.test_renderer import soundfont_bytes, track


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _complete_midi() -> bytes:
    import struct
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 3, 480)
    conductor = track([
        (0, b"\xff\x51\x03\x07\xa1\x20"),
        (0, b"\xff\x58\x04\x04\x02\x18\x08"),
        (1920, b"\xff\x2f\x00"),
    ])
    music = track([
        (0, b"\xc0\x00"), (0, b"\x90\x3c\x64"),
        (1800, b"\x80\x3c\x00"), (120, b"\xff\x2f\x00"),
    ])
    heartbeat = track([
        (0, b"\x99\x24\x64"), (60, b"\x89\x24\x00"),
        (180, b"\x99\x26\x5a"), (60, b"\x89\x26\x00"),
        (660, b"\x99\x24\x64"), (60, b"\x89\x24\x00"),
        (180, b"\x99\x26\x5a"), (60, b"\x89\x26\x00"),
        (720, b"\xff\x2f\x00"),
    ])
    return header + conductor + music + heartbeat


def _package(root: Path, package_id: str, frequency: float) -> Path:
    package = root / package_id
    samples = package / "event_samples"
    samples.mkdir(parents=True)
    entries = []
    for event_type, factor in (("S1", 1.0), ("S2", 1.3)):
        time = np.arange(800) / 8000
        signal = 0.4 * np.sin(2 * np.pi * frequency * factor * time) * np.exp(-25 * time)
        path = samples / f"{event_type.lower()}_cycle001.wav"
        wavfile.write(path, 8000, np.rint(signal * 32767).astype("<i2"))
        entries.append({
            "event_type": event_type, "cycle_index": 1,
            "file": f"event_samples/{path.name}", "sha256": _sha256(path),
            "quality_score": 0.9, "alignment_offset_samples": 8,
        })
    (package / "heartbeat_manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "event_samples": entries,
    }), encoding="utf-8")
    return package


def _plan(root: Path, *, publish_stems: bool = False) -> Path:
    path = root / "plan.json"
    path.write_text(json.dumps({
        "schema_version": "1.0", "heartbeat_channel": 10,
        "note_map": {"36": "S1", "38": "S2"},
        "heartbeat_conditioning_profile": "none", "velocity_gamma": 1.0,
        "default_material_rule": {
            "mode": "round_robin_per_cycle", "package_ids": ["a", "b"], "gain_db": 0.0,
        },
        "section_material_rules": [],
        "mix": {
            "mode": "manual", "music_gain_db": -6.0, "heartbeat_gain_db": 2.0,
            "true_peak_ceiling_dbtp": -1.0, "publish_stems": publish_stems,
        },
    }), encoding="utf-8")
    return path


class PackageRendererTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.midi = self.root / "complete.mid"
        self.sf2 = self.root / "general.sf2"
        self.midi.write_bytes(_complete_midi())
        self.sf2.write_bytes(soundfont_bytes([("Piano", 0, 0)]))
        self.a = _package(self.root, "a", 70)
        self.b = _package(self.root, "b", 105)
        self.plan = _plan(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _executor(command, output_path, timeout):
        del timeout
        with wave.open(str(output_path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(48_000)
            value = np.rint(np.full((120_000, 2), 0.08) * 32767).astype("<i2")
            handle.writeframes(value.tobytes())
        return subprocess.CompletedProcess(command, 0, "rendered", "")

    def test_cycle_rotation_balance_audit_and_no_plot(self):
        source = self.midi.read_bytes()
        result = render_with_packages(
            self.midi, self.sf2, self.plan, {"a": self.a, "b": self.b},
            self.root / "output", fluidsynth="fake", executor=self._executor,
        )
        self.assertEqual(self.midi.read_bytes(), source)
        with result.assignments_csv.open(encoding="utf-8", newline="") as handle:
            import csv
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["package_id"] for row in rows], ["a", "a", "b", "b"])
        self.assertEqual([row["event_type"] for row in rows], ["S1", "S2", "S1", "S2"])
        files = sorted(path.name for path in result.output_dir.iterdir())
        self.assertEqual(files, [
            "final_mix.wav", "heartbeat_event_assignments.csv", "stage3_render_manifest.json",
        ])
        self.assertFalse(any(path.suffix.lower() in (".png", ".svg") for path in result.output_dir.iterdir()))
        manifest = json.loads(result.manifest_json.read_text(encoding="utf-8"))
        self.assertEqual(manifest["mix"]["requested_music_gain_db"], -6.0)
        self.assertEqual(manifest["mix"]["effective_heartbeat_gain_db_before_peak_protection"], 2.0)
        self.assertLessEqual(manifest["mix"]["post_protection_true_peak_dbtp"], -0.9)
        self.assertFalse(manifest["policy"]["midi_timing_modified"])

    def test_hash_mismatch_and_unknown_plan_field_are_rejected(self):
        manifest_path = self.a / "heartbeat_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["event_samples"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(Stage3RenderError, "hash mismatch"):
            render_with_packages(
                self.midi, self.sf2, self.plan, {"a": self.a, "b": self.b},
                self.root / "bad", fluidsynth="fake", executor=self._executor,
            )
        raw = json.loads(self.plan.read_text(encoding="utf-8"))
        raw["mystery"] = True
        self.plan.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(Stage3RenderError, "unknown render plan"):
            load_render_plan(self.plan)

    def test_cli_accepts_repeatable_package_bindings(self):
        args = build_parser().parse_args([
            "render-packages", "--input-midi", "complete.mid", "--general-sf2", "general.sf2",
            "--render-plan", "plan.json", "--heartbeat-package", "a=A", "--heartbeat-package", "b=B",
        ])
        self.assertEqual(args.command, "render-packages")
        self.assertEqual(args.heartbeat_package, ["a=A", "b=B"])
        self.assertEqual(args.sample_rate, 48_000)


if __name__ == "__main__":
    unittest.main()
