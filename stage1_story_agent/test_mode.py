"""Single source of truth for the deterministic Stage 1 test profile."""

from __future__ import annotations

import copy
from typing import Any

TEST_MODE_TOTAL_BARS = 32
TEST_MODE_SECTION_BARS = (8, 16, 8)
TEST_MODE_FORM_LABELS = ("A", "B", "A")
TEST_MODE_TONIC = "C"
TEST_MODE_MODE = "minor"
TEST_MODE_TEMPO_BPM = 96.0
TEST_MODE_TIME_SIGNATURE = "4/4"
TEST_MODE_DANCEABILITY = "not_danceable"
TEST_MODE_RHYTHMIC_INTENSITY = "medium"
TEST_MODE_INPUT_MOTIF_BARS = 8
TEST_MODE_SECTION_TENSIONS = {"S1": 0.2, "S2": 0.8, "S3": 0.2}
TEST_MODE_DRUM_PATTERNS = (
    "single_pulse_per_bar",
    "pulse_each_beat",
    "single_pulse_per_bar",
)
TEST_MODE_PITCH_RANGE_OCTAVES = 2
TEST_MODE_GENRES = ("classical",)
TEST_MODE_MELODIC_FAMILY_PROFILE = {
    "A": {"instruments": ("piano",), "artist": "chopin"},
    "B": {"instruments": ("violin",), "artist": "schubert"},
}


def apply_test_mode_melodic_profile(
    payload: Any,
    seed_bars: int = TEST_MODE_INPUT_MOTIF_BARS,
) -> Any:
    """Override MuseCoco choices before validation so the profile is hard, not advisory."""

    if not isinstance(payload, dict):
        return payload
    normalized = copy.deepcopy(payload)
    families = normalized.get("theme_families")
    if not isinstance(families, list):
        return normalized
    for family in families:
        if not isinstance(family, dict):
            continue
        profile = TEST_MODE_MELODIC_FAMILY_PROFILE.get(family.get("base_symbol"))
        if profile is None:
            continue
        choices = family.get("musecoco_choices")
        if not isinstance(choices, dict):
            choices = {}
            family["musecoco_choices"] = choices
        family["seed_bars"] = seed_bars
        choices.update(
            {
                "I1s2": list(profile["instruments"]),
                "R1": TEST_MODE_DANCEABILITY,
                "R3": TEST_MODE_RHYTHMIC_INTENSITY,
                "S2s1": profile["artist"],
                "S4": list(TEST_MODE_GENRES),
                "P4": TEST_MODE_PITCH_RANGE_OCTAVES,
            }
        )
    return normalized

TEST_MODE_RULES = {
    "form": "A-B-A",
    "sections": [
        {"section_id": "S1", "base_symbol": "A", "relation": "introduce", "variant_index": 0, "source_section_id": None, "bar_count": 8, "tempo_bpm": 96},
        {"section_id": "S2", "base_symbol": "B", "relation": "introduce", "variant_index": 0, "source_section_id": None, "bar_count": 16, "tempo_bpm": 96},
        {"section_id": "S3", "base_symbol": "A", "relation": "reprise", "variant_index": 0, "source_section_id": "S1", "bar_count": 8, "tempo_bpm": 96},
    ],
    "global": {"tonic": "C", "mode": "minor", "tempo_bpm": 96, "time_signature": "4/4"},
    "shared_rhythm": {"R1": "not_danceable", "R3": "medium"},
    "melodic_profile": {
        "hard_override": True,
        "seed_bars": TEST_MODE_INPUT_MOTIF_BARS,
        "pitch_range_octaves": 2,
        "genres": ["classical"],
        "families": {
            "A": {"instruments": ["piano"], "artist": "chopin"},
            "B": {"instruments": ["violin"], "artist": "schubert"},
        },
    },
    "stage2": {"input_motif_bars": 8, "drum_patterns": list(TEST_MODE_DRUM_PATTERNS), "section_tensions": TEST_MODE_SECTION_TENSIONS, "section_tempos_bpm": [96, 96, 96], "midigpt_access": ["fixed", "extension_only", "fixed"]},
    "heartbeat": {"low_tension": {"trigger_mode": "once_per_bar", "heart_sounds": ["S1", "S2"]}, "high_tension_below_110_bpm": {"trigger_mode": "every_beat", "heart_sounds": ["S1"]}},
    "theme_families": ["A", "B"],
}


def test_mode_rules(
    output_bars: int = TEST_MODE_INPUT_MOTIF_BARS,
    generation_bars: int = 12,
) -> dict[str, Any]:
    """Return the fixed test profile with request-specific MuseCoco lengths."""

    rules = copy.deepcopy(TEST_MODE_RULES)
    rules["melodic_profile"]["seed_bars"] = output_bars
    rules["stage2"]["input_motif_bars"] = output_bars
    rules["stage2"]["midigpt_access"] = [
        "fixed"
        if index == 2 or bars == output_bars
        else "extension_only"
        for index, bars in enumerate(TEST_MODE_SECTION_BARS)
    ]
    rules["musecoco_lengths"] = {
        "generation_target_bars": generation_bars,
        "output_motif_bars": output_bars,
        "short_input_policy": "error",
        "long_input_policy": "trim_and_close_active_notes",
    }
    return rules
