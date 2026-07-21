from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import stage1_story_agent.artifacts as artifacts_module
from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.artifacts import delivery_directories, write_plan_run
from stage1_story_agent.backends import FakeBackend
from stage1_story_agent.errors import ArtifactWriteError
from stage1_story_agent.tests.fixtures import draft_data, make_request


def make_run():
    return Stage1StoryAgent(FakeBackend([json.dumps(draft_data(), ensure_ascii=False)])).plan(make_request())


class ArtifactTests(unittest.TestCase):
    def test_writes_complete_success_directory_and_matching_hashes(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "story-001"
            run = make_run()
            destinations = write_plan_run(run, output)
            self.assertEqual(set(destinations), {"musecoco", "heartbeat", "stage2", "audit"})
            self.assertEqual(
                {item.name for item in destinations["musecoco"].iterdir()},
                {
                    "musecoco_plan.json",
                    "predict.json",
                    "predict_index.json",
                    "direct_attribute_labels.json",
                    "predict_attributes.json",
                    "encoding_manifest.json",
                    "task_packages",
                },
            )
            task_dirs = sorted((destinations["musecoco"] / "task_packages").iterdir())
            self.assertEqual(len(task_dirs), 3)
            self.assertEqual(
                {item.name for item in task_dirs[0].iterdir()},
                {"predict_attributes.json", "predict.json", "predict_index.json", "audit.json"},
            )
            self.assertEqual({item.name for item in destinations["heartbeat"].iterdir()}, {"heartbeat_processing_plan.json"})
            self.assertEqual({item.name for item in destinations["stage2"].iterdir()}, {"stage2_plan.json"})
            self.assertEqual({item.name for item in destinations["audit"].iterdir()}, {"content_plan.json", "raw_response.json", "run_manifest.json"})
            manifest = json.loads((destinations["audit"] / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"], run.manifest.files)
            raw = (destinations["audit"] / "raw_response.json").read_text(encoding="utf-8")
            self.assertNotIn("reasoning_content", raw)

    def test_consumer_deliveries_do_not_mix_responsibilities(self):
        with tempfile.TemporaryDirectory() as root:
            destinations = write_plan_run(make_run(), Path(root) / "story-001")
            musecoco = (destinations["musecoco"] / "musecoco_plan.json").read_text(encoding="utf-8").lower()
            heartbeat = (destinations["heartbeat"] / "heartbeat_processing_plan.json").read_text(encoding="utf-8").lower()
            stage2 = (destinations["stage2"] / "stage2_plan.json").read_text(encoding="utf-8").lower()
            self.assertIn("musecoco_text", musecoco)
            self.assertNotIn("midigpt_access", musecoco)
            self.assertIn("final_midigpt_drum_track", heartbeat)
            self.assertNotIn("musecoco_text", heartbeat)
            self.assertIn("midigpt_access", stage2)
            self.assertNotIn("heart", stage2)

    def test_existing_directory_is_preserved_without_force(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "story-001"
            target = delivery_directories(output)["stage2"]
            target.mkdir()
            marker = target / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaises(ArtifactWriteError) as caught:
                write_plan_run(make_run(), output)
            self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_force_replaces_existing_directory(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "story-001"
            target = delivery_directories(output)["stage2"]
            target.mkdir()
            (target / "old.txt").write_text("old", encoding="utf-8")
            write_plan_run(make_run(), output, force=True)
            self.assertFalse((target / "old.txt").exists())
            self.assertTrue((target / "stage2_plan.json").exists())

    def test_force_publish_failure_restores_old_directory(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "story-001"
            destinations = delivery_directories(output)
            for target in destinations.values():
                target.mkdir()
            marker = destinations["stage2"] / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            original_replace = artifacts_module.os.replace

            def fail_staging_publish(source, destination):
                source_path = Path(source)
                if source_path.name.startswith(".story-001-stage2.staging-"):
                    raise OSError("simulated publish failure")
                return original_replace(source, destination)

            with patch("stage1_story_agent.artifacts.os.replace", side_effect=fail_staging_publish):
                with self.assertRaises(ArtifactWriteError) as caught:
                    write_plan_run(make_run(), output, force=True)
            self.assertEqual(caught.exception.code, "ATOMIC_PUBLISH_FAILED")
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
