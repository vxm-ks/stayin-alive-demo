"""Run every offline module test suite without network or model weights."""

from __future__ import annotations

import sys
import subprocess


TEST_DIRECTORIES = (
    "heart_extraction/tests",
    "tempo_bar_renderer/tests",
    "heartbeat_stage1/tests",
    "heartbeat_midi_exporter/tests",
    "midi_motif_detector/tests",
    "stage1_story_agent/tests",
    "midigpt_scaffold_builder/tests",
    "musecoco_runtime_bridge/tests",
    "stage3_midi_renderer/tests",
    "wav_track_mixer/tests",
    "heartbeat_post_renderer/tests",
)


def main() -> int:
    failures: list[str] = []
    for directory in TEST_DIRECTORIES:
        print(f"\n=== {directory} ===", flush=True)
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", directory, "-v"],
            check=False,
        )
        if completed.returncode:
            failures.append(directory)
    if failures:
        print("\nFailed suites: " + ", ".join(failures), file=sys.stderr)
        return 1
    print(f"\nAll {len(TEST_DIRECTORIES)} offline suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
