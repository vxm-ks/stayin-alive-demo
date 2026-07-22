from __future__ import annotations

import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from legasynth_orchestrator.pipeline import (
    PipelineConfig,
    _dry_heartbeat,
    _dry_stage2,
    _dry_stage3,
    _dry_story,
    run_pipeline,
)


class NoProcessRunner:
    def run(self, *args, **kwargs):
        raise AssertionError("dry-run must not start a subprocess")


class SyntheticProcessRunner:
    def __init__(self):
        self.stages: list[str] = []

    def run(self, stage, command, *, cwd, log_path, timeout_seconds):
        del cwd, timeout_seconds
        self.stages.append(stage)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(repr(command), encoding="utf-8")
        if stage == "heartbeat_stage1":
            root = Path(command[command.index("--output-dir") + 1])
            _dry_heartbeat(root.parents[1], Path(command[2]))
        elif stage == "story_and_musecoco":
            base = Path(command[command.index("--output-dir") + 1])
            request = json.loads(Path(command[command.index("--input") + 1]).read_text(encoding="utf-8"))
            _dry_story(base.parents[1], base, request["story_id"])
        elif stage == "stage2_midigpt":
            output = Path(command[command.index("--output") + 1])
            binding = command[command.index("--heartbeat-package") + 1]
            package_id, package_path = binding.split("=", 1)
            render = Path(command[command.index("--stage3-render-plan") + 1])
            request = json.loads((output.parents[1] / "input" / "story_request.json").read_text(encoding="utf-8"))
            _dry_stage2(output.parent.parent, Path(package_path), render, request["story_id"], package_id)
        elif stage == "stage3_render":
            output_dir = Path(command[command.index("--output-dir") + 1])
            _dry_stage3(output_dir.parent)


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = Path(__file__).resolve().parents[2]
        self.wav = self.root / "input.wav"
        with wave.open(str(self.wav), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 800)
        self.rhythm = self.root / "rhythm.json"
        self.rhythm.write_text(json.dumps({
            "schema_version": "1.0", "plan_id": "test", "meter": "4/4",
            "target_bpm": 96, "pattern_length_beats": 4,
            "repeat_to_fill_bar": True,
            "events": [
                {"type": "S1", "offset_beats": 0},
                {"type": "S2", "offset_beats": 0.5},
            ],
            "event_bank_mode": "paired_rotate", "midi_note_duration_beats": 0.25,
            "peak_target_dbfs": -1.0,
            "audio_policy": {"preserve_real_timbre": True},
        }), encoding="utf-8")
        self.render = self.root / "render.json"
        self.render.write_text(json.dumps({
            "schema_version": "1.0", "heartbeat_channel": 10,
            "note_map": {"36": "S1", "38": "S2"},
            "heartbeat_conditioning_profile": "none", "velocity_gamma": 1.0,
            "default_material_rule": {
                "mode": "round_robin_per_cycle", "package_ids": ["patient"], "gain_db": 0.0,
            },
            "section_material_rules": [],
            "mix": {
                "mode": "manual", "music_gain_db": -4.0, "heartbeat_gain_db": 0.0,
                "true_peak_ceiling_dbtp": -1.0, "publish_stems": False,
            },
        }), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, *, with_assets: bool = False) -> PipelineConfig:
        executable = Path(sys.executable)
        sf2 = self.root / "general.sf2"
        fluid = self.root / "fluidsynth.exe"
        if with_assets:
            sf2.write_bytes(b"sf2")
            fluid.write_bytes(b"exe")
        return PipelineConfig(
            workspace=self.workspace, runtime_root=self.root / "runtime",
            stage1_python=executable, midigpt_python=executable,
            stage3_python=executable, fluidsynth=fluid, general_sf2=sf2,
        )

    def assert_complete_job(self, job: Path, *, dry_run: bool):
        state = json.loads((job / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["current_stage"], "completed")
        self.assertEqual(state["dry_run"], dry_run)
        self.assertTrue((job / "stage2" / "stage3_handoff.json").is_file())
        self.assertTrue((job / "stage3" / "final_mix.wav").is_file())
        self.assertEqual(len(state["artifacts"]["final_mix_sha256"]), 64)

    def test_dry_run_completes_without_starting_any_process(self):
        job = run_pipeline(
            story_text="测试故事", heartbeat_wav=self.wav,
            rhythm_plan=self.rhythm, render_plan=self.render,
            config=self.config(), dry_run=True, runner=NoProcessRunner(),
        )
        self.assert_complete_job(job, dry_run=True)

    def test_production_branch_builds_all_stage_commands_with_fake_runner(self):
        runner = SyntheticProcessRunner()
        job = run_pipeline(
            story_text="测试故事", heartbeat_wav=self.wav,
            rhythm_plan=self.rhythm, render_plan=self.render,
            config=self.config(with_assets=True), dry_run=False, runner=runner,
        )
        self.assertEqual(runner.stages, [
            "heartbeat_stage1", "story_and_musecoco", "stage2_midigpt", "stage3_render",
        ])
        self.assert_complete_job(job, dry_run=False)


if __name__ == "__main__":
    unittest.main()
