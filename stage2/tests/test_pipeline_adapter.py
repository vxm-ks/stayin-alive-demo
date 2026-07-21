from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

from stage2.combine_midi_with_drums import build_heartbeat_track_from_plan, combine_midi_with_drums
from stage2.run_pipeline_relative import load_stage2_plan, resolve_stage1_inputs


def make_plan(bpm: float = 96.0) -> dict:
    gap = 8
    sections = []
    start = 1
    for index in range(3):
        end = start + 8 + gap - 1
        sections.append({
            "section_id": f"S{index + 1}", "bar_start": start, "bar_end": end,
            "input_motif_bars": 8, "target_section_bars": 8 + gap,
            "extension_bars": gap, "extension_method": "midigpt_extend",
            "tempo_bpm": bpm, "source_tension": 0.8 if index == 1 else 0.3,
            "tension_level": "high" if index == 1 else "low",
            "drum_pattern": {"pattern": "single_pulse_per_bar", "time_signature": "4/4", "kick_beats": [1.0], "snare_beats": [], "closed_hihat_beats": []},
            "midigpt_access": "extension_only",
            "protected_bar_ranges": [{"bar_start": start, "bar_end": start + 7}],
            "editable_bar_ranges": [{"bar_start": start + 8, "bar_end": end}],
        })
        start = end + 1
    return {"schema_version": "0.2-draft", "story_id": "story-1", "total_bars": start - 1, "form_string": "A-B-A", "time_signature": "4/4", "bar_numbering": "one_based_closed", "drum_track_scope": "instructions_only", "sections": sections}


class PipelineAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_plan(self, data: dict) -> Path:
        path = self.root / "stage2_plan.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_plan_maps_to_fixed_test_8_plus_8_layout(self) -> None:
        settings = load_stage2_plan(self.write_plan(make_plan()), self.root)
        self.assertEqual(settings.gap_bars, 8)
        self.assertEqual(settings.total_bars, 48)

    def test_plan_rejects_legacy_four_plus_twelve_layout(self) -> None:
        data = make_plan()
        data["sections"][0]["input_motif_bars"] = 4
        with self.assertRaisesRegex(ValueError, "8-bar motif"):
            load_stage2_plan(self.write_plan(data), self.root)

    def test_plan_with_unprotected_motif_is_rejected(self) -> None:
        data = make_plan()
        data["sections"][0]["protected_bar_ranges"] = []
        with self.assertRaisesRegex(ValueError, "protect"):
            load_stage2_plan(self.write_plan(data), self.root)

    def test_low_tension_has_s1_s2_and_high_tension_has_s1(self) -> None:
        path = self.write_plan(make_plan())
        track = build_heartbeat_track_from_plan(path, 48, 4, 4, 480)
        midi = MidiFile(type=1, ticks_per_beat=480)
        midi.tracks.append(track)
        output = self.root / "heartbeat.mid"
        midi.save(output)
        notes = [message.note for message in MidiFile(output).tracks[0] if message.type == "note_on" and message.velocity]
        self.assertIn(36, notes)
        self.assertIn(38, notes)
        self.assertEqual(notes[:2], [36, 38])

    def test_existing_channel_ten_is_removed_before_plan_refill(self) -> None:
        def source(path: Path) -> None:
            midi = MidiFile(type=1, ticks_per_beat=480)
            track = MidiTrack()
            track.append(Message("note_on", channel=9, note=42, velocity=100, time=0))
            track.append(Message("note_off", channel=9, note=42, velocity=0, time=120))
            track.append(Message("note_on", channel=0, note=60, velocity=100, time=0))
            track.append(Message("note_off", channel=0, note=60, velocity=0, time=120))
            track.append(MetaMessage("end_of_track", time=0))
            midi.tracks.append(track)
            midi.save(path)

        a, b = self.root / "A.mid", self.root / "B.mid"
        source(a)
        source(b)
        plan = self.write_plan(make_plan())
        output = self.root / "combined.mid"
        combine_midi_with_drums(a, b, 96.0, (4, 4), output, plan)
        channel_ten_notes = [
            message.note
            for track in MidiFile(output).tracks
            for message in track
            if getattr(message, "channel", None) == 9 and hasattr(message, "note")
        ]
        self.assertNotIn(42, channel_ten_notes)
        self.assertEqual(set(channel_ten_notes), {36, 38})

    def test_stage1_output_base_resolves_hash_checked_final_themes(self) -> None:
        base = self.root / "run"
        stage2_dir = self.root / "run-stage2"
        themes = self.root / "run-musecoco" / "generated_themes"
        stage2_dir.mkdir()
        (themes / "theme-A").mkdir(parents=True)
        (themes / "theme-B").mkdir(parents=True)
        plan = stage2_dir / "stage2_plan.json"
        plan.write_text(json.dumps(make_plan() | {"story_id": "story-1"}), encoding="utf-8")
        items = []
        import hashlib
        for symbol in ("A", "B"):
            final = themes / f"theme-{symbol}" / "final.mid"
            final.write_bytes(b"MThd" + symbol.encode())
            items.append({
                "base_symbol": symbol,
                "final_midi": f"theme-{symbol}/final.mid",
                "final_sha256": hashlib.sha256(final.read_bytes()).hexdigest(),
            })
        (themes / "theme_manifest.json").write_text(json.dumps({
            "schema_version": "musecoco-final-themes-v1",
            "story_id": "story-1", "output_motif_bars": 8, "items": items,
        }), encoding="utf-8")
        resolved = resolve_stage1_inputs(base, self.root)
        self.assertEqual(resolved.stage2_plan, plan)
        self.assertEqual(resolved.a_midi.name, "final.mid")
        self.assertEqual(resolved.b_midi.parent.name, "theme-B")


if __name__ == "__main__":
    unittest.main()
