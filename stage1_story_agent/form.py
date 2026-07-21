"""Deterministic compilation of LLM form drafts into executable structure."""

from __future__ import annotations

from collections.abc import Sequence

from .errors import FormValidationError
from .models import (
    CompiledFormSection,
    FormCompilation,
    FormPlan,
    FormRelation,
    LLMFormSection,
    MaterialSource,
    StoryPlanConstraints,
    VariationTask,
)


RELATION_TO_SOURCE = {
    FormRelation.INTRODUCE: MaterialSource.MUSECOCO_SEED,
    FormRelation.REPRISE: MaterialSource.REUSE_THEME,
    FormRelation.VARIATION: MaterialSource.MIDIGPT_VARIATION,
    FormRelation.DEVELOPMENT: MaterialSource.MIDIGPT_DEVELOPMENT,
}


def make_form_label(base_symbol: str, variant_index: int) -> str:
    """Return the canonical ASCII form label, such as A, A', or A''."""

    if len(base_symbol) != 1 or base_symbol < "A" or base_symbol > "H":
        raise FormValidationError(
            "FORM_SYMBOL_INVALID",
            "base_symbol",
            "base_symbol must be one of A through H",
        )
    if variant_index < 0 or variant_index > 3:
        raise FormValidationError(
            "FORM_VARIANT_INDEX_INVALID",
            "variant_index",
            "variant_index must be between 0 and 3",
        )
    return base_symbol + "'" * variant_index


def _fail(code: str, path: str, message: str) -> None:
    raise FormValidationError(code, path, message)


def _validate_section_ids(sections: Sequence[LLMFormSection]) -> None:
    for index, section in enumerate(sections, start=1):
        expected = f"S{index}"
        if section.section_id != expected:
            _fail(
                "FORM_SECTION_ID_SEQUENCE_INVALID",
                f"form_sections[{index - 1}].section_id",
                f"expected {expected}, got {section.section_id}",
            )


def _validate_relation_shape(section: LLMFormSection, index: int) -> None:
    path = f"form_sections[{index}]"
    if section.relation is FormRelation.INTRODUCE:
        if section.variant_index != 0:
            _fail(
                "FORM_VARIANT_INDEX_INVALID",
                f"{path}.variant_index",
                "introduce sections must use variant_index 0",
            )
        if section.source_section_id is not None:
            _fail(
                "FORM_SOURCE_FORBIDDEN",
                f"{path}.source_section_id",
                "introduce sections must not have a source",
            )
        return

    if section.source_section_id is None:
        _fail(
            "FORM_SOURCE_REQUIRED",
            f"{path}.source_section_id",
            f"{section.relation.value} sections require a source",
        )

    if section.relation is FormRelation.REPRISE and section.variant_index != 0:
        _fail(
            "FORM_VARIANT_INDEX_INVALID",
            f"{path}.variant_index",
            "reprise sections must use variant_index 0",
        )
    if (
        section.relation in {FormRelation.VARIATION, FormRelation.DEVELOPMENT}
        and section.variant_index == 0
    ):
        _fail(
            "FORM_VARIANT_INDEX_INVALID",
            f"{path}.variant_index",
            f"{section.relation.value} sections require a positive variant_index",
        )


