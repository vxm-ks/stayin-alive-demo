"""Run the repository-integrated Stage 2 test pipeline using relative paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from mido import MidiFile

try:
    from stage2.repetition_quality_gate import run_repetition_quality_gate
    from stage2.plan_assembler import assemble_from_plan, restore_tempo_map
except ModuleNotFoundError:  # Direct execution from the stage2 directory.
    from repetition_quality_gate import run_repetition_quality_gate
    from plan_assembler import assemble_from_plan, restore_tempo_map


STAGE2_STATUS = "production"


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
    time_signature: str
    total_bars: int
    form_string: str
    sections: tuple[dict, ...]
    sha256: str


@dataclass(frozen=True)
class Stage1Inputs:
    theme_midis: dict[str, Path]
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
    items = data.get("items", [])
    if not isinstance(items, list) or not items:
        raise ValueError("Stage 1 finalized theme manifest contains no themes")
    by_family = {item.get("theme_family_id"): item for item in items}
    if len(by_family) != len(items) or None in by_family:
        raise ValueError("Stage 1 theme manifest has duplicate or missing theme_family_id")

    def checked_theme(theme_family_id: str) -> Path:
        item = by_family[theme_family_id]
        path = (themes_root / str(item.get("final_midi", ""))).resolve()
        require_file(path, f"Stage 1 {theme_family_id} final.mid")
        if item.get("final_sha256") != sha256_file(path):
            raise ValueError(f"Stage 1 {theme_family_id} final.mid hash mismatch")
        return path

    plan_data = json.loads(stage2_plan.read_text(encoding="utf-8"))
    if plan_data.get("story_id") != data.get("story_id"):
        raise ValueError("Stage 1 theme manifest and stage2_plan story_id differ")
    return Stage1Inputs(
        {theme_id: checked_theme(theme_id) for theme_id in sorted(by_family)},
        stage2_plan,
    )


def parse_package_binding(value: str, base_dir: Path) -> tuple[str, Path]:
    package_id, separator, raw_path = value.partition("=")
    if not separator or not package_id.strip() or not raw_path.strip():
        raise ValueError("heartbeat package must use ID=PATH")
    path = resolve_path(Path(raw_path.strip()), base_dir)
    require_file(path / "heartbeat_manifest.json", f"heartbeat package {package_id.strip()}")
    return package_id.strip(), path


def parse_theme_binding(value: str, base_dir: Path) -> tuple[str, Path]:
    theme_id, separator, raw_path = value.partition("=")
    if not separator or not theme_id.strip() or not raw_path.strip():
        raise ValueError("theme binding must use theme-X=PATH")
    theme_id = theme_id.strip()
    if not theme_id.startswith("theme-"):
        raise ValueError("theme ID must start with theme-")
    path = resolve_path(Path(raw_path.strip()), base_dir)
    require_file(path, f"finalized theme {theme_id}")
    return theme_id, path


def parse_time_signature(value: str) -> tuple[int, int]:
    try:
        numerator, denominator = (int(part) for part in value.split("/", 1))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"invalid time signature: {value!r}") from exc
    if numerator <= 0 or denominator <= 0:
        raise ValueError("time signature values must be positive")
    return numerator, denominator


def load_stage2_plan(path: Path, base_dir: Path) -> Stage2PlanSettings:
    """Validate and normalize an arbitrary contiguous Stage 1 form plan."""
    absolute = resolve_path(path, base_dir)
    require_file(absolute, "stage2_plan.json")
    try:
        data = json.loads(absolute.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid stage2_plan.json: {exc}") from exc
    if data.get("schema_version") != "0.2-draft":
        raise ValueError("unsupported stage2_plan.schema_version")
    sections = data.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 16:
        raise ValueError("stage2_plan requires between 1 and 16 sections")
    form_labels = str(data.get("form_string", "")).split("-")
    if len(form_labels) != len(sections) or any(not label for label in form_labels):
        raise ValueError("form_string must contain one label per section")
    time_signature = data.get("time_signature")
    parse_time_signature(time_signature)
    expected_start = 1
    normalized: list[dict] = []
    prior_by_family: dict[str, str] = {}
    valid_relations = {"introduce", "reprise", "variation", "development"}
    valid_sources = {"musecoco_seed", "reuse_theme", "midigpt_variation", "midigpt_development"}
    for number, (raw_section, label) in enumerate(zip(sections, form_labels, strict=True), 1):
        section = dict(raw_section)
        if section.get("section_id") != f"S{number}":
            raise ValueError("stage2_plan section IDs must be contiguous S1..Sn")
        motif_bars = int(section.get("input_motif_bars", 0))
        extension_bars = int(section.get("extension_bars", -1))
        target_bars = int(section.get("target_section_bars", 0))
        expected_end = expected_start + target_bars - 1
        if target_bars <= 0 or motif_bars <= 0 or motif_bars + extension_bars != target_bars:
            raise ValueError(f"section {number} motif/extension/target lengths are inconsistent")
        if (section.get("bar_start"), section.get("bar_end")) != (expected_start, expected_end):
            raise ValueError(f"section {number} bar range is not contiguous")
        if float(section.get("tempo_bpm", 0)) <= 0:
            raise ValueError(f"section {number} tempo must be positive")
        if section.get("drum_pattern", {}).get("time_signature") != time_signature:
            raise ValueError(f"section {number} drum time signature differs from the plan")

        section["form_label"] = section.get("form_label", label)
        if section["form_label"] != label:
            raise ValueError(f"section {number} form_label differs from form_string")
        base_symbol = label[0]
        theme_id = section.get("theme_family_id", f"theme-{base_symbol}")
        section["theme_family_id"] = theme_id
        prior_source = prior_by_family.get(theme_id)
        relation = section.get("relation")
        if relation is None:
            relation = "introduce" if prior_source is None else ("variation" if "'" in label else "reprise")
        if relation not in valid_relations:
            raise ValueError(f"section {number} has invalid relation")
        section["relation"] = relation
        source_section_id = section.get("source_section_id")
        if relation == "introduce":
            if prior_source is not None or source_section_id is not None:
                raise ValueError(f"section {number} cannot re-introduce an existing family")
        else:
            source_section_id = source_section_id or prior_source
            if source_section_id is None:
                raise ValueError(f"section {number} requires an earlier source section")
        section["source_section_id"] = source_section_id
        default_material = {
            "introduce": "musecoco_seed", "reprise": "reuse_theme",
            "variation": "midigpt_variation", "development": "midigpt_development",
        }[relation]
        section["material_source"] = section.get("material_source", default_material)
        if section["material_source"] not in valid_sources:
            raise ValueError(f"section {number} has invalid material_source")

        access = section.get("midigpt_access")
        protected = section.get("protected_bar_ranges", [])
        editable = section.get("editable_bar_ranges", [])
        full = [{"bar_start": expected_start, "bar_end": expected_end}]
        motif = [{"bar_start": expected_start, "bar_end": expected_start + motif_bars - 1}]
        extension = ([{"bar_start": expected_start + motif_bars, "bar_end": expected_end}]
                     if extension_bars else [])
        expected_ranges = {
            "fixed": (full, []),
            "extension_only": (motif, extension),
            "modifiable": ([], full),
        }
        if access not in expected_ranges:
            raise ValueError(f"section {number} has invalid midigpt_access")
        if (protected, editable) != expected_ranges[access]:
            raise ValueError(f"section {number} protected/editable ranges conflict with access mode")
        if access == "modifiable" and relation not in {"variation", "development"}:
            raise ValueError(f"section {number} modifiable access requires variation/development")
        prior_by_family[theme_id] = section["section_id"]
        normalized.append(section)
        expected_start = expected_end + 1
    section_by_id = {section["section_id"]: section for section in normalized}
    section_index = {section["section_id"]: index for index, section in enumerate(normalized)}
    for index, section in enumerate(normalized):
        source_id = section.get("source_section_id")
        if source_id is None:
            continue
        if source_id not in section_by_id or section_index[source_id] >= index:
            raise ValueError(f"{section['section_id']} source_section_id must refer to an earlier section")
        if section_by_id[source_id]["theme_family_id"] != section["theme_family_id"]:
            raise ValueError(f"{section['section_id']} source section belongs to another theme family")
    total_bars = expected_start - 1
    if data.get("total_bars") != total_bars:
        raise ValueError("stage2_plan.total_bars is inconsistent")
    return Stage2PlanSettings(
        absolute, str(data.get("story_id", "")), time_signature,
        total_bars, str(data.get("form_string")), tuple(normalized), sha256_file(absolute),
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
    theme_midis: dict[str, Path],
    stage2_plan: Path,
    output_path: Path,
    *,
    model_name: str = "yellow",
    seed: int = 42,
    resume: bool = False,
    heartbeat_packages: tuple[str, ...] = (),
    stage3_render_plan: Path | None = None,
    repetition_mode: str = "off",
    repetition_threshold: float = 0.82,
    repetition_max_occurrences: int = 2,
    repetition_candidates: int = 4,
) -> PipelineResult:
    stage2_dir = Path(__file__).resolve().parent
    completion_script = stage2_dir / "complete_all_gaps_v3.py"
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
    original_plan = load_stage2_plan(stage2_plan, stage2_dir)
    parse_time_signature(original_plan.time_signature)
    normalized_themes: dict[str, Path] = {}
    for theme_id, raw_path in theme_midis.items():
        absolute = resolve_path(raw_path, stage2_dir)
        require_file(absolute, f"finalized theme {theme_id}")
        normalized_themes[theme_id] = absolute
    referenced_families = {str(section["theme_family_id"]) for section in original_plan.sections}
    missing = sorted(referenced_families - set(normalized_themes))
    if missing:
        raise ValueError(f"Stage 1 theme manifest is missing families referenced by the form: {missing}")

    output_absolute = resolve_path(output_path, stage2_dir)
    output_absolute.parent.mkdir(parents=True, exist_ok=True)
    resolved_plan_path = output_absolute.parent / "stage2_plan.resolved.json"
    source_plan_data = json.loads(original_plan.path.read_text(encoding="utf-8"))
    source_plan_data["sections"] = list(original_plan.sections)
    resolved_plan_path.write_text(
        json.dumps(source_plan_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    plan = replace(original_plan, path=resolved_plan_path, sha256=sha256_file(resolved_plan_path))
    combined = output_absolute.parent / "combined_with_drums.mid"
    combined_manifest = output_absolute.parent / "combined_with_drums.manifest.json"
    checkpoint_inputs = {
        "theme_midi_sha256": {
            theme_id: sha256_file(path) for theme_id, path in sorted(normalized_themes.items())
        },
        "source_stage2_plan_sha256": original_plan.sha256,
        "resolved_stage2_plan_sha256": plan.sha256,
        "form_string": plan.form_string,
        "total_bars": plan.total_bars,
        "time_signature": plan.time_signature,
        "stage2_status": STAGE2_STATUS,
    }

    combined_arg = relative_argument(combined, stage2_dir, "combined MIDI")
    output_arg = relative_argument(output_absolute, stage2_dir, "complete MIDI")
    if not resume:
        assembly_audit = assemble_from_plan(
            theme_midis=normalized_themes, plan_path=plan.path, output_path=combined,
        )
        checkpoint = {
            "inputs": checkpoint_inputs,
            "assembly": assembly_audit,
            "combined_midi_sha256": sha256_file(combined),
        }
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
    if not channel_note_numbers(combined).issubset({36, 38}):
        raise RuntimeError("legacy channel-10 notes survived cleanup before heartbeat refill")
    before = heartbeat_signature(combined)
    if not before:
        raise RuntimeError("stage2_plan produced no S1/S2 heartbeat events")

    command = [
        sys.executable, relative_argument(completion_script, stage2_dir, "completion script"),
        combined_arg, "--output", output_arg, "--model", model_name, "--seed", str(seed),
        "--stage2-plan", relative_argument(plan.path, stage2_dir, "resolved stage2 plan"),
    ]
    if resume:
        command.append("--resume")
    run_command(command, "MIDI-GPT extension", stage2_dir)
    restore_tempo_map(output_absolute, plan.path)
    validate_midi(output_absolute, "complete MIDI")
    repetition_report_path = output_absolute.parent / "stage2_repetition_report.json"
    repetition_report = run_repetition_quality_gate(
        output_absolute,
        repetition_report_path,
        mode=repetition_mode,
        stage2_plan=plan.path,
        model_name=model_name,
        seed=seed,
        threshold=repetition_threshold,
        max_occurrences=repetition_max_occurrences,
        candidates=repetition_candidates,
    )
    restore_tempo_map(output_absolute, plan.path)
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
        "story_id": plan.story_id,
        "inputs": {
            "themes": {
                theme_id: {"path": str(path), "sha256": sha256_file(path)}
                for theme_id, path in sorted(normalized_themes.items())
            },
            "source_stage2_plan": {
                "path": str(original_plan.path), "sha256": original_plan.sha256,
            },
            "resolved_stage2_plan": {"path": str(plan.path), "sha256": plan.sha256},
        },
        "settings": {
            "form_string": plan.form_string, "section_count": len(plan.sections),
            "total_bars": plan.total_bars, "time_signature": plan.time_signature,
            "section_tempos_bpm": [float(section["tempo_bpm"]) for section in plan.sections],
            "model": model_name, "seed": seed,
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
            "removed_midi_messages": checkpoint.get("assembly", {}).get("removed_channel_10_messages", 0),
            "verified_refill_notes_only": sorted(channel_note_numbers(output_absolute)) == [36, 38],
        },
        "output": {"complete_midi": str(output_absolute), "sha256": sha256_file(output_absolute)},
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    handoff = {
        "schema_version": "stage2-stage3-handoff-v1",
        "stage2_status": STAGE2_STATUS,
        "story_id": plan.story_id,
        "form_string": plan.form_string,
        "total_bars": plan.total_bars,
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
    parser = argparse.ArgumentParser(description="Run the production arbitrary-form Stage 2 pipeline")
    parser.add_argument("a_midi", type=Path, nargs="?")
    parser.add_argument("b_midi", type=Path, nargs="?")
    parser.add_argument(
        "--stage1-output-base", type=Path,
        help="Stage 1 output base; resolves the plan and every finalized theme family",
    )
    parser.add_argument("--theme", action="append", default=[], metavar="theme-X=PATH")
    parser.add_argument("--stage2-plan", type=Path, help="Stage 1 stage2_plan.json", required=False)
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
    theme_midis: dict[str, Path] = {}
    if args.stage1_output_base:
        if args.a_midi or args.b_midi or args.stage2_plan or args.theme:
            parser.error("--stage1-output-base cannot be combined with manual themes or --stage2-plan")
        discovered = resolve_stage1_inputs(args.stage1_output_base, Path(__file__).resolve().parent)
        theme_midis, args.stage2_plan = discovered.theme_midis, discovered.stage2_plan
    else:
        for binding in args.theme:
            theme_id, theme_path = parse_theme_binding(binding, Path(__file__).resolve().parent)
            if theme_id in theme_midis:
                parser.error(f"duplicate theme binding: {theme_id}")
            theme_midis[theme_id] = theme_path
        if args.a_midi or args.b_midi:
            if not args.a_midi or not args.b_midi:
                parser.error("legacy positional mode requires both A.mid and B.mid")
            theme_midis.setdefault("theme-A", args.a_midi)
            theme_midis.setdefault("theme-B", args.b_midi)
        if not args.stage2_plan or not theme_midis:
            parser.error("provide --stage1-output-base, or --stage2-plan with one or more --theme bindings")
    run_pipeline(
        theme_midis, args.stage2_plan, args.output,
        model_name=args.model, seed=args.seed, resume=args.resume,
        heartbeat_packages=tuple(args.heartbeat_package), stage3_render_plan=args.stage3_render_plan,
        repetition_mode=args.repetition_mode, repetition_threshold=args.repetition_threshold,
        repetition_max_occurrences=args.repetition_max_occurrences,
        repetition_candidates=args.repetition_candidates,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
