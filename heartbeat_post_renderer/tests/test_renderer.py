from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from heart_extraction import audio_diagnostics, heart_sound_processor
from heartbeat_post_renderer.renderer import (
    HeartbeatPostRenderError,
    TempoChange,
    TempoMap,
    parse_timing_midi,
    render_heartbeat_track,
)
from midigpt_scaffold_builder.midi import MidiNote, MidiTrack, midi_bytes
from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.backends import FakeBackend
from stage1_story_agent.tests.fixtures import (
    make_test_mode_request,
    test_mode_draft_data,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_plan():
    response = json.dumps(test_mode_draft_data(), ensure_ascii=False)
    run = Stage1StoryAgent(FakeBackend([response])).plan(make_test_mode_request())
    return run.heartbeat_delivery


def _write_package(root: Path) -> Path:
    package = root / "heartbeat_package"
    samples = package / "event_samples"
    samples.mkdir(parents=True)
    sample_rate = 8000
    manifest_samples = []
    for cycle in (1, 2):
        for event_type, frequency, amplitude in (
            ("S1", 70.0, 0.65),
            ("S2", 95.0, 0.45),
        ):
            time = np.arange(1200, dtype=np.float64) / sample_rate
            envelope = np.exp(-time * 20.0)
            signal = amplitude * np.sin(2 * np.pi * frequency * time) * envelope
            path = samples / f"{event_type.lower()}_cycle{cycle:03d}.wav"
            heart_sound_processor.write_wav(path, sample_rate, signal)
            manifest_samples.append(
                {
                    "event_type": event_type,
                    "cycle_index": cycle,
                    "file": str(path.relative_to(package)).replace("\\", "/"),
                    "sha256": _sha256(path),
                    "quality_score": 0.9 + cycle / 100,
                    "alignment_offset_samples": 40,
                }
            )
    (package / "heartbeat_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "builder": {"name": "test", "version": "1"},
                "event_samples": manifest_samples,
            }
        ),
        encoding="utf-8",
    )
    with (package / "heartbeat_events.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["event_type", "time_s"])
        writer.writeheader()
        writer.writerows(
            [
                {"event_type": "S1", "time_s": 0.0},
                {"event_type": "S2", "time_s": 0.25},
            ]
        )
    return package


def _write_final_midi(path: Path, plan, *, omit_last: bool = False) -> int:
    ppq = 480
    notes = []
    for section in plan.sections:
        for bar in range(section.bar_start, section.bar_end + 1):
            for beat in section.trigger_beats:
                notes.append(
                    MidiNote(
                        start_tick=(bar - 1) * 4 * ppq + int(round((beat - 1) * ppq)),
                        duration_ticks=120,
                        pitch=36,
                        velocity=100,
                        channel=9,
                    )
                )
    if omit_last:
        notes.pop()
    path.write_bytes(
        midi_bytes(
            resolution=ppq,
            tempo=625_000,
            numerator=4,
            denominator=4,
            tracks=[MidiTrack("Heartbeat Trigger", 0, "drum", notes)],
            end_tick=plan.sections[-1].bar_end * 4 * ppq,
        )
    )
    return len(notes)


class HeartbeatPostRendererTests(unittest.TestCase):
    def test_tempo_map_converts_across_boundary(self):
        tempo = TempoMap(480, (TempoChange(0, 500_000), TempoChange(480, 1_000_000)))
        self.assertAlmostEqual(tempo.seconds_at_tick(480), 0.5)
        self.assertAlmostEqual(tempo.seconds_at_tick(960), 1.5)
        self.assertEqual(tempo.tick_at_seconds(1.5), 960)

    def test_integrates_agent_plan_final_midi_and_heartbeat_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = _make_plan()
            plan_path = root / "heartbeat_processing_plan.json"
            plan_path.write_text(
                json.dumps(plan.model_dump(mode="json"), ensure_ascii=False),
                encoding="utf-8",
            )
            package = _write_package(root)
            midi_path = root / "final.mid"
            trigger_count = _write_final_midi(midi_path, plan)

            result = render_heartbeat_track(
                midi_path,
                plan_path,
                package,
                root / "render",
                render_diagnostics=False,
            )

            self.assertEqual(trigger_count, 96)
            self.assertEqual(result.trigger_count, 96)
            self.assertEqual(result.event_count, 128)
            self.assertTrue(result.heartbeat_wav.is_file())
            self.assertTrue(result.heartbeat_midi.is_file())
            audio = audio_diagnostics.load_wav(result.heartbeat_wav)
            self.assertEqual(audio.sample_rate, 8000)
            rendered_midi = parse_timing_midi(
                result.heartbeat_midi,
                trigger_notes=(36, 38),
            )
            self.assertEqual(len(rendered_midi.triggers), 128)
            manifest = json.loads(result.manifest_json.read_text(encoding="utf-8"))
            self.assertEqual(manifest["story_id"], "test-mode-story")
            self.assertEqual(manifest["render"]["s1_count"], 96)
            self.assertEqual(manifest["render"]["s2_count"], 32)
            self.assertFalse(manifest["policy"]["time_stretch"])

    def test_rejects_final_drum_track_that_no_longer_matches_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = _make_plan()
            plan_path = root / "heartbeat_processing_plan.json"
            plan_path.write_text(
                json.dumps(plan.model_dump(mode="json")), encoding="utf-8"
            )
            package = _write_package(root)
            midi_path = root / "missing-trigger.mid"
            _write_final_midi(midi_path, plan, omit_last=True)
            with self.assertRaisesRegex(HeartbeatPostRenderError, "trigger count"):
                render_heartbeat_track(
                    midi_path,
                    plan_path,
                    package,
                    root / "render",
                    render_diagnostics=False,
                )


if __name__ == "__main__":
    unittest.main()
