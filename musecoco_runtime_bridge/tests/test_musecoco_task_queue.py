from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from musecoco_runtime_bridge.musecoco_task_queue import (
    MuseCocoQueueError,
    RuntimePaths,
    cleanup_runtime_state,
    collect_task_results,
    enqueue_task_packages,
    recover_running_tasks,
    run_queue,
    run_worker,
    sha256_file,
    validate_task_package,
    verify_clean_state,
)
from stage1_story_agent.musecoco_encoder import ATT_KEY, build_task_package_files
from stage1_story_agent.tests.test_musecoco_encoder import make_delivery


VALID_REMI = "I4_0 <sep> s-9 t-32 o-0 i-0 p-60 d-1 v-1 b-1"
VALID_MIDI = (
    b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x01\xe0"
    b"MTrk\x00\x00\x00\x04\xff\x2f\x00"
)


class MuseCocoTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        root = self.home / "musecoco_runtime"
        output = self.home / "MuseCoco_outputs"
        tools = self.home / "musecoco_tools"
        conda = self.home / "miniforge3" / "envs" / "MuseCoco"
        self.paths = RuntimePaths(
            home=self.home,
            root=root,
            stage1=root / "1-text2attribute_model",
            stage2=root / "2-attribute2music_model",
            output_root=output,
            tools=tools,
            queue=root / "task_queue",
            running=root / "task_running",
            failed=root / "task_failed",
            done=root / "task_done",
            lock=root / "task_queue.lock",
            queue_logs=output / "queue_logs",
            runtime_backups=output / "runtime_state_backups",
            conda_prefix=conda,
        )
        (self.paths.stage1 / "data").mkdir(parents=True)
        (self.paths.stage1 / "tmp").mkdir()
        (self.paths.stage2 / "linear_mask").mkdir(parents=True)
        (self.paths.stage2 / "data" / "infer_input").mkdir(parents=True)
        (self.paths.stage1 / "data" / "att_key.json").write_text(
            json.dumps(list(ATT_KEY)), encoding="utf-8"
        )
        (self.paths.stage1 / "stage2_pre.py").write_text(
            "from pathlib import Path\nPath('infer_test.bin').write_bytes(b'fake-infer')\n",
            encoding="utf-8",
        )
        generation = (
            "generation/0505/linear_mask-1billion-checkpoint_2_280000/"
            "infer_test/topk15-t1.0-ngram0/0"
        )
        (self.paths.stage2 / "interactive_1billion.sh").write_bytes(
            (
                "#!/bin/bash\nset -e\n"
                f"mkdir -p '{generation}/remi' '{generation}/midi'\n"
                f"printf '{VALID_REMI}' > '{generation}/remi/0.txt'\n"
                f"printf '\\x4d\\x54\\x68\\x64\\x00\\x00\\x00\\x06"
                f"\\x00\\x00\\x00\\x01\\x01\\xe0\\x4d\\x54\\x72\\x6b"
                f"\\x00\\x00\\x00\\x04\\xff\\x2f\\x00' > '{generation}/midi/0.mid'\n"
            ).encode("utf-8")
        )
        for name in ("interactive_dict_v5_1billion.py", "A2M_task_new.py"):
            (self.paths.stage2 / "linear_mask" / name).write_text(
                "# immutable fake official file\n", encoding="utf-8"
            )
        self.expected_hashes = {
            name: sha256_file(path)
            for name, path in self.paths.official_files().items()
        }
        package_files = build_task_package_files(make_delivery())
        first_root = sorted({path.split("/")[1] for path in package_files})[0]
        self.task_source = Path(self.temp.name) / "packages"
        self.task = self.task_source / first_root
        self.task.mkdir(parents=True)
        prefix = f"task_packages/{first_root}/"
        for name, data in package_files.items():
            if name.startswith(prefix):
                (self.task / name.removeprefix(prefix)).write_bytes(data)

    def tearDown(self):
        self.temp.cleanup()

    def test_task_is_one_sample_complete_and_hash_checked(self):
        validated = validate_task_package(self.task, self.paths)
        self.assertEqual(len(validated["attributes"]), 60)
        self.assertTrue(all(len(batch) == 1 for batch in validated["attributes"].values()))
        (self.task / "predict.json").write_text("[]", encoding="utf-8")
        with self.assertRaises(MuseCocoQueueError):
            validate_task_package(self.task, self.paths)

    def test_cleanup_snapshots_before_removing_only_allowed_runtime_state(self):
        self.paths.stage1_predict.write_text('[{"text":"old"}]', encoding="utf-8")
        self.paths.stage1_bin.write_bytes(b"old-stage1")
        self.paths.stage2_bin.write_bytes(b"old-stage2")
        old_generation = self.paths.generation_dir / "old.txt"
        old_generation.parent.mkdir(parents=True)
        old_generation.write_text("old", encoding="utf-8")
        snapshots = Path(self.temp.name) / "snapshots"
        snapshot = cleanup_runtime_state(self.paths, snapshots, "test")
        self.assertIsNotNone(snapshot)
        self.assertTrue((snapshot / "snapshot_manifest.json").is_file())
        self.assertTrue(
            (snapshot / "musecoco_runtime" / "1-text2attribute_model" / "infer_test.bin").is_file()
        )
        verify_clean_state(self.paths)
        self.assertTrue((self.paths.stage1 / "stage2_pre.py").is_file())

    def test_worker_runs_official_entries_archives_results_and_cleans(self):
        def fake_run_logged(command, cwd, log_path, environment):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake command completed\n", encoding="utf-8")
            if Path(command[0]).name.startswith("python"):
                self.paths.stage1_bin.write_bytes(b"fake-infer")
            else:
                self.paths.remi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.remi_file.write_text(VALID_REMI, encoding="utf-8")
                self.paths.midi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.midi_file.write_bytes(VALID_MIDI)
            return 0

        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue._run_logged",
            side_effect=fake_run_logged,
        ):
            output = run_worker(
                self.task,
                self.paths,
                expected_hashes=self.expected_hashes,
                enforce_environment=False,
            )
        self.assertGreater((output / "result.remi.txt").stat().st_size, 0)
        self.assertGreater((output / "result.mid").stat().st_size, 0)
        self.assertEqual(
            (output / "predict_attributes.json").read_bytes(),
            (output / "softmax_probs.json").read_bytes(),
        )
        audit = json.loads((output / "result_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "success")
        self.assertEqual(audit["probability_source"], "deterministic_one_hot")
        self.assertTrue(audit["runtime_clean_verified"])
        self.assertEqual(audit["output_validation"]["status"], "passed")
        verify_clean_state(self.paths)

        enqueue_task_packages(self.task_source, self.paths)
        queued = self.paths.queue / self.task.name
        queued.rename(self.paths.done / self.task.name)
        collected = Path(self.temp.name) / "collected"
        result = collect_task_results(
            self.task_source,
            collected,
            self.paths,
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            (collected / self.task.name / "raw.mid").read_bytes(),
            (output / "result.mid").read_bytes(),
        )
        manifest = json.loads(
            (collected / "collection_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["items"][0]["theme_family_id"], "theme-A")

    def test_worker_rejects_remi_when_official_decoder_produces_no_midi(self):
        def fake_run_logged(command, cwd, log_path, environment):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake command completed\n", encoding="utf-8")
            if Path(command[0]).name.startswith("python"):
                self.paths.stage1_bin.write_bytes(b"fake-infer")
            else:
                self.paths.remi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.remi_file.write_text(VALID_REMI, encoding="utf-8")
            return 0

        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue._run_logged",
            side_effect=fake_run_logged,
        ), self.assertRaisesRegex(MuseCocoQueueError, "no MIDI was decoded"):
            run_worker(
                self.task,
                self.paths,
                expected_hashes=self.expected_hashes,
                enforce_environment=False,
            )

        output = max(self.paths.output_root.glob("from_task_*"))
        audit = json.loads((output / "result_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "failed")
        self.assertEqual(audit["output_validation"]["status"], "failed")
        self.assertIn("no MIDI was decoded", audit["primary_error"])
        verify_clean_state(self.paths)

    def test_worker_rejects_invalid_remi_transition_even_if_midi_exists(self):
        malformed_remi = (
            "I4_0 <sep> s-9 t-32 o-0 i-0 p-60 d-1 v-1 "
            "o-10 s-9 d-7 v-1 b-1"
        )

        def fake_run_logged(command, cwd, log_path, environment):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake command completed\n", encoding="utf-8")
            if Path(command[0]).name.startswith("python"):
                self.paths.stage1_bin.write_bytes(b"fake-infer")
            else:
                self.paths.remi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.remi_file.write_text(malformed_remi, encoding="utf-8")
                self.paths.midi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.midi_file.write_bytes(VALID_MIDI)
            return 0

        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue._run_logged",
            side_effect=fake_run_logged,
        ), self.assertRaisesRegex(MuseCocoQueueError, "duration follows 's'"):
            run_worker(
                self.task,
                self.paths,
                expected_hashes=self.expected_hashes,
                enforce_environment=False,
            )
        verify_clean_state(self.paths)

    def test_worker_truncates_only_corruption_after_generation_target(self):
        complete_bar = "s-9 t-32 o-0 i-0 p-60 d-1 v-1 b-1"
        malformed_tail = "o-10 s-9 d-7 v-1"
        remi = "I4_0 <sep> " + " ".join([complete_bar] * 12) + " " + malformed_tail

        def fake_run_logged(command, cwd, log_path, environment):
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("fake command completed\n", encoding="utf-8")
            if Path(command[0]).name.startswith("python"):
                self.paths.stage1_bin.write_bytes(b"fake-infer")
            else:
                self.paths.remi_file.parent.mkdir(parents=True, exist_ok=True)
                self.paths.remi_file.write_text(remi, encoding="utf-8")
            return 0

        def fake_decode(remi_path, midi_path, paths):
            midi_path.write_bytes(VALID_MIDI)

        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue._run_logged",
            side_effect=fake_run_logged,
        ), patch(
            "musecoco_runtime_bridge.musecoco_task_queue._decode_remi_to_midi",
            side_effect=fake_decode,
        ):
            output = run_worker(
                self.task,
                self.paths,
                expected_hashes=self.expected_hashes,
                enforce_environment=False,
            )

        audit = json.loads((output / "result_audit.json").read_text(encoding="utf-8"))
        validation = audit["output_validation"]
        self.assertEqual(validation["status"], "passed")
        self.assertEqual(validation["mode"], "target_bar_truncation_fallback")
        self.assertEqual(validation["truncation"]["target_bars"], 12)
        self.assertEqual(validation["truncation"]["discarded_music_token_count"], 4)
        self.assertIn("d-7", (output / "result.remi.raw.txt").read_text(encoding="utf-8"))
        self.assertNotIn("d-7", (output / "result.remi.txt").read_text(encoding="utf-8"))
        verify_clean_state(self.paths)

    def test_enqueue_is_atomic_and_done_task_is_idempotent(self):
        result = enqueue_task_packages(self.task_source, self.paths)
        self.assertEqual(result["enqueued"], [self.task.name])
        queued = self.paths.queue / self.task.name
        self.assertTrue(queued.is_dir())
        self.paths.done.mkdir(parents=True, exist_ok=True)
        queued.rename(self.paths.done / self.task.name)
        repeated = enqueue_task_packages(self.task_source, self.paths)
        self.assertEqual(repeated["skipped_already_done"], [self.task.name])

    def test_collector_rejects_legacy_success_without_output_validation(self):
        enqueue_task_packages(self.task_source, self.paths)
        queued = self.paths.queue / self.task.name
        queued.rename(self.paths.done / self.task.name)
        output = self.paths.output_root / f"from_task_legacy_{self.task.name}"
        output.mkdir(parents=True)
        (output / "result.remi.txt").write_text(VALID_REMI, encoding="utf-8")
        (output / "result.mid").write_bytes(VALID_MIDI)
        task_audit = json.loads((self.task / "audit.json").read_text(encoding="utf-8"))
        (output / "result_audit.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "task_id": self.task.name,
                    "task_audit": task_audit,
                    "output_sha256": {
                        "result.remi.txt": sha256_file(output / "result.remi.txt"),
                        "result.mid": sha256_file(output / "result.mid"),
                    },
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MuseCocoQueueError, "no successful"):
            collect_task_results(
                self.task_source,
                Path(self.temp.name) / "collected",
                self.paths,
            )

    def test_running_residue_moves_to_failed_not_back_to_queue(self):
        self.paths.running.mkdir(parents=True, exist_ok=True)
        self.paths.failed.mkdir(parents=True, exist_ok=True)
        residue = self.paths.running / self.task.name
        residue.mkdir()
        recovered = recover_running_tasks(self.paths)
        self.assertEqual(len(recovered), 1)
        self.assertFalse(residue.exists())
        self.assertTrue((self.paths.failed / recovered[0]).is_dir())

    def test_queue_retries_failed_task_then_succeeds(self):
        enqueue_task_packages(self.task_source, self.paths)
        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue.validate_official_installation",
            return_value={},
        ), patch(
            "musecoco_runtime_bridge.musecoco_task_queue._stream_worker",
            side_effect=[2, 0],
        ) as worker:
            result = run_queue(self.paths, max_attempts=3)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["attempts_by_task"][self.task.name], 2)
        self.assertEqual(worker.call_count, 2)
        self.assertTrue((self.paths.done / self.task.name).is_dir())
        self.assertEqual(list(self.paths.failed.iterdir()), [])

    def test_queue_exhausts_retries_then_continues_with_next_task(self):
        enqueue_task_packages(self.task_source, self.paths)
        second = self.paths.queue / "task_999_unprocessed"
        shutil.copytree(self.task, second)
        with patch(
            "musecoco_runtime_bridge.musecoco_task_queue.validate_official_installation",
            return_value={},
        ), patch(
            "musecoco_runtime_bridge.musecoco_task_queue._stream_worker",
            side_effect=[2, 2, 0],
        ) as worker:
            result = run_queue(self.paths, max_attempts=2)
        self.assertEqual(result["status"], "PARTIAL_FAILURE")
        self.assertEqual(result["attempts_by_task"][self.task.name], 2)
        self.assertEqual(result["attempts_by_task"][second.name], 1)
        self.assertEqual(worker.call_count, 3)
        self.assertTrue((self.paths.done / second.name).is_dir())
        self.assertEqual(len(list(self.paths.failed.iterdir())), 1)
        self.assertEqual(list(self.paths.running.iterdir()), [])
        self.assertEqual(list(self.paths.queue.iterdir()), [])
        self.assertFalse(self.paths.lock.exists())


if __name__ == "__main__":
    unittest.main()
