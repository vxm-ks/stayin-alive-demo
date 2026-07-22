"""Run the repository-integrated Stage 2 test pipeline using relative paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mido import MidiFile

try:
    from stage2.repetition_quality_gate import run_repetition_quality_gate
except ModuleNotFoundError:  # Direct execution from the stage2 directory.
    from repetition_quality_gate import run_repetition_quality_gate


STAGE2_STATUS = "test"
TEST_MOTIF_BARS = 8
TEST_EXTENSION_BARS = 8


@dataclass(frozen=True)
class PipelineResult:
    combined_midi: Path
    completed_midi: Path
    manifest: Path
    stage3_handoff: Path


@dataclass(frozen=True)
class Stage2PlanSettings:
    path: Path
    story_id: str
    bpm: float
    time_signature: str
    gap_bars: int
    total_bars: int
    sha256: str


@dataclass(frozen=True)
class Stage1Inputs:
    a_midi: Path
    b_midi: Path
    stage2_plan: Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_path(path: Path, base_dir: Path) -> Path:
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def resolve_stage1_inputs(output_base: Path, base_dir: Path) -> Stage1Inputs:
    """Resolve Stage 1 sibling deliveries and verify the finalized theme manifest."""
    base = resolve_path(output_base, base_dir)
    if base.name.endswith("-stage2"):
        base = base.with_name(base.name[:-7])
    stage2_plan = base.with_name(f"{base.name}-stage2") / "stage2_plan.json"
    themes_root = base.with_name(f"{base.name}-musecoco") / "generated_themes"
    manifest_path = themes_root / "theme_manifest.json"
    require_file(stage2_plan, "Stage 1 stage2_plan.json")
    require_file(manifest_path, "Stage 1 finalized theme manifest")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "musecoco-final-themes-v1":
        raise ValueError("unsupported Stage 1 theme manifest schema")
    if data.get("output_motif_bars") != TEST_MOTIF_BARS:
        raise ValueError("Stage 1 themes must be finalized to exactly 8 bars")
    by_symbol = {item.get("base_symbol"): item for item in data.get("items", [])}
    if not {"A", "B"}.issubset(by_symbol):
        raise ValueError("Stage 1 test delivery must contain finalized A and B themes")

    def checked_theme(symbol: str) -> Path:
        item = by_symbol[symbol]
        path = (themes_root / str(item.get("final_midi", ""))).resolve()
        require_file(path, f"Stage 1 {symbol} final.mid")
        if item.get("final_sha256") != sha256_file(path):
            raise ValueError(f"Stage 1 {symbol} final.mid hash mismatch")
        return path

    plan_data = json.loads(stage2_plan.read_text(encoding="utf-8"))
    if plan_data.get("story_id") != data.get("story_id"):
        raise ValueError("Stage 1 theme manifest and stage2_plan story_id differ")
    return Stage1Inputs(checked_theme("A"), checked_theme("B"), stage2_plan)


def parse_package_binding(value: str, base_dir: Path) -> tuple[str, Path]:
    package_id, separator, raw_path = value.partition("=")
    if not separator or not package_id.strip() or not raw_path.strip():
        raise ValueError("heartbeat package must use ID=PATH")
    path = resolve_path(Path(raw_path.strip()), base_dir)
    require_file(path / "heartbeat_manifest.json", f"heartbeat package {package_id.strip()}")
    return package_id.strip(), path


def parse_time_signature(value: str) -> tuple[int, int]:
    try:
        numerator, denominator = (int(part) for part in value.split("/", 1))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid time signature: {value!r}") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("time signature values must be positive")
    return numerator, denominator


def load_stage2_plan(path: Path, base_dir: Path) -> Stage2PlanSettings:
    """Fail closed unless Stage 1's plan matches the current 8+8 A/B/A test pipeline."""
    absolute = resolve_path(path, base_dir)
    require_file(absolute, "stage2_plan.json")
    try:
        data = json.loads(absolute.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid stage2_plan.json: {exc}") from exc
    if data.get("schema_version") != "0.2-draft":
        raise ValueError("stage2_plan.schema_version must be 0.2-draft")
    sections = data.get("sections")
    if not isinstance(sections, list) or len(sections) != 3:
        raise ValueError("the Stage 2 test pipeline requires exactly three A/B/A sections")
    if data.get("form_string") != "A-B-A":
        raise ValueError("the Stage 2 test pipeline requires form_string A-B-A")
    time_signature = data.get("time_signature")
    parse_time_signature(time_signature)
    tempos = {float(section.get("tempo_bpm", 0)) for section in sections}
    if len(tempos) != 1 or next(iter(tempos)) <= 0:
        raise ValueError("all sections must use one positive tempo")
    bpm = next(iter(tempos))
    gap_bars = TEST_EXTENSION_BARS
    expected_start = 1
    for number, section in enumerate(sections, 1):
        expected_end = expected_start + TEST_MOTIF_BARS + gap_bars - 1
        if (section.get("bar_start"), section.get("bar_end")) != (expected_start, expected_end):
            raise ValueError(f"section {number} does not match the A/B/A bar layout")
        if section.get("input_motif_bars") != TEST_MOTIF_BARS or section.get("extension_bars") != gap_bars:
            raise ValueError(f"section {number} must have an 8-bar motif and 8-bar extension")
        if section.get("target_section_bars") != TEST_MOTIF_BARS + gap_bars:
            raise ValueError(f"section {number}.target_section_bars is inconsistent")
        if (section.get("midigpt_access"), section.get("extension_method")) != ("extension_only", "midigpt_extend"):
            raise ValueError(f"section {number} must use extension_only/midigpt_extend")
        protected = [{"bar_start": expected_start, "bar_end": expected_start + TEST_MOTIF_BARS - 1}]
        editable = [{"bar_start": expected_start + TEST_MOTIF_BARS, "bar_end": expected_end}]
        if section.get("protected_bar_ranges") != protected:
            raise ValueError(f"section {number} does not protect its eight motif bars")
        if section.get("editable_bar_ranges") != editable:
            raise ValueError(f"section {number} editable bars differ from its extension")
        if section.get("drum_pattern", {}).get("time_signature") != time_signature:
            raise ValueError(f"section {number} drum time signature differs from the plan")
        expected_start = expected_end + 1
    total_bars = expected_start - 1
    if data.get("total_bars") != total_bars:
        raise ValueError("stage2_plan.total_bars is inconsistent")
    return Stage2PlanSettings(
        absolute, str(data.get("story_id", "")), bpm, time_signature,
        gap_bars, total_bars, sha256_file(absolute),
    )


def relative_argument(path: Path, child_cwd: Path, label: str) -> str:
    try:
        relative = os.path.relpath(path, child_cwd)
    except ValueError as exc:
        raise ValueError(f"{label} must be on the same drive as Stage 2") from exc
    try:
        relative.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} relative path must contain ASCII characters only: {relative}") from exc
    return relative if relative.startswith(".") else f".{os.sep}{relative}"


