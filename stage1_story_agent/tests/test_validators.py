from __future__ import annotations

import copy
import unittest

from stage1_story_agent.errors import PlanValidationError
from stage1_story_agent.models import LLMContentPlanDraft
from stage1_story_agent.tests.fixtures import (
    draft_data,
    make_draft,
    make_request,
    make_test_mode_request,
    test_mode_draft_data,
)
from stage1_story_agent.validators import validate_and_compile_draft


class DraftValidatorTests(unittest.TestCase):
    def assert_code(self, data: dict, code: str) -> None:
        with self.assertRaises(PlanValidationError) as caught:
            validate_and_compile_draft(make_request(), LLMContentPlanDraft.model_validate(data))
        self.assertIn(code, [item["code"] for item in caught.exception.issues])

    def test_valid_a_b_a_prime_c_compiles(self):
        result = validate_and_compile_draft(make_request(), make_draft())
        self.assertEqual(result.form_plan.form_string, "A-B-A'-C")
        self.assertEqual(len(result.theme_family_ids), 3)
        self.assertEqual(len(result.variation_tasks), 1)

    def test_rejects_uncovered_narrative_segment(self):
        data = draft_data()
        data["form_sections"][3]["narrative_segment_ids"] = ["N3"]
        self.assert_code(data, "NARRATIVE_SEGMENT_UNCOVERED")

    def test_rejects_invalid_emotional_arc_order(self):
        data = draft_data()
        data["story_analysis"]["emotional_arc"][1]["position"] = 1
        self.assert_code(data, "EMOTIONAL_ARC_ORDER_INVALID")

    def test_rejects_family_set_mismatch(self):
        data = draft_data()
        data["theme_families"] = data["theme_families"][:2]
        self.assert_code(data, "THEME_FAMILY_SET_MISMATCH")

    def test_rejects_seed_longer_than_introduction(self):
        data = copy.deepcopy(draft_data())
        data["form_sections"][0]["bar_count"] = 7
        self.assert_code(data, "THEME_SEED_TOO_LONG")

    def test_rejects_global_choice_outside_request(self):
        data = draft_data()
        data["global_proposal"]["global_tonality"]["mode"] = "major"
        self.assert_code(data, "GLOBAL_MODE_MISMATCH")

    def test_rejects_section_tempo_outside_request(self):
        data = draft_data()
        data["form_sections"][1]["tempo_bpm"] = 140
        self.assert_code(data, "SECTION_TEMPO_OUT_OF_RANGE")

    def test_rejects_target_section_shorter_than_theme_motif(self):
        data = draft_data()
        data["form_sections"][2]["bar_count"] = 3
        data["form_sections"][3]["bar_count"] = 21
        self.assert_code(data, "THEME_SEED_TOO_LONG")

    def test_test_mode_rejects_nonuniform_section_tempo(self):
        data = test_mode_draft_data()
        data["form_sections"][1]["tempo_bpm"] = 100
        with self.assertRaises(PlanValidationError) as caught:
            validate_and_compile_draft(
                make_test_mode_request(),
                LLMContentPlanDraft.model_validate(data),
            )
        self.assertIn("TEST_MODE_SECTION_TEMPO_MISMATCH", [item["code"] for item in caught.exception.issues])

    def test_test_mode_compiles_exact_aba_without_variation_task(self):
        draft = LLMContentPlanDraft.model_validate(test_mode_draft_data())
        result = validate_and_compile_draft(make_test_mode_request(), draft)
        self.assertEqual(result.form_plan.form_string, "A-B-A")
        self.assertEqual([item.bar_count for item in result.form_plan.sections], [16, 16, 16])
        self.assertEqual(result.theme_family_ids, ["theme-A", "theme-B"])
        self.assertEqual(result.variation_tasks, [])

    def test_test_mode_rejects_different_rhythm_profile(self):
        data = test_mode_draft_data()
        data["theme_families"][0]["musecoco_choices"]["R3"] = "high"
        with self.assertRaises(PlanValidationError) as caught:
            validate_and_compile_draft(
                make_test_mode_request(),
                LLMContentPlanDraft.model_validate(data),
            )
        self.assertIn("TEST_MODE_RHYTHM_MISMATCH", [item["code"] for item in caught.exception.issues])

    def test_test_mode_rejects_a_prime_instead_of_reprise_a(self):
        data = test_mode_draft_data()
        data["form_sections"][2]["relation"] = "variation"
        data["form_sections"][2]["variant_index"] = 1
        with self.assertRaises(PlanValidationError) as caught:
            validate_and_compile_draft(
                make_test_mode_request(),
                LLMContentPlanDraft.model_validate(data),
            )
        self.assertIn("TEST_MODE_FORM_MISMATCH", [item["code"] for item in caught.exception.issues])


if __name__ == "__main__":
    unittest.main()
