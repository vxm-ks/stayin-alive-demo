"""Windows-to-WSL bridge for the external MuseCoco task-package queue."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class MuseCocoQueueBridgeError(RuntimeError):
    """Raised when task publication or WSL queue invocation fails."""


@dataclass(frozen=True)
class MuseCocoQueueBridgeResult:
    task_packages_dir: Path
    wsl_task_packages_dir: str
    enqueue_result: dict[str, object]
    queue_ran: bool
    queue_stdout: str | None = None
    collected_results_dir: Path | None = None
    collection_result: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_packages_dir": str(self.task_packages_dir),
            "wsl_task_packages_dir": self.wsl_task_packages_dir,
            "enqueue_result": self.enqueue_result,
            "queue_ran": self.queue_ran,
            "queue_stdout": self.queue_stdout,
            "collected_results_dir": (
                str(self.collected_results_dir)
                if self.collected_results_dir is not None
                else None
            ),
            "collection_result": self.collection_result,
        }


Executor = Callable[..., subprocess.CompletedProcess[str]]


def _default_executor(command: Sequence[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), **kwargs)


def _run_checked(
    command: Sequence[str],
    *,
    executor: Executor,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = executor(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MuseCocoQueueBridgeError(f"could not invoke WSL MuseCoco queue: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown WSL error").strip()
        raise MuseCocoQueueBridgeError(
            f"WSL MuseCoco queue command failed ({completed.returncode}): {detail[-2000:]}"
        )
    return completed


def _task_package_root(musecoco_dir: str | Path) -> Path:
    source = Path(musecoco_dir).expanduser().resolve()
    packages = source if source.name == "task_packages" else source / "task_packages"
    if not packages.is_dir():
        raise MuseCocoQueueBridgeError(f"task_packages directory not found: {packages}")
    tasks = sorted(path for path in packages.iterdir() if path.is_dir())
    if not tasks:
        raise MuseCocoQueueBridgeError(f"no task packages found: {packages}")
    required = {"predict_attributes.json", "predict.json", "predict_index.json", "audit.json"}
    for task in tasks:
        actual = {path.name for path in task.iterdir() if path.is_file()}
        if actual != required:
            raise MuseCocoQueueBridgeError(
                f"task package {task.name} files mismatch: expected {sorted(required)}, got {sorted(actual)}"
            )
    return packages


def enqueue_musecoco_tasks(
    musecoco_dir: str | Path,
    *,
    wsl_distro: str = "Ubuntu",
    run_queue: bool = False,
    max_tasks: int | None = None,
    max_attempts: int = 3,
    collect_results: bool = True,
    force_collect: bool = False,
    timeout_seconds: int = 86_400,
    queue_command: str | None = None,
    executor: Executor | None = None,
) -> MuseCocoQueueBridgeResult:
    """Atomically enqueue Stage 1 task packages and optionally run the WSL queue."""

    if max_tasks is not None and max_tasks < 1:
        raise MuseCocoQueueBridgeError("max_tasks must be positive")
    if max_tasks is not None and not run_queue:
        raise MuseCocoQueueBridgeError("max_tasks requires run_queue=True")
    if max_attempts < 1:
        raise MuseCocoQueueBridgeError("max_attempts must be positive")
    packages = _task_package_root(musecoco_dir)
    invoke = executor or _default_executor
    prefix = ["wsl.exe", "-d", wsl_distro, "--"]
    queue_executable = queue_command or os.environ.get(
        "MUSECOCO_QUEUE_COMMAND", "legasynth-musecoco"
    )
    if not queue_executable.strip() or any(character.isspace() for character in queue_executable):
        raise MuseCocoQueueBridgeError(
            "MuseCoco queue command must be one executable name or absolute WSL path"
        )
    converted = _run_checked(
        [*prefix, "wslpath", "-a", str(packages)],
        executor=invoke,
        timeout_seconds=30,
    )
    wsl_path = converted.stdout.strip().splitlines()[-1] if converted.stdout.strip() else ""
    if not wsl_path.startswith("/"):
        raise MuseCocoQueueBridgeError(f"wslpath returned an invalid path: {wsl_path!r}")
    enqueued = _run_checked(
        [*prefix, queue_executable, "enqueue", "--source", wsl_path],
        executor=invoke,
        timeout_seconds=300,
    )
    try:
        enqueue_result = json.loads(enqueued.stdout)
    except json.JSONDecodeError as exc:
        raise MuseCocoQueueBridgeError(
            "WSL enqueue returned non-JSON output: " + enqueued.stdout[-1000:]
        ) from exc

    queue_stdout = None
    collected_results_dir = None
    collection_result = None
    if run_queue:
        command = [
            *prefix,
            queue_executable,
            "run",
            "--max-attempts",
            str(max_attempts),
        ]
        if max_tasks is not None:
            command.extend(["--max-tasks", str(max_tasks)])
        queue_result = _run_checked(
            command,
            executor=invoke,
            timeout_seconds=timeout_seconds,
        )
        queue_stdout = queue_result.stdout
        if collect_results:
            collected_results_dir = packages.parent / "raw_results"
            converted_destination = _run_checked(
                [*prefix, "wslpath", "-a", str(collected_results_dir)],
                executor=invoke,
                timeout_seconds=30,
            )
            wsl_destination = (
                converted_destination.stdout.strip().splitlines()[-1]
                if converted_destination.stdout.strip()
                else ""
            )
            if not wsl_destination.startswith("/"):
                raise MuseCocoQueueBridgeError(
                    f"wslpath returned an invalid result destination: {wsl_destination!r}"
                )
            collect_command = [
                *prefix,
                queue_executable,
                "collect",
                "--source",
                wsl_path,
                "--destination",
                wsl_destination,
            ]
            if force_collect:
                collect_command.append("--force")
            collected = _run_checked(
                collect_command,
                executor=invoke,
                timeout_seconds=300,
            )
            try:
                collection_result = json.loads(collected.stdout)
            except json.JSONDecodeError as exc:
                raise MuseCocoQueueBridgeError(
                    "WSL collection returned non-JSON output: " + collected.stdout[-1000:]
                ) from exc
    return MuseCocoQueueBridgeResult(
        task_packages_dir=packages,
        wsl_task_packages_dir=wsl_path,
        enqueue_result=enqueue_result,
        queue_ran=run_queue,
        queue_stdout=queue_stdout,
        collected_results_dir=collected_results_dir,
        collection_result=collection_result,
    )
