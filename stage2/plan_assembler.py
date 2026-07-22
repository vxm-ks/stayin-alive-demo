"""Assemble an arbitrary Stage 1 form into a complete Stage 2 MIDI scaffold."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mido
from mido import MetaMessage, MidiFile, MidiTrack

try:
    from stage2.combine_midi_with_drums import (
        DRUM_CHANNEL,
        build_heartbeat_track_from_plan,
        message_priority,
    )
except ModuleNotFoundError:
    from combine_midi_with_drums import DRUM_CHANNEL, build_heartbeat_track_from_plan, message_priority


@dataclass(frozen=True)
class GenerationRegion:
    name: str
    source_start: int
    source_end: int
    gap_start: int
    gap_end: int


def build_generation_regions(plan_path: Path, total_bars: int) -> list[GenerationRegion]:
    """Compile arbitrary Stage 1 editable ranges into ordered MIDI-GPT jobs."""
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if int(data.get("total_bars", 0)) != total_bars:
        raise ValueError("stage2_plan total_bars differs from the assembled MIDI")
    sections = data.get("sections", [])
    by_id = {section["section_id"]: section for section in sections}
    result: list[GenerationRegion] = []
    for section in sections:
        access = section["midigpt_access"]
        if access == "fixed":
            continue
        if access == "extension_only":
            source_start = int(section["bar_start"]) - 1
            source_end = source_start + int(section["input_motif_bars"]) - 1
        elif access == "modifiable":
            source_id = section.get("source_section_id")
            if not source_id or source_id not in by_id:
                raise ValueError(f"{section['section_id']} has no valid transformation source")
            source = by_id[source_id]
            source_start = int(source["bar_start"]) - 1
            source_end = int(source["bar_end"]) - 1
        else:
            raise ValueError(f"unsupported midigpt_access: {access}")
        for range_index, editable in enumerate(section.get("editable_bar_ranges", []), 1):
            result.append(GenerationRegion(
                name=f"{section['section_id']}:{section['form_label']}:editable{range_index}",
                source_start=source_start,
                source_end=source_end,
                gap_start=int(editable["bar_start"]) - 1,
                gap_end=int(editable["bar_end"]) - 1,
            ))
    return result


def _absolute_events(track: MidiTrack) -> list[tuple[int, Any]]:
    tick = 0
    result: list[tuple[int, Any]] = []
    for message in track:
        tick += message.time
        result.append((tick, message))
    return result


def _track_end(midi: MidiFile) -> int:
    return max((sum(message.time for message in track) for track in midi.tracks), default=0)


def _shifted_track(
    source: MidiTrack,
    *,
    source_ppq: int,
    output_ppq: int,
    offset_ticks: int,
    motif_ticks: int,
    name: str,
) -> tuple[MidiTrack, int]:
    events: list[tuple[int, Any]] = []
    removed_channel_ten = 0
    scale = output_ppq / source_ppq
    for source_tick, original in _absolute_events(source):
        if original.type in {"end_of_track", "track_name", "set_tempo", "time_signature"}:
            continue
        if getattr(original, "channel", None) == DRUM_CHANNEL:
            removed_channel_ten += 1
            continue
        local_tick = round(source_tick * scale)
        if local_tick > motif_ticks:
            raise ValueError(f"theme event exceeds declared motif boundary in {name}")
        message = copy.copy(original)
        message.time = 0
        events.append((offset_ticks + local_tick, message))
    events.sort(key=lambda item: (item[0], message_priority(item[1])))
    output = MidiTrack([MetaMessage("track_name", name=name, time=0)])
    previous = 0
    for tick, message in events:
        message.time = tick - previous
        output.append(message)
        previous = tick
    output.append(MetaMessage("end_of_track", time=0))
    return output, removed_channel_ten


def restore_tempo_map(midi_path: Path, plan_path: Path) -> None:
    """Restore Stage 1's section tempo map after MIDI-GPT Score serialization."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    midi = MidiFile(midi_path)
    numerator, denominator = (int(part) for part in plan["time_signature"].split("/", 1))
    beat_ticks = round(midi.ticks_per_beat * 4 / denominator)
    bar_ticks = numerator * beat_ticks
    total_ticks = int(plan["total_bars"]) * bar_ticks

    for track in midi.tracks:
        kept: list[tuple[int, Any]] = []
        tick = 0
        for message in track:
            tick += message.time
            if message.type in {"set_tempo", "time_signature"}:
                continue
            clone = copy.copy(message)
            clone.time = 0
            kept.append((tick, clone))
        track.clear()
        previous = 0
        for tick, message in kept:
            message.time = tick - previous
            track.append(message)
            previous = tick

    conductor = MidiTrack([MetaMessage("track_name", name="LegaSynth Conductor", time=0)])
    events: list[tuple[int, Any]] = [
        (0, MetaMessage("time_signature", numerator=numerator, denominator=denominator, time=0))
    ]
    previous_tempo: float | None = None
    for section in plan["sections"]:
        tempo = float(section["tempo_bpm"])
        if previous_tempo is None or abs(tempo - previous_tempo) > 1e-9:
            events.append(((int(section["bar_start"]) - 1) * bar_ticks,
                           MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo), time=0)))
        previous_tempo = tempo
    events.sort(key=lambda item: (item[0], 0 if item[1].type == "time_signature" else 1))
    previous = 0
    for tick, message in events:
        message.time = tick - previous
        conductor.append(message)
        previous = tick
    conductor.append(MetaMessage("end_of_track", time=max(0, total_ticks - previous)))
    midi.tracks.insert(0, conductor)
    midi.save(midi_path)