def compile_form(
    sections: Sequence[LLMFormSection],
    constraints: StoryPlanConstraints,
) -> FormCompilation:
    """Validate and compile form sections into deterministic final fields."""

    if not sections:
        _fail("FORM_EMPTY", "form_sections", "at least one form section is required")

    if (
        constraints.target_form_sections is not None
        and len(sections) != constraints.target_form_sections
    ):
        _fail(
            "FORM_SECTION_COUNT_MISMATCH",
            "form_sections",
            (
                f"expected {constraints.target_form_sections} sections, "
                f"got {len(sections)}"
            ),
        )

    if len(sections) > 16:
        _fail(
            "FORM_SECTION_COUNT_INVALID",
            "form_sections",
            "at most 16 form sections are supported",
        )

    _validate_section_ids(sections)

    bar_total = sum(section.bar_count for section in sections)
    if bar_total != constraints.total_bars:
        _fail(
            "FORM_BAR_TOTAL_MISMATCH",
            "form_sections",
            f"section bars total {bar_total}, expected {constraints.total_bars}",
        )

    section_by_id = {section.section_id: section for section in sections}
    section_index_by_id = {
        section.section_id: index for index, section in enumerate(sections)
    }
    introduced_symbols: list[str] = []
    introduced_at: dict[str, str] = {}
    used_positive_variants: dict[str, set[int]] = {}
    compiled_sections: list[CompiledFormSection] = []
    variation_tasks: list[VariationTask] = []
    next_bar = 1

    for index, section in enumerate(sections):
        _validate_relation_shape(section, index)
        path = f"form_sections[{index}]"
        is_new_family = section.base_symbol not in introduced_at

        if is_new_family:
            expected_symbol = chr(ord("A") + len(introduced_symbols))
            if section.base_symbol != expected_symbol:
                _fail(
                    "FORM_SYMBOL_ORDER_INVALID",
                    f"{path}.base_symbol",
                    f"expected next new family {expected_symbol}, got {section.base_symbol}",
                )
            if section.relation is not FormRelation.INTRODUCE:
                _fail(
                    "THEME_FAMILY_MISSING",
                    f"{path}.relation",
                    "the first section in a theme family must introduce it",
                )
            introduced_symbols.append(section.base_symbol)
            introduced_at[section.base_symbol] = section.section_id
            used_positive_variants[section.base_symbol] = set()
            if len(introduced_symbols) > constraints.max_theme_families:
                _fail(
                    "THEME_FAMILY_LIMIT_EXCEEDED",
                    f"{path}.base_symbol",
                    (
                        f"theme family count exceeds "
                        f"{constraints.max_theme_families}"
                    ),
                )
        elif section.relation is FormRelation.INTRODUCE:
            _fail(
                "THEME_FAMILY_DUPLICATE",
                f"{path}.relation",
                f"theme family {section.base_symbol} was already introduced",
            )

        if section.relation is not FormRelation.INTRODUCE:
            source_id = section.source_section_id
            assert source_id is not None
            if source_id not in section_by_id:
                _fail(
                    "FORM_SOURCE_NOT_FOUND",
                    f"{path}.source_section_id",
                    f"source section {source_id} does not exist",
                )
            if section_index_by_id[source_id] >= index:
                _fail(
                    "FORM_SOURCE_NOT_EARLIER",
                    f"{path}.source_section_id",
                    "source section must occur earlier in playback order",
                )
            source = section_by_id[source_id]
            if source.base_symbol != section.base_symbol:
                _fail(
                    "FORM_SOURCE_FAMILY_MISMATCH",
                    f"{path}.source_section_id",
                    (
                        f"source belongs to {source.base_symbol}, "
                        f"target belongs to {section.base_symbol}"
                    ),
                )

        if section.variant_index > 0:
            used = used_positive_variants[section.base_symbol]
            if section.variant_index in used:
                _fail(
                    "FORM_VARIANT_INDEX_DUPLICATE",
                    f"{path}.variant_index",
                    (
                        f"variant {section.variant_index} already exists for "
                        f"family {section.base_symbol}"
                    ),
                )
            used.add(section.variant_index)
            if len(used) > constraints.max_variants_per_family:
                _fail(
                    "FORM_VARIANT_LIMIT_EXCEEDED",
                    f"{path}.variant_index",
                    (
                        f"family {section.base_symbol} exceeds the maximum of "
                        f"{constraints.max_variants_per_family} variants"
                    ),
                )

        form_label = make_form_label(section.base_symbol, section.variant_index)
        bar_start = next_bar
        bar_end = bar_start + section.bar_count - 1
        material_source = RELATION_TO_SOURCE[section.relation]
        compiled_sections.append(
            CompiledFormSection(
                section_id=section.section_id,
                form_label=form_label,
                theme_family_id=f"theme-{section.base_symbol}",
                relation=section.relation,
                source_section_id=section.source_section_id,
                material_source=material_source,
                bar_start=bar_start,
                bar_end=bar_end,
                bar_count=section.bar_count,
                narrative_segment_ids=section.narrative_segment_ids,
                musical_intent=section.musical_intent,
            )
        )
        next_bar = bar_end + 1

        if section.relation in {FormRelation.VARIATION, FormRelation.DEVELOPMENT}:
            assert section.source_section_id is not None
            variation_tasks.append(
                VariationTask(
                    task_id=f"variation-{section.section_id}",
                    target_section_id=section.section_id,
                    source_section_id=section.source_section_id,
                    theme_family_id=f"theme-{section.base_symbol}",
                    strategy=material_source.value,
                    target_bars=section.bar_count,
                    intent=section.musical_intent,
                )
            )

    labels = [section.form_label for section in compiled_sections]
    return FormCompilation(
        form_plan=FormPlan(
            form_string="-".join(labels),
            sections=compiled_sections,
        ),
        theme_family_ids=[f"theme-{symbol}" for symbol in introduced_symbols],
        variation_tasks=variation_tasks,
    )
