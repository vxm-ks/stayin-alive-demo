from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack

from stage2.combine_midi_with_drums import build_heartbeat_track_from_plan, combine_midi_with_drums
from stage2.run_pipeline_relative import load_stage2_plan, resolve_stage1_inputs
from stage2.plan_assembler import assemble_from_plan, build_generation_regions


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
        self.assertEqual(len(settings.sections), 3)
        self.assertEqual(settings.form_string, "A-B-A")
        self.assertEqual(settings.total_bars, 48)

    def test_plan_rejects_legacy_four_plus_twelve_layout(self) -> None:
        data = make_plan()
        data["sections"][0]["input_motif_bars"] = 4
        with self.assertRaisesRegex(ValueError, "inconsistent"):
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
                "theme_family_id": f"theme-{symbol}",
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
        self.assertEqual(resolved.theme_midis["theme-A"].name, "final.mid")
        self.assertEqual(resolved.theme_midis["theme-B"].parent.name, "theme-B")

    def test_arbitrary_form_compiles_dynamic_generation_ranges(self) -> None:
        data = make_plan()
        data["form_string"] = "A-B-A'-C"
        data["total_bars"] = 64
        sections = []
        labels = ("A", "B", "A'", "C")
        for index, label in enumerate(labels, 1):
            start = 1 + (index - 1) * 16
            relation = "variation" if label == "A'" else "introduce"
            access = "modifiable" if relation == "variation" else "extension_only"
            sections.append({
                "section_id": f"S{index}", "form_label": label,
                "theme_family_id": f"theme-{label[0]}", "relation": relation,
                "source_section_id": "S1" if relation == "variation" else None,
                "material_source": "midigpt_variation" if relation == "variation" else "musecoco_seed",
                "bar_start": start, "bar_end": start + 15,
                "input_motif_bars": 8, "target_section_bars": 16, "extension_bars": 8,
                "extension_method": "midigpt_transform" if relation == "variation" else "midigpt_extend",
                "tempo_bpm": 80 + index * 4, "source_tension": 0.3,
                "tension_level": "low",
                "drum_pattern": {"pattern": "single_pulse_per_bar", "time_signature": "4/4", "kick_beats": [1.0], "snare_beats": [], "closed_hihat_beats": []},
                "midigpt_access": access,
                "protected_bar_ranges": [] if access == "modifiable" else [{"bar_start": start, "bar_end": start + 7}],
                "editable_bar_ranges": [{"bar_start": start, "bar_end": start + 15}] if access == "modifiable" else [{"bar_start": start + 8, "bar_end": start + 15}],
            })
        data["sections"] = sections
        path = self.write_plan(data)
        settings = load_stage2_plan(path, self.root)
        self.assertEqual(settings.form_string, "A-B-A'-C")
        self.assertEqual(len(settings.sections), 4)
        resolved = self.root / "resolved.json"
        data["sections"] = list(settings.sections)
        resolved.write_text(json.dumps(data), encoding="utf-8")
        jobs = build_generation_regions(resolved, 64)
        self.assertEqual([(job.gap_start, job.gap_end) for job in jobs], [(8, 15), (24, 31), (32, 47), (56, 63)])

        def write_theme(path: Path, pitch: int, ppq: int) -> None:
            midi = MidiFile(type=1, ticks_per_beat=ppq)
            track = MidiTrack()
            track.append(Message("note_on", channel=0, note=pitch, velocity=80, time=0))
            track.append(Message("note_off", channel=0, note=pitch, velocity=0, time=ppq))
            track.append(Message("note_on", channel=9, note=42, velocity=70, time=0))
            track.append(Message("note_off", channel=9, note=42, velocity=0, time=ppq // 4))
            track.append(MetaMessage("end_of_track", time=8 * 4 * ppq - ppq - ppq // 4))
            midi.tracks.append(track)
            midi.save(path)

        themes = {}
        for symbol, pitch, ppq in (("A", 60, 480), ("B", 65, 240), ("C", 67, 960)):
            theme = self.root / f"{symbol}.mid"
            write_theme(theme, pitch, ppq)
            themes[f"theme-{symbol}"] = theme
        assembled = self.root / "arbitrary.mid"
        audit = assemble_from_plan(theme_midis=themes, plan_path=resolved, output_path=assembled)
        self.assertEqual(audit["form_string"], "A-B-A'-C")
        self.assertEqual(audit["section_count"], 4)
        self.assertEqual(audit["removed_channel_10_messages"], 6)
        rendered = MidiFile(assembled)
        self.assertEqual(rendered.ticks_per_beat, 480)
        music_onsets = []
        for track in rendered.tracks:
            tick = 0
            for message in track:
                tick += message.time
                if message.type == "note_on" and message.velocity and getattr(message, "channel", None) != 9:
                    music_onsets.append((tick, message.note))
        self.assertIn((0, 60), music_onsets)
        self.assertIn((16 * 1920, 65), music_onsets)
        self.assertNotIn((32 * 1920, 60), music_onsets)
        self.assertIn((48 * 1920, 67), music_onsets)


if __name__ == "__main__":
    unittest.main()
