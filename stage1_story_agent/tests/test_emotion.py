from __future__ import annotations

import unittest

from stage1_story_agent.emotion import (
    EMOTION_DERIVATION_VERSION,
    derive_emotion_quadrant,
    derive_theme_emotions,
)
from stage1_story_agent.models import NarrativeSegment
from stage1_story_agent.tests.fixtures import make_draft


def segment(segment_id: str, valence: float, tension: float) -> NarrativeSegment:
    return NarrativeSegment(
        segment_id=segment_id,
        summary="fixture",
        narrative_role="other",
        emotion="fixture",
        valence=valence,
        tension=tension,
    )


class EmotionDerivationTests(unittest.TestCase):
    def test_four_quadrants_follow_fixed_thresholds(self):
        cases = [
            (0.8, 0.8, "Q1"),
            (-0.8, 0.8, "Q2"),
            (-0.8, 0.2, "Q3"),
            (0.8, 0.2, "Q4"),
            (0.0, 0.8, "Q2"),
            (0.0, 0.2, "Q4"),
        ]
        for index, (valence, tension, expected) in enumerate(cases, start=1):
            with self.subTest(valence=valence, tension=tension):
                derived = derive_emotion_quadrant(
                    [segment(f"N{index}", valence, tension)]
                )
                self.assertEqual(derived.quadrant, expected)

    def test_theme_emotion_uses_introduction_segments(self):
        derived = derive_theme_emotions(make_draft())
        self.assertEqual(
            {symbol: item.quadrant for symbol, item in derived.items()},
            {"A": "Q4", "B": "Q2", "C": "Q4"},
        )
        self.assertEqual(derived["B"].source_segment_ids, ("N2",))
        self.assertEqual(EMOTION_DERIVATION_VERSION, "valence-arousal-v1")

    def test_high_tension_segment_dominates_arousal_split(self):
        derived = derive_emotion_quadrant(
            [segment("N1", 0.4, 0.2), segment("N2", -0.8, 0.9)]
        )
        self.assertEqual(derived.quadrant, "Q2")
        self.assertEqual(derived.peak_arousal, 0.9)


if __name__ == "__main__":
    unittest.main()
