"""Versioned MuseCoco planning knowledge injected into every LLM request."""

from __future__ import annotations

from typing import Any

from .test_mode import test_mode_rules
from .musecoco_policy import BLOCKED_ARTISTS, BLOCKED_INSTRUMENTS, POLICY_VERSION


MUSECOCO_KNOWLEDGE_VERSION = "musecoco-prompting-v4"

EMOTION_QUADRANTS = {
    "Q1": "happy, excited, and positive",
    "Q2": "tense, uneasy, anxious, or high-arousal inner turmoil",
    "Q3": "sad, depressed, or melancholic",
    "Q4": "calm, relaxed, and serene",
}

_COMMON_RULES = [
    "Plan from MuseCoco's supported discrete attributes and values; do not invent new categories.",
    "Keep every requested attribute explicit and internally consistent with bars, meter, mode, tempo, and duration.",
    "Treat literary adjectives as narrative context, but express controllable intent through supported attributes.",
    "Use a compact attribute-aligned description that can be rendered by the deterministic English template.",
    "Estimate valence from -1 (strongly negative) to +1 (strongly positive) and tension from 0 (low arousal) to 1 (high arousal) for every narrative segment and emotional-arc point.",
    "Do not output EM1 in musecoco_choices; Python derives it deterministically from the introduction segments' valence and tension.",
    "Never select a project-blocked instrument or artist; blocked values are rejected rather than repaired silently.",
    "Treat musecoco_generation_bars as the requested MuseCoco source length and musecoco_output_bars as the shorter delivered motif length; do not conflate them.",
]

_MELODIC_SOFT_PREFERENCES = [
    "When the story does not require a dense ensemble, prefer one clear melodic instrument such as piano, violin, or flute.",
    "Prefer a focused pitch range of two or three octaves rather than a very wide range.",
    "Prefer moderate tempo and low or medium rhythmic intensity when melodic clarity is more important than rhythmic drive.",
    "Prefer a supported melody-oriented classical artist/style value when it remains compatible with the story.",
    "Do not add drum or large ensemble instruments merely to make the prompt sound richer; use them only when narratively justified.",
]


def musecoco_prompting_knowledge(*, test_mode: bool, output_bars: int = 8) -> dict[str, Any]:
    """Return JSON-ready knowledge; test mode adds an exact non-negotiable target."""

    knowledge: dict[str, Any] = {
        "version": MUSECOCO_KNOWLEDGE_VERSION,
        "common_rules": list(_COMMON_RULES),
        "melodic_soft_preferences": list(_MELODIC_SOFT_PREFERENCES),
        "emotion_quadrants": dict(EMOTION_QUADRANTS),
        "project_policy": {
            "version": POLICY_VERSION,
            "blocked_instruments": sorted(BLOCKED_INSTRUMENTS),
            "blocked_artists": sorted(BLOCKED_ARTISTS),
            "enforcement": "hard validation at LLM draft, final targets, and encoder",
        },
        "emotion_derivation": {
            "authority": "Python-only; the LLM must not choose EM1",
            "high_arousal_threshold": 0.55,
            "positive_valence_threshold": 0.15,
            "negative_valence_threshold": -0.15,
            "mapping": {
                "high_arousal_positive": "Q1",
                "high_arousal_nonpositive_or_neutral": "Q2",
                "low_arousal_negative": "Q3",
                "low_arousal_nonnegative_or_neutral": "Q4",
            },
        },
        "authority": {
            "normal_mode": "soft preferences; story intent may justify another supported value",
            "test_mode": "exact target; Python will enforce it after the response",
        },
    }
    if test_mode:
        knowledge["test_mode_exact_melodic_profile"] = test_mode_rules(output_bars)[
            "melodic_profile"
        ]
    return knowledge
