"""Cross-object validation for LLM drafts and enriched content plans."""

from __future__ import annotations

from .errors import FormValidationError, PlanValidationError
from .emotion import derive_emotion_quadrant
from .form import compile_form
from .handoff import HIGH_TENSION_EVERY_BEAT_BELOW_BPM, TENSION_THRESHOLD
from .models import ContentPlan, FormCompilation, FormRelation, LLMContentPlanDraft, StoryPlanRequest
from .musecoco import MUSECOCO_ATTRIBUTE_KEYS, bar_bucket, duration_bucket, duration_seconds, tempo_class
from .test_mode import (
    TEST_MODE_DANCEABILITY,
    TEST_MODE_FORM_LABELS,
    TEST_MODE_GENRES,
    TEST_MODE_MELODIC_FAMILY_PROFILE,
    TEST_MODE_MODE,
    TEST_MODE_RHYTHMIC_INTENSITY,
    TEST_MODE_DRUM_PATTERNS,
    TEST_MODE_SECTION_TENSIONS,
    TEST_MODE_PITCH_RANGE_OCTAVES,
    TEST_MODE_SECTION_BARS,
    TEST_MODE_TEMPO_BPM,
    TEST_MODE_TIME_SIGNATURE,
    TEST_MODE_TONIC,
)


def _issue(code: str, path: str, message: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message}


