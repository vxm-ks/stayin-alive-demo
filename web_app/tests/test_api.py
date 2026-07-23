from __future__ import annotations

import io
import os
import time
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
        response = self.client.post(
            "/api/tasks",
            files={"heartbeat": ("heartbeat.wav", synthetic_wav(), "audio/wav")},
            data={"story": "A quiet summer night.", "dry_run": "true"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()["task_id"]
        state = response.json()
        for _ in range(100):
            state = self.client.get(f"/api/tasks/{task_id}").json()
            if state["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.05)
        self.assertEqual(state["status"], "COMPLETED", state)
        self.assertEqual(state["progress"], 100)
        audio = self.client.get(f"/api/tasks/{task_id}/audio")
        self.assertEqual(audio.status_code, 200)
        self.assertTrue(audio.content.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
