"""Optional whole-texture repetition detection and local MIDI-GPT regeneration.

The detector deliberately does not classify melody and harmony.  It merges every
pitched track into two-bar sonority sequences, excludes channel 10, and compares
the sounding pitches at corresponding ordered onsets.  Rhythm is not an
independent score component.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import Any

from mido import MidiFile


SCHEMA_VERSION = "legasynth-stage2-repetition-gate-v1"
WINDOW_BARS = 2
DEFAULT_THRESHOLD = 0.82
DEFAULT_MAX_OCCURRENCES = 2
DEFAULT_CANDIDATES = 4


@dataclass(frozen=True)
class SectionScope:
    section_id: str
    start_bar: int  # zero-based, inclusive
    end_bar: int  # zero-based, inclusive
    editable_ranges: tuple[tuple[int, int], ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _time_signature(midi: MidiFile) -> tuple[int, int]:
    signatures = {
        (message.numerator, message.denominator)
        for track in midi.tracks
        for message in track
        if message.type == "time_signature"
    }
    if not signatures:
        return 4, 4
    if len(signatures) != 1:
        raise ValueError("repetition quality gate requires one constant time signature")
    return next(iter(signatures))


def _total_ticks(midi: MidiFile) -> int:
    return max(
        (sum(message.time for message in track) for track in midi.tracks),
        default=0,
    )


def _scopes(midi: MidiFile, stage2_plan: Path | None) -> list[SectionScope]:
    numerator, denominator = _time_signature(midi)
    ticks_per_bar = midi.ticks_per_beat * numerator * 4 / denominator
    total_bars = max(1, round(_total_ticks(midi) / ticks_per_bar))
    if stage2_plan is None:
        return [SectionScope("whole_song", 0, total_bars - 1, ((0, total_bars - 1),))]

    data = json.loads(stage2_plan.read_text(encoding="utf-8"))
    scopes: list[SectionScope] = []
    for index, section in enumerate(data.get("sections", []), 1):
        editable = tuple(
            (int(item["bar_start"]) - 1, int(item["bar_end"]) - 1)
            for item in section.get("editable_bar_ranges", [])
        )
        scopes.append(SectionScope(
            str(section.get("section_id") or f"section_{index}"),
            int(section["bar_start"]) - 1,
            int(section["bar_end"]) - 1,
            editable,
        ))
    if not scopes:
        raise ValueError("stage2_plan contains no sections for repetition analysis")
    return scopes


def _midi_onsets(midi: MidiFile) -> dict[int, set[int]]:
    """Return quantized absolute onset -> all non-drum pitches sounding there."""
    quantum = max(1, midi.ticks_per_beat // 4)
    groups: dict[int, set[int]] = {}
    for track in midi.tracks:
        tick = 0
        for message in track:
            tick += message.time
            if (
                message.type == "note_on"
                and message.velocity > 0
                and getattr(message, "channel", None) != 9
            ):
                slot = round(tick / quantum)
                groups.setdefault(slot, set()).add(int(message.note))
    return groups


def _window_from_midi(
    midi: MidiFile,
    onset_groups: dict[int, set[int]],
    start_bar: int,
    bars: int = WINDOW_BARS,
) -> list[set[int]]:
    numerator, denominator = _time_signature(midi)
    slots_per_bar = round(numerator * 4 / denominator * 4)
    start = start_bar * slots_per_bar
    end = (start_bar + bars) * slots_per_bar
    return [onset_groups[slot] for slot in sorted(onset_groups) if start <= slot < end]


def _sonority_similarity(left: set[int], right: set[int], shift: int = 0) -> float:
    shifted = {pitch + shift for pitch in right}
    if not left and not shifted:
        return 1.0
    if not left or not shifted:
        return 0.0
    return 2.0 * len(left & shifted) / (len(left) + len(shifted))


def _sequence_similarity(
    left: list[set[int]], right: list[set[int]], shift: int = 0
) -> float:
    """Weighted LCS over ordered sonorities; silent grid locations never score."""
    if not left or not right:
        return 0.0
    previous = [0.0] * (len(right) + 1)
    for left_item in left:
        current = [0.0]
        for column, right_item in enumerate(right, 1):
            current.append(max(
                previous[column],
                current[column - 1],
                previous[column - 1] + _sonority_similarity(left_item, right_item, shift),
            ))
        previous = current
    return previous[-1] / max(len(left), len(right))


def _cosine(left: Counter[int], right: Counter[int]) -> float:
    keys = set(left) | set(right)
    left_norm = sqrt(sum(value * value for value in left.values()))
    right_norm = sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return sum(left[key] * right[key] for key in keys) / (left_norm * right_norm)


def _pitch_class_histogram(window: list[set[int]], shift: int = 0) -> Counter[int]:
    return Counter((pitch + shift) % 12 for sonority in window for pitch in sonority)


def _interval_class_histogram(window: list[set[int]]) -> Counter[int]:
    result: Counter[int] = Counter()
    for sonority in window:
        ordered = sorted(sonority)
        for index, lower in enumerate(ordered):
            for upper in ordered[index + 1:]:
                result[(upper - lower) % 12] += 1
    return result


def _combined_similarity(left: list[set[int]], right: list[set[int]], shift: int) -> float:
    event_overlap = _sequence_similarity(left, right, shift)
    interval_structure = _cosine(
        _interval_class_histogram(left), _interval_class_histogram(right)
    )
    pitch_class_content = _cosine(
        _pitch_class_histogram(left), _pitch_class_histogram(right, shift)
    )
    return 0.60 * event_overlap + 0.25 * interval_structure + 0.15 * pitch_class_content


def compare_windows(left: list[set[int]], right: list[set[int]]) -> dict[str, float | int]:
    absolute = _combined_similarity(left, right, 0)
    candidates = [(_combined_similarity(left, right, shift), shift) for shift in range(-12, 13)]
    transposed, shift = max(candidates)
    return {
        "absolute_similarity": round(absolute, 6),
        "transposition_invariant_similarity": round(transposed, 6),
        "best_transposition_semitones": shift,
        "overall_similarity": round(max(absolute, transposed), 6),
    }


def detect_repetitions(
    midi_path: Path,
    *,
    stage2_plan: Path | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    max_occurrences: int = DEFAULT_MAX_OCCURRENCES,
) -> tuple[list[dict[str, Any]], list[SectionScope]]:
    if not 0.0 < threshold <= 1.0:
        raise ValueError("repetition threshold must be in (0, 1]")
    if max_occurrences < 1:
        raise ValueError("maximum unchanged occurrences must be positive")
    midi = MidiFile(midi_path)
    groups = _midi_onsets(midi)
    scopes = _scopes(midi, stage2_plan)
    issues: list[dict[str, Any]] = []

    for scope in scopes:
        starts = list(range(scope.start_bar, scope.end_bar - WINDOW_BARS + 2, WINDOW_BARS))
        windows = {start: _window_from_midi(midi, groups, start) for start in starts}
        occurrence_count: dict[int, int] = {}
        for position, current_start in enumerate(starts):
            best: tuple[int, float, int, dict[str, float | int]] | None = None
            for previous_start in starts[:position]:
                scores = compare_windows(windows[previous_start], windows[current_start])
                score = float(scores["overall_similarity"])
                if score < threshold:
                    continue
                candidate = occurrence_count.get(previous_start, 1), score, previous_start, scores
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
            if best is None:
                occurrence_count[current_start] = 1
                continue
            previous_occurrence, score, previous_start, scores = best
            occurrence = previous_occurrence + 1
            occurrence_count[current_start] = occurrence
            if occurrence <= max_occurrences:
                continue
            target_end = current_start + WINDOW_BARS - 1
            editable = any(
                start <= current_start and target_end <= end
                for start, end in scope.editable_ranges
            )
            issues.append({
                "section_id": scope.section_id,
                "reference_bars": [previous_start + 1, previous_start + WINDOW_BARS],
                "repeated_bars": [current_start + 1, current_start + WINDOW_BARS],
                "occurrence": occurrence,
                "editable": editable,
                **scores,
            })
    return issues, scopes


def _heartbeat_signature(path: Path) -> list[tuple[int, str, int, int]]:
    events: list[tuple[int, str, int, int]] = []
    for track in MidiFile(path).tracks:
        tick = 0
        for message in track:
            tick += message.time
            if getattr(message, "channel", None) != 9:
                continue
            if message.type not in {"note_on", "note_off"}:
                continue
            events.append((tick, message.type, int(message.note), int(getattr(message, "velocity", 0))))
    return sorted(events)


def _score_window(score: Any, start_bar: int, bars: int = WINDOW_BARS) -> list[set[int]]:
    groups: dict[tuple[int, int], set[int]] = {}
    for track in score.tracks:
        if track.track_type == "drum":
            continue
        for bar_id in range(start_bar, start_bar + bars):
            for note in track.bars[bar_id].notes:
                groups.setdefault((bar_id, int(note.onset_ticks)), set()).add(int(note.pitch))
    return [groups[key] for key in sorted(groups)]


def _regenerate(
    source: Path,
    destination: Path,
    issues: list[dict[str, Any]],
    scopes: list[SectionScope],
    *,
    model_name: str,
    seed: int,
    threshold: float,
    candidates: int,
) -> list[dict[str, Any]]:
    from midigpt import Score
    from midigpt.inference import GenerationRequest, InferenceConfig, InferenceEngine, TrackPrompt

    if candidates < 1:
        raise ValueError("repetition candidate count must be positive")
    score = Score.from_midi(str(source))
    engine = InferenceEngine.from_pretrained(model_name)
    scope_by_id = {scope.section_id: scope for scope in scopes}
    actions: list[dict[str, Any]] = []

    for issue_index, issue in enumerate(issues):
        if not issue["editable"]:
            actions.append({"bars": issue["repeated_bars"], "status": "protected_not_modified"})
            continue
        target_start = int(issue["repeated_bars"][0]) - 1
        target_bars = list(range(target_start, target_start + WINDOW_BARS))
        # Preserve the final two-bar cadence written by complete_all_gaps_v3.
        if target_bars[-1] >= len(score.tracks[0].bars) - 2:
            actions.append({"bars": issue["repeated_bars"], "status": "final_cadence_not_modified"})
            continue
        scope = scope_by_id[str(issue["section_id"])]
        active = [
            track_id for track_id, track in enumerate(score.tracks)
            if track.track_type != "drum"
            and any(track.bars[bar].notes for bar in range(scope.start_bar, scope.end_bar + 1))
        ]
        if not active:
            raise RuntimeError(f"no pitched tracks available for bars {issue['repeated_bars']}")
        prompts = [
            TrackPrompt(
                id=track_id,
                bars=target_bars if track_id in active else [],
                autoregressive=False,
                ignore=(track_id not in active and track.track_type != "drum"),
            )
            for track_id, track in enumerate(score.tracks)
        ]
        best: tuple[float, Any, int] | None = None
        prior_starts = list(range(scope.start_bar, target_start, WINDOW_BARS))
        for attempt in range(candidates):
            candidate_seed = seed + issue_index * candidates + attempt
            request = GenerationRequest(
                tracks=prompts,
                config=InferenceConfig(
                    temperature=1.0 * (1.04 ** attempt), top_p=0.95, seed=candidate_seed,
                    max_attempts=1, novelty_check=False, silence_check=False,
                    temperature_escalation=1.0, model_dim=8, mask_mode="attention",
                    bars_per_step=WINDOW_BARS, tracks_per_step=1, shuffle=False,
                ),
            )
            generated = engine.session(score, request).run()
            candidate = copy.deepcopy(score)
            scale = candidate.resolution / generated.resolution
            note_count = 0
            for track_id in active:
                for bar_id in target_bars:
                    notes = []
                    for source_note in generated.tracks[track_id].bars[bar_id].notes:
                        note = copy.deepcopy(source_note)
                        note.onset_ticks = round(note.onset_ticks * scale)
                        note.duration_ticks = max(1, round(note.duration_ticks * scale))
                        if hasattr(note, "delta"):
                            note.delta = round(note.delta * scale)
                        notes.append(note)
                    candidate.tracks[track_id].bars[bar_id].notes = notes
                    note_count += len(notes)
            if note_count == 0:
                continue
            target = _score_window(candidate, target_start)
            worst = max(
                (float(compare_windows(_score_window(candidate, start), target)["overall_similarity"])
                 for start in prior_starts),
                default=0.0,
            )
            if best is None or worst < best[0]:
                best = worst, candidate, candidate_seed
        if best is None or best[0] >= threshold:
            raise RuntimeError(
                f"MIDI-GPT could not remove excessive repetition in bars {issue['repeated_bars']} "
                f"after {candidates} candidates"
            )
        score = best[1]
        actions.append({
            "bars": issue["repeated_bars"], "status": "regenerated",
            "selected_seed": best[2], "maximum_similarity_after": round(best[0], 6),
        })

    temporary = destination.with_name(f".{destination.stem}.repetition.tmp{destination.suffix}")
    score.to_midi(str(temporary))
    if _heartbeat_signature(source) != _heartbeat_signature(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("repetition regeneration changed protected channel-10 events")
    os.replace(temporary, destination)
    return actions


def run_repetition_quality_gate(
    midi_path: Path,
    report_path: Path,
    *,
    mode: str,
    stage2_plan: Path | None,
    model_name: str,
    seed: int,
    threshold: float = DEFAULT_THRESHOLD,
    max_occurrences: int = DEFAULT_MAX_OCCURRENCES,
    candidates: int = DEFAULT_CANDIDATES,
) -> dict[str, Any]:
    if mode not in {"off", "detect", "regenerate"}:
        raise ValueError(f"unsupported repetition mode: {mode}")
    if mode == "regenerate" and stage2_plan is None:
        raise ValueError("repetition regeneration requires stage2_plan editable ranges")
    before_hash = _sha256(midi_path)
    issues: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    scopes: list[SectionScope] = []
    if mode != "off":
        issues, scopes = detect_repetitions(
            midi_path, stage2_plan=stage2_plan, threshold=threshold,
            max_occurrences=max_occurrences,
        )
    if mode == "regenerate" and issues:
        backup = midi_path.with_name(f"{midi_path.stem}.pre_repetition_gate{midi_path.suffix}")
        if backup.exists() and _sha256(backup) != before_hash:
            raise RuntimeError(f"existing repetition-gate backup differs from current input: {backup}")
        if not backup.exists():
            backup.write_bytes(midi_path.read_bytes())
        actions = _regenerate(
            backup, midi_path, issues, scopes, model_name=model_name, seed=seed + 10000,
            threshold=threshold, candidates=candidates,
        )
    final_issues = (
        detect_repetitions(
            midi_path, stage2_plan=stage2_plan, threshold=threshold,
            max_occurrences=max_occurrences,
        )[0]
        if mode == "regenerate" else issues
    )
    if mode == "regenerate" and issues:
        final_bar = max(scope.end_bar for scope in scopes) + 1
        unresolved_editable = [
            issue for issue in final_issues
            if issue["editable"] and int(issue["repeated_bars"][1]) < final_bar - 1
        ]
        if unresolved_editable:
            midi_path.write_bytes(backup.read_bytes())
            raise RuntimeError(
                "repetition regeneration left editable excessive repetition; original MIDI restored"
            )
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "algorithm": "two_bar_whole_texture_pitch_similarity_v1",
        "similarity_weights": {
            "ordered_pitch_event_overlap": 0.60,
            "interval_class_structure": 0.25,
            "pitch_class_content": 0.15,
        },
        "rhythm_scored": False,
        "melody_harmony_classified": False,
        "excluded_midi_channel": 10,
        "settings": {
            "window_bars": WINDOW_BARS, "threshold": threshold,
            "maximum_unchanged_occurrences": max_occurrences,
            "regeneration_candidates": candidates,
        },
        "input_sha256": before_hash,
        "output_sha256": _sha256(midi_path),
        "initial_issue_count": len(issues),
        "initial_issues": issues,
        "actions": actions,
        "final_issue_count": len(final_issues),
        "final_issues": final_issues,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
