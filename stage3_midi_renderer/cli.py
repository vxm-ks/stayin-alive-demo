"""CLI for Stage 3 whole-MIDI validation and rendering."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .heartbeat_conditioning import (
    HeartbeatConditioningError,
    PROFILE_REGISTRY,
    available_conditioning_profiles,
    condition_heartbeat_wav,
)
from .renderer import Stage3RenderError, render_complete_midi, validate_render_inputs
from .package_renderer import parse_package_binding, render_with_packages
from .batch_validator import LoudnessLimits, batch_validate_midis


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_stage2_handoff(path: Path) -> tuple[Path, Path, dict[str, Path]]:
    """Resolve and hash-check the automated Stage 2 -> Stage 3 handoff."""
    source = path.expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stage3RenderError(f"invalid Stage 2 handoff: {exc}") from exc
    if data.get("schema_version") != "stage2-stage3-handoff-v1":
        raise Stage3RenderError("unsupported Stage 2 handoff schema")
    if data.get("stage2_status") != "test":
        raise Stage3RenderError("Stage 2 handoff must declare the current test status")
    if data.get("heartbeat_channel") != 10 or data.get("note_map") != {"36": "S1", "38": "S2"}:
        raise Stage3RenderError("Stage 2 handoff heartbeat channel or note map differs from contract")
    midi_entry = data.get("complete_midi")
    plan_entry = data.get("render_plan")
    packages = data.get("heartbeat_packages")
    if not isinstance(midi_entry, dict) or not isinstance(plan_entry, dict):
        raise Stage3RenderError("Stage 2 handoff lacks complete_midi or render_plan")
    if not isinstance(packages, list) or not packages:
        raise Stage3RenderError("Stage 2 handoff contains no heartbeat packages")

    def checked_file(entry: dict, label: str) -> Path:
        candidate = (source.parent / str(entry.get("path", ""))).resolve()
        if not candidate.is_file():
            raise Stage3RenderError(f"{label} not found: {candidate}")
        if entry.get("sha256") != _sha256(candidate):
            raise Stage3RenderError(f"{label} hash mismatch")
        return candidate

    midi = checked_file(midi_entry, "complete MIDI")
    render_plan = checked_file(plan_entry, "Stage 3 render plan")
    bindings: dict[str, Path] = {}
    for entry in packages:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise Stage3RenderError("invalid heartbeat package entry in Stage 2 handoff")
        package_id = entry["id"]
        package = (source.parent / str(entry.get("path", ""))).resolve()
        manifest = package / "heartbeat_manifest.json"
        if package_id in bindings or not manifest.is_file():
            raise Stage3RenderError(f"invalid or duplicate heartbeat package: {package_id}")
        if entry.get("manifest_sha256") != _sha256(manifest):
            raise Stage3RenderError(f"heartbeat package manifest hash mismatch: {package_id}")
        bindings[package_id] = package
    return midi, render_plan, bindings


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-midi", type=Path, required=True, help="complete Stage 2 MIDI")
    parser.add_argument("--general-sf2", type=Path, required=True, help="general instrument SoundFont")
    parser.add_argument(
        "--heartbeat-sf2", type=Path, required=True,
        help="percussion-only patient heartbeat SoundFont (bank 128/program 0)",
    )
    parser.add_argument(
        "--heartbeat-channel", type=int, default=10,
        help="human MIDI channel for the existing heartbeat track (default: 10)",
    )
    parser.add_argument(
        "--heartbeat-note", type=int, action="append",
        help="repeatable allowed heartbeat note (default: 36 and 38)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stage3_midi_renderer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate complete MIDI and SoundFonts")
    _common(validate)
    render = subparsers.add_parser("render", help="render the complete MIDI to one final WAV")
    _common(render)
    render.add_argument("--output-dir", type=Path)
    render.add_argument("--fluidsynth", default="fluidsynth")
    render.add_argument("--sample-rate", type=int, default=48_000)
    render.add_argument("--gain", type=float, default=0.5)
    render.add_argument("--timeout", type=int, default=600, metavar="SECONDS")
    render.add_argument("--force", action="store_true")
    packages = subparsers.add_parser(
        "render-packages",
        help="render complete MIDI with one or more real-heartbeat packages and independent balance",
    )
    packages_input = packages.add_mutually_exclusive_group(required=True)
    packages_input.add_argument("--input-midi", type=Path, help="complete Stage 2 MIDI")
    packages_input.add_argument("--stage2-handoff", type=Path, help="hash-checked Stage 2 automated handoff")
    packages.add_argument("--general-sf2", type=Path, required=True, help="general instrument SoundFont")
    packages.add_argument("--render-plan", type=Path, help="Stage 3 package/mix JSON plan")
    packages.add_argument(
        "--heartbeat-package", action="append", metavar="ID=PATH",
        help="repeatable heartbeat package binding used by the render plan",
    )
    packages.add_argument("--output-dir", type=Path)
    packages.add_argument("--fluidsynth", default="fluidsynth")
    packages.add_argument("--sample-rate", type=int, default=48_000)
    packages.add_argument("--synth-gain", type=float, default=0.5)
    packages.add_argument("--timeout", type=int, default=600, metavar="SECONDS")
    packages.add_argument("--force", action="store_true")
    batch = subparsers.add_parser(
        "batch-validate",
        help="batch-render Stage-2-compliant MIDI files and audit loudness acceptance",
    )
    batch.add_argument(
        "--input-midi", type=Path, action="append",
        help="repeatable complete Stage 2 MIDI",
    )
    batch.add_argument(
        "--input-dir", type=Path, action="append",
        help="repeatable directory recursively searched for .mid/.midi files",
    )
    batch.add_argument("--general-sf2", type=Path, required=True)
    batch.add_argument("--render-plan", type=Path, required=True)
    batch.add_argument(
        "--heartbeat-package", action="append", required=True, metavar="ID=PATH",
    )
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--fluidsynth", default="fluidsynth")
    batch.add_argument("--sample-rate", type=int, default=48_000)
    batch.add_argument("--synth-gain", type=float, default=0.5)
    batch.add_argument("--timeout", type=int, default=600, metavar="SECONDS")
    batch.add_argument("--min-integrated-lufs", type=float, default=-18.0)
    batch.add_argument("--max-integrated-lufs", type=float, default=-12.0)
    batch.add_argument("--max-true-peak-dbtp", type=float, default=-1.0)
    batch.add_argument("--min-heartbeat-over-music-db", type=float, default=2.0)
    batch.add_argument("--max-heartbeat-over-music-db", type=float, default=8.0)
    batch.add_argument("--force", action="store_true")
    condition = subparsers.add_parser(
        "condition-heartbeat",
        help="condition an isolated heartbeat WAV before SoundFont construction",
    )
    condition.add_argument("--input-wav", type=Path, required=True)
    condition.add_argument("--output-dir", type=Path)
    condition.add_argument(
        "--profile",
        choices=available_conditioning_profiles(),
        default="speaker_safe_loud_v1",
    )
    condition.add_argument("--force", action="store_true")
    subparsers.add_parser(
        "conditioning-profiles",
        help="list the registered heartbeat conditioning profiles",
    )
    return parser


def _default_output() -> Path:
    return Path("stage3_midi_renderer") / "outputs" / datetime.now().astimezone().strftime(
        "%Y%m%d-%H%M%S-%f"
    )


def _default_conditioning_output() -> Path:
    return Path("stage3_midi_renderer") / "outputs" / (
        datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        + "-heartbeat-conditioning"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "conditioning-profiles":
            print(json.dumps({
                "component": "stage3_midi_renderer.heartbeat_conditioning",
                "profiles": {
                    name: {
                        key: value
                        for key, value in profile.__dict__.items()
                    }
                    for name, profile in PROFILE_REGISTRY.items()
                },
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "condition-heartbeat":
            result = condition_heartbeat_wav(
                args.input_wav,
                args.output_dir or _default_conditioning_output(),
                profile=args.profile,
                force=args.force,
            )
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "render-packages":
            if args.stage2_handoff:
                if args.render_plan or args.heartbeat_package:
                    raise Stage3RenderError(
                        "--stage2-handoff cannot be combined with --render-plan or --heartbeat-package"
                    )
                input_midi, render_plan, bindings = load_stage2_handoff(args.stage2_handoff)
            else:
                if not args.render_plan or not args.heartbeat_package:
                    raise Stage3RenderError(
                        "manual render-packages requires --render-plan and --heartbeat-package"
                    )
                input_midi, render_plan = args.input_midi, args.render_plan
                bindings = {}
                for value in args.heartbeat_package:
                    package_id, package_path = parse_package_binding(value)
                    if package_id in bindings:
                        raise Stage3RenderError(f"duplicate heartbeat package ID: {package_id}")
                    bindings[package_id] = package_path
            result = render_with_packages(
                input_midi,
                args.general_sf2,
                render_plan,
                bindings,
                args.output_dir or _default_output(),
                fluidsynth=args.fluidsynth,
                sample_rate=args.sample_rate,
                synth_gain=args.synth_gain,
                timeout_seconds=args.timeout,
                force=args.force,
            )
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "batch-validate":
            bindings: dict[str, Path] = {}
            for value in args.heartbeat_package:
                package_id, package_path = parse_package_binding(value)
                if package_id in bindings:
                    raise Stage3RenderError(f"duplicate heartbeat package ID: {package_id}")
                bindings[package_id] = package_path
            midis = list(args.input_midi or [])
            for directory in args.input_dir or []:
                if not directory.is_dir():
                    raise Stage3RenderError(f"batch input directory not found: {directory}")
                midis.extend(path for path in directory.rglob("*") if path.suffix.lower() in (".mid", ".midi"))
            result = batch_validate_midis(
                midis, args.general_sf2, args.render_plan, bindings, args.output_dir,
                fluidsynth=args.fluidsynth, sample_rate=args.sample_rate,
                synth_gain=args.synth_gain, timeout_seconds=args.timeout,
                limits=LoudnessLimits(
                    min_integrated_lufs=args.min_integrated_lufs,
                    max_integrated_lufs=args.max_integrated_lufs,
                    max_true_peak_dbtp=args.max_true_peak_dbtp,
                    min_heartbeat_over_music_db=args.min_heartbeat_over_music_db,
                    max_heartbeat_over_music_db=args.max_heartbeat_over_music_db,
                ),
                force=args.force,
            )
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0 if result.status == "PASS" else 1
        channel = args.heartbeat_channel - 1
        notes = tuple(args.heartbeat_note or [36, 38])
        if not 1 <= args.heartbeat_channel <= 16:
            raise Stage3RenderError("--heartbeat-channel must be between 1 and 16")
        if args.command == "validate":
            midi, general, heartbeat = validate_render_inputs(
                args.input_midi,
                args.general_sf2,
                args.heartbeat_sf2,
                heartbeat_channel=channel,
                heartbeat_notes=notes,
            )
            print(json.dumps({
                "status": "PASS",
                "midi": midi.as_dict(),
                "general_soundfont": general.as_dict(),
                "heartbeat_soundfont": heartbeat.as_dict(),
            }, ensure_ascii=False, indent=2))
            return 0
        result = render_complete_midi(
            args.input_midi,
            args.general_sf2,
            args.heartbeat_sf2,
            args.output_dir or _default_output(),
            fluidsynth=args.fluidsynth,
            heartbeat_channel=channel,
            heartbeat_notes=notes,
            sample_rate=args.sample_rate,
            gain=args.gain,
            timeout_seconds=args.timeout,
            force=args.force,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except (
        OSError,
        RuntimeError,
        HeartbeatConditioningError,
        Stage3RenderError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Stage 3 failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
