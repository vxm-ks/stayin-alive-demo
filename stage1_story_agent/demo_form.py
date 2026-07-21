"""Offline demonstration of the first Stage 1 implementation slice."""

from __future__ import annotations

import json

from .form import compile_form
from .models import LLMFormSection, StoryPlanConstraints


def build_demo() -> dict[str, object]:
    constraints = StoryPlanConstraints(
        total_bars=40,
        target_form_sections=4,
        max_theme_families=4,
        max_variants_per_family=2,
    )
    sections = [
        LLMFormSection(
            section_id="S1",
            base_symbol="A",
            variant_index=0,
            relation="introduce",
            source_section_id=None,
            bar_count=8,
            tempo_bpm=88,
            narrative_segment_ids=["N1"],
            musical_intent="Establish the restrained main theme.",
        ),
        LLMFormSection(
            section_id="S2",
            base_symbol="B",
            variant_index=0,
            relation="introduce",
            source_section_id=None,
            bar_count=8,
            tempo_bpm=104,
            narrative_segment_ids=["N2"],
            musical_intent="Introduce a contrasting, more tense theme.",
        ),
        LLMFormSection(
            section_id="S3",
            base_symbol="A",
            variant_index=1,
            relation="variation",
            source_section_id="S1",
            bar_count=8,
            tempo_bpm=100,
            narrative_segment_ids=["N3"],
            musical_intent="Retain A's identity while increasing density and tension.",
        ),
        LLMFormSection(
            section_id="S4",
            base_symbol="C",
            variant_index=0,
            relation="introduce",
            source_section_id=None,
            bar_count=16,
            tempo_bpm=92,
            narrative_segment_ids=["N4"],
            musical_intent="Introduce the final decision as a new theme.",
        ),
    ]
    return compile_form(sections, constraints).model_dump(mode="json")


def main() -> None:
    print(json.dumps(build_demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
