from __future__ import annotations

import unittest

from pydantic import ValidationError

from stage1_story_agent.errors import FormValidationError
from stage1_story_agent.form import compile_form, make_form_label
from stage1_story_agent.models import LLMFormSection, StoryPlanConstraints


def section(
    number: int,
    symbol: str,
    *,
    relation: str = "introduce",
    variant: int = 0,
    source: str | None = None,
    bars: int = 16,
) -> LLMFormSection:
    return LLMFormSection(
        section_id=f"S{number}",
        base_symbol=symbol,
        variant_index=variant,
        relation=relation,
        source_section_id=source,
        bar_count=bars,
        tempo_bpm=96,
        narrative_segment_ids=[f"N{number}"],
        musical_intent=f"Intent for section S{number}",
    )


def constraints(total_bars: int, section_count: int, **overrides) -> StoryPlanConstraints:
    values = {
        "total_bars": total_bars,
        "target_form_sections": section_count,
        "max_theme_families": min(section_count, 4),
        "max_variants_per_family": 2,
    }
    values.update(overrides)
    return StoryPlanConstraints(**values)


class FormCompilerTests(unittest.TestCase):
    def test_canonical_form_labels_use_ascii_apostrophes(self):
        self.assertEqual(make_form_label("A", 0), "A")
        self.assertEqual(make_form_label("A", 1), "A'")
        self.assertEqual(make_form_label("A", 2), "A''")
        self.assertEqual(make_form_label("A", 3), "A'''")

    def test_compiles_a_b_a_prime_c(self):
        result = compile_form(
            [
                section(1, "A"),
                section(2, "B"),
                section(3, "A", relation="variation", variant=1, source="S1"),
                section(4, "C"),
            ],
            constraints(64, 4),
        )

        self.assertEqual(result.form_plan.form_string, "A-B-A'-C")
        self.assertEqual(
            result.theme_family_ids,
            ["theme-A", "theme-B", "theme-C"],
        )
        self.assertEqual(
            [
                (item.bar_start, item.bar_end)
                for item in result.form_plan.sections
            ],
            [(1, 16), (17, 32), (33, 48), (49, 64)],
        )
        self.assertEqual(
            [item.material_source.value for item in result.form_plan.sections],
            [
                "musecoco_seed",
                "musecoco_seed",
                "midigpt_variation",
                "musecoco_seed",
            ],
        )
        self.assertEqual(len(result.variation_tasks), 1)
        task = result.variation_tasks[0]
        self.assertEqual(task.target_section_id, "S3")
        self.assertEqual(task.source_section_id, "S1")
        self.assertEqual(task.theme_family_id, "theme-A")
        self.assertEqual(task.target_bars, 16)

    def test_reprise_reuses_theme_without_variation_task(self):
        result = compile_form(
            [
                section(1, "A"),
                section(2, "B"),
                section(3, "A", relation="reprise", source="S1"),
            ],
            constraints(48, 3),
        )

        self.assertEqual(result.form_plan.form_string, "A-B-A")
        self.assertEqual(
            result.form_plan.sections[2].material_source.value,
            "reuse_theme",
        )
        self.assertEqual(result.variation_tasks, [])

    def test_development_creates_development_task(self):
        result = compile_form(
            [
                section(1, "A"),
                section(
                    2,
                    "A",
                    relation="development",
                    variant=1,
                    source="S1",
                ),
            ],
            constraints(32, 2, max_theme_families=1),
        )

        self.assertEqual(
            result.variation_tasks[0].strategy,
            "midigpt_development",
        )

    def test_rejects_total_bar_mismatch(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [section(1, "A", bars=15), section(2, "B")],
                constraints(32, 2),
            )
        self.assertEqual(caught.exception.code, "FORM_BAR_TOTAL_MISMATCH")

    def test_rejects_nonsequential_section_ids(self):
        bad = section(2, "A")
        with self.assertRaises(FormValidationError) as caught:
            compile_form([bad], constraints(16, 1))
        self.assertEqual(
            caught.exception.code,
            "FORM_SECTION_ID_SEQUENCE_INVALID",
        )

    def test_rejects_skipped_new_family_symbol(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [section(1, "A"), section(2, "C")],
                constraints(32, 2),
            )
        self.assertEqual(caught.exception.code, "FORM_SYMBOL_ORDER_INVALID")

    def test_rejects_first_family_occurrence_as_variation(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [
                    section(
                        1,
                        "A",
                        relation="variation",
                        variant=1,
                        source="S2",
                    ),
                    section(2, "A"),
                ],
                constraints(32, 2, max_theme_families=1),
            )
        self.assertEqual(caught.exception.code, "THEME_FAMILY_MISSING")

    def test_rejects_source_that_occurs_in_the_future(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [
                    section(1, "A"),
                    section(2, "A", relation="variation", variant=1, source="S3"),
                    section(3, "A", relation="reprise", source="S1"),
                ],
                constraints(48, 3, max_theme_families=1),
            )
        self.assertEqual(caught.exception.code, "FORM_SOURCE_NOT_EARLIER")

    def test_single_section_can_keep_default_family_limit(self):
        result = compile_form(
            [section(1, "A")],
            StoryPlanConstraints(
                total_bars=16,
                target_form_sections=1,
            ),
        )
        self.assertEqual(result.form_plan.form_string, "A")
        self.assertEqual(result.theme_family_ids, ["theme-A"])

    def test_llm_may_choose_one_a_section_when_target_count_is_unspecified(self):
        result = compile_form(
            [section(1, "A")],
            StoryPlanConstraints(total_bars=16),
        )
        self.assertEqual(result.form_plan.form_string, "A")
        self.assertEqual(len(result.form_plan.sections), 1)
        self.assertEqual(
            (result.form_plan.sections[0].bar_start, result.form_plan.sections[0].bar_end),
            (1, 16),
        )
        self.assertEqual(result.variation_tasks, [])

    def test_rejects_cross_family_source(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [
                    section(1, "A"),
                    section(2, "B"),
                    section(3, "B", relation="variation", variant=1, source="S1"),
                ],
                constraints(48, 3),
            )
        self.assertEqual(caught.exception.code, "FORM_SOURCE_FAMILY_MISMATCH")

    def test_rejects_duplicate_introduction(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [section(1, "A"), section(2, "A")],
                constraints(32, 2, max_theme_families=1),
            )
        self.assertEqual(caught.exception.code, "THEME_FAMILY_DUPLICATE")

    def test_rejects_variant_limit(self):
        with self.assertRaises(FormValidationError) as caught:
            compile_form(
                [
                    section(1, "A"),
                    section(2, "A", relation="variation", variant=1, source="S1"),
                    section(3, "A", relation="variation", variant=2, source="S1"),
                ],
                constraints(
                    48,
                    3,
                    max_theme_families=1,
                    max_variants_per_family=1,
                ),
            )
        self.assertEqual(caught.exception.code, "FORM_VARIANT_LIMIT_EXCEEDED")

    def test_rejects_duplicate_narrative_references_in_model(self):
        with self.assertRaises(ValidationError):
            LLMFormSection(
                section_id="S1",
                base_symbol="A",
                variant_index=0,
                relation="introduce",
                source_section_id=None,
                bar_count=8,
                tempo_bpm=96,
                narrative_segment_ids=["N1", "N1"],
                musical_intent="Intent",
            )

    def test_rejects_unknown_model_fields(self):
        with self.assertRaises(ValidationError):
            LLMFormSection.model_validate(
                {
                    "section_id": "S1",
                    "base_symbol": "A",
                    "variant_index": 0,
                    "relation": "introduce",
                    "source_section_id": None,
                    "bar_count": 8,
                    "tempo_bpm": 96,
                    "narrative_segment_ids": ["N1"],
                    "musical_intent": "Intent",
                    "form_label": "A",
                }
            )


if __name__ == "__main__":
    unittest.main()
