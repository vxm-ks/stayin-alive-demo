"""Run Stage 3 mixing comparisons from a user-supplied case configuration."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PLANS = HERE / "plans"
MODES = {
    "direct_manual": PLANS / "direct_manual.json",
    "global_relative": PLANS / "global_relative.json",
    "adaptive_current": PLANS / "adaptive_current.json",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render each configured MIDI/heartbeat pair with three mix plans."
    )
    parser.add_argument(
        "--cases-json",
        required=True,
        type=Path,
        help="JSON mapping case names to input_midi and heartbeat_package paths.",
    )
    parser.add_argument("--general-sf2", required=True, type=Path)
    parser.add_argument("--fluidsynth", required=True, type=Path)
    parser.add_argument("--output-root", type=Path, default=HERE / "outputs")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    return parser.parse_args()


def load_cases(path: Path) -> dict[str, dict[str, Path]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("cases JSON must be a non-empty object")

    cases: dict[str, dict[str, Path]] = {}
    for name, item in payload.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise ValueError("each case must be a named object")
        try:
            input_midi = Path(item["input_midi"]).expanduser().resolve()
            heartbeat_package = Path(item["heartbeat_package"]).expanduser().resolve()
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"{name!r} must define input_midi and heartbeat_package"
            ) from exc
        if not input_midi.is_file():
            raise FileNotFoundError(f"missing input MIDI for {name}: {input_midi}")
        if not heartbeat_package.is_dir():
            raise FileNotFoundError(
                f"missing heartbeat package for {name}: {heartbeat_package}"
            )
        cases[name] = {
            "input_midi": input_midi,
            "heartbeat_package": heartbeat_package,
        }
    return cases


def write_progress(progress_path: Path, data: dict) -> None:
    temporary = progress_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(progress_path)


def main() -> int:
    args = parse_args()
    cases = load_cases(args.cases_json)
    output_root = args.output_root.resolve()
    logs = output_root.parent / "logs"
    progress_path = output_root.parent / "ablation_progress.json"
    output_root.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    progress = {
        "schema_version": "stage3-ablation-progress-v1",
        "started_at": now(),
        "updated_at": now(),
        "status": "RUNNING",
        "tasks": {},
    }
    write_progress(progress_path, progress)

    for case_name, case in cases.items():
        for mode_name, plan in MODES.items():
            task_name = f"{case_name}_{mode_name}"
            output_dir = output_root / task_name
            manifest = output_dir / "stage3_render_manifest.json"
            task = {
                "case": case_name,
                "mode": mode_name,
                "status": "RUNNING",
                "started_at": now(),
                "output_dir": str(output_dir),
            }
            progress["tasks"][task_name] = task
            progress["updated_at"] = now()
            write_progress(progress_path, progress)

            if manifest.is_file():
                task.update(status="SKIPPED_EXISTING", finished_at=now(), returncode=0)
                progress["updated_at"] = now()
                write_progress(progress_path, progress)
                continue

            command = [
                str(args.python.resolve()),
                "-m",
                "stage3_midi_renderer",
                "render-packages",
                "--input-midi",
                str(case["input_midi"]),
                "--general-sf2",
                str(args.general_sf2.resolve()),
                "--render-plan",
                str(plan),
                "--heartbeat-package",
                f"patient={case['heartbeat_package']}",
                "--output-dir",
                str(output_dir),
                "--fluidsynth",
                str(args.fluidsynth.resolve()),
            ]
            stdout_path = logs / f"{task_name}.stdout.log"
            stderr_path = logs / f"{task_name}.stderr.log"
            with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr:
                completed = subprocess.run(
                    command,
                    cwd=REPO,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                )

            task.update(
                status="COMPLETED" if completed.returncode == 0 else "FAILED",
                finished_at=now(),
                returncode=completed.returncode,
                stdout=str(stdout_path),
                stderr=str(stderr_path),
            )
            progress["updated_at"] = now()
            write_progress(progress_path, progress)
            if completed.returncode != 0:
                progress["status"] = "FAILED"
                progress["finished_at"] = now()
                write_progress(progress_path, progress)
                return completed.returncode

    progress["status"] = "COMPLETED"
    progress["finished_at"] = now()
    progress["updated_at"] = now()
    write_progress(progress_path, progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
