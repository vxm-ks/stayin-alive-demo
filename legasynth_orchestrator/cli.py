"""Command-line entry point for the local one-click pipeline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .pipeline import PipelineConfig, PipelineError, run_pipeline


def _load_local_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding the shell environment."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _default_workspace() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_asset_root() -> Path:
    workspace = _default_workspace()
    if (workspace / "tools").is_dir():
        return workspace
    return workspace.parent.parent


def build_parser() -> argparse.ArgumentParser:
    workspace = _default_workspace()
    _load_local_env(workspace / ".env")
    assets = _default_asset_root()
    parser = argparse.ArgumentParser(prog="legasynth_orchestrator")
    parser.add_argument("--story", required=True, help="story text")
    parser.add_argument("--heartbeat-wav", type=Path, required=True)
    parser.add_argument("--rhythm-plan", type=Path, required=True)
    parser.add_argument("--render-plan", type=Path, required=True)
    parser.add_argument("--package-id", default="patient")
    parser.add_argument("--runtime-root", type=Path, default=workspace / "runtime")
    parser.add_argument("--stage1-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--midigpt-python", type=Path,
        default=Path(os.getenv("LEGASYNTH_MIDIGPT_PYTHON", sys.executable)),
    )
    parser.add_argument(
        "--midigpt-model",
        default=os.getenv("LEGASYNTH_MIDIGPT_MODEL", "yellow"),
        help="MIDI-GPT pretrained model name (default: yellow)",
    )
    parser.add_argument(
        "--stage2-repetition-mode", choices=["off", "detect", "regenerate"],
        default=os.getenv("LEGASYNTH_STAGE2_REPETITION_MODE", "off"),
        help="Stage 2 tail repetition gate: off, detect, or regenerate (default: off)",
    )
    parser.add_argument("--stage2-repetition-threshold", type=float, default=0.82)
    parser.add_argument("--stage2-repetition-max-occurrences", type=int, default=2)
    parser.add_argument("--stage2-repetition-candidates", type=int, default=4)
    parser.add_argument("--stage3-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--fluidsynth", type=Path,
        default=assets / "tools" / "fluidsynth-2.5.6" / "fluidsynth-v2.5.6-win10-x64-cpp11" / "bin" / "fluidsynth.exe",
    )
    parser.add_argument(
        "--general-sf2", type=Path,
        default=assets / "tools" / "soundfonts" / "GeneralUser-GS.sf2",
    )
    parser.add_argument("--wsl-distro", default="Ubuntu")
    parser.add_argument(
        "--tonality-policy", choices=["loose", "strict"],
        default=os.getenv("LEGASYNTH_TONALITY_POLICY", "strict"),
        help="Stage 1 generated-tonality policy (default: strict)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="exercise orchestration with synthetic artifacts; never invoke any model",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PipelineConfig(
        workspace=_default_workspace(), runtime_root=args.runtime_root,
        stage1_python=args.stage1_python, midigpt_python=args.midigpt_python,
        midigpt_model=args.midigpt_model,
        stage2_repetition_mode=args.stage2_repetition_mode,
        stage2_repetition_threshold=args.stage2_repetition_threshold,
        stage2_repetition_max_occurrences=args.stage2_repetition_max_occurrences,
        stage2_repetition_candidates=args.stage2_repetition_candidates,
        stage3_python=args.stage3_python, fluidsynth=args.fluidsynth,
        general_sf2=args.general_sf2, wsl_distro=args.wsl_distro,
        tonality_policy=args.tonality_policy,
    )
    try:
        job = run_pipeline(
            story_text=args.story, heartbeat_wav=args.heartbeat_wav,
            rhythm_plan=args.rhythm_plan, render_plan=args.render_plan,
            config=config, package_id=args.package_id, dry_run=args.dry_run,
        )
    except PipelineError as exc:
        print(f"LegaSynth pipeline failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "COMPLETED", "job_dir": str(job)}, ensure_ascii=False))
    return 0
