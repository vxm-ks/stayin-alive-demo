from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from midigpt_scaffold_builder.builder import (
    build_scaffold,
    build_scaffold_directory,
)
from midigpt_scaffold_builder.errors import ScaffoldValidationError
from midigpt_scaffold_builder.midi import MidiNote, MidiTrack, midi_bytes, parse_midi


class ScaffoldBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.motif_path = self.root / "motif-A.mid"
        self.motif_path.write_bytes(
            midi_bytes(
                resolution=480,
                tempo=625_000,
                numerator=4,
                denominator=4,
                tracks=[
                    MidiTrack(
                        name="Theme A",
                        program=0,
                        track_type="melodic",
                        notes=[
                            MidiNote(0, 480, 60, 90),
                            MidiNote(480, 480, 64, 88),
                            MidiNote(1800, 240, 65, 87),
                            MidiNote(1920, 960, 67, 86),
                        ],
                    )
                ],
                end_tick=3840,
            )
        )
        motif_hash = hashlib.sha256(self.motif_path.read_bytes()).hexdigest()
        self.content = {
            "schema_version": "0.2-draft",
            "story_id": "story-001",
            "global": {
                "total_bars": 8,
                "tempo_bpm": 96.0,
                "time_signature": "4/4",
                "global_tonality": {"tonic": "C", "mode": "minor"},
            },
            "story_analysis": {},
            "form_plan": {
                "form_string": "A-A'",
                "sections": [
                    {
                        "section_id": "S1",
                        "form_label": "A",
                        "theme_family_id": "theme-A",
                        "relation": "introduce",
                        "source_section_id": None,
                        "material_source": "musecoco_seed",
                        "bar_start": 1,
                        "bar_end": 4,
                        "bar_count": 4,
                        "narrative_segment_ids": ["N1"],
                        "musical_intent": "state the theme",
                    },
                    {
                        "section_id": "S2",
                        "form_label": "A'",
                        "theme_family_id": "theme-A",
                        "relation": "variation",
                        "source_section_id": "S1",
                        "material_source": "midigpt_variation",
                        "bar_start": 5,
                        "bar_end": 8,
                        "bar_count": 4,
                        "narrative_segment_ids": ["N2"],
                        "musical_intent": "increase tension",
                    },
                ],
            },
            "theme_families": [],
            "variation_tasks": [
                {
                    "task_id": "variation-S2",
                    "target_section_id": "S2",
                    "source_section_id": "S1",
                    "theme_family_id": "theme-A",
                    "strategy": "midigpt_variation",
                    "target_bars": 4,
                    "intent": "increase tension",
                }
            ],
            "musecoco_requests": [],
        }
        self.music = {
            "schema_version": "0.1-draft",
            "story_id": "story-001",
            "ppq": 480,
            "tracks": [
                {
                    "track_id": "heartbeat",
                    "role": "heartbeat",
                    "instrument": 0,
                    "track_type": "drum",
                    "behavior": "context",
                },
                {
                    "track_id": "theme",
                    "role": "melody",
                    "instrument": 0,
                    "track_type": "melodic",
                    "behavior": "generate",
                },
            ],
            "motif_placements": [
                {
                    "placement_id": "place-A-S1",
                    "motif_id": "motif-A",
                    "target_track_id": "theme",
                    "target_section_id": "S1",
                    "target_bar": 1,
                    "repeat_count": 2,
                    "transpose_semitones": 0,
                    "velocity_scale": 1.0,
                    "protected": True,
                }
            ],
            "heartbeat_arrangement": {
                "role": "protected_rhythm_anchor",
                "preserve_complete_events": True,
                "sections": [
                    {
                        "section_id": "S1",
                        "bar_start": 1,
                        "bar_end": 4,
                        "intent": "one complete heartbeat per bar",
                        "pattern": {
                            "mode": "hits_per_bar",
                            "hit_unit": "s1_s2_pair",
                            "hits_per_bar": 1,
                            "beat_positions": [1.0],
                            "velocity_scale": 0.65,
                        },
                    },
                    {
                        "section_id": "S2",
                        "bar_start": 5,
                        "bar_end": 8,
                        "intent": "alternating events every half beat",
                        "pattern": {
                            "mode": "event_sequence",
                            "event_sequence": ["S1", "S2"],
                            "event_spacing_beats": 0.5,
                            "start_beat": 1.0,
                            "fill_to_bar_end": True,
                            "velocity_scale": 1.0,
                        },
                    },
                ],
                "unplanned_bars_policy": "error",
            },
            "midigpt": {
                "checkpoint": "yellow",
                "model_dim_bars": 4,
                "fill_regions": [
                    {
                        "track_id": "theme",
                        "bar_start": 5,
                        "bar_end": 8,
                        "mode": "infill",
                    }
                ],
                "generation_config": {
                    "temperature": 1.0,
                    "seed": 20260717,
                    "top_p": 0.95,
                    "mask_mode": "attention",
                },
            },
        }
        self.heartbeat = {
            "midi": {
                "target_bpm": 96.0,
                "ppq": 480,
                "outputs": {
                    "4/4": {
                        "file": "heartbeat.mid",
                        "sha256": "0" * 64,
                        "events": [
                            {
                                "event_type": "S1",
                                "time_s": 0.0,
                                "tick": 0,
                                "duration_ticks": 120,
                                "midi_note": 36,
                                "velocity": 100,
                                "source_event_time_s": 0.5,
                            },
                            {
                                "event_type": "S2",
                                "time_s": 0.3125,
                                "tick": 240,
                                "duration_ticks": 96,
                                "midi_note": 38,
                                "velocity": 85,
                                "source_event_time_s": 0.75,
                            },
                        ],
                    }
                },
            }
        }
        self.motifs = {
            "schema_version": "0.1-draft",
            "story_id": "story-001",
            "motifs": [
                {
                    "motif_id": "motif-A",
                    "theme_family_id": "theme-A",
                    "midi_file": self.motif_path.name,
                    "sha256": motif_hash,
                    "bars": 2,
                    "ppq": 480,
                    "time_signature": "4/4",
                    "source_track_index": 0,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(self):
        return build_scaffold(
            self.content,
            self.music,
            self.heartbeat,
            self.motifs,
            motif_base_dir=self.root,
        )

    def test_builds_equal_length_score_and_zero_based_request(self):
        result = self.build()
        self.assertEqual(len(result.score["tracks"]), 2)
        self.assertEqual([len(track["bars"]) for track in result.score["tracks"]], [8, 8])
        self.assertEqual(result.generation_request["tracks"][0]["bars"], [])
        self.assertFalse(result.generation_request["tracks"][0]["ignore"])
        self.assertEqual(result.generation_request["tracks"][1]["bars"], [4, 5, 6, 7])
        self.assertEqual(result.generation_request["config"]["mask_mode"], "attention")
        self.assertEqual(result.generation_request["config"]["model_dim"], 4)
        self.assertEqual(result.generate_payload["score"], result.score)
        self.assertEqual(
            result.generate_payload["request"], result.generation_request
        )
        self.assertTrue(result.validation_report["valid"])
        self.assertEqual(len(result.heartbeat_render_manifest["events"]), 40)

        # The source note begins near the end of bar 1 and crosses the barline.
        # Its complete duration must survive in the MIDI-GPT Score.
        crossing = [
            note
            for note in result.score["tracks"][1]["bars"][0]["notes"]
            if note["pitch"] == 65
        ]
        self.assertEqual(crossing[0]["duration_ticks"], 240)

    def test_midi_outputs_are_parseable_and_keep_both_tracks(self):
        result = self.build()
        scaffold = self.root / "scaffold.mid"
        scaffold.write_bytes(result.scaffold_midi)
        parsed = parse_midi(scaffold)
        self.assertEqual(parsed.resolution, 480)
        self.assertEqual((parsed.numerator, parsed.denominator), (4, 4))
        self.assertEqual(len(parsed.tracks), 2)
        self.assertTrue(any(track.track_type == "drum" for track in parsed.tracks))

    def test_rejects_heartbeat_section_coordinate_drift(self):
        self.music["heartbeat_arrangement"]["sections"][1]["bar_end"] = 7
        with self.assertRaises(ScaffoldValidationError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "HEARTBEAT_SECTION_COORDINATES_MISMATCH")

    def test_rejects_protected_motif_and_fill_overlap(self):
        self.music["midigpt"]["fill_regions"][0]["bar_start"] = 4
        with self.assertRaises(ScaffoldValidationError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "PROTECTED_FILL_OVERLAP")

    def test_rejects_unknown_music_plan_fields(self):
        self.music["midigpt"]["generation_config"]["top_probability"] = 0.8
        with self.assertRaises(ScaffoldValidationError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_directory_build_publishes_all_audit_artifacts(self):
        inputs = {}
        for name, value in {
            "content": self.content,
            "music": self.music,
            "heartbeat": self.heartbeat,
            "motifs": self.motifs,
        }.items():
            path = self.root / f"{name}.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            inputs[name] = path
        output = self.root / "output"
        result = build_scaffold_directory(
            inputs["content"],
            inputs["music"],
            inputs["heartbeat"],
            inputs["motifs"],
            output,
        )
        self.assertEqual(result.output_directory, output.resolve())
        self.assertEqual(
            {path.name for path in output.iterdir()},
            {
                "heartbeat_arranged.mid",
                "scaffold.mid",
                "score.json",
                "generation_request.json",
                "generate_payload.json",
                "heartbeat_render_manifest.json",
                "assembly_manifest.json",
                "validation_report.json",
            },
        )
        manifest = json.loads((output / "assembly_manifest.json").read_text(encoding="utf-8"))
        self.assertIn("score.json", manifest["outputs"])
        self.assertIn("content_plan", manifest["inputs"])

    def test_autoregressive_region_must_be_right_suffix(self):
        self.music["midigpt"]["fill_regions"][0].update(
            {"bar_start": 5, "bar_end": 7, "mode": "autoregressive"}
        )
        with self.assertRaises(ScaffoldValidationError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "AUTOREGRESSIVE_SUFFIX_INVALID")

    def test_rejects_missing_stage1_variation_task(self):
        self.content["variation_tasks"] = []
        with self.assertRaises(ScaffoldValidationError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "VARIATION_TASK_COVERAGE_INVALID")


if __name__ == "__main__":
    unittest.main()
