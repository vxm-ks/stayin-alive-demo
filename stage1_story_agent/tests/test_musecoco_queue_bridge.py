from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from stage1_story_agent.musecoco_queue_bridge import (
    MuseCocoQueueBridgeError,
    enqueue_musecoco_tasks,
)


class MuseCocoQueueBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.packages = self.root / "story-musecoco" / "task_packages"
        task = self.packages / "task_001_story_theme_hash"
        task.mkdir(parents=True)
        for name in ("predict_attributes.json", "predict.json", "predict_index.json", "audit.json"):
            (task / name).write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_bridge_converts_path_enqueues_and_optionally_runs(self):
        commands = []

        def fake_executor(command, **kwargs):
            del kwargs
            commands.append(list(command))
            if "wslpath" in command:
                converted = (
                    "/mnt/d/raw_results\n"
                    if str(command[-1]).endswith("raw_results")
                    else "/mnt/d/task_packages\n"
                )
                return subprocess.CompletedProcess(command, 0, converted, "")
            if "enqueue" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"enqueued": ["task_001_story_theme_hash"], "skipped_already_done": []}),
                    "",
                )
            if "collect" in command:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps({"status": "PASS", "destination": "/mnt/d/raw_results"}),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "queue complete", "")

        result = enqueue_musecoco_tasks(
            self.packages.parent,
            run_queue=True,
            max_tasks=1,
            max_attempts=4,
            executor=fake_executor,
        )
        self.assertTrue(result.queue_ran)
        self.assertEqual(result.enqueue_result["enqueued"], ["task_001_story_theme_hash"])
        self.assertEqual(len(commands), 5)
        self.assertIn("legasynth-musecoco", commands[1])
        self.assertEqual(commands[1][-3:], ["enqueue", "--source", "/mnt/d/task_packages"])
        self.assertEqual(
            commands[2][-4:],
            ["--max-attempts", "4", "--max-tasks", "1"],
        )
        self.assertEqual(commands[4][-5:], ["collect", "--source", "/mnt/d/task_packages", "--destination", "/mnt/d/raw_results"])
        self.assertEqual(result.collected_results_dir, self.packages.parent / "raw_results")

    def test_bridge_rejects_incomplete_task_before_wsl(self):
        (next(self.packages.iterdir()) / "predict.json").unlink()
        with self.assertRaisesRegex(MuseCocoQueueBridgeError, "files mismatch"):
            enqueue_musecoco_tasks(self.packages, executor=lambda *_args, **_kwargs: None)


if __name__ == "__main__":
    unittest.main()
