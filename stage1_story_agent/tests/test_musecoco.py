from __future__ import annotations

import unittest

from stage1_story_agent.musecoco import (
    MUSECOCO_ATTRIBUTE_KEYS,
    bar_bucket,
    derive_targets,
    duration_bucket,
    duration_seconds,
    render_musecoco_text,
    tempo_class,
)
from stage1_story_agent.emotion import derive_theme_emotions
from stage1_story_agent.tests.fixtures import make_draft


class MuseCocoTests(unittest.TestCase):
    def test_bar_bucket_boundaries(self):
        self.assertEqual([bar_bucket(value) for value in (1, 4, 5, 8, 9, 12, 13, 16)], ["1-4", "1-4", "5-8", "5-8", "9-12", "9-12", "13-16", "13-16"])

    def test_tempo_boundaries(self):
        self.assertEqual(tempo_class(76), "slow")
        self.assertEqual(tempo_class(76.1), "moderate")
        self.assertEqual(tempo_class(119.9), "moderate")
        self.assertEqual(tempo_class(120), "fast")

    def test_duration_formula_and_boundaries(self):
        self.assertEqual(duration_seconds(4, "4/4", 60), 16)
        self.assertEqual([duration_bucket(value) for value in (15, 15.1, 30, 30.1, 45, 45.1, 60, 60.1)], ["0-15", "15-30", "15-30", "30-45", "30-45", "45-60", "45-60", "60+"])

    def test_rendering_is_deterministic_and_covers_all_attributes(self):
        draft = make_draft()
        family = draft.theme_families[0]
        emotion = derive_theme_emotions(draft)[family.base_symbol].quadrant
        targets = derive_targets(
            family, draft.global_proposal, emotion, generation_bars=12
        )
        first = render_musecoco_text(family, draft.global_proposal, targets)
        second = render_musecoco_text(family, draft.global_proposal, targets)
        self.assertEqual(first, second)
        self.assertEqual(set(first.fragments_by_attribute), MUSECOCO_ATTRIBUTE_KEYS)
        self.assertEqual(set(targets.model_dump()), MUSECOCO_ATTRIBUTE_KEYS)
        self.assertEqual(targets.K1, "minor")
        self.assertEqual(targets.TS1s1, "4/4")
        self.assertNotIn("NA", first.text)
        self.assertIn("representative example of the classical style", first.text)
        self.assertIn("composed in the minor key", first.text)
        self.assertIn("follows a 4/4 meter", first.text)
        self.assertIn("in the vein of Chopin", first.text)
        self.assertIn("use of piano and cello", first.text)
        self.assertIn("spans approximately 9 ~ 12 bars", first.text)
        self.assertIn("duration of 16 ~ 30 seconds", first.text)
        self.assertNotIn("Create a", first.text)
        self.assertNotIn("96 BPM", first.text)
        self.assertNotIn("C minor", first.text)


if __name__ == "__main__":
    unittest.main()
