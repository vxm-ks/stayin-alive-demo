"""Compile Stage-1/Stage-2 plans into an auditable MIDI-GPT scaffold."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import ScaffoldValidationError
from .midi import MidiNote, MidiTrack, midi_bytes, parse_midi
from .models import (
    ContentPlan,
    EventSequencePattern,
    HeartbeatEvent,
    HeartbeatManifest,
    HitsPerBarPattern,
    MotifManifest,
    MusicPlan,
)


VERSION = "0.1.1"
ModelT = TypeVar("ModelT", bound=BaseModel)


def _fail(code: str, path: str, message: str) -> None:
    raise ScaffoldValidationError(code, path, message)


def _parse(model: type[ModelT], value: ModelT | Mapping[str, Any]) -> ModelT:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        _fail("SCHEMA_INVALID", model.__name__, str(exc))
    raise AssertionError("unreachable")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _meter(value: str) -> tuple[int, int]:
    numerator, denominator = value.split("/", 1)
    return int(numerator), int(denominator)


def _scale_tick(value: int, source_ppq: int, target_ppq: int) -> int:
    return int(round(value * target_ppq / source_ppq))


def _velocity(value: int, scale: float) -> int:
    return max(1, min(127, int(round(value * scale))))


@dataclass
class BuildResult:
    score: dict[str, Any]
    generation_request: dict[str, Any]
    generate_payload: dict[str, Any]
    heartbeat_render_manifest: dict[str, Any]
    assembly_manifest: dict[str, Any]
    validation_report: dict[str, Any]
    scaffold_midi: bytes
    heartbeat_midi: bytes
    output_directory: Path | None = None


def _validate_content(content: ContentPlan) -> dict[str, Any]:
    sections = content.form_plan.sections
    expected_start = 1
    by_id: dict[str, Any] = {}
    for index, section in enumerate(sections):
        if section.section_id in by_id:
            _fail(
                "SECTION_ID_DUPLICATE",
                f"content_plan.form_plan.sections[{index}].section_id",
                section.section_id,
            )
        if section.bar_start != expected_start:
            _fail(
                "SECTION_COORDINATES_INVALID",
                f"content_plan.form_plan.sections[{index}].bar_start",
                f"expected {expected_start}, got {section.bar_start}",
            )
        if section.bar_end - section.bar_start + 1 != section.bar_count:
            _fail(
                "SECTION_BAR_COUNT_INVALID",
                f"content_plan.form_plan.sections[{index}]",
                "bar_start/bar_end do not match bar_count",
            )
        expected_start = section.bar_end + 1
        by_id[section.section_id] = section
    if expected_start - 1 != content.global_.total_bars:
        _fail(
            "TOTAL_BARS_MISMATCH",
            "content_plan.global.total_bars",
            f"sections end at {expected_start - 1}",
        )
    task_by_target = {item.target_section_id: item for item in content.variation_tasks}
    if len(task_by_target) != len(content.variation_tasks):
        _fail(
            "VARIATION_TASK_TARGET_DUPLICATE",
            "content_plan.variation_tasks",
            "target_section_id must be unique",
        )
    expected_targets = {
        item.section_id
        for item in sections
        if item.relation in {"variation", "development"}
    }
    if set(task_by_target) != expected_targets:
        _fail(
            "VARIATION_TASK_COVERAGE_INVALID",
            "content_plan.variation_tasks",
            f"expected targets {sorted(expected_targets)}, got {sorted(task_by_target)}",
        )
    for target_id, task in task_by_target.items():
        section = by_id[target_id]
        if (
            task.source_section_id != section.source_section_id
            or task.theme_family_id != section.theme_family_id
            or task.target_bars != section.bar_count
            or task.strategy
            != (
                "midigpt_variation"
                if section.relation == "variation"
                else "midigpt_development"
            )
        ):
            _fail(
                "VARIATION_TASK_MISMATCH",
                f"content_plan.variation_tasks.{task.task_id}",
                "task strategy, source, family, or target_bars differs from its form section",
            )
    return by_id


def _source_templates(
    events: list[HeartbeatEvent],
) -> tuple[dict[str, HeartbeatEvent], tuple[HeartbeatEvent, HeartbeatEvent, int]]:
    ordered = sorted(events, key=lambda item: (item.tick, item.event_type))
    templates: dict[str, HeartbeatEvent] = {}
    for event in ordered:
        templates.setdefault(event.event_type, event)
    for required in ("S1", "S2"):
        if required not in templates:
            _fail(
                "HEARTBEAT_EVENT_MISSING",
                "heartbeat_manifest.midi.outputs.events",
                f"no {required} event is available",
            )
    pair: tuple[HeartbeatEvent, HeartbeatEvent, int] | None = None
    for index, first in enumerate(ordered):
        if first.event_type != "S1":
            continue
        second = next(
            (item for item in ordered[index + 1 :] if item.event_type == "S2"),
            None,
        )
        if second is not None:
            pair = (first, second, second.tick - first.tick)
            break
    if pair is None or pair[2] <= 0:
        _fail(
            "HEARTBEAT_PAIR_MISSING",
            "heartbeat_manifest.midi.outputs.events",
            "no ordered S1 -> S2 pair is available",
        )
    return templates, pair


def _append_heartbeat_event(
    *,
    notes: list[MidiNote],
    audit: list[dict[str, Any]],
    source: HeartbeatEvent,
    event_type: str,
    bar_number: int,
    local_tick: int,
    bar_ticks: int,
    source_ppq: int,
    target_ppq: int,
    velocity_scale: float,
    section_id: str,
    rule_mode: str,
) -> None:
    if local_tick < 0 or local_tick >= bar_ticks:
        _fail(
            "HEARTBEAT_BEAT_OUT_OF_RANGE",
            f"heartbeat_arrangement.sections[{section_id}]",
            f"event tick {local_tick} is outside its bar",
        )
    duration = max(1, _scale_tick(source.duration_ticks, source_ppq, target_ppq))
    absolute_tick = (bar_number - 1) * bar_ticks + local_tick
    note = MidiNote(
        start_tick=absolute_tick,
        duration_ticks=duration,
        pitch=source.midi_note,
        velocity=_velocity(source.velocity, velocity_scale),
        channel=9,
    )
    notes.append(note)
    audit.append(
        {
            "section_id": section_id,
            "bar": bar_number,
            "event_type": event_type,
            "target_tick": absolute_tick,
            "bar_local_tick": local_tick,
            "duration_ticks": duration,
            "midi_note": note.pitch,
            "velocity": note.velocity,
            "velocity_scale": velocity_scale,
            "source_tick": source.tick,
            "source_event_time_s": source.source_event_time_s,
            "rule_mode": rule_mode,
        }
    )


def _render_heartbeat(
    content: ContentPlan,
    music: MusicPlan,
    heartbeat: HeartbeatManifest,
    sections: dict[str, Any],
    bar_ticks: int,
    numerator: int,
    denominator: int,
) -> tuple[list[MidiNote], list[dict[str, Any]]]:
    meter_name = content.global_.time_signature
    source_output = heartbeat.midi.outputs.get(meter_name)
    if source_output is None:
        _fail(
            "HEARTBEAT_METER_MISSING",
            "heartbeat_manifest.midi.outputs",
            f"no source events for {meter_name}",
        )
    templates, pair = _source_templates(source_output.events)
    rules = music.heartbeat_arrangement.sections
    if len(rules) != len(sections):
        _fail(
            "HEARTBEAT_SECTION_COVERAGE_INVALID",
            "music_plan.heartbeat_arrangement.sections",
            "there must be exactly one heartbeat rule per form section",
        )
    rule_by_section: dict[str, Any] = {}
    covered_bars: set[int] = set()
    for index, rule in enumerate(rules):
        source_section = sections.get(rule.section_id)
        if source_section is None:
            _fail(
                "HEARTBEAT_SECTION_UNKNOWN",
                f"music_plan.heartbeat_arrangement.sections[{index}].section_id",
                rule.section_id,
            )
        if rule.section_id in rule_by_section:
            _fail(
                "HEARTBEAT_SECTION_DUPLICATE",
                f"music_plan.heartbeat_arrangement.sections[{index}].section_id",
                rule.section_id,
            )
        if (rule.bar_start, rule.bar_end) != (
            source_section.bar_start,
            source_section.bar_end,
        ):
            _fail(
                "HEARTBEAT_SECTION_COORDINATES_MISMATCH",
                f"music_plan.heartbeat_arrangement.sections[{index}]",
                "bar range must exactly match the Stage-1 form section",
            )
        for bar in range(rule.bar_start, rule.bar_end + 1):
            if bar in covered_bars:
                _fail(
                    "HEARTBEAT_BAR_OVERLAP",
                    f"music_plan.heartbeat_arrangement.sections[{index}]",
                    f"bar {bar} is covered more than once",
                )
            covered_bars.add(bar)
        rule_by_section[rule.section_id] = rule
    expected_bars = set(range(1, content.global_.total_bars + 1))
    if covered_bars != expected_bars:
        missing = sorted(expected_bars - covered_bars)
        _fail(
            "HEARTBEAT_BAR_GAP",
            "music_plan.heartbeat_arrangement.sections",
            f"unplanned bars: {missing}",
        )

    source_ppq = heartbeat.midi.ppq
    ticks_per_denominator_beat = music.ppq * 4 / denominator
    if not ticks_per_denominator_beat.is_integer():
        _fail(
            "PPQ_METER_INCOMPATIBLE",
            "music_plan.ppq",
            f"{music.ppq} cannot exactly represent a {denominator} denominator beat",
        )
    beat_ticks = int(ticks_per_denominator_beat)
    notes: list[MidiNote] = []
    audit: list[dict[str, Any]] = []
    for section in content.form_plan.sections:
        rule = rule_by_section[section.section_id]
        pattern = rule.pattern
        for bar in range(section.bar_start, section.bar_end + 1):
            if isinstance(pattern, HitsPerBarPattern):
                for beat in pattern.beat_positions:
                    if beat < 1 or beat >= numerator + 1:
                        _fail(
                            "HEARTBEAT_BEAT_OUT_OF_RANGE",
                            f"music_plan.heartbeat_arrangement.{section.section_id}.beat_positions",
                            f"beat {beat} is outside 1 <= beat < {numerator + 1}",
                        )
                    local = int(round((beat - 1) * beat_ticks))
                    if pattern.hit_unit == "s1_s2_pair":
                        first, second, source_offset = pair
                        _append_heartbeat_event(
                            notes=notes,
                            audit=audit,
                            source=first,
                            event_type="S1",
                            bar_number=bar,
                            local_tick=local,
                            bar_ticks=bar_ticks,
                            source_ppq=source_ppq,
                            target_ppq=music.ppq,
                            velocity_scale=pattern.velocity_scale,
                            section_id=section.section_id,
                            rule_mode=pattern.mode,
                        )
                        second_tick = local + _scale_tick(
                            source_offset, source_ppq, music.ppq
                        )
                        _append_heartbeat_event(
                            notes=notes,
                            audit=audit,
                            source=second,
                            event_type="S2",
                            bar_number=bar,
                            local_tick=second_tick,
                            bar_ticks=bar_ticks,
                            source_ppq=source_ppq,
                            target_ppq=music.ppq,
                            velocity_scale=pattern.velocity_scale,
                            section_id=section.section_id,
                            rule_mode=pattern.mode,
                        )
                    else:
                        source = templates[pattern.hit_unit]
                        _append_heartbeat_event(
                            notes=notes,
                            audit=audit,
                            source=source,
                            event_type=pattern.hit_unit,
                            bar_number=bar,
                            local_tick=local,
                            bar_ticks=bar_ticks,
                            source_ppq=source_ppq,
                            target_ppq=music.ppq,
                            velocity_scale=pattern.velocity_scale,
                            section_id=section.section_id,
                            rule_mode=pattern.mode,
                        )
            elif isinstance(pattern, EventSequencePattern):
                if pattern.start_beat >= numerator + 1:
                    _fail(
                        "HEARTBEAT_BEAT_OUT_OF_RANGE",
                        f"music_plan.heartbeat_arrangement.{section.section_id}.start_beat",
                        f"beat {pattern.start_beat} is outside 1 <= beat < {numerator + 1}",
                    )
                beat = pattern.start_beat
                sequence_index = 0
                limit = math.inf if pattern.fill_to_bar_end else len(pattern.event_sequence)
                while beat < numerator + 1 and sequence_index < limit:
                    event_type = pattern.event_sequence[
                        sequence_index % len(pattern.event_sequence)
                    ]
                    _append_heartbeat_event(
                        notes=notes,
                        audit=audit,
                        source=templates[event_type],
                        event_type=event_type,
                        bar_number=bar,
                        local_tick=int(round((beat - 1) * beat_ticks)),
                        bar_ticks=bar_ticks,
                        source_ppq=source_ppq,
                        target_ppq=music.ppq,
                        velocity_scale=pattern.velocity_scale,
                        section_id=section.section_id,
                        rule_mode=pattern.mode,
                    )
                    beat += pattern.event_spacing_beats
                    sequence_index += 1
    return sorted(notes, key=lambda item: (item.start_tick, item.pitch)), audit


def _compile_regions(
    content: ContentPlan,
    music: MusicPlan,
    track_by_id: dict[str, Any],
) -> tuple[dict[str, set[int]], dict[str, bool]]:
    total = content.global_.total_bars
    generated: dict[str, set[int]] = {item.track_id: set() for item in music.tracks}
    modes: dict[str, set[str]] = {item.track_id: set() for item in music.tracks}
    for index, region in enumerate(music.midigpt.fill_regions):
        track = track_by_id.get(region.track_id)
        if track is None:
            _fail(
                "FILL_TRACK_UNKNOWN",
                f"music_plan.midigpt.fill_regions[{index}].track_id",
                region.track_id,
            )
        if track.behavior != "generate":
            _fail(
                "FILL_TRACK_NOT_GENERATIVE",
                f"music_plan.midigpt.fill_regions[{index}].track_id",
                f"track {region.track_id} behavior is {track.behavior}",
            )
        if region.bar_start > region.bar_end or region.bar_end > total:
            _fail(
                "FILL_RANGE_INVALID",
                f"music_plan.midigpt.fill_regions[{index}]",
                f"range must lie within 1..{total}",
            )
        bars = set(range(region.bar_start, region.bar_end + 1))
        overlap = bars & generated[region.track_id]
        if overlap:
            _fail(
                "FILL_REGION_OVERLAP",
                f"music_plan.midigpt.fill_regions[{index}]",
                f"bars overlap: {sorted(overlap)}",
            )
        generated[region.track_id].update(bars)
        modes[region.track_id].add(region.mode)

    autoregressive: dict[str, bool] = {}
    for track in music.tracks:
        bars = generated[track.track_id]
        if track.behavior == "generate" and not bars:
            _fail(
                "GENERATIVE_TRACK_EMPTY",
                f"music_plan.tracks.{track.track_id}",
                "generate behavior requires at least one fill region",
            )
        if track.behavior != "generate" and bars:
            _fail(
                "NON_GENERATIVE_TRACK_HAS_FILL",
                f"music_plan.tracks.{track.track_id}",
                "only generate tracks may have fill regions",
            )
        if len(modes[track.track_id]) > 1:
            _fail(
                "GENERATION_MODE_MIXED",
                f"music_plan.midigpt.fill_regions.{track.track_id}",
                "one TrackPrompt cannot mix infill and autoregressive modes",
            )
        is_ar = modes[track.track_id] == {"autoregressive"}
        if is_ar and bars:
            expected = set(range(min(bars), total + 1))
            if bars != expected:
                _fail(
                    "AUTOREGRESSIVE_SUFFIX_INVALID",
                    f"music_plan.midigpt.fill_regions.{track.track_id}",
                    "autoregressive bars must form a contiguous right suffix",
                )
        autoregressive[track.track_id] = is_ar
    return generated, autoregressive


def _place_motifs(
    *,
    content: ContentPlan,
    music: MusicPlan,
    motif_manifest: MotifManifest,
    motif_base_dir: Path,
    sections: dict[str, Any],
    track_by_id: dict[str, Any],
    track_notes: dict[str, list[MidiNote]],
    generated: dict[str, set[int]],
    bar_ticks: int,
    numerator: int,
    denominator: int,
) -> list[dict[str, Any]]:
    motif_by_id = {item.motif_id: item for item in motif_manifest.motifs}
    audit: list[dict[str, Any]] = []
    covered_sections: dict[str, list[str]] = {}
    for index, placement in enumerate(music.motif_placements):
        asset = motif_by_id.get(placement.motif_id)
        if asset is None:
            _fail(
                "MOTIF_UNKNOWN",
                f"music_plan.motif_placements[{index}].motif_id",
                placement.motif_id,
            )
        track = track_by_id.get(placement.target_track_id)
        if track is None:
            _fail(
                "MOTIF_TRACK_UNKNOWN",
                f"music_plan.motif_placements[{index}].target_track_id",
                placement.target_track_id,
            )
        if track.track_type != "melodic":
            _fail(
                "MOTIF_TRACK_TYPE_INVALID",
                f"music_plan.motif_placements[{index}].target_track_id",
                "motifs must be placed on a melodic track",
            )
        if track.behavior == "ignore":
            _fail(
                "MOTIF_TRACK_IGNORED",
                f"music_plan.motif_placements[{index}].target_track_id",
                "an approved motif must remain visible to MIDI-GPT",
            )
        section = sections.get(placement.target_section_id)
        if section is None:
            _fail(
                "MOTIF_SECTION_UNKNOWN",
                f"music_plan.motif_placements[{index}].target_section_id",
                placement.target_section_id,
            )
        if asset.theme_family_id != section.theme_family_id:
            _fail(
                "MOTIF_THEME_FAMILY_MISMATCH",
                f"music_plan.motif_placements[{index}]",
                f"{asset.theme_family_id} cannot supply {section.theme_family_id}",
            )
        final_bar = placement.target_bar + asset.bars * placement.repeat_count - 1
        if placement.target_bar < section.bar_start or final_bar > section.bar_end:
            _fail(
                "MOTIF_SECTION_BOUNDARY_EXCEEDED",
                f"music_plan.motif_placements[{index}]",
                f"placement occupies bars {placement.target_bar}..{final_bar}, outside {section.section_id}",
            )
        occupied = set(range(placement.target_bar, final_bar + 1))
        conflict = occupied & generated[placement.target_track_id]
        if conflict:
            _fail(
                "PROTECTED_FILL_OVERLAP",
                f"music_plan.motif_placements[{index}]",
                f"protected motif overlaps generated bars {sorted(conflict)}",
            )

        path = Path(asset.midi_file)
        if not path.is_absolute():
            path = motif_base_dir / path
        path = path.resolve()
        if not path.is_file():
            _fail("MOTIF_FILE_MISSING", str(path), asset.motif_id)
        actual_hash = _sha256(path)
        if actual_hash.lower() != asset.sha256.lower():
            _fail(
                "MOTIF_HASH_MISMATCH",
                str(path),
                f"expected {asset.sha256}, got {actual_hash}",
            )
        parsed = parse_midi(path)
        if parsed.resolution != asset.ppq:
            _fail(
                "MOTIF_PPQ_MANIFEST_MISMATCH",
                f"motif_manifest.{asset.motif_id}.ppq",
                f"manifest {asset.ppq}, MIDI {parsed.resolution}",
            )
        if (parsed.numerator, parsed.denominator) != _meter(asset.time_signature):
            _fail(
                "MOTIF_METER_MANIFEST_MISMATCH",
                f"motif_manifest.{asset.motif_id}.time_signature",
                "manifest and MIDI meta event disagree",
            )
        if (parsed.numerator, parsed.denominator) != (numerator, denominator):
            _fail(
                "MOTIF_METER_INCOMPATIBLE",
                f"motif_manifest.{asset.motif_id}",
                "motif meter differs from the global meter",
            )
        candidates = [item for item in parsed.tracks if item.track_type == "melodic"]
        if not candidates:
            _fail("MOTIF_MELODY_MISSING", str(path), "no melodic note track")
        source_index = asset.source_track_index or 0
        if source_index >= len(candidates):
            _fail(
                "MOTIF_TRACK_INDEX_INVALID",
                f"motif_manifest.{asset.motif_id}.source_track_index",
                f"only {len(candidates)} melodic note tracks are available",
            )
        source_track = candidates[source_index]
        source_bar_ticks = int(
            round(parsed.resolution * parsed.numerator * 4 / parsed.denominator)
        )
        source_limit = source_bar_ticks * asset.bars
        target_asset_ticks = bar_ticks * asset.bars
        added = 0
        for repeat in range(placement.repeat_count):
            repeat_origin = (
                (placement.target_bar - 1) * bar_ticks + repeat * target_asset_ticks
            )
            for source_note in source_track.notes:
                if source_note.start_tick >= source_limit:
                    continue
                pitch = source_note.pitch + placement.transpose_semitones
                if not 0 <= pitch <= 127:
                    _fail(
                        "MOTIF_TRANSPOSE_OUT_OF_RANGE",
                        f"music_plan.motif_placements[{index}].transpose_semitones",
                        f"pitch {source_note.pitch} becomes {pitch}",
                    )
                local_start = _scale_tick(
                    source_note.start_tick, parsed.resolution, music.ppq
                )
                duration = max(
                    1,
                    _scale_tick(
                        source_note.duration_ticks, parsed.resolution, music.ppq
                    ),
                )
                source_end = source_note.start_tick + source_note.duration_ticks
                if source_end > source_limit:
                    _fail(
                        "MOTIF_NOTE_EXCEEDS_ASSET",
                        f"motif_manifest.{asset.motif_id}.bars",
                        "a protected source note extends beyond the declared motif length",
                    )
                track_notes[placement.target_track_id].append(
                    MidiNote(
                        start_tick=repeat_origin + local_start,
                        duration_ticks=duration,
                        pitch=pitch,
                        velocity=_velocity(
                            source_note.velocity, placement.velocity_scale
                        ),
                    )
                )
                added += 1
        if added == 0:
            _fail("MOTIF_NOTES_EMPTY", str(path), "selected motif range has no notes")
        covered_sections.setdefault(section.section_id, []).append(asset.theme_family_id)
        audit.append(
            {
                "placement_id": placement.placement_id,
                "motif_id": asset.motif_id,
                "theme_family_id": asset.theme_family_id,
                "source_midi": str(path),
                "source_sha256": actual_hash,
                "source_ppq": parsed.resolution,
                "target_ppq": music.ppq,
                "target_track_id": placement.target_track_id,
                "target_section_id": placement.target_section_id,
                "target_bars": [placement.target_bar, final_bar],
                "transpose_semitones": placement.transpose_semitones,
                "velocity_scale": placement.velocity_scale,
                "notes_added": added,
                "protected": True,
            }
        )

    for section in content.form_plan.sections:
        if section.relation in {"introduce", "reprise"}:
            families = covered_sections.get(section.section_id, [])
            if section.theme_family_id not in families:
                _fail(
                    "SECTION_MOTIF_PLACEMENT_MISSING",
                    f"music_plan.motif_placements.{section.section_id}",
                    f"{section.relation} section requires a {section.theme_family_id} motif",
                )
        else:
            target = set(range(section.bar_start, section.bar_end + 1))
            if not any(target <= bars for bars in generated.values()):
                _fail(
                    "VARIATION_FILL_MISSING",
                    f"music_plan.midigpt.fill_regions.{section.section_id}",
                    "variation/development section must be fully generated on one track",
                )
    return audit


def _score_track(
    track: Any,
    notes: list[MidiNote],
    total_bars: int,
    bar_ticks: int,
    numerator: int,
    denominator: int,
) -> dict[str, Any]:
    bars: list[dict[str, Any]] = []
    for bar_index in range(total_bars):
        origin = bar_index * bar_ticks
        bar_notes = []
        for note in notes:
            if origin <= note.start_tick < origin + bar_ticks:
                local = note.start_tick - origin
                bar_notes.append(
                    {
                        "pitch": note.pitch,
                        "velocity": note.velocity,
                        "onset_ticks": local,
                        "duration_ticks": note.duration_ticks,
                        "delta": 0,
                    }
                )
        bars.append(
            {
                "ts_numerator": numerator,
                "ts_denominator": denominator,
                "future": False,
                "notes": sorted(
                    bar_notes,
                    key=lambda item: (item["onset_ticks"], item["pitch"]),
                ),
            }
        )
    return {
        "instrument": track.instrument,
        "track_type": track.track_type,
        "bars": bars,
    }


def build_scaffold(
    content_plan: ContentPlan | Mapping[str, Any],
    music_plan: MusicPlan | Mapping[str, Any],
    heartbeat_manifest: HeartbeatManifest | Mapping[str, Any],
    motif_manifest: MotifManifest | Mapping[str, Any],
    *,
    motif_base_dir: str | Path = ".",
) -> BuildResult:
    """Validate and deterministically compile all inputs without model weights."""

    content = _parse(ContentPlan, content_plan)
    music = _parse(MusicPlan, music_plan)
    heartbeat = _parse(HeartbeatManifest, heartbeat_manifest)
    motifs = _parse(MotifManifest, motif_manifest)
    if len({content.story_id, music.story_id, motifs.story_id}) != 1:
        _fail(
            "STORY_ID_MISMATCH",
            "inputs.story_id",
            f"got {content.story_id}, {music.story_id}, {motifs.story_id}",
        )
    sections = _validate_content(content)
    total_bars = content.global_.total_bars
    if total_bars < music.midigpt.model_dim_bars:
        _fail(
            "MODEL_DIM_EXCEEDS_SCORE",
            "music_plan.midigpt.model_dim_bars",
            f"score has only {total_bars} bars",
        )
    if music.midigpt.generation_config.bars_per_step > music.midigpt.model_dim_bars:
        _fail(
            "BARS_PER_STEP_EXCEEDS_MODEL_DIM",
            "music_plan.midigpt.generation_config.bars_per_step",
            str(music.midigpt.model_dim_bars),
        )
    numerator, denominator = _meter(content.global_.time_signature)
    raw_bar_ticks = music.ppq * numerator * 4 / denominator
    if not raw_bar_ticks.is_integer():
        _fail(
            "PPQ_METER_INCOMPATIBLE",
            "music_plan.ppq",
            "bar length is not an integer number of ticks",
        )
    bar_ticks = int(raw_bar_ticks)
    track_by_id = {item.track_id: item for item in music.tracks}
    heartbeat_tracks = [item for item in music.tracks if item.role == "heartbeat"]
    if len(heartbeat_tracks) != 1:
        _fail(
            "HEARTBEAT_TRACK_COUNT_INVALID",
            "music_plan.tracks",
            "exactly one heartbeat track is required",
        )
    heartbeat_track = heartbeat_tracks[0]
    if (
        heartbeat_track.track_type != "drum"
        or heartbeat_track.behavior != "context"
    ):
        _fail(
            "HEARTBEAT_TRACK_POLICY_INVALID",
            f"music_plan.tracks.{heartbeat_track.track_id}",
            "heartbeat must be a drum context track",
        )
    generated, autoregressive = _compile_regions(content, music, track_by_id)
    track_notes: dict[str, list[MidiNote]] = {
        item.track_id: [] for item in music.tracks
    }
    heartbeat_notes, heartbeat_audit = _render_heartbeat(
        content,
        music,
        heartbeat,
        sections,
        bar_ticks,
        numerator,
        denominator,
    )
    track_notes[heartbeat_track.track_id] = heartbeat_notes
    motif_audit = _place_motifs(
        content=content,
        music=music,
        motif_manifest=motifs,
        motif_base_dir=Path(motif_base_dir),
        sections=sections,
        track_by_id=track_by_id,
        track_notes=track_notes,
        generated=generated,
        bar_ticks=bar_ticks,
        numerator=numerator,
        denominator=denominator,
    )
    for track_id, notes in track_notes.items():
        for note in notes:
            bar_number = note.start_tick // bar_ticks + 1
            if bar_number in generated[track_id]:
                _fail(
                    "PROTECTED_FILL_OVERLAP",
                    f"tracks.{track_id}.bar[{bar_number}]",
                    "a known note occupies a MIDI-GPT target bar",
                )
        notes.sort(key=lambda item: (item.start_tick, item.pitch, item.duration_ticks))

    tempo = int(round(60_000_000 / content.global_.tempo_bpm))
    score_tracks = [
        _score_track(
            track,
            track_notes[track.track_id],
            total_bars,
            bar_ticks,
            numerator,
            denominator,
        )
        for track in music.tracks
    ]
    score = {"resolution": music.ppq, "tempo": tempo, "tracks": score_tracks}
    prompts = []
    for index, track in enumerate(music.tracks):
        bars = sorted(generated[track.track_id])
        prompts.append(
            {
                "id": index,
                "bars": [bar - 1 for bar in bars],
                "autoregressive": autoregressive[track.track_id],
                "ignore": track.behavior == "ignore",
                "mask_bars": [],
                "attributes": {},
                "controls": {},
                "bar_attributes": {},
                "bar_controls": {},
            }
        )
    config = music.midigpt.generation_config.model_dump(mode="json")
    config["model_dim"] = music.midigpt.model_dim_bars
    generation_request = {"tracks": prompts, "config": config, "controls": {}}
    generate_payload = {"score": score, "request": generation_request}

    midi_tracks = [
        MidiTrack(
            name=track.track_id,
            program=track.instrument,
            track_type=track.track_type,
            notes=track_notes[track.track_id],
        )
        for track in music.tracks
    ]
    heartbeat_midi = midi_bytes(
        resolution=music.ppq,
        tempo=tempo,
        numerator=numerator,
        denominator=denominator,
        tracks=[
            MidiTrack(
                name=heartbeat_track.track_id,
                program=heartbeat_track.instrument,
                track_type="drum",
                notes=heartbeat_notes,
            )
        ],
        end_tick=total_bars * bar_ticks,
    )
    scaffold_midi = midi_bytes(
        resolution=music.ppq,
        tempo=tempo,
        numerator=numerator,
        denominator=denominator,
        tracks=midi_tracks,
        end_tick=total_bars * bar_ticks,
    )
    heartbeat_render_manifest = {
        "schema_version": "0.1-draft",
        "builder_version": VERSION,
        "story_id": content.story_id,
        "track_id": heartbeat_track.track_id,
        "source_ppq": heartbeat.midi.ppq,
        "target_ppq": music.ppq,
        "time_signature": content.global_.time_signature,
        "preserve_complete_events": True,
        "events": heartbeat_audit,
    }
    assembly_manifest = {
        "schema_version": "0.1-draft",
        "builder_version": VERSION,
        "story_id": content.story_id,
        "checkpoint": music.midigpt.checkpoint,
        "coordinate_conversion": {
            "plan_bars": "1-based inclusive",
            "midigpt_bars": "0-based",
            "note_onset_ticks": "relative to each bar in score.json",
        },
        "global": {
            "ppq": music.ppq,
            "tempo_bpm": content.global_.tempo_bpm,
            "tempo_microseconds_per_quarter": tempo,
            "time_signature": content.global_.time_signature,
            "total_bars": total_bars,
            "bar_ticks": bar_ticks,
        },
        "track_ids": {
            track.track_id: index for index, track in enumerate(music.tracks)
        },
        "motif_placements": motif_audit,
        "protected_regions": [
            {
                "track_id": heartbeat_track.track_id,
                "bars": [1, total_bars],
                "kind": "heartbeat_anchor",
            },
            *[
                {
                    "track_id": row["target_track_id"],
                    "bars": row["target_bars"],
                    "kind": "motif_anchor",
                    "placement_id": row["placement_id"],
                }
                for row in motif_audit
            ],
        ],
        "fill_regions": [
            item.model_dump(mode="json") for item in music.midigpt.fill_regions
        ],
        "sampling_seed": music.midigpt.generation_config.seed,
        "checkpoint_runtime_validation_required": True,
    }
    validation_report = {
        "schema_version": "0.1-draft",
        "builder_version": VERSION,
        "story_id": content.story_id,
        "valid": True,
        "checks": {
            "story_ids_match": True,
            "section_coordinates_close": True,
            "heartbeat_rules_cover_every_bar_once": True,
            "heartbeat_events_traceable": True,
            "motif_hashes_match": True,
            "motifs_stay_inside_sections": True,
            "all_tracks_have_equal_bar_count": True,
            "protected_and_fill_regions_disjoint": True,
            "all_score_tracks_have_prompts": True,
            "request_has_generation_target": any(generated.values()),
            "mask_mode_is_attention": True,
        },
        "counts": {
            "tracks": len(music.tracks),
            "bars_per_track": total_bars,
            "heartbeat_events": len(heartbeat_audit),
            "motif_placements": len(motif_audit),
            "generated_bar_targets": sum(len(item) for item in generated.values()),
        },
        "runtime_checks_pending": [
            "checkpoint model_dim support",
            "checkpoint time-signature support",
            "checkpoint analyzer attribute sizes/labels/track types",
            "post-generation protected-event equality",
        ],
    }
    if not validation_report["checks"]["request_has_generation_target"]:
        _fail(
            "GENERATION_TARGET_MISSING",
            "music_plan.midigpt.fill_regions",
            "at least one bar must be generated",
        )
    return BuildResult(
        score=score,
        generation_request=generation_request,
        generate_payload=generate_payload,
        heartbeat_render_manifest=heartbeat_render_manifest,
        assembly_manifest=assembly_manifest,
        validation_report=validation_report,
        scaffold_midi=scaffold_midi,
        heartbeat_midi=heartbeat_midi,
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail("JSON_READ_FAILED", str(path), str(exc))
    if not isinstance(value, dict):
        _fail("JSON_OBJECT_REQUIRED", str(path), "top level must be an object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_scaffold_directory(
    content_plan_path: str | Path,
    music_plan_path: str | Path,
    heartbeat_manifest_path: str | Path,
    motif_manifest_path: str | Path,
    output_directory: str | Path,
) -> BuildResult:
    """Compile path-based inputs and atomically publish the seven artifacts."""

    paths = {
        "content_plan": Path(content_plan_path).expanduser().resolve(),
        "music_plan": Path(music_plan_path).expanduser().resolve(),
        "heartbeat_manifest": Path(heartbeat_manifest_path).expanduser().resolve(),
        "motif_manifest": Path(motif_manifest_path).expanduser().resolve(),
    }
    for name, path in paths.items():
        if not path.is_file():
            _fail("INPUT_FILE_MISSING", name, str(path))
    result = build_scaffold(
        _load_json(paths["content_plan"]),
        _load_json(paths["music_plan"]),
        _load_json(paths["heartbeat_manifest"]),
        _load_json(paths["motif_manifest"]),
        motif_base_dir=paths["motif_manifest"].parent,
    )
    target = Path(output_directory).expanduser().resolve()
    if target.exists():
        _fail("OUTPUT_EXISTS", "output_directory", str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent)
    )
    try:
        (temporary / "heartbeat_arranged.mid").write_bytes(result.heartbeat_midi)
        (temporary / "scaffold.mid").write_bytes(result.scaffold_midi)
        _write_json(temporary / "score.json", result.score)
        _write_json(
            temporary / "generation_request.json", result.generation_request
        )
        _write_json(temporary / "generate_payload.json", result.generate_payload)
        _write_json(
            temporary / "heartbeat_render_manifest.json",
            result.heartbeat_render_manifest,
        )
        _write_json(
            temporary / "validation_report.json", result.validation_report
        )
        result.assembly_manifest["inputs"] = {
            name: {"path": str(path), "sha256": _sha256(path)}
            for name, path in paths.items()
        }
        result.assembly_manifest["outputs"] = {
            path.name: {"sha256": _sha256(path)}
            for path in sorted(temporary.iterdir())
        }
        _write_json(
            temporary / "assembly_manifest.json", result.assembly_manifest
        )
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    result.output_directory = target
    return result
