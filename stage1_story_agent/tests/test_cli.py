from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from stage1_story_agent.backends import FakeBackend
from stage1_story_agent.artifacts import delivery_directories
from stage1_story_agent.cli import build_parser, build_request_from_text, main, timestamped_output_dir
from stage1_story_agent.tests.fixtures import draft_data, request_data, test_mode_draft_data


class ClosingFakeBackend(FakeBackend):
    def close(self):
        return None


def default_32_bar_draft() -> dict:
    data = draft_data()
    data["story_analysis"]["emotional_arc"] = [
        {"position": 0, "emotion": "calm", "valence": 0.2, "tension": 0.2},
        {"position": 1, "emotion": "resolved", "valence": 0.5, "tension": 0.3},
    ]
    data["story_analysis"]["narrative_segments"] = data["story_analysis"]["narrative_segments"][:1]
    data["form_sections"] = data["form_sections"][:1]
    data["form_sections"][0]["bar_count"] = 32
    data["theme_families"] = data["theme_families"][:1]
    return data


class CliTests(unittest.TestCase):
    def test_default_output_directory_uses_timestamp_only(self):
        path = timestamped_output_dir(datetime(2026, 7, 17, 15, 30, 45, 123456))
        self.assertEqual(
            path,
            Path("stage1_story_agent") / "outputs" / "20260717-153045-123456",
        )

    def test_natural_text_converts_to_request_defaults(self):
        request = build_request_from_text("这是一个中文故事。")
        payload = request.model_dump(mode="json", by_alias=True)
        self.assertEqual(payload["story_id"], "cli-story")
        self.assertEqual(payload["language"], "zh-CN")
        self.assertEqual(payload["constraints"]["total_bars"], 32)
        self.assertIsNone(payload["constraints"]["target_form_sections"])
        self.assertEqual(payload["constraints"]["allowed_time_signatures"], ["4/4"])
        self.assertEqual(payload["constraints"]["musecoco_output_bars"], 8)
        self.assertEqual(payload["constraints"]["musecoco_generation_bars"], 12)

    def test_natural_text_accepts_configurable_musecoco_lengths(self):
        request = build_request_from_text(
            "一个中文故事。",
            musecoco_output_bars=6,
            musecoco_generation_bars=10,
        )
        self.assertEqual(request.constraints.musecoco_output_bars, 6)
        self.assertEqual(request.constraints.musecoco_generation_bars, 10)

    def test_natural_text_runs_complete_cli_with_fake_backend(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "natural-output"
            backend = ClosingFakeBackend([json.dumps(default_32_bar_draft(), ensure_ascii=False)])
            environment = {
                "DEEPSEEK_API_KEY": "test-secret",
                "STAGE1_ENV_FILE": str(Path(root) / "missing.env"),
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch("stage1_story_agent.cli.DeepSeekBackend", return_value=backend):
                    with patch("stage1_story_agent.cli.timestamped_output_dir", return_value=output):
                        code = main([
                            "plan",
                            "--story-text",
                            "一个平静而完整的中文故事。",
                        ])
            self.assertEqual(code, 0)
            plan = json.loads((delivery_directories(output)["audit"] / "content_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["story_id"], "cli-story")
            self.assertEqual(plan["global"]["total_bars"], 32)
            self.assertEqual(plan["form_plan"]["form_string"], "A")
            prompt_payload = json.loads(backend.requests[0].messages[1].content)
            normalized = prompt_payload["request"]
            self.assertEqual(normalized["story_text"], "一个平静而完整的中文故事。")
            self.assertEqual(normalized["constraints"]["total_bars"], 32)

    def test_cli_test_mode_generates_fixed_aba(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "test-mode-output"
            backend = ClosingFakeBackend([json.dumps(test_mode_draft_data(), ensure_ascii=False)])
            environment = {
                "DEEPSEEK_API_KEY": "test-secret",
                "STAGE1_ENV_FILE": str(Path(root) / "missing.env"),
            }
            with patch.dict(os.environ, environment, clear=True):
                with patch("stage1_story_agent.cli.DeepSeekBackend", return_value=backend):
                    code = main([
                        "plan",
                        "--story-text",
                        "平静开始，冲突展开，最终回归。",
                        "--test-mode",
                        "--output-dir",
                        str(output),
                    ])
            self.assertEqual(code, 0)
            plan = json.loads((delivery_directories(output)["audit"] / "content_plan.json").read_text(encoding="utf-8"))
            self.assertTrue(plan["test_mode"])
            self.assertEqual(plan["form_plan"]["form_string"], "A-B-A")
            self.assertEqual([item["bar_count"] for item in plan["form_plan"]["sections"]], [8, 16, 8])
            self.assertEqual(plan["global"]["global_tonality"]["tonic"], "C")
            self.assertEqual(plan["global"]["global_tonality"]["mode"], "minor")
            self.assertEqual(plan["global"]["tempo_bpm"], 96.0)
            self.assertEqual(plan["variation_tasks"], [])

    def test_cli_has_no_api_key_argument(self):
        help_text = build_parser().format_help()
        self.assertNotIn("api-key", help_text)

    def test_cli_does_not_expose_legacy_post_heartbeat_renderer(self):
        help_text = build_parser().format_help()
        self.assertNotIn("render-heartbeat", help_text)

    def test_cli_exposes_task_package_queue_without_api_secret_arguments(self):
        args = build_parser().parse_args([
            "enqueue-musecoco",
            "--musecoco-dir", "story-musecoco",
            "--run",
            "--max-tasks", "1",
            "--max-attempts", "4",
        ])
        self.assertEqual(args.command, "enqueue-musecoco")
        self.assertTrue(args.run)
        self.assertEqual(args.max_tasks, 1)
        self.assertEqual(args.max_attempts, 4)

    def test_missing_key_returns_configuration_exit_and_failure_report(self):
        with tempfile.TemporaryDirectory() as root:
            input_path = Path(root) / "input.json"
            output = Path(root) / "output"
            input_path.write_text(json.dumps(request_data(), ensure_ascii=False), encoding="utf-8")
            environment = dict(os.environ)
            environment.pop("DEEPSEEK_API_KEY", None)
            environment["STAGE1_ENV_FILE"] = str(Path(root) / "missing.env")
            with patch.dict(os.environ, environment, clear=True):
                code = main(["plan", "--input", str(input_path), "--output-dir", str(output)])
            self.assertEqual(code, 2)
            report = json.loads((delivery_directories(output)["audit"] / "failure_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["error_code"], "DEEPSEEK_API_KEY_MISSING")
            self.assertNotIn("test-secret", json.dumps(report).lower())

    def test_invalid_input_returns_input_exit(self):
        with tempfile.TemporaryDirectory() as root:
            input_path = Path(root) / "input.json"
            output = Path(root) / "output"
            input_path.write_text("{}", encoding="utf-8")
            code = main(["plan", "--input", str(input_path), "--output-dir", str(output)])
            self.assertEqual(code, 2)
            report = json.loads((delivery_directories(output)["audit"] / "failure_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["category"], "input")
            self.assertTrue(report["issues"])


if __name__ == "__main__":
    unittest.main()
