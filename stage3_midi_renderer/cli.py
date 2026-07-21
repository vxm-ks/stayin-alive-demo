"""CLI for Stage 3 whole-MIDI validation and rendering."""

from __future__ import annotations

import argparse
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
    packages.add_argument("--input-midi", type=Path, required=True, help="complete Stage 2 MIDI")
    packages.add_argument("--general-sf2", type=Path, required=True, help="general instrument SoundFont")
    packages.add_argument("--render-plan", type=Path, required=True, help="Stage 3 package/mix JSON plan")
    packages.add_argument(
        "--heartbeat-package", action="append", required=True, metavar="ID=PATH",
        help="repeatable heartbeat package binding used by the render plan",
    )
    packages.add_argument("--output-dir", type=Path)
    packages.add_argument("--fluidsynth", default="fluidsynth")
    packages.add_argument("--sample-rate", type=int, default=48_000)
    packages.add_argument("--synth-gain", type=float, default=0.5)
    packages.add_argument("--timeout", type=int, default=600, metavar="SECONDS")
    packages.add_argument("--force", action="store_true")
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
            bindings: dict[str, Path] = {}
            for value in args.heartbeat_package:
                package_id, package_path = parse_package_binding(value)
                if package_id in bindings:
                    raise Stage3RenderError(f"duplicate heartbeat package ID: {package_id}")
                bindings[package_id] = package_path
            result = render_with_packages(
                args.input_midi,
                args.general_sf2,
                args.render_plan,
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
