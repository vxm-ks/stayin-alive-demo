#!/usr/bin/env python3
"""Detect the first recurring, transposition-invariant motif in a MIDI file."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pretty_midi


VERSION = "1.0.0"


@dataclass(frozen=True)
class MelodyEvent:
    start_s: float
    end_s: float
    start_beat: float
    end_beat: float
    pitch: int
    velocity: int
    chord_size: int


@dataclass(frozen=True)
class MotifMatch:
    start_s: float
    end_s: float
    start_beat: float
    end_beat: float
    transposition_semitones: int
    similarity: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_midi(path: Path) -> pretty_midi.PrettyMIDI:
    if not path.is_file():
        raise FileNotFoundError(f"MIDI file not found: {path}")
    if path.suffix.lower() not in {".mid", ".midi"}:
        raise ValueError("Input must use the .mid or .midi extension.")
    try:
        return pretty_midi.PrettyMIDI(str(path))
    except Exception as exc:
        raise ValueError(f"Cannot parse MIDI file: {path}") from exc


def _monophonic_ratio(notes: list[pretty_midi.Note]) -> float:
    if not notes:
        return 0.0
    starts = np.sort(np.asarray([note.start for note in notes], dtype=np.float64))
    distinct = 1 + int(np.count_nonzero(np.diff(starts) > 0.010))
    return distinct / len(notes)


def inspect_tracks(midi_path: str | Path) -> list[dict[str, Any]]:
    """Return pitched-instrument summaries in pretty_midi instrument order."""
    path = Path(midi_path).expanduser().resolve()
    midi = _load_midi(path)
    rows: list[dict[str, Any]] = []
    for index, instrument in enumerate(midi.instruments):
        notes = instrument.notes
        mean_pitch = float(np.mean([note.pitch for note in notes])) if notes else 0.0
        mono = _monophonic_ratio(notes)
        score = mean_pitch + 18.0 * mono + 4.0 * math.log1p(len(notes))
        if instrument.is_drum:
            score = -math.inf
        rows.append(
            {
                "index": index,
                "name": instrument.name or f"Track {index}",
                "program": int(instrument.program),
                "program_name": pretty_midi.program_to_instrument_name(
                    instrument.program
                ),
                "is_drum": instrument.is_drum,
                "note_count": len(notes),
                "mean_pitch": round(mean_pitch, 3),
                "monophonic_ratio": round(mono, 4),
                "melody_selection_score": (
                    round(score, 4) if math.isfinite(score) else None
                ),
            }
        )
    return rows


def _select_track(
    midi: pretty_midi.PrettyMIDI, track_index: int | None
) -> tuple[int, pretty_midi.Instrument, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for index, instrument in enumerate(midi.instruments):
        notes = instrument.notes
        mean_pitch = float(np.mean([note.pitch for note in notes])) if notes else 0.0
        mono = _monophonic_ratio(notes)
        score = mean_pitch + 18.0 * mono + 4.0 * math.log1p(len(notes))
        eligible = bool(notes) and not instrument.is_drum
        rows.append(
            {
                "index": index,
                "name": instrument.name or f"Track {index}",
                "program": int(instrument.program),
                "program_name": pretty_midi.program_to_instrument_name(
                    instrument.program
                ),
                "is_drum": instrument.is_drum,
                "note_count": len(notes),
                "mean_pitch": round(mean_pitch, 3),
                "monophonic_ratio": round(mono, 4),
                "melody_selection_score": round(score, 4) if eligible else None,
            }
        )

    if track_index is not None:
        if track_index < 0 or track_index >= len(midi.instruments):
            raise ValueError(f"track_index must be between 0 and {len(midi.instruments)-1}.")
        selected = midi.instruments[track_index]
        if selected.is_drum or not selected.notes:
            raise ValueError("Selected track must be a non-drum track containing notes.")
        return track_index, selected, rows

    eligible = [row for row in rows if row["melody_selection_score"] is not None]
    if not eligible:
        raise ValueError("MIDI contains no pitched non-drum notes.")
    best = max(eligible, key=lambda row: float(row["melody_selection_score"]))
    index = int(best["index"])
    return index, midi.instruments[index], rows


def _beat_at_time(midi: pretty_midi.PrettyMIDI, time_s: float) -> float:
    return float(midi.time_to_tick(time_s)) / midi.resolution


def _time_at_beat(midi: pretty_midi.PrettyMIDI, beat: float) -> float:
    return float(midi.tick_to_time(int(round(beat * midi.resolution))))


def _melody_events(
    midi: pretty_midi.PrettyMIDI,
    instrument: pretty_midi.Instrument,
    onset_tolerance_beats: float,
) -> list[MelodyEvent]:
    rows = sorted(instrument.notes, key=lambda note: (note.start, -note.pitch, note.end))
    converted = [
        (
            note,
            _beat_at_time(midi, note.start),
            _beat_at_time(midi, note.end),
        )
        for note in rows
        if note.end > note.start
    ]
    events: list[MelodyEvent] = []
    position = 0
    while position < len(converted):
        anchor = converted[position][1]
        group = [converted[position]]
        position += 1
        while (
            position < len(converted)
            and converted[position][1] - anchor <= onset_tolerance_beats
        ):
            group.append(converted[position])
            position += 1
        selected, start_beat, end_beat = max(group, key=lambda item: item[0].pitch)
        events.append(
            MelodyEvent(
                start_s=float(selected.start),
                end_s=float(selected.end),
                start_beat=start_beat,
                end_beat=end_beat,
                pitch=int(selected.pitch),
                velocity=int(selected.velocity),
                chord_size=len(group),
            )
        )
    return events


def _meter_context_at_start(
    midi: pretty_midi.PrettyMIDI, time_s: float
) -> tuple[int, int, float, int]:
    numerator, denominator = 4, 4
    origin_beat = 0.0
    origin_bar = 1
    for change in sorted(midi.time_signature_changes, key=lambda item: item.time):
        if change.time > time_s + 1e-9:
            break
        change_beat = _beat_at_time(midi, change.time)
        old_bar_length = numerator * 4.0 / denominator
        elapsed = max(0.0, change_beat - origin_beat)
        if elapsed > 1e-9:
            origin_bar += int(math.ceil(elapsed / old_bar_length - 1e-9))
        numerator, denominator = change.numerator, change.denominator
        origin_beat = change_beat
    return int(numerator), int(denominator), origin_beat, origin_bar


def _position_in_meter(
    beat: float,
    origin_beat: float,
    origin_bar: int,
    numerator: int,
    denominator: int,
) -> tuple[int, float]:
    denominator_beat = 4.0 / denominator
    bar_length = numerator * denominator_beat
    relative = max(0.0, beat - origin_beat)
    return origin_bar + int(math.floor(relative / bar_length)), (
        relative % bar_length
    ) / denominator_beat + 1.0


def _sequence_similarity(
    first: list[MelodyEvent], other: list[MelodyEvent]
) -> tuple[float, int, float]:
    count = len(first)
    if count != len(other) or count < 2:
        return 0.0, 0, 1.0

    pitch_a = np.asarray([event.pitch for event in first], dtype=np.float64)
    pitch_b = np.asarray([event.pitch for event in other], dtype=np.float64)
    transposition = int(round(float(np.median(pitch_b - pitch_a))))
    pitch_error = np.abs((pitch_b - transposition) - pitch_a)
    pitch_score = float(np.mean(np.exp(-pitch_error / 1.5)))

    onset_a = np.asarray([event.start_beat for event in first], dtype=np.float64)
    onset_b = np.asarray([event.start_beat for event in other], dtype=np.float64)
    onset_a -= onset_a[0]
    onset_b -= onset_b[0]
    diff_a = np.diff(onset_a)
    diff_b = np.diff(onset_b)
    positive = diff_a > 1e-9
    tempo_scale = (
        float(np.median(diff_b[positive] / diff_a[positive]))
        if np.any(positive)
        else 1.0
    )
    if tempo_scale < 0.75 or tempo_scale > 1.333:
        return 0.0, transposition, tempo_scale
    aligned_b = onset_b / tempo_scale
    rhythm_error = float(np.sqrt(np.mean((aligned_b - onset_a) ** 2)))
    rhythm_score = math.exp(-rhythm_error / 0.125)

    duration_a = np.asarray(
        [max(event.end_beat - event.start_beat, 1e-6) for event in first]
    )
    duration_b = np.asarray(
        [max(event.end_beat - event.start_beat, 1e-6) for event in other]
    ) / tempo_scale
    duration_error = float(np.median(np.abs(np.log2(duration_b / duration_a))))
    duration_score = math.exp(-duration_error / 0.5)

    score = 0.58 * pitch_score + 0.32 * rhythm_score + 0.10 * duration_score
    return float(score), transposition, tempo_scale


def _candidate_boundaries(
    events: list[MelodyEvent],
    bar_length: float,
    min_bars: float,
    max_bars: float,
    min_notes: int,
) -> list[int]:
    start = events[0].start_beat
    boundaries: list[int] = []
    for index in range(min_notes, len(events)):
        duration = events[index].start_beat - start
        if duration < min_bars * bar_length:
            continue
        if duration > max_bars * bar_length:
            break
        boundaries.append(index)
    return boundaries


def detect_first_motif(
    midi_path: str | Path,
    *,
    track_index: int | None = None,
    min_bars: float = 0.5,
    max_bars: float = 4.0,
    min_notes: int = 4,
    similarity_threshold: float = 0.78,
    onset_tolerance_beats: float = 1.0 / 32.0,
) -> dict[str, Any]:
    """Detect the first motif and return a JSON-serialisable analysis report."""
    path = Path(midi_path).expanduser().resolve()
    if min_bars <= 0 or max_bars < min_bars:
        raise ValueError("Require 0 < min_bars <= max_bars.")
    if min_notes < 3:
        raise ValueError("min_notes must be at least 3.")
    if not 0.0 < similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be in (0, 1].")

    midi = _load_midi(path)
    selected_index, instrument, tracks = _select_track(midi, track_index)
    events = _melody_events(midi, instrument, onset_tolerance_beats)
    if len(events) < min_notes + 1:
        raise ValueError(
            f"Selected track has only {len(events)} melodic onset groups; "
            f"at least {min_notes + 1} are required."
        )

    start = events[0]
    numerator, denominator, meter_origin_beat, meter_origin_bar = (
        _meter_context_at_start(midi, start.start_s)
    )
    denominator_beat = 4.0 / denominator
    bar_length = numerator * denominator_beat
    boundaries = _candidate_boundaries(
        events, bar_length, min_bars, max_bars, min_notes
    )
    if not boundaries:
        raise ValueError("No candidate motif boundary falls inside the requested range.")

    accepted: list[dict[str, Any]] = []
    for boundary in boundaries:
        motif_events = events[:boundary]
        end_beat = events[boundary].start_beat
        duration = end_beat - start.start_beat
        matches: list[tuple[MotifMatch, int]] = []
        for later in range(boundary, len(events) - boundary + 1):
            if events[later].start_beat < end_beat - onset_tolerance_beats:
                continue
            other = events[later : later + boundary]
            similarity, transposition, tempo_scale = _sequence_similarity(
                motif_events, other
            )
            if similarity < similarity_threshold:
                continue
            match_start = other[0].start_beat
            match_end = match_start + duration * tempo_scale
            matches.append(
                (
                    MotifMatch(
                        start_s=other[0].start_s,
                        end_s=_time_at_beat(midi, match_end),
                        start_beat=match_start,
                        end_beat=match_end,
                        transposition_semitones=transposition,
                        similarity=round(similarity, 6),
                    ),
                    later,
                )
            )
        if not matches:
            continue

        matches.sort(key=lambda item: (-item[0].similarity, item[0].start_beat))
        best_match, best_later = matches[0]
        gap_to_match = max(0.0, best_match.start_beat - end_beat)
        adjacency = math.exp(-gap_to_match / max(bar_length, 1e-9))
        bar_fraction = duration / bar_length
        standard_lengths = np.asarray([0.5, 1.0, 2.0, 3.0, 4.0])
        bar_alignment = math.exp(
            -float(np.min(np.abs(standard_lengths - bar_fraction))) / 0.10
        )
        quality = (
            best_match.similarity
            + 0.12 * adjacency
            + 0.06 * bar_alignment
            + 0.015 * math.log1p(len(matches))
        )
        accepted.append(
            {
                "boundary": boundary,
                "end_beat": end_beat,
                "duration": duration,
                "quality": quality,
                "similarity": best_match.similarity,
                "adjacency": adjacency,
                "bar_alignment": bar_alignment,
                "best_later": best_later,
                "matches": matches,
            }
        )

    if accepted:
        chosen = max(accepted, key=lambda candidate: candidate["quality"])
        method = "recurring_pitch_interval_and_rhythm_pattern"
        confidence = min(
            0.99,
            0.72 * chosen["similarity"]
            + 0.12 * chosen["adjacency"]
            + 0.08 * chosen["bar_alignment"]
            + 0.03 * min(2, len(chosen["matches"])),
        )
        matches = [asdict(item[0]) for item in chosen["matches"]]
    else:
        fallback: list[tuple[float, int, float, float]] = []
        for boundary in boundaries:
            end_beat = events[boundary].start_beat
            duration = end_beat - start.start_beat
            sounding_end = max(event.end_beat for event in events[:boundary])
            rest = max(0.0, end_beat - sounding_end)
            fraction = duration / bar_length
            alignment = math.exp(-abs(fraction - round(fraction * 2) / 2) / 0.10)
            fallback.append((rest / bar_length + 0.25 * alignment, boundary, rest, alignment))
        _, boundary, rest, alignment = max(fallback, key=lambda item: item[0])
        chosen = {
            "boundary": boundary,
            "end_beat": events[boundary].start_beat,
            "duration": events[boundary].start_beat - start.start_beat,
        }
        method = "phrase_boundary_fallback_no_recurrence"
        confidence = min(0.59, 0.30 + 0.15 * alignment + 0.25 * min(1.0, rest))
        matches = []

    boundary = int(chosen["boundary"])
    end_beat = float(chosen["end_beat"])
    motif_events = events[:boundary]
    sounding_end_s = max(event.end_s for event in motif_events)
    start_bar, start_beat_in_bar = _position_in_meter(
        start.start_beat,
        meter_origin_beat,
        meter_origin_bar,
        numerator,
        denominator,
    )
    end_bar, end_beat_in_bar = _position_in_meter(
        end_beat,
        meter_origin_beat,
        meter_origin_bar,
        numerator,
        denominator,
    )
    tempo_times, tempi = midi.get_tempo_changes()

    return {
        "schema_version": "1.0",
        "detector_version": VERSION,
        "source": {
            "path": str(path),
            "sha256": _sha256(path),
            "duration_s": round(float(midi.get_end_time()), 6),
            "resolution_ppq": int(midi.resolution),
        },
        "tracks": tracks,
        "selected_track": tracks[selected_index],
        "selection": "explicit" if track_index is not None else "automatic_melody_heuristic",
        "meter_at_motif_start": {
            "numerator": numerator,
            "denominator": denominator,
            "bar_length_quarter_beats": bar_length,
            "meter_origin_beat": meter_origin_beat,
            "meter_origin_bar": meter_origin_bar,
        },
        "tempo_changes": [
            {"time_s": round(float(time), 6), "bpm": round(float(bpm), 6)}
            for time, bpm in zip(tempo_times, tempi)
        ],
        "parameters": {
            "min_bars": min_bars,
            "max_bars": max_bars,
            "min_notes": min_notes,
            "similarity_threshold": similarity_threshold,
            "onset_tolerance_beats": onset_tolerance_beats,
            "polyphony_policy": "highest_pitch_at_each_onset",
            "transposition_invariant": True,
        },
        "motif": {
            "method": method,
            "confidence": round(float(confidence), 6),
            "start_s": round(start.start_s, 6),
            "end_s": round(_time_at_beat(midi, end_beat), 6),
            "sounding_end_s": round(float(sounding_end_s), 6),
            "start_beat": round(start.start_beat, 6),
            "end_beat": round(end_beat, 6),
            "duration_beats": round(float(chosen["duration"]), 6),
            "duration_bars": round(float(chosen["duration"] / bar_length), 6),
            "start_bar": start_bar,
            "start_beat_in_bar": round(start_beat_in_bar, 6),
            "end_bar": end_bar,
            "end_beat_in_bar": round(end_beat_in_bar, 6),
            "note_count": boundary,
            "pitches": [event.pitch for event in motif_events],
            "events": [asdict(event) for event in motif_events],
        },
        "matches": matches,
        "warnings": [
            "Automatic motif detection is an engineering estimate, not a definitive musicological annotation."
        ]
        + (
            ["No sufficiently similar recurrence was found; a phrase boundary fallback was used."]
            if not matches
            else []
        ),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect the first recurring motif in a Standard MIDI File."
    )
    parser.add_argument("midi", type=Path, help="input .mid or .midi file")
    parser.add_argument("--track", type=int, help="instrument index from --list-tracks")
    parser.add_argument("--list-tracks", action="store_true")
    parser.add_argument("--min-bars", type=float, default=0.5)
    parser.add_argument("--max-bars", type=float, default=4.0)
    parser.add_argument("--min-notes", type=int, default=4)
    parser.add_argument("--similarity", type=float, default=0.78)
    parser.add_argument("--output", type=Path, help="analysis JSON path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.list_tracks:
            print(json.dumps(inspect_tracks(args.midi), ensure_ascii=False, indent=2))
            return 0
        report = detect_first_motif(
            args.midi,
            track_index=args.track,
            min_bars=args.min_bars,
            max_bars=args.max_bars,
            min_notes=args.min_notes,
            similarity_threshold=args.similarity,
        )
        output = args.output
        if output is None:
            output = Path(__file__).resolve().parent / "outputs" / (
                args.midi.stem + "_motif_analysis.json"
            )
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    motif = report["motif"]
    print("status=PASS")
    print(f"track={report['selected_track']['index']}:{report['selected_track']['name']}")
    print(f"method={motif['method']}")
    print(f"motif_start_s={motif['start_s']:.6f}")
    print(f"motif_end_s={motif['end_s']:.6f}")
    print(f"motif_start_beat={motif['start_beat']:.6f}")
    print(f"motif_end_beat={motif['end_beat']:.6f}")
    print(f"confidence={motif['confidence']:.3f}")
    print(f"matches={len(report['matches'])}")
    print(f"report={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
