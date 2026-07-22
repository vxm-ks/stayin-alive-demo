"""Deterministic consumer-specific handoff compilation."""

from __future__ import annotations

from .models import (
    BarRange,
    DrumPatternInstruction,
    FormPlan,
    FormRelation,
    HeartbeatProcessingDelivery,
    HeartbeatProcessingSection,
    LLMContentPlanDraft,
    MuseCocoDelivery,
    Stage2Delivery,
    Stage2Handoff,
    Stage2SectionInstruction,
    ThemeFamily,
)
from .test_mode import TEST_MODE_SECTION_TENSIONS


TENSION_THRESHOLD = 0.55
HIGH_TENSION_EVERY_BEAT_BELOW_BPM = 110.0


def _beat_grid(numerator: int, step: float) -> list[float]:
    count = int(numerator / step)
    return [1.0 + index * step for index in range(count)]


def _drum_pattern(
    tension_level: str,
    tempo_bpm: float,
    time_signature: str,
) -> DrumPatternInstruction:
    numerator = int(time_signature.split("/", 1)[0])
    integer_beats = _beat_grid(numerator, 1.0)
    if tension_level == "high" and tempo_bpm < HIGH_TENSION_EVERY_BEAT_BELOW_BPM:
        return DrumPatternInstruction(
            pattern="pulse_each_beat",
            time_signature=time_signature,
            kick_beats=integer_beats,
            snare_beats=[],
            closed_hihat_beats=[],
        )
    return DrumPatternInstruction(
        pattern="single_pulse_per_bar",
        time_signature=time_signature,
        kick_beats=[1.0],
        snare_beats=[],
        closed_hihat_beats=[],
    )


def _section_tension(
    draft: LLMContentPlanDraft,
    section_id: str,
    narrative_segment_ids: list[str],
    *,
    test_mode: bool,
) -> float:
    if test_mode:
        return TEST_MODE_SECTION_TENSIONS[section_id]
    segments = {
        item.segment_id: item for item in draft.story_analysis.narrative_segments
    }
    values = [
        segments[segment_id].tension
        for segment_id in narrative_segment_ids
        if segment_id in segments
    ]
    if not values:
        raise ValueError(f"section {section_id} has no narrative tension source")
    return max(values)


def compile_stage2_handoff(
    draft: LLMContentPlanDraft,
    form_plan: FormPlan,
    *,
    test_mode: bool = False,
) -> Stage2Handoff:
    family_by_symbol = {family.base_symbol: family for family in draft.theme_families}
    instructions: list[Stage2SectionInstruction] = []

    for llm_section, compiled in zip(draft.form_sections, form_plan.sections, strict=True):
        family = family_by_symbol[llm_section.base_symbol]
        input_bars = family.seed_bars
        target_bars = compiled.bar_count
        extension_bars = target_bars - input_bars
        source_tension = _section_tension(
            draft,
            compiled.section_id,
            llm_section.narrative_segment_ids,
            test_mode=test_mode,
        )
        tension_level = "high" if source_tension >= TENSION_THRESHOLD else "low"
        if extension_bars < 0:
            raise ValueError(
                f"section {compiled.section_id} is shorter than its input motif"
            )
        full_range = BarRange(bar_start=compiled.bar_start, bar_end=compiled.bar_end)

        if compiled.relation is FormRelation.REPRISE:
            if extension_bars == 0:
                access = "fixed"
                method = "reuse_source_section"
                protected = [full_range]
                editable: list[BarRange] = []
            else:
                access = "extension_only"
                method = "midigpt_extend"
                protected = [
                    BarRange(
                        bar_start=compiled.bar_start,
                        bar_end=compiled.bar_start + input_bars - 1,
                    )
                ]
                editable = [
                    BarRange(
                        bar_start=compiled.bar_start + input_bars,
                        bar_end=compiled.bar_end,
                    )
                ]
        elif compiled.relation in {FormRelation.VARIATION, FormRelation.DEVELOPMENT}:
            access = "modifiable"
            method = "midigpt_transform"
            protected = []
            editable = [full_range]
        elif extension_bars == 0:
            access = "fixed"
            method = "none"
            protected = [full_range]
            editable = []
        else:
            access = "extension_only"
            method = "midigpt_extend"
            protected = [
                BarRange(
                    bar_start=compiled.bar_start,
                    bar_end=compiled.bar_start + input_bars - 1,
                )
            ]
            editable = [
                BarRange(
                    bar_start=compiled.bar_start + input_bars,
                    bar_end=compiled.bar_end,
                )
            ]

        instructions.append(
            Stage2SectionInstruction(
                section_id=compiled.section_id,
                form_label=compiled.form_label,
                theme_family_id=compiled.theme_family_id,
                relation=compiled.relation,
                source_section_id=compiled.source_section_id,
                material_source=compiled.material_source,
                bar_start=compiled.bar_start,
                bar_end=compiled.bar_end,
                input_motif_bars=input_bars,
                target_section_bars=target_bars,
                extension_bars=extension_bars,
                extension_method=method,
                tempo_bpm=llm_section.tempo_bpm,
                source_tension=source_tension,
                tension_level=tension_level,
                drum_pattern=_drum_pattern(
                    tension_level,
                    llm_section.tempo_bpm,
                    draft.global_proposal.time_signature,
                ),
                midigpt_access=access,
                protected_bar_ranges=protected,
                editable_bar_ranges=editable,
            )
        )
    return Stage2Handoff(sections=instructions)


def make_deliveries(
    *,
    story_id: str,
    draft: LLMContentPlanDraft,
    form_plan: FormPlan,
    families: list[ThemeFamily],
    handoff: Stage2Handoff,
    generation_target_bars: int,
    output_motif_bars: int,
) -> tuple[MuseCocoDelivery, HeartbeatProcessingDelivery, Stage2Delivery]:
    musecoco = MuseCocoDelivery(
        story_id=story_id,
        tempo_bpm=draft.global_proposal.tempo_bpm,
        time_signature=draft.global_proposal.time_signature,
        global_tonality=draft.global_proposal.global_tonality,
        generation_target_bars=generation_target_bars,
        output_motif_bars=output_motif_bars,
        theme_families=families,
    )
    heartbeat = HeartbeatProcessingDelivery(
        story_id=story_id,
        sections=[
            HeartbeatProcessingSection(
                section_id=item.section_id,
                bar_start=item.bar_start,
                bar_end=item.bar_end,
                tempo_bpm=item.tempo_bpm,
                source_tension=item.source_tension,
                tension_level=item.tension_level,
                trigger_mode=(
                    "every_beat"
                    if item.drum_pattern.pattern == "pulse_each_beat"
                    else "once_per_bar"
                ),
                trigger_beats=item.drum_pattern.kick_beats,
                heart_sounds=(
                    ["S1"] if item.tension_level == "high" else ["S1", "S2"]
                ),
                source_drum_pattern=item.drum_pattern,
            )
            for item in handoff.sections
        ],
    )
    stage2 = Stage2Delivery(
        story_id=story_id,
        total_bars=sum(item.target_section_bars for item in handoff.sections),
        form_string=form_plan.form_string,
        time_signature=draft.global_proposal.time_signature,
        sections=handoff.sections,
    )
    return musecoco, heartbeat, stage2
