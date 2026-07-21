from __future__ import annotations

import json
import unittest

from stage1_story_agent.agent import Stage1StoryAgent
from stage1_story_agent.backends import BackendResponse, FakeBackend
from stage1_story_agent.config import Stage1Config
from stage1_story_agent.errors import ModelContentError
from stage1_story_agent.tests.fixtures import (
    draft_data,
    make_request,
    make_test_mode_request,
    test_mode_draft_data,
)


def valid_response() -> str:
    return json.dumps(draft_data(), ensure_ascii=False)


class AgentTests(unittest.TestCase):
    def test_successful_plan_enriches_all_families(self):
        backend = FakeBackend([valid_response()])
        run = Stage1StoryAgent(backend).plan(make_request())
        plan = run.content_plan
        self.assertEqual(plan.form_plan.form_string, "A-B-A'-C")
        self.assertEqual(len(plan.theme_families), 3)
        self.assertEqual(len(plan.variation_tasks), 1)
        self.assertEqual(plan.musecoco_requests, [])
        self.assertEqual({family.musecoco_attribute_targets.K1 for family in plan.theme_families}, {"minor"})
        self.assertEqual(plan.provenance.content_attempts, 1)
        prompt_payload = json.loads(backend.requests[0].messages[1].content)
        knowledge = prompt_payload["musecoco_planning_knowledge"]
        self.assertEqual(knowledge["authority"]["normal_mode"], "soft preferences; story intent may justify another supported value")
        self.assertIn("high-arousal inner turmoil", knowledge["emotion_quadrants"]["Q2"])
        self.assertIn("calm", knowledge["emotion_quadrants"]["Q4"])
        self.assertNotIn("test_mode_exact_melodic_profile", knowledge)
        handoff = plan.stage2_handoff.sections
        self.assertEqual([item.target_section_bars for item in handoff], [16, 16, 16, 16])
        self.assertEqual([item.extension_bars for item in handoff], [8, 8, 8, 8])
        self.assertEqual([item.tempo_bpm for item in handoff], [88, 104, 100, 92])
        self.assertEqual([item.midigpt_access for item in handoff], ["extension_only", "extension_only", "modifiable", "extension_only"])
        self.assertEqual([item.drum_pattern.pattern for item in handoff], ["single_pulse_per_bar", "pulse_each_beat", "pulse_each_beat", "single_pulse_per_bar"])
        self.assertEqual([item.tension_level for item in handoff], ["low", "high", "high", "low"])
        self.assertNotIn("heart", run.stage2_delivery.model_dump_json().lower())
        self.assertEqual(run.heartbeat_delivery.render_stage, "after_midigpt")
        self.assertTrue(run.musecoco_delivery.tempo_normalization_required)
        self.assertTrue(run.musecoco_delivery.key_normalization_required)
        self.assertEqual(
            run.musecoco_delivery.key_normalization_policy,
            "transpose_tonic_reject_mode_mismatch",
        )

    def test_invalid_json_is_repaired_with_complete_second_response(self):
        backend = FakeBackend(["{not json", valid_response()])
        run = Stage1StoryAgent(backend).plan(make_request())
        self.assertEqual(run.content_plan.provenance.content_attempts, 2)
        self.assertEqual(len(backend.requests), 2)
        repair_payload = json.loads(backend.requests[1].messages[1].content)
        self.assertEqual(repair_payload["issues"][0]["code"], "MODEL_RESPONSE_NOT_JSON")
        self.assertIn("previous_response", repair_payload)

    def test_derived_field_in_draft_triggers_repair(self):
        bad = draft_data()
        bad["form_string"] = "A-B-A'-C"
        backend = FakeBackend([json.dumps(bad), valid_response()])
        run = Stage1StoryAgent(backend).plan(make_request())
        self.assertEqual(run.content_plan.provenance.content_attempts, 2)

    def test_legacy_q4_is_ignored_and_python_derives_q2(self):
        bad = draft_data()
        bad["theme_families"][1]["intent"] = (
            "An intense violin theme representing the peak of sadness and inner turmoil."
        )
        bad["theme_families"][1]["musecoco_choices"]["EM1"] = "Q4"
        backend = FakeBackend([json.dumps(bad)])
        run = Stage1StoryAgent(backend).plan(make_request())
        self.assertEqual(run.content_plan.provenance.content_attempts, 1)
        self.assertEqual(len(backend.requests), 1)
        raw = json.loads(run.raw_response.content)
        self.assertEqual(raw["theme_families"][1]["musecoco_choices"]["EM1"], "Q4")
        self.assertEqual(
            run.content_plan.theme_families[1].musecoco_attribute_targets.EM1,
            "Q2",
        )
        musecoco_text = run.content_plan.theme_families[1].musecoco_text
        self.assertIn("conveys tension, unease, and anxiety", musecoco_text)
        self.assertNotIn("calmness, relaxation, and serenity", musecoco_text)

    def test_truncation_is_repaired(self):
        truncated = BackendResponse(content="{}", model="fake", provider="fake", request_id="one", finish_reason="length")
        backend = FakeBackend([truncated, valid_response()])
        run = Stage1StoryAgent(backend).plan(make_request())
        self.assertEqual(run.content_plan.provenance.content_attempts, 2)

    def test_content_attempts_exhaust(self):
        backend = FakeBackend(["", "no", "{}"])
        with self.assertRaises(ModelContentError) as caught:
            Stage1StoryAgent(backend, Stage1Config(max_content_attempts=3)).plan(make_request())
        self.assertEqual(caught.exception.code, "MODEL_CONTENT_ATTEMPTS_EXHAUSTED")
        self.assertEqual(caught.exception.content_attempts, 3)
        self.assertEqual(len(backend.requests), 3)

    def test_test_mode_builds_fixed_aba_plan(self):
        backend = FakeBackend([json.dumps(test_mode_draft_data(), ensure_ascii=False)])
        run = Stage1StoryAgent(backend).plan(make_test_mode_request())
        plan = run.content_plan
        self.assertTrue(plan.test_mode)
        self.assertEqual(plan.form_plan.form_string, "A-B-A")
        self.assertEqual([item.bar_count for item in plan.form_plan.sections], [16, 16, 16])
        self.assertEqual(plan.global_.global_tonality.tonic, "C")
        self.assertEqual(plan.global_.global_tonality.mode, "minor")
        self.assertEqual(plan.global_.tempo_bpm, 96)
        self.assertEqual(plan.global_.time_signature, "4/4")
        self.assertEqual(len(plan.theme_families), 2)
        self.assertEqual(plan.variation_tasks, [])
        self.assertEqual({family.musecoco_attribute_targets.R3 for family in plan.theme_families}, {"medium"})
        self.assertEqual([family.musecoco_attribute_targets.I1s2 for family in plan.theme_families], [["piano"], ["violin"]])
        self.assertEqual([family.musecoco_attribute_targets.S2s1 for family in plan.theme_families], ["chopin", "schubert"])
        self.assertEqual({tuple(family.musecoco_attribute_targets.S4) for family in plan.theme_families}, {("classical",)})
        self.assertEqual({family.musecoco_attribute_targets.P4 for family in plan.theme_families}, {2})
        self.assertEqual(
            [family.musecoco_attribute_targets.EM1 for family in plan.theme_families],
            ["Q4", "Q2"],
        )
        self.assertIn("use of piano", plan.theme_families[0].musecoco_text)
        self.assertIn("use of violin", plan.theme_families[1].musecoco_text)
        self.assertEqual(
            {family.musecoco_text_style for family in plan.theme_families},
            {"official-template-aligned-v1"},
        )
        handoff = plan.stage2_handoff.sections
        self.assertEqual([item.input_motif_bars for item in handoff], [8, 8, 8])
        self.assertEqual([item.target_section_bars for item in handoff], [16, 16, 16])
        self.assertEqual([item.extension_bars for item in handoff], [8, 8, 8])
        self.assertEqual([item.tempo_bpm for item in handoff], [96, 96, 96])
        self.assertEqual(
            [item.drum_pattern.pattern for item in handoff],
            ["single_pulse_per_bar", "pulse_each_beat", "single_pulse_per_bar"],
        )
        heartbeat = run.heartbeat_delivery.sections
        self.assertEqual(
            [item.trigger_mode for item in heartbeat],
            ["once_per_bar", "every_beat", "once_per_bar"],
        )
        self.assertEqual(
            [item.heart_sounds for item in heartbeat],
            [["S1", "S2"], ["S1"], ["S1", "S2"]],
        )
        self.assertEqual(heartbeat[0].trigger_beats, [1.0])
        self.assertEqual(heartbeat[1].trigger_beats, [1.0, 2.0, 3.0, 4.0])
        self.assertEqual([item.midigpt_access for item in handoff], ["extension_only", "extension_only", "extension_only"])
        self.assertEqual([(item.bar_start, item.bar_end) for item in handoff[0].protected_bar_ranges], [(1, 8)])
        self.assertEqual([(item.bar_start, item.bar_end) for item in handoff[0].editable_bar_ranges], [(9, 16)])
        self.assertEqual([(item.bar_start, item.bar_end) for item in handoff[2].protected_bar_ranges], [(33, 40)])
        self.assertEqual([(item.bar_start, item.bar_end) for item in handoff[2].editable_bar_ranges], [(41, 48)])
        prompt_payload = json.loads(backend.requests[0].messages[1].content)
        self.assertEqual(prompt_payload["test_mode_rules"]["form"], "A-B-A")
        knowledge = prompt_payload["musecoco_planning_knowledge"]
        self.assertEqual(knowledge["version"], "musecoco-prompting-v4")
        self.assertEqual(knowledge["project_policy"]["blocked_artists"], ["stravinsky"])
        self.assertEqual(knowledge["project_policy"]["blocked_instruments"], ["synthesizer"])
        self.assertEqual(
            knowledge["emotion_derivation"]["authority"],
            "Python-only; the LLM must not choose EM1",
        )
        self.assertIn("test_mode_exact_melodic_profile", knowledge)
        self.assertTrue(knowledge["melodic_soft_preferences"])

    def test_test_mode_overrides_conflicting_musecoco_choices(self):
        data = test_mode_draft_data()
        for family in data["theme_families"]:
            family["seed_bars"] = 7
            family["musecoco_choices"].update(
                {
                    "I1s2": ["drum", "brass"],
                    "R1": "danceable",
                    "R3": "high",
                    "S2s1": "stravinsky",
                    "S4": ["electronic"],
                    "P4": 6,
                }
            )
        run = Stage1StoryAgent(
            FakeBackend([json.dumps(data, ensure_ascii=False)])
        ).plan(make_test_mode_request())
        families = run.content_plan.theme_families
        self.assertEqual(run.content_plan.provenance.content_attempts, 1)
        self.assertEqual([family.seed_bars for family in families], [8, 8])
        self.assertEqual([family.musecoco_attribute_targets.I1s2 for family in families], [["piano"], ["violin"]])
        self.assertEqual([family.musecoco_attribute_targets.S2s1 for family in families], ["chopin", "schubert"])
        self.assertEqual({tuple(family.musecoco_attribute_targets.S4) for family in families}, {("classical",)})
        self.assertEqual({family.musecoco_attribute_targets.P4 for family in families}, {2})


if __name__ == "__main__":
    unittest.main()
