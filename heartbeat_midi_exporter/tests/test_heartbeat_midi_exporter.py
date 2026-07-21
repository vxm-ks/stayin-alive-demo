from __future__ import annotations

import csv
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import heartbeat_midi_exporter as exporter


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_vlq(data: bytes, cursor: int) -> tuple[int, int]:
    value = 0
    while True:
        byte = data[cursor]
        cursor += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, cursor


def parse_midi(path: Path) -> dict:
    data = path.read_bytes()
    assert data[:4] == b"MThd"
    header_length, midi_format, track_count, ppq = struct.unpack(">IHHH", data[4:14])
    cursor = 8 + header_length
    tracks = []
    for _ in range(track_count):
        assert data[cursor : cursor + 4] == b"MTrk"
        length = struct.unpack(">I", data[cursor + 4 : cursor + 8])[0]
        body = data[cursor + 8 : cursor + 8 + length]
        cursor += 8 + length
        tick = 0
        index = 0
        events = []
        while index < len(body):
            delta, index = read_vlq(body, index)
            tick += delta
            status = body[index]
            index += 1
            if status == 0xFF:
                meta_type = body[index]
                index += 1
                size, index = read_vlq(body, index)
                payload = body[index : index + size]
                index += size
                events.append((tick, "meta", meta_type, payload))
            elif status & 0xF0 in (0x80, 0x90):
                note, velocity = body[index : index + 2]
                index += 2
                events.append((tick, status, note, velocity))
            elif status & 0xF0 == 0xC0:
                index += 1
            else:
                raise AssertionError(f"Unexpected MIDI status 0x{status:02x}")
        tracks.append(events)
    return {"format": midi_format, "tracks": tracks, "ppq": ppq}


def riff_chunks(data: bytes, start: int, end: int):
    cursor = start
    while cursor < end:
        tag = data[cursor : cursor + 4]
        size = struct.unpack("<I", data[cursor + 4 : cursor + 8])[0]
        payload_start = cursor + 8
        payload_end = payload_start + size
        yield tag, data[payload_start:payload_end]
        cursor = payload_end + (size & 1)


class ExporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rate = 11025
        signal = np.zeros(rate * 2, dtype=np.float64)
        for time_s, amplitude in ((0.50, 0.65), (0.75, 0.42)):
            centre = int(time_s * rate)
            length = int(0.08 * rate)
            pulse = amplitude * np.sin(2 * np.pi * 70 * np.arange(length) / rate)
            pulse *= np.hanning(length)
            signal[centre : centre + length] += pulse
        self.wav = self.root / "patient.wav"
        wavfile.write(self.wav, rate, np.rint(signal * 32767).astype(np.int16))
        self.events = self.root / "case_bar_events.csv"
        fields = [
            "meter",
            "event_type",
            "time_s",
            "source_event_time_s",
            "source_amplitude",
            "exemplar_quality_score",
        ]
        with self.events.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for meter, count in (("4/4", 8), ("3/4", 6), ("2/4", 4)):
                for index in range(count):
                    event_type = "S1" if index % 2 == 0 else "S2"
                    writer.writerow(
                        {
                            "meter": meter,
                            "event_type": event_type,
                            "time_s": index * 0.25,
                            "source_event_time_s": 0.50 if event_type == "S1" else 0.75,
                            "source_amplitude": 0.9 if event_type == "S1" else 0.7,
                            "exemplar_quality_score": 0.95 - index * 0.001,
                        }
                    )
        self.report = self.root / "case_bar_analysis.json"
        report = {
            "event_table": self.events.name,
            "source": {
                "selected_wav": str(self.wav),
                "selected_wav_sha256": file_hash(self.wav),
            },
            "timing": {"target_bpm": 120.0, "pair_rhythm": "even"},
            "meters": {
                meter: {"numerator": int(meter[0]), "denominator": 4}
                for meter in ("4/4", "3/4", "2/4")
            },
        }
        self.report.write_text(json.dumps(report), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_export_produces_exact_midi_events_and_preserves_inputs(self):
        before = {path: (file_hash(path), path.stat().st_mtime_ns) for path in (self.wav, self.events, self.report)}
        output = exporter.export_bundle(self.report, output_dir=self.root / "out")
        expected = {"4-4": 8, "3-4": 6, "2-4": 4}
        for meter, count in expected.items():
            parsed = parse_midi(next(output.glob(f"*_heartbeat_{meter}.mid")))
            self.assertEqual((parsed["format"], parsed["ppq"]), (1, 480))
            tempo = [event[3] for event in parsed["tracks"][0] if event[1:3] == ("meta", 0x51)]
            time_signature = [event[3] for event in parsed["tracks"][0] if event[1:3] == ("meta", 0x58)]
            self.assertEqual(int.from_bytes(tempo[0], "big"), 500_000)
            self.assertEqual(time_signature[0][:2], bytes([int(meter[0]), 2]))
            note_ons = [event for event in parsed["tracks"][1] if isinstance(event[1], int) and event[1] & 0xF0 == 0x90]
            note_offs = [event for event in parsed["tracks"][1] if isinstance(event[1], int) and event[1] & 0xF0 == 0x80]
            self.assertEqual([event[0] for event in note_ons], [index * 240 for index in range(count)])
            self.assertEqual([event[0] for event in note_offs], [(index + 1) * 240 for index in range(count)])
            self.assertEqual([event[2] for event in note_ons], [36, 38] * (count // 2))
            self.assertTrue(all(event[1] & 0x0F == 9 for event in note_ons))
        for path, identity in before.items():
            self.assertEqual((file_hash(path), path.stat().st_mtime_ns), identity)
        manifest = json.loads(next(output.glob("*_midi_mapping.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["midi"]["target_bpm"], 120.0)
        self.assertEqual(manifest["midi"]["mapping"]["S1"]["note"], 36)

    def test_soundfont_has_required_hydra_tables_and_percussion_only_preset(self):
        output = exporter.export_bundle(self.report, output_dir=self.root / "out")
        sf2 = next(output.glob("*.sf2")).read_bytes()
        self.assertEqual(sf2[:4], b"RIFF")
        self.assertEqual(sf2[8:12], b"sfbk")
        lists = {}
        for tag, payload in riff_chunks(sf2, 12, len(sf2)):
            self.assertEqual(tag, b"LIST")
            lists[payload[:4]] = payload[4:]
        self.assertEqual(set(lists), {b"INFO", b"sdta", b"pdta"})
        pdta = {tag: payload for tag, payload in riff_chunks(lists[b"pdta"], 0, len(lists[b"pdta"]))}
        self.assertEqual(
            list(pdta),
            [b"phdr", b"pbag", b"pmod", b"pgen", b"inst", b"ibag", b"imod", b"igen", b"shdr"],
        )
        self.assertEqual(len(pdta[b"phdr"]), 2 * 38)
        first = struct.unpack("<20sHHHIII", pdta[b"phdr"][:38])
        self.assertEqual((first[1], first[2]), (0, 128))
        self.assertEqual(len(pdta[b"shdr"]), 3 * 46)
        self.assertTrue((output / "case_soundfont_s1.wav").is_file())
        self.assertTrue((output / "case_soundfont_s2.wav").is_file())

    def test_single_s1_quarter_note_4_4_midi_only_export(self):
        output = exporter.export_bundle(
            self.report,
            output_dir=self.root / "out",
            event_mode="s1",
            selected_meters=("4/4",),
            note_division=4,
            midi_only=True,
        )
        midi_files = list(output.glob("*.mid"))
        self.assertEqual(len(midi_files), 1)
        self.assertIn("_s1_quarter_4-4.mid", midi_files[0].name)
        parsed = parse_midi(midi_files[0])
        note_ons = [event for event in parsed["tracks"][1] if isinstance(event[1], int) and event[1] & 0xF0 == 0x90]
        note_offs = [event for event in parsed["tracks"][1] if isinstance(event[1], int) and event[1] & 0xF0 == 0x80]
        self.assertEqual([event[0] for event in note_ons], [0, 480, 960, 1440])
        self.assertEqual([event[0] for event in note_offs], [480, 960, 1440, 1920])
        self.assertEqual([event[2] for event in note_ons], [36, 36, 36, 36])
        self.assertFalse(list(output.glob("*.sf2")))
        manifest = json.loads(next(output.glob("*_midi_mapping.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["midi"]["event_mode"], "s1")
        self.assertEqual(manifest["midi"]["equal_note_value"], "1/4")
        self.assertFalse(manifest["soundfont"]["generated"])

    def test_source_hash_mismatch_is_rejected(self):
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["source"]["selected_wav_sha256"] = "0" * 64
        self.report.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            exporter.export_bundle(self.report, output_dir=self.root / "out")

    def test_speaker_safe_conditioning_reduces_high_frequency_energy(self):
        rate = 44100
        time = np.arange(rate // 2, dtype=np.float64) / rate
        raw = 0.55 * np.sin(2 * np.pi * 60 * time) + 0.12 * np.sin(2 * np.pi * 5000 * time)
        conditioned, audit = exporter.condition_heartbeat_sample(raw, rate)
        frequencies = np.fft.rfftfreq(len(raw), 1.0 / rate)
        raw_power = np.abs(np.fft.rfft(raw)) ** 2
        conditioned_power = np.abs(np.fft.rfft(conditioned)) ** 2
        raw_ratio = float(raw_power[frequencies >= 1000].sum() / raw_power.sum())
        conditioned_ratio = float(conditioned_power[frequencies >= 1000].sum() / conditioned_power.sum())
        self.assertLess(conditioned_ratio, raw_ratio * 0.001)
        self.assertLessEqual(float(np.max(np.abs(conditioned))), 10 ** (-2.9 / 20.0))
        self.assertEqual(audit["profile"], "speaker_safe_loud_v1")
        self.assertEqual(audit["lowpass_hz"], 220.0)

    def test_conditioned_s1_soundfont_records_processing_audit(self):
        output = exporter.export_bundle(
            self.report,
            output_dir=self.root / "out",
            event_mode="s1",
            selected_meters=("4/4",),
            s1_note=42,
            soundfont_layers=4,
            soundfont_conditioning="speaker_safe_loud_v1",
        )
        manifest = json.loads(next(output.glob("*_midi_mapping.json")).read_text(encoding="utf-8"))
        soundfont = manifest["soundfont"]
        self.assertEqual(soundfont["conditioning_profile"], "speaker_safe_loud_v1")
        self.assertEqual(soundfont["layers_per_sample"], 4)
        self.assertEqual(set(soundfont["samples"]), {"S1"})
        self.assertEqual(soundfont["samples"]["S1"]["conditioning"]["highpass_hz"], 35.0)
        self.assertTrue((output / "case_soundfont_s1.wav").is_file())
        self.assertFalse((output / "case_soundfont_s2.wav").exists())

    def test_direct_script_can_resolve_stage3_component_outside_project_cwd(self):
        completed = subprocess.run(
            [sys.executable, str(MODULE_DIR / "heartbeat_midi_exporter.py"), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--soundfont-conditioning", completed.stdout)


if __name__ == "__main__":
    unittest.main()
