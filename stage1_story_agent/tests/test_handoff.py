from __future__ import annotations

import unittest

from stage1_story_agent.handoff import compile_stage2_handoff
from stage1_story_agent.tests.fixtures import make_draft, make_request
from stage1_story_agent.validators import validate_and_compile_draft


class HandoffPulseTests(unittest.TestCase):
    def _handoff_with_conflict_tempo(self, tempo_bpm: float):
        draft = make_draft()
        draft.form_sections[1].tempo_bpm = tempo_bpm
        compilation = validate_and_compile_draft(make_request(), draft)
        return compile_stage2_handoff(draft, compilation.form_plan)

    def test_low_tension_is_one_pulse_on_first_beat(self):
        handoff = self._handoff_with_conflict_tempo(104)
        section = handoff.sections[0]
        self.assertEqual(section.tension_level, "low")
        self.assertEqual(section.drum_pattern.pattern, "single_pulse_per_bar")
        self.assertEqual(section.drum_pattern.kick_beats, [1.0])
        self.assertEqual(section.drum_pattern.snare_beats, [])
        self.assertEqual(section.drum_pattern.closed_hihat_beats, [])

    def test_high_tension_below_110_pulses_every_beat(self):
        handoff = self._handoff_with_conflict_tempo(109.9)
        section = handoff.sections[1]
        self.assertEqual(section.tension_level, "high")
        self.assertEqual(section.drum_pattern.pattern, "pulse_each_beat")
        self.assertEqual(section.drum_pattern.kick_beats, [1.0, 2.0, 3.0, 4.0])

    def test_high_tension_at_110_is_rate_limited_to_one_per_bar(self):
        handoff = self._handoff_with_conflict_tempo(110)
        section = handoff.sections[1]
        self.assertEqual(section.tension_level, "high")
        self.assertEqual(section.drum_pattern.pattern, "single_pulse_per_bar")
        self.assertEqual(section.drum_pattern.kick_beats, [1.0])


if __name__ == "__main__":
    unittest.main()