def assemble_from_plan(
    *,
    theme_midis: dict[str, Path],
    plan_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    sections = plan["sections"]
    if not sections:
        raise ValueError("stage2_plan contains no sections")
    numerator, denominator = (int(part) for part in plan["time_signature"].split("/", 1))

    referenced = {
        str(section["theme_family_id"])
        for section in sections
        if section.get("material_source") in {"musecoco_seed", "reuse_theme"}
        or section.get("midigpt_access") in {"fixed", "extension_only"}
    }
    missing = sorted(referenced - set(theme_midis))
    if missing:
        raise ValueError(f"missing finalized theme MIDI bindings: {missing}")
    loaded = {theme_id: MidiFile(path) for theme_id, path in theme_midis.items() if theme_id in referenced}
    if not loaded:
        raise ValueError("no finalized theme MIDI is referenced by the form")
    output_ppq = next(iter(loaded.values())).ticks_per_beat
    beat_ticks = round(output_ppq * 4 / denominator)
    bar_ticks = numerator * beat_ticks
    total_bars = int(plan["total_bars"])

    output = MidiFile(type=1, ticks_per_beat=output_ppq)
    conductor = MidiTrack([MetaMessage("track_name", name="LegaSynth Conductor", time=0)])
    conductor_events: list[tuple[int, Any]] = [
        (0, MetaMessage("time_signature", numerator=numerator, denominator=denominator, time=0))
    ]
    previous_tempo: float | None = None
    for section in sections:
        tempo = float(section["tempo_bpm"])
        if previous_tempo is None or abs(tempo - previous_tempo) > 1e-9:
            conductor_events.append(((int(section["bar_start"]) - 1) * bar_ticks,
                                     MetaMessage("set_tempo", tempo=mido.bpm2tempo(tempo), time=0)))
        previous_tempo = tempo
    conductor_events.sort(key=lambda item: (item[0], 0 if item[1].type == "time_signature" else 1))
    previous = 0
    for tick, message in conductor_events:
        message.time = tick - previous
        conductor.append(message)
        previous = tick
    conductor.append(MetaMessage("end_of_track", time=total_bars * bar_ticks - previous))
    output.tracks.append(conductor)

    removed_channel_ten = 0
    placements: list[dict[str, Any]] = []
    occurrence_by_family: dict[str, int] = {}
    for section in sections:
        access = section["midigpt_access"]
        if access == "modifiable":
            placements.append({"section_id": section["section_id"], "status": "blank_for_transform"})
            continue
        theme_id = str(section["theme_family_id"])
        source = loaded[theme_id]
        motif_bars = int(section["input_motif_bars"])
        source_boundary = motif_bars * round(source.ticks_per_beat * 4 / denominator) * numerator
        if _track_end(source) > source_boundary:
            raise ValueError(f"{theme_id} exceeds section input_motif_bars={motif_bars}")
        occurrence_by_family[theme_id] = occurrence_by_family.get(theme_id, 0) + 1
        offset = (int(section["bar_start"]) - 1) * bar_ticks
        motif_ticks = motif_bars * bar_ticks
        for track_index, source_track in enumerate(source.tracks):
            track, removed = _shifted_track(
                source_track,
                source_ppq=source.ticks_per_beat,
                output_ppq=output_ppq,
                offset_ticks=offset,
                motif_ticks=motif_ticks,
                name=(f"{section['section_id']}:{section['form_label']}:{theme_id}:"
                      f"occ{occurrence_by_family[theme_id]}:track{track_index}"),
            )
            removed_channel_ten += removed
            output.tracks.append(track)
        placements.append({
            "section_id": section["section_id"], "status": "theme_placed",
            "theme_family_id": theme_id, "bar_start": section["bar_start"],
            "motif_bars": motif_bars,
        })

    output.tracks.append(build_heartbeat_track_from_plan(
        plan_path, total_bars, numerator, denominator, output_ppq,
    ))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.save(output_path)
    return {
        "form_string": plan["form_string"],
        "section_count": len(sections),
        "total_bars": total_bars,
        "time_signature": plan["time_signature"],
        "tempo_changes": len([message for message in conductor if message.type == "set_tempo"]),
        "removed_channel_10_messages": removed_channel_ten,
        "placements": placements,
    }