def validate_and_compile_draft(
    request: StoryPlanRequest,
    draft: LLMContentPlanDraft,
) -> FormCompilation:
    """Validate all user/LLM relationships, then compile deterministic form fields."""

    issues: list[dict[str, str]] = []
    constraints = request.constraints
    proposal = draft.global_proposal

    if not constraints.tempo_bpm_min <= proposal.tempo_bpm <= constraints.tempo_bpm_max:
        issues.append(_issue("GLOBAL_TEMPO_OUT_OF_RANGE", "global_proposal.tempo_bpm", "tempo must be inside the requested range"))
    if proposal.time_signature not in constraints.allowed_time_signatures:
        issues.append(_issue("GLOBAL_TIME_SIGNATURE_NOT_ALLOWED", "global_proposal.time_signature", "time signature is not allowed by the request"))
    tonality = proposal.global_tonality
    if tonality.mode not in constraints.allowed_modes:
        issues.append(_issue("GLOBAL_MODE_MISMATCH", "global_proposal.global_tonality.mode", "mode is not allowed by the request"))
    if constraints.allowed_tonics is not None and tonality.tonic not in constraints.allowed_tonics:
        issues.append(_issue("GLOBAL_TONIC_NOT_ALLOWED", "global_proposal.global_tonality.tonic", "tonic is not allowed by the request"))

    segments = draft.story_analysis.narrative_segments
    for index, segment in enumerate(segments, start=1):
        expected = f"N{index}"
        if segment.segment_id != expected:
            issues.append(_issue("NARRATIVE_SEGMENT_ID_SEQUENCE_INVALID", f"story_analysis.narrative_segments[{index - 1}].segment_id", f"expected {expected}, got {segment.segment_id}"))
    segment_ids = {segment.segment_id for segment in segments}
    referenced_ids: set[str] = set()
    for section_index, section in enumerate(draft.form_sections):
        if not constraints.tempo_bpm_min <= section.tempo_bpm <= constraints.tempo_bpm_max:
            issues.append(_issue("SECTION_TEMPO_OUT_OF_RANGE", f"form_sections[{section_index}].tempo_bpm", "section tempo must be inside the requested range"))
        for ref_index, segment_id in enumerate(section.narrative_segment_ids):
            referenced_ids.add(segment_id)
            if segment_id not in segment_ids:
                issues.append(_issue("NARRATIVE_SEGMENT_NOT_FOUND", f"form_sections[{section_index}].narrative_segment_ids[{ref_index}]", f"narrative segment {segment_id} does not exist"))
    for index, segment in enumerate(segments):
        if segment.segment_id not in referenced_ids:
            issues.append(_issue("NARRATIVE_SEGMENT_UNCOVERED", f"story_analysis.narrative_segments[{index}].segment_id", f"narrative segment {segment.segment_id} is not referenced by any form section"))

    arc = draft.story_analysis.emotional_arc
    if arc[0].position != 0:
        issues.append(_issue("EMOTIONAL_ARC_START_INVALID", "story_analysis.emotional_arc[0].position", "the emotional arc must start at 0"))
    if arc[-1].position != 1:
        issues.append(_issue("EMOTIONAL_ARC_END_INVALID", f"story_analysis.emotional_arc[{len(arc) - 1}].position", "the emotional arc must end at 1"))
    for index in range(1, len(arc)):
        if arc[index].position <= arc[index - 1].position:
            issues.append(_issue("EMOTIONAL_ARC_ORDER_INVALID", f"story_analysis.emotional_arc[{index}].position", "emotional arc positions must be strictly increasing"))

    compilation: FormCompilation | None = None
    try:
        compilation = compile_form(draft.form_sections, constraints)
    except FormValidationError as exc:
        issues.append(exc.as_dict())

    form_symbols = []
    introduce_by_symbol: dict[str, object] = {}
    for section in draft.form_sections:
        if section.base_symbol not in form_symbols:
            form_symbols.append(section.base_symbol)
        if section.relation is FormRelation.INTRODUCE:
            introduce_by_symbol[section.base_symbol] = section

    family_symbols = [family.base_symbol for family in draft.theme_families]
    if len(family_symbols) != len(set(family_symbols)):
        issues.append(_issue("THEME_FAMILY_DUPLICATE", "theme_families", "theme family base symbols must be unique"))
    if family_symbols != form_symbols:
        issues.append(_issue("THEME_FAMILY_SET_MISMATCH", "theme_families", f"expected theme families {form_symbols}, got {family_symbols}"))
    for index, family in enumerate(draft.theme_families):
        introduce = introduce_by_symbol.get(family.base_symbol)
        if introduce is None:
            issues.append(_issue("THEME_FAMILY_MISSING_INTRODUCTION", f"theme_families[{index}].base_symbol", "theme family has no introduce section"))
        elif family.seed_bars > introduce.bar_count:  # type: ignore[attr-defined]
            issues.append(_issue("THEME_SEED_TOO_LONG", f"theme_families[{index}].seed_bars", "seed_bars must not exceed the introduce section bar_count"))
        for section_index, section in enumerate(draft.form_sections):
            if section.base_symbol == family.base_symbol and family.seed_bars > section.bar_count:
                issues.append(_issue("THEME_SEED_TOO_LONG", f"form_sections[{section_index}].bar_count", "every target section must be at least as long as its input theme motif"))
        if family.seed_bars != constraints.musecoco_output_bars:
            issues.append(
                _issue(
                    "MUSECOCO_OUTPUT_BARS_MISMATCH",
                    f"theme_families[{index}].seed_bars",
                    "seed_bars must equal constraints.musecoco_output_bars",
                )
            )

    if request.test_mode:
        actual_shape = tuple(
            (
                section.base_symbol,
                section.relation.value,
                section.variant_index,
                section.source_section_id,
                section.bar_count,
            )
            for section in draft.form_sections
        )
        expected_shape = (
            ("A", "introduce", 0, None, TEST_MODE_SECTION_BARS[0]),
            ("B", "introduce", 0, None, TEST_MODE_SECTION_BARS[1]),
            ("A", "reprise", 0, "S1", TEST_MODE_SECTION_BARS[2]),
        )
        if actual_shape != expected_shape:
            issues.append(_issue("TEST_MODE_FORM_MISMATCH", "form_sections", "test mode requires exact A-B-A sections with 8, 16, and 8 bars"))
        if (
            proposal.tempo_bpm != TEST_MODE_TEMPO_BPM
            or proposal.time_signature != TEST_MODE_TIME_SIGNATURE
            or tonality.tonic != TEST_MODE_TONIC
            or tonality.mode != TEST_MODE_MODE
        ):
            issues.append(_issue("TEST_MODE_GLOBAL_MISMATCH", "global_proposal", "test mode requires C minor, 96 BPM, and 4/4"))
        for index, family in enumerate(draft.theme_families):
            choices = family.musecoco_choices
            if choices.R1 != TEST_MODE_DANCEABILITY or choices.R3 != TEST_MODE_RHYTHMIC_INTENSITY:
                issues.append(_issue("TEST_MODE_RHYTHM_MISMATCH", f"theme_families[{index}].musecoco_choices", "all test-mode families require not_danceable and medium rhythmic intensity"))
            if family.seed_bars != request.constraints.musecoco_output_bars:
                issues.append(_issue("TEST_MODE_MOTIF_LENGTH_MISMATCH", f"theme_families[{index}].seed_bars", "test-mode seed_bars must equal constraints.musecoco_output_bars"))
            melodic = TEST_MODE_MELODIC_FAMILY_PROFILE.get(family.base_symbol)
            if melodic is not None and (
                tuple(choices.I1s2) != melodic["instruments"]
                or choices.S2s1 != melodic["artist"]
                or tuple(choices.S4) != TEST_MODE_GENRES
                or choices.P4 != TEST_MODE_PITCH_RANGE_OCTAVES
            ):
                issues.append(_issue("TEST_MODE_MELODIC_PROFILE_MISMATCH", f"theme_families[{index}].musecoco_choices", "test mode requires the fixed MuseCoco melodic profile"))
        for index, section in enumerate(draft.form_sections):
            if section.tempo_bpm != TEST_MODE_TEMPO_BPM:
                issues.append(_issue("TEST_MODE_SECTION_TEMPO_MISMATCH", f"form_sections[{index}].tempo_bpm", "test mode requires 96 BPM in every section"))

    if issues:
        raise PlanValidationError(issues)
    assert compilation is not None
    return compilation