def validate_midi(path: Path, label: str) -> None:
    require_file(path, label)
    if path.stat().st_size < 14 or path.read_bytes()[:4] != b"MThd":
        raise RuntimeError(f"{label} is not a standard MIDI file: {path}")
    try:
        midi = MidiFile(path)
    except Exception as exc:
        raise RuntimeError(f"{label} cannot be read by mido: {path}") from exc
    print(f"{label}: {path.stat().st_size} bytes, {len(midi.tracks)} tracks, PPQ={midi.ticks_per_beat}")


def heartbeat_signature(path: Path) -> list[tuple[int, str, int, int]]:
    """Collect semantic channel-10 S1/S2 messages independent of track order."""
    result: list[tuple[int, str, int, int]] = []
    for track in MidiFile(path).tracks:
        tick = 0
        for message in track:
            tick += message.time
            if getattr(message, "channel", None) != 9 or getattr(message, "note", None) not in {36, 38}:
                continue
            on = message.type == "note_on" and message.velocity > 0
            result.append((tick, "on" if on else "off", message.note, message.velocity if on else 0))
    return sorted(result)


def channel_message_count(path: Path, channel: int = 9) -> int:
    """Count source channel messages that assembly must remove before refill."""
    return sum(
        1
        for track in MidiFile(path).tracks
        for message in track
        if getattr(message, "channel", None) == channel
    )


