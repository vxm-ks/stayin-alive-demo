from __future__ import annotations

import io
import os
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from web_app.api import _config, app, tasks


def synthetic_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000)
    return output.getvalue()


class ImmediateThread:
    def __init__(self, *, target, args, **kwargs):
        del kwargs
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        tasks.clear()
        self.client = TestClient(app)

    def test_health(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_web_config_forwards_formal_pipeline_settings(self) -> None:
        settings = {
            "LEGASYNTH_MIDIGPT_PYTHON": r"D:\runtime\midigpt\python.exe",
            "LEGASYNTH_MIDIGPT_MODEL": "yellow",
            "LEGASYNTH_TONALITY_POLICY": "loose",
            "LEGASYNTH_STAGE2_REPETITION_MODE": "detect",
            "LEGASYNTH_STAGE2_REPETITION_THRESHOLD": "0.75",
            "LEGASYNTH_STAGE2_REPETITION_MAX_OCCURRENCES": "1",
            "LEGASYNTH_STAGE2_REPETITION_CANDIDATES": "3",
        }
        with patch.dict(os.environ, settings, clear=False):
            config = _config()
        self.assertEqual(
            config.midigpt_python, Path(r"D:\runtime\midigpt\python.exe")
        )
        self.assertEqual(config.midigpt_model, "yellow")
        self.assertEqual(config.tonality_policy, "loose")
        self.assertEqual(config.stage2_repetition_mode, "detect")
        self.assertEqual(config.stage2_repetition_threshold, 0.75)
        self.assertEqual(config.stage2_repetition_max_occurrences, 1)
        self.assertEqual(config.stage2_repetition_candidates, 3)

    def test_dry_run_task_publishes_audio(self) -> None:
        with patch("web_app.api.threading.Thread", ImmediateThread):
            response = self.client.post(
                "/api/tasks",
                files={"heartbeat": ("heartbeat.wav", synthetic_wav(), "audio/wav")},
                data={
                    "story": "A quiet summer night.",
                    "dry_run": "true",
                    "test_mode": "false",
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        state = self.client.get(f"/api/tasks/{task_id}").json()
        self.assertEqual(state["status"], "COMPLETED", state)
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["generation_mode"], "dry_run")
        self.assertFalse(state["test_mode"])
        audio = self.client.get(f"/api/tasks/{task_id}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertTrue(audio.content.startswith(b"RIFF"))

    def test_dry_run_and_test_mode_cannot_be_combined(self) -> None:
        response = self.client.post(
            "/api/tasks",
            files={"heartbeat": ("heartbeat.wav", synthetic_wav(), "audio/wav")},
            data={
                "story": "A quiet summer night.",
                "dry_run": "true",
                "test_mode": "true",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