def validate_content_plan(request: StoryPlanRequest, plan: ContentPlan) -> None:
    """Defend deterministic enrichment invariants before persistence."""

    issues: list[dict[str, str]] = []
    global_plan = plan.global_
    sections = plan.form_plan.sections
    if global_plan.total_bars != request.constraints.total_bars:
        issues.append(_issue("GLOBAL_BAR_TOTAL_MISMATCH", "global.total_bars", "final total_bars differs from the request"))
    expected_start = 1
    for index, section in enumerate(sections):
        if section.bar_start != expected_start or section.bar_end != section.bar_start + section.bar_count - 1:
            issues.append(_issue("FORM_COORDINATES_INVALID", f"form_plan.sections[{index}]", "section coordinates must be continuous and match bar_count"))
        expected_start = section.bar_end + 1
    if expected_start - 1 != global_plan.total_bars:
        issues.append(_issue("FORM_BAR_TOTAL_MISMATCH", "form_plan.sections", "final section coordinates do not close at total_bars"))

    family_ids = {family.theme_family_id for family in plan.theme_families}
    section_family_ids = {section.theme_family_id for section in sections}
    if family_ids != section_family_ids:
        issues.append(_issue("THEME_FAMILY_SET_MISMATCH", "theme_families", "final theme families must match form section families"))
    section_by_id = {section.section_id: section for section in sections}
    narrative_by_id = {
        segment.segment_id: segment
        for segment in plan.story_analysis.narrative_segments
    }
    for index, family in enumerate(plan.theme_families):
        path = f"theme_families[{index}]"
        targets = family.musecoco_attribute_targets
        if set(targets.model_dump()) != MUSECOCO_ATTRIBUTE_KEYS:
            issues.append(_issue("MUSECOCO_ATTRIBUTE_SET_INVALID", f"{path}.musecoco_attribute_targets", "exactly 12 MuseCoco attributes are required"))
        if targets.TS1s1 != global_plan.time_signature:
            issues.append(_issue("GLOBAL_TIME_SIGNATURE_MISMATCH", f"{path}.musecoco_attribute_targets.TS1s1", "TS1s1 must inherit global time signature"))
        if targets.K1 != global_plan.global_tonality.mode:
            issues.append(_issue("GLOBAL_MODE_MISMATCH", f"{path}.musecoco_attribute_targets.K1", "K1 must inherit global mode"))
        if targets.T1s1 != tempo_class(global_plan.tempo_bpm):
            issues.append(_issue("GLOBAL_TEMPO_CLASS_MISMATCH", f"{path}.musecoco_attribute_targets.T1s1", "T1s1 must be derived from global BPM"))
        if targets.B1s1 != bar_bucket(request.constraints.musecoco_generation_bars):
            issues.append(_issue("MUSECOCO_BAR_BUCKET_MISMATCH", f"{path}.musecoco_attribute_targets.B1s1", "B1s1 must be derived from musecoco_generation_bars"))
        seconds = duration_seconds(request.constraints.musecoco_generation_bars, global_plan.time_signature, global_plan.tempo_bpm)
        if targets.TM1 != duration_bucket(seconds):
            issues.append(_issue("MUSECOCO_DURATION_BUCKET_MISMATCH", f"{path}.musecoco_attribute_targets.TM1", "TM1 must be derived from seed duration"))
        introduced = section_by_id.get(family.introduced_in_section_id)
        if introduced is None or introduced.relation is not FormRelation.INTRODUCE or introduced.theme_family_id != family.theme_family_id:
            issues.append(_issue("THEME_INTRODUCTION_MISMATCH", f"{path}.introduced_in_section_id", "introduced_in_section_id must name this family's introduce section"))
        elif family.seed_bars > introduced.bar_count:
            issues.append(_issue("THEME_SEED_TOO_LONG", f"{path}.seed_bars", "seed_bars must not exceed introduce section length"))
        else:
            source_segments = [
                narrative_by_id[segment_id]
                for segment_id in introduced.narrative_segment_ids
                if segment_id in narrative_by_id
            ]
            if source_segments:
                expected_emotion = derive_emotion_quadrant(source_segments).quadrant
                if targets.EM1 != expected_emotion:
                    issues.append(
                        _issue(
                            "MUSECOCO_EMOTION_DERIVATION_MISMATCH",
                            f"{path}.musecoco_attribute_targets.EM1",
                            f"EM1 must be deterministically derived as {expected_emotion} from introduction-segment valence and tension",
                        )
                    )

    tasks_by_target = {task.target_section_id: task for task in plan.variation_tasks}
    for section in sections:
        needs_task = section.relation in {FormRelation.VARIATION, FormRelation.DEVELOPMENT}
        if needs_task != (section.section_id in tasks_by_target):
            issues.append(_issue("VARIATION_TASK_MISMATCH", "variation_tasks", f"task presence does not match section {section.section_id}"))
    if plan.musecoco_requests:
        issues.append(_issue("MUSECOCO_REQUESTS_NOT_EMPTY", "musecoco_requests", "adapter requests must remain empty in Stage 1"))
    handoff = plan.stage2_handoff.sections
    if len(handoff) != len(sections):
        issues.append(_issue("STAGE2_SECTION_COUNT_MISMATCH", "stage2_handoff.sections", "there must be exactly one Stage 2 instruction per form section"))
    else:
        for index, (section, instruction) in enumerate(zip(sections, handoff, strict=True)):
            path = f"stage2_handoff.sections[{index}]"
            if (
                instruction.section_id != section.section_id
                or instruction.bar_start != section.bar_start
                or instruction.bar_end != section.bar_end
                or instruction.target_section_bars != section.bar_count
                or instruction.extension_bars != section.bar_count - instruction.input_motif_bars
            ):
                issues.append(_issue("STAGE2_SECTION_COORDINATE_MISMATCH", path, "Stage 2 length and coordinates must match the compiled form"))
            if request.test_mode:
                expected_tension = TEST_MODE_SECTION_TENSIONS[section.section_id]
            else:
                expected_tension = max(
                    (
                        narrative_by_id[segment_id].tension
                        for segment_id in section.narrative_segment_ids
                        if segment_id in narrative_by_id
                    ),
                    default=-1.0,
                )
            expected_level = "high" if expected_tension >= TENSION_THRESHOLD else "low"
            expected_pattern = (
                "pulse_each_beat"
                if expected_level == "high"
                and instruction.tempo_bpm < HIGH_TENSION_EVERY_BEAT_BELOW_BPM
                else "single_pulse_per_bar"
            )
            numerator = int(instruction.drum_pattern.time_signature.split("/", 1)[0])
            expected_kicks = (
                [float(beat) for beat in range(1, numerator + 1)]
                if expected_pattern == "pulse_each_beat"
                else [1.0]
            )
            if (
                instruction.source_tension != expected_tension
                or instruction.tension_level != expected_level
                or instruction.drum_pattern.pattern != expected_pattern
                or instruction.drum_pattern.kick_beats != expected_kicks
                or instruction.drum_pattern.snare_beats
                or instruction.drum_pattern.closed_hihat_beats
            ):
                issues.append(
                    _issue(
                        "DRUM_TENSION_PATTERN_MISMATCH",
                        f"{path}.drum_pattern",
                        "drum pulses must follow section tension and the below-110-BPM density rule",
                    )
                )
            protected = {
                bar
                for item in instruction.protected_bar_ranges
                for bar in range(item.bar_start, item.bar_end + 1)
            }
            editable = {
                bar
                for item in instruction.editable_bar_ranges
                for bar in range(item.bar_start, item.bar_end + 1)
            }
            section_bars = set(range(section.bar_start, section.bar_end + 1))
            if protected & editable or protected | editable != section_bars:
                issues.append(_issue("STAGE2_BAR_ACCESS_INVALID", path, "protected and editable ranges must be disjoint and cover the section"))
            beats = (
                instruction.drum_pattern.kick_beats
                + instruction.drum_pattern.snare_beats
                + instruction.drum_pattern.closed_hihat_beats
            )
            if any(beat < 1 or beat >= numerator + 1 for beat in beats):
                issues.append(_issue("DRUM_BEAT_OUT_OF_RANGE", f"{path}.drum_pattern", "drum beat positions must lie inside one bar"))
    if request.test_mode:
        if tuple(section.form_label for section in sections) != TEST_MODE_FORM_LABELS:
            issues.append(_issue("TEST_MODE_FORM_MISMATCH", "form_plan.sections", "final test-mode form must be A-B-A"))
        if tuple(section.bar_count for section in sections) != TEST_MODE_SECTION_BARS:
            issues.append(_issue("TEST_MODE_FORM_MISMATCH", "form_plan.sections", "final test-mode bars must be 8, 16, and 8"))
        if (
            global_plan.tempo_bpm != TEST_MODE_TEMPO_BPM
            or global_plan.time_signature != TEST_MODE_TIME_SIGNATURE
            or global_plan.global_tonality.tonic != TEST_MODE_TONIC
            or global_plan.global_tonality.mode != TEST_MODE_MODE
        ):
            issues.append(_issue("TEST_MODE_GLOBAL_MISMATCH", "global", "final test-mode global settings must be C minor, 96 BPM, and 4/4"))
        for index, family in enumerate(plan.theme_families):
            targets = family.musecoco_attribute_targets
            if targets.R1 != TEST_MODE_DANCEABILITY or targets.R3 != TEST_MODE_RHYTHMIC_INTENSITY:
                issues.append(_issue("TEST_MODE_RHYTHM_MISMATCH", f"theme_families[{index}].musecoco_attribute_targets", "final test-mode rhythm controls must be shared"))
            if family.seed_bars != request.constraints.musecoco_output_bars:
                issues.append(_issue("TEST_MODE_MOTIF_LENGTH_MISMATCH", f"theme_families[{index}].seed_bars", "final test-mode seed_bars must equal constraints.musecoco_output_bars"))
            melodic = TEST_MODE_MELODIC_FAMILY_PROFILE.get(family.base_symbol)
            if melodic is not None and (
                tuple(targets.I1s2) != melodic["instruments"]
                or targets.S2s1 != melodic["artist"]
                or tuple(targets.S4) != TEST_MODE_GENRES
                or targets.P4 != TEST_MODE_PITCH_RANGE_OCTAVES
            ):
                issues.append(_issue("TEST_MODE_MELODIC_PROFILE_MISMATCH", f"theme_families[{index}].musecoco_attribute_targets", "final test-mode MuseCoco attributes must match the melodic profile"))
        expected_access = tuple(
            "fixed"
            if index == 2 or section.bar_count == request.constraints.musecoco_output_bars
            else "extension_only"
            for index, section in enumerate(sections)
        )
        for index, (instruction, expected, expected_pattern) in enumerate(zip(plan.stage2_handoff.sections, expected_access, TEST_MODE_DRUM_PATTERNS, strict=True)):
            if instruction.tempo_bpm != TEST_MODE_TEMPO_BPM:
                issues.append(_issue("TEST_MODE_SECTION_TEMPO_MISMATCH", f"stage2_handoff.sections[{index}].tempo_bpm", "test-mode section tempo must be 96 BPM"))
            if instruction.input_motif_bars != request.constraints.musecoco_output_bars:
                issues.append(_issue("TEST_MODE_MOTIF_LENGTH_MISMATCH", f"stage2_handoff.sections[{index}].input_motif_bars", "test-mode input motif must equal constraints.musecoco_output_bars"))
            if instruction.drum_pattern.pattern != expected_pattern:
                issues.append(_issue("TEST_MODE_DRUM_PATTERN_MISMATCH", f"stage2_handoff.sections[{index}].drum_pattern.pattern", f"test mode expected {expected_pattern}"))
            if instruction.midigpt_access != expected:
                issues.append(_issue("TEST_MODE_MIDIGPT_ACCESS_MISMATCH", f"stage2_handoff.sections[{index}].midigpt_access", f"expected {expected}"))
    if issues:
        raise PlanValidationError(issues)
