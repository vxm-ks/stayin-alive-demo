from __future__ import annotations

import unittest

from pydantic import ValidationError

from stage1_story_agent.models import ContentPlan, LLMContentPlanDraft, StoryPlanRequest
from stage1_story_agent.tests.fixtures import draft_data, make_draft, make_request, make_test_mode_request


class ModelTests(unittest.TestCase):
    def test_complete_draft_and_request_validate(self):
        self.assertEqual(make_request().constraints.total_bars, 40)
        self.assertEqual(len(make_draft().theme_families), 3)

    def test_draft_rejects_program_derived_fields(self):
        data = draft_data()
        data["form_string"] = "A-B-A'-C"
        with self.assertRaises(ValidationError):
            LLMContentPlanDraft.model_validate(data)

    def test_content_plan_uses_global_alias(self):
        fields = ContentPlan.model_json_schema(by_alias=True)["properties"]
        self.assertIn("global", fields)
        self.assertNotIn("global_", fields)

    def test_test_mode_normalizes_all_fixed_request_constraints(self):
        request = make_test_mode_request()
        constraints = request.constraints
        self.assertTrue(request.test_mode)
        self.assertEqual(constraints.total_bars, 32)
        self.assertEqual(constraints.target_form_sections, 3)
        self.assertEqual(constraints.max_theme_families, 2)
        self.assertEqual(constraints.max_variants_per_family, 0)
        self.assertEqual(constraints.allowed_time_signatures, ["4/4"])
        self.assertEqual((constraints.tempo_bpm_min, constraints.tempo_bpm_max), (96, 96))
        self.assertEqual(constraints.allowed_modes, ["minor"])
        self.assertEqual(constraints.allowed_tonics, ["C"])
        self.assertEqual(constraints.musecoco_output_bars, 8)
        self.assertEqual(constraints.musecoco_generation_bars, 12)

    def test_test_mode_preserves_configurable_musecoco_lengths(self):
        payload = make_test_mode_request().model_dump(mode="json", by_alias=True)
        payload["constraints"]["musecoco_output_bars"] = 6
        payload["constraints"]["musecoco_generation_bars"] = 10
        request = StoryPlanRequest.model_validate(payload)
        self.assertEqual(request.constraints.musecoco_output_bars, 6)
        self.assertEqual(request.constraints.musecoco_generation_bars, 10)

        payload["constraints"]["musecoco_output_bars"] = 9
        with self.assertRaisesRegex(ValidationError, "must not exceed 8"):
            StoryPlanRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