def channel_note_numbers(path: Path, channel: int = 9) -> set[int]:
    return {
        message.note
        for track in MidiFile(path).tracks
        for message in track
        if getattr(message, "channel", None) == channel and hasattr(message, "note")
    }


def run_command(command: list[str], label: str, cwd: Path) -> None:
    print(f"\n=== {label} ===")
    completed = subprocess.run(command, cwd=str(cwd), check=False)
    if completed.returncode:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")


def run_pipeline(
    a_path: Path,
    b_path: Path,
    bpm: float | None,
    time_signature: str | None,
    output_path: Path,
    *,
    model_name: str = "yellow",
    seed: int = 42,
    resume: bool = False,
    stage2_plan: Path | None = None,
    heartbeat_packages: tuple[str, ...] = (),
    stage3_render_plan: Path | None = None,
    repetition_mode: str = "off",
    repetition_threshold: float = 0.82,
    repetition_max_occurrences: int = 2,
    repetition_candidates: int = 4,
) -> PipelineResult:
    stage2_dir = Path(__file__).resolve().parent
    combine_script = stage2_dir / "combine_midi_with_drums.py"
    completion_script = stage2_dir / "complete_all_gaps_v3.py"
    require_file(combine_script, "combine script")
    require_file(completion_script, "completion script")
    package_bindings: dict[str, Path] = {}
    for binding in heartbeat_packages:
        package_id, package_path = parse_package_binding(binding, stage2_dir)
        if package_id in package_bindings:
            raise ValueError(f"duplicate heartbeat package ID: {package_id}")
        package_bindings[package_id] = package_path
    render_plan_absolute = (
        resolve_path(stage3_render_plan, stage2_dir) if stage3_render_plan else None
    )
    if render_plan_absolute:
        require_file(render_plan_absolute, "Stage 3 render plan")
    if bool(package_bindings) != bool(render_plan_absolute):
        raise ValueError("heartbeat packages and --stage3-render-plan must be supplied together")
    plan = load_stage2_plan(stage2_plan, stage2_dir) if stage2_plan else None
    if plan:
        if bpm is not None and abs(plan.bpm - bpm) > 1e-9:
            raise ValueError("CLI BPM differs from stage2_plan.json")
        if time_signature is not None and plan.time_signature != time_signature:
            raise ValueError("CLI time signature differs from stage2_plan.json")
        bpm = plan.bpm
        time_signature = plan.time_signature
    if bpm is None or time_signature is None:
        raise ValueError("--bpm and --time-signature are required without --stage2-plan")
    if bpm <= 0:
        raise ValueError("BPM must be positive")
    parse_time_signature(time_signature)

    a_absolute = resolve_path(a_path, stage2_dir)
    b_absolute = resolve_path(b_path, stage2_dir)
    output_absolute = resolve_path(output_path, stage2_dir)
    require_file(a_absolute, "A.mid")
    require_file(b_absolute, "B.mid")
    removed_channel_10_messages = (
        2 * channel_message_count(a_absolute) + channel_message_count(b_absolute)
    )
    output_absolute.parent.mkdir(parents=True, exist_ok=True)
    combined = output_absolute.parent / "combined_with_drums.mid"
    combined_manifest = output_absolute.parent / "combined_with_drums.manifest.json"
    checkpoint_inputs = {
        "a_midi_sha256": sha256_file(a_absolute),
        "b_midi_sha256": sha256_file(b_absolute),
        "stage2_plan_sha256": plan.sha256 if plan else None,
        "bpm": bpm,
        "time_signature": time_signature,
        "stage2_status": STAGE2_STATUS,
        "source_channel_10_messages_to_remove": removed_channel_10_messages,
    }

    combined_arg = relative_argument(combined, stage2_dir, "combined MIDI")
    output_arg = relative_argument(output_absolute, stage2_dir, "complete MIDI")
    if not resume:
        command = [
            sys.executable, relative_argument(combine_script, stage2_dir, "combine script"),
            relative_argument(a_absolute, stage2_dir, "A.mid"), relative_argument(b_absolute, stage2_dir, "B.mid"),
            "--bpm", str(bpm), "--time-signature", time_signature, "--output", combined_arg,
        ]
        if plan:
            command.extend(["--stage2-plan", relative_argument(plan.path, stage2_dir, "stage2_plan.json")])
        run_command(command, "assemble A/B/A and protected heartbeat", stage2_dir)
        checkpoint = {"inputs": checkpoint_inputs, "combined_midi_sha256": sha256_file(combined)}
        combined_manifest.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        require_file(combined_manifest, "combined MIDI manifest")
        checkpoint = json.loads(combined_manifest.read_text(encoding="utf-8"))
        if checkpoint.get("inputs") != checkpoint_inputs:
            raise RuntimeError("--resume inputs differ from the combined MIDI checkpoint")
        require_file(combined, "combined MIDI")
        if checkpoint.get("combined_midi_sha256") != sha256_file(combined):
            raise RuntimeError("combined MIDI hash differs from its checkpoint manifest")
    validate_midi(combined, "combined MIDI")
    if plan and not channel_note_numbers(combined).issubset({36, 38}):
        raise RuntimeError("legacy channel-10 notes survived cleanup before heartbeat refill")
    before = heartbeat_signature(combined)
    if plan and not before:
        raise RuntimeError("stage2_plan produced no S1/S2 heartbeat events")

    command = [
        sys.executable, relative_argument(completion_script, stage2_dir, "completion script"),
        combined_arg, "--output", output_arg, "--model", model_name, "--seed", str(seed),
    ]
    if resume:
        command.append("--resume")
    run_command(command, "MIDI-GPT extension", stage2_dir)
    validate_midi(output_absolute, "complete MIDI")
    repetition_report_path = output_absolute.parent / "stage2_repetition_report.json"
    repetition_report = run_repetition_quality_gate(
        output_absolute,
        repetition_report_path,
        mode=repetition_mode,
        stage2_plan=plan.path if plan else None,
        model_name=model_name,
        seed=seed,
        threshold=repetition_threshold,
        max_occurrences=repetition_max_occurrences,
        candidates=repetition_candidates,
    )
    validate_midi(output_absolute, "quality-gated complete MIDI")
    after = heartbeat_signature(output_absolute)
    if after != before:
        raise RuntimeError("MIDI-GPT changed protected S1/S2 heartbeat events")

    manifest_path = output_absolute.parent / "stage2_completion_manifest.json"
    handoff_path = output_absolute.parent / "stage3_handoff.json"
    manifest = {
        "schema_version": "1.0", "stage": "stage2", "status": STAGE2_STATUS,
        "entry_point": "stage2/run_pipeline_relative.py",
        "generated_at": datetime.now(UTC).isoformat(),
        "story_id": plan.story_id if plan else None,
        "inputs": {
            "a_midi": {"path": str(a_absolute), "sha256": sha256_file(a_absolute)},
            "b_midi": {"path": str(b_absolute), "sha256": sha256_file(b_absolute)},
            "stage2_plan": ({"path": str(plan.path), "sha256": plan.sha256} if plan else None),
        },
        "settings": {
            "bpm": bpm, "time_signature": time_signature, "model": model_name, "seed": seed,
            "repetition_mode": repetition_mode,
            "repetition_threshold": repetition_threshold,
            "repetition_max_occurrences": repetition_max_occurrences,
            "repetition_candidates": repetition_candidates,
        },
        "repetition_quality_gate": {
            "report": str(repetition_report_path),
            "report_sha256": sha256_file(repetition_report_path),
            "initial_issue_count": repetition_report["initial_issue_count"],
            "final_issue_count": repetition_report["final_issue_count"],
        },
        "protected_heartbeat": {"preserved": True, "midi_messages": len(after)},
        "input_channel_10_cleanup": {
            "policy": "remove_all_before_heartbeat_refill",
            "removed_midi_messages": removed_channel_10_messages,
            "verified_refill_notes_only": sorted(channel_note_numbers(output_absolute)) == [36, 38],
        },
        "output": {"complete_midi": str(output_absolute), "sha256": sha256_file(output_absolute)},
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    handoff = {
        "schema_version": "stage2-stage3-handoff-v1",
        "stage2_status": STAGE2_STATUS,
        "story_id": plan.story_id if plan else None,
        "complete_midi": {
            "path": os.path.relpath(output_absolute, handoff_path.parent),
            "sha256": sha256_file(output_absolute),
        },
        "heartbeat_channel": 10,
        "note_map": {"36": "S1", "38": "S2"},
        "heartbeat_packages": [
            {
                "id": package_id,
                "path": os.path.relpath(package_path, handoff_path.parent),
                "manifest_sha256": sha256_file(package_path / "heartbeat_manifest.json"),
            }
            for package_id, package_path in sorted(package_bindings.items())
        ],
        "render_plan": (
            {
                "path": os.path.relpath(render_plan_absolute, handoff_path.parent),
                "sha256": sha256_file(render_plan_absolute),
            }
            if render_plan_absolute else None
        ),
    }
    handoff_path.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return PipelineResult(combined, output_absolute, manifest_path, handoff_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TEST-STATUS relative-path 8+8 A/B/A Stage 2 pipeline")
    parser.add_argument("a_midi", type=Path, nargs="?")
    parser.add_argument("b_midi", type=Path, nargs="?")
    parser.add_argument(
        "--stage1-output-base", type=Path,
        help="Stage 1 output base; automatically resolves stage2_plan and finalized A/B themes",
    )
    parser.add_argument("--bpm", type=float, help="optional when --stage2-plan supplies it")
    parser.add_argument("--time-signature", help="optional when --stage2-plan supplies it")
    parser.add_argument("--stage2-plan", type=Path, help="Stage 1 stage2_plan.json")
    parser.add_argument("--heartbeat-package", action="append", default=[], metavar="ID=PATH")
    parser.add_argument("--stage3-render-plan", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/final_completed.mid"))
    parser.add_argument("--model", default="yellow")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--repetition-mode", choices=["off", "detect", "regenerate"], default="off",
        help="optional Stage 2 tail quality gate (default: off)",
    )
    parser.add_argument("--repetition-threshold", type=float, default=0.82)
    parser.add_argument("--repetition-max-occurrences", type=int, default=2)
    parser.add_argument("--repetition-candidates", type=int, default=4)
    args = parser.parse_args(argv)
    if args.stage1_output_base:
        if args.a_midi or args.b_midi or args.stage2_plan:
            parser.error("--stage1-output-base cannot be combined with A/B positional paths or --stage2-plan")
        discovered = resolve_stage1_inputs(args.stage1_output_base, Path(__file__).resolve().parent)
        args.a_midi, args.b_midi, args.stage2_plan = (
            discovered.a_midi, discovered.b_midi, discovered.stage2_plan
        )
    elif not args.a_midi or not args.b_midi:
        parser.error("provide A.mid and B.mid, or use --stage1-output-base")
    run_pipeline(
        args.a_midi, args.b_midi, args.bpm, args.time_signature, args.output,
        model_name=args.model, seed=args.seed, resume=args.resume, stage2_plan=args.stage2_plan,
        heartbeat_packages=tuple(args.heartbeat_package), stage3_render_plan=args.stage3_render_plan,
        repetition_mode=args.repetition_mode, repetition_threshold=args.repetition_threshold,
        repetition_max_occurrences=args.repetition_max_occurrences,
        repetition_candidates=args.repetition_candidates,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
