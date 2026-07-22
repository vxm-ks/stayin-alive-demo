"""Safe subprocess execution with per-stage logs and timeouts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence


class ProcessExecutionError(RuntimeError):
    pass


class ProcessRunner:
    def run(
        self,
        stage: str,
        command: Sequence[str],
        *,
        cwd: Path,
        log_path: Path,
        timeout_seconds: int,
    ) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"stage={stage}\n")
            log.write("argv=" + repr(list(command)) + "\n")
            log.flush()
            try:
                completed = subprocess.run(
                    list(command), cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                    text=True, check=False, timeout=timeout_seconds, shell=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProcessExecutionError(
                    f"{stage} exceeded its {timeout_seconds}s timeout"
                ) from exc
        if completed.returncode:
            raise ProcessExecutionError(
                f"{stage} failed with exit code {completed.returncode}; see {log_path}"
            )
