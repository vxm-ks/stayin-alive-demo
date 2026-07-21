import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import pretty_midi

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from midi_motif_detector import midi_motif_detector as detector


def add_sequence(
    instrument: pretty_midi.Instrument,
    pitches: list[int],
    start_beat: float,
    *,
    beat_s: float = 0.5,
    step_beats: float = 1.0,
    duration_beats: float | None = None,
) -> None:
    duration = 0.8 * (step_beats if duration_beats is None else duration_beats)
    for index, pitch in enumerate(pitches):
        start = (start_beat + index * step_beats) * beat_s
        instrument.notes.append(
            pretty_midi.Note(
                velocity=96,
                pitch=pitch,
                start=start,
                end=start + duration * beat_s,
            )
        )


def write_repeating_midi(path: Path, include_accompaniment: bool = False) -> None:
    midi = pretty_midi.PrettyMIDI(initial_tempo=120.0, resolution=480)
    if include_accompaniment:
        accompaniment = pretty_midi.Instrument(program=0, name="Chords")
        for beat in range(8):
            add_sequence(
                accompaniment,
                [48, 52, 55],
                beat,
                step_beats=0.0,
                duration_beats=0.8,
            )
        midi.instruments.append(accompaniment)
    melody = pretty_midi.Instrument(program=40, name="Lead")
    add_sequence(melody, [60, 62, 64, 67], 0.0)
    add_sequence(melody, [65, 67, 69, 72], 4.0)
    midi.instruments.append(melody)
    midi.write(str(path))


class MidiMotifDetectorTests(unittest.TestCase):
    def test_detects_adjacent_transposed_repetition(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "repeat.mid"
            write_repeating_midi(path)

            report = detector.detect_first_motif(path)
            motif = report["motif"]

            self.assertEqual(motif["method"], "recurring_pitch_interval_and_rhythm_pattern")
            self.assertAlmostEqual(motif["start_s"], 0.0, places=4)
            self.assertAlmostEqual(motif["end_s"], 2.0, places=4)
            self.assertAlmostEqual(motif["duration_beats"], 4.0, places=4)
            self.assertEqual(motif["pitches"], [60, 62, 64, 67])
            self.assertGreater(motif["confidence"], 0.9)
            self.assertEqual(report["matches"][0]["transposition_semitones"], 5)
            self.assertAlmostEqual(report["matches"][0]["similarity"], 1.0)

    def test_automatically_selects_melody_track_over_chords(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "arrangement.mid"
            write_repeating_midi(path, include_accompaniment=True)

            report = detector.detect_first_motif(path)

            self.assertEqual(report["selected_track"]["name"], "Lead")
            self.assertEqual(report["selected_track"]["index"], 1)
            self.assertEqual(report["selection"], "automatic_melody_heuristic")

    def test_explicit_track_selection_is_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "arrangement.mid"
            write_repeating_midi(path, include_accompaniment=True)

            report = detector.detect_first_motif(path, track_index=1)

            self.assertEqual(report["selection"], "explicit")
            self.assertEqual(report["selected_track"]["name"], "Lead")

    def test_bar_position_preserves_leading_rest(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "pickup.mid"
            midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
            melody = pretty_midi.Instrument(program=40, name="Lead")
            add_sequence(melody, [60, 62, 64, 67], 2.0)
            add_sequence(melody, [65, 67, 69, 72], 6.0)
            midi.instruments.append(melody)
            midi.write(str(path))

            motif = detector.detect_first_motif(path)["motif"]

            self.assertEqual(motif["start_bar"], 1)
            self.assertAlmostEqual(motif["start_beat_in_bar"], 3.0)
            self.assertEqual(motif["end_bar"], 2)
            self.assertAlmostEqual(motif["end_beat_in_bar"], 3.0)

    def test_uses_phrase_boundary_fallback_without_recurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "no_repeat.mid"
            midi = pretty_midi.PrettyMIDI(initial_tempo=120.0)
            melody = pretty_midi.Instrument(program=40, name="Lead")
            add_sequence(melody, [60, 62, 65, 69, 71, 74], 0.0)
            midi.instruments.append(melody)
            midi.write(str(path))

            report = detector.detect_first_motif(path)

            self.assertEqual(
                report["motif"]["method"],
                "phrase_boundary_fallback_no_recurrence",
            )
            self.assertLess(report["motif"]["confidence"], 0.6)
            self.assertEqual(report["matches"], [])

    def test_cli_writes_json_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "repeat.mid"
            output = root / "analysis.json"
            write_repeating_midi(path)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                status = detector.main([str(path), "--output", str(output)])

            self.assertEqual(status, 0)
            self.assertIn("status=PASS", stdout.getvalue())
            self.assertIn("motif_end_s=2.000000", stdout.getvalue())
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["motif"]["note_count"], 4)


if __name__ == "__main__":
    unittest.main()
