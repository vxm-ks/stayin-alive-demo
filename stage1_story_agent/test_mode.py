"""Single source of truth for the structural Stage 1 test profile."""

from __future__ import annotations

from typing import Any

TEST_MODE_TOTAL_BARS = 48
TEST_MODE_SECTION_BARS = (16, 16, 16)
TEST_MODE_FORM_LABELS = ("A", "B", "A")
TEST_MODE_TONIC = "C"
TEST_MODE_MODE = "minor"
TEST_MODE_TEMPO_BPM = 96.0
TEST_MODE_TIME_SIGNATURE = "4/4"
TEST_MODE_INPUT_MOTIF_BARS = 8

TEST_MODE_RULES = {
    "form": "A-B-A",
    "sections": [
        {"section_id": "S1", "base_symbol": "A", "relation": "introduce", "variant_index": 0, "source_section_id": None, "bar_count": 16, "tempo_bpm": 96},
        {"section_id": "S2", "base_symbol": "B", "relation": "introduce", "variant_index": 0, "source_section_id": None, "bar_count": 16, "tempo_bpm": 96},
        {"section_id": "S3", "base_symbol": "A", "relation": "reprise", "variant_index": 0, "source_section_id": "S1", "bar_count": 16, "tempo_bpm": 96},
    ],
    "global": {"tonic": "C", "mode": "minor", "tempo_bpm": 96, "time_signature": "4/4"},
    "story_controlled": [
        "narrative analysis",
        "theme intent",
        "MuseCoco instruments, artist, genre, pitch range, danceability, and rhythmic intensity",
        "section tension and heartbeat density",
    ],
    "stage2": {
        "input_motif_bars": 8,
        "default_extension_bars": 8,
        "section_tempos_bpm": [96, 96, 96],
        "midigpt_access": ["extension_only", "extension_only", "extension_only"],
    },
    "theme_families": ["A", "B"],
}


def test_mode_rules(
    output_bars: int = TEST_MODE_INPUT_MOTIF_BARS,
    generation_bars: int = 12,
) -> dict[str, Any]:
    """Return the fixed test profile with request-specific MuseCoco lengths."""

    rules = {
        **TEST_MODE_RULES,
        "stage2": dict(TEST_MODE_RULES["stage2"]),
    }
    rules["stage2"]["input_motif_bars"] = output_bars
    rules["stage2"]["midigpt_access"] = ["extension_only"] * len(TEST_MODE_SECTION_BARS)
    rules["musecoco_lengths"] = {
        "generation_target_bars": generation_bars,
        "output_motif_bars": output_bars,
        "short_input_policy": "error",
        "long_input_policy": "trim_and_close_active_notes",
    }
    return rules
