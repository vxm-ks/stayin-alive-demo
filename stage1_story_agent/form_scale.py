"""Story-driven musical form scale decision before detailed Stage 1 planning."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .backends import ChatMessage, CompletionRequest, LLMBackend
from .errors import ModelContentError
from .models import (
    FormBlueprintSection,
    FormRelation,
    FormScaleDecision,
    StoryPlanRequest,
)

FORM_SCALE_PROMPT_VERSION = "stage1-form-scale-v1"


class _ScaleSectionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    narrative_function: str = Field(min_length=1, max_length=300)
    theme_action: FormRelation
    source_section: int | None = Field(default=None, ge=1, le=16)


class _ScaleDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section_count: int
    rationale: str = Field(min_length=1, max_length=500)
    sections: list[_ScaleSectionDraft] = Field(min_length=1, max_length=16)


def _messages(
    request: StoryPlanRequest,
    *,
    previous_response: str = "",
    issues: list[dict[str, str]] | None = None,
) -> CompletionRequest:
    constraints = request.constraints
    section_bars = (
        constraints.musecoco_output_bars + constraints.default_extension_bars
    )
    payload: dict[str, Any] = {
        "prompt_version": FORM_SCALE_PROMPT_VERSION,
        "task": "Choose the number of musical form sections required by the story.",
        "story": {
            "language": request.language,
            "text": request.story_text,
        },
        "rules": {
            "minimum_sections": constraints.auto_form_sections_min,
            "maximum_sections": constraints.auto_form_sections_max,
            "maximum_new_themes": constraints.max_theme_families,
            "maximum_variants_per_theme": constraints.max_variants_per_family,
            "bars_per_section": section_bars,
            "selection_guidance": {
                "1": "one sustained dramatic state with no meaningful structural turn",
                "2": "a clear binary contrast or departure",
                "3": "a simple opening-change-resolution arc",
                "4": "several clearly distinct dramatic functions or one important turn",
                "5": "a complex multi-stage journey with multiple meaningful turns",
                "6": "reserve for unusually complex stories with several independent developments",
            },
            "do_not_count": [
                "sentence length by itself",
                "decorative details that do not change the dramatic function",
            ],
        },
        "output_schema": {
            "section_count": "integer inside the permitted range",
            "rationale": "brief explanation grounded in the story's dramatic functions",
            "sections": [
                {
                    "narrative_function": "the dramatic function of this section",
                    "theme_action": "introduce | reprise | variation | development",
                    "source_section": (
                        "null for introduce; otherwise the 1-based index of an "
                        "earlier section whose theme returns"
                    ),
                }
            ],
        },
        "theme_reuse_policy": {
            "introduce": "use only when the story needs genuinely new thematic identity",
            "reprise": "reuse an earlier theme when the same place, memory, person, or state returns recognizably",
            "variation": "reuse but transform an earlier theme when familiar content returns changed",
            "development": "develop an earlier theme when its conflict or implication is pushed forward",
            "example": "returning to an earlier idea before a new ending may yield A-B-A-C",
        },
    }
    if previous_response:
        payload["repair"] = {
            "previous_response": previous_response,
            "issues": issues or [],
            "instruction": "Return a complete corrected JSON object only.",
        }
    return CompletionRequest(
        messages=[
            ChatMessage(
                role="system",
                content=(
                    "You are a musical-form scale classifier. Decide only how many "
                    "sections the narrative requires. Output one strict JSON object "
                    "and no Markdown."
                ),
            ),
            ChatMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        ]
    )


def _fixed_decision(
    request: StoryPlanRequest,
    *,
    strategy: str,
    rationale: str,
) -> tuple[StoryPlanRequest, FormScaleDecision, int]:
    section_bars = (
        request.constraints.musecoco_output_bars
        + request.constraints.default_extension_bars
    )
    section_count = (
        request.constraints.target_form_sections
        or request.constraints.total_bars // section_bars
    )
    return (
        request,
        FormScaleDecision(
            strategy=strategy,
            section_count=section_count,
            section_bars=section_bars,
            total_bars=request.constraints.total_bars,
            rationale=rationale,
        ),
        0,
    )


def _compile_blueprint(
    draft: _ScaleDraft,
    request: StoryPlanRequest,
) -> tuple[list[FormBlueprintSection], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    if len(draft.sections) != draft.section_count:
        return [], [
            {
                "code": "FORM_SCALE_SECTION_COUNT_MISMATCH",
                "path": "sections",
                "message": "sections length must equal section_count",
            }
        ]

    blueprint: list[FormBlueprintSection] = []
    next_symbol_index = 0
    variant_counts: dict[str, int] = {}
    for index, section in enumerate(draft.sections, start=1):
        path = f"sections[{index - 1}]"
        if section.theme_action is FormRelation.INTRODUCE:
            if section.source_section is not None:
                issues.append(
                    {
                        "code": "FORM_SCALE_SOURCE_FORBIDDEN",
                        "path": f"{path}.source_section",
                        "message": "introduce must not name a source section",
                    }
                )
                continue
            if next_symbol_index >= request.constraints.max_theme_families:
                issues.append(
                    {
                        "code": "FORM_SCALE_THEME_FAMILY_LIMIT",
                        "path": f"{path}.theme_action",
                        "message": "new theme count exceeds max_theme_families",
                    }
                )
                continue
            base_symbol = chr(ord("A") + next_symbol_index)
            next_symbol_index += 1
            variant_index = 0
            variant_counts[base_symbol] = 0
            source_section_id = None
        else:
            source = section.source_section
            if source is None or source >= index or source > len(blueprint):
                issues.append(
                    {
                        "code": "FORM_SCALE_SOURCE_INVALID",
                        "path": f"{path}.source_section",
                        "message": "theme reuse must reference an earlier section",
                    }
                )
                continue
            source_blueprint = blueprint[source - 1]
            base_symbol = source_blueprint.base_symbol
            source_section_id = f"S{source}"
            if section.theme_action is FormRelation.REPRISE:
                variant_index = 0
            else:
                variant_counts[base_symbol] += 1
                variant_index = variant_counts[base_symbol]
                if variant_index > request.constraints.max_variants_per_family:
                    issues.append(
                        {
                            "code": "FORM_SCALE_VARIANT_LIMIT",
                            "path": f"{path}.theme_action",
                            "message": (
                                f"theme {base_symbol} exceeds max_variants_per_family"
                            ),
                        }
                    )
                    continue
        blueprint.append(
            FormBlueprintSection(
                section_id=f"S{index}",
                base_symbol=base_symbol,
                variant_index=variant_index,
                relation=section.theme_action,
                source_section_id=source_section_id,
                narrative_function=section.narrative_function,
            )
        )
    return blueprint, issues


def resolve_form_scale(
    backend: LLMBackend,
    request: StoryPlanRequest,
    *,
    max_content_attempts: int,
) -> tuple[StoryPlanRequest, FormScaleDecision, int]:
    """Resolve auto section count and return an immutable effective request."""

    if request.test_mode:
        return _fixed_decision(
            request,
            strategy="test_mode",
            rationale="Test mode fixes an exact three-section A-B-A form.",
        )
    if request.constraints.target_form_sections is not None:
        return _fixed_decision(
            request,
            strategy="fixed_request",
            rationale="The caller explicitly fixed the form section count.",
        )

    previous_response = ""
    issues: list[dict[str, str]] = []
    network_attempts = 0
    constraints = request.constraints
    section_bars = (
        constraints.musecoco_output_bars + constraints.default_extension_bars
    )
    for content_attempt in range(1, max_content_attempts + 1):
        response = backend.complete(
            _messages(
                request,
                previous_response=previous_response,
                issues=issues,
            )
        )
        network_attempts += response.network_attempts
        previous_response = response.content
        issues = []
        if not response.content.strip():
            issues.append(
                {
                    "code": "FORM_SCALE_RESPONSE_EMPTY",
                    "path": "response.content",
                    "message": "form scale response is empty",
                }
            )
        elif response.finish_reason == "length":
            issues.append(
                {
                    "code": "FORM_SCALE_RESPONSE_TRUNCATED",
                    "path": "response.finish_reason",
                    "message": "form scale response was truncated",
                }
            )
        else:
            try:
                raw = json.loads(response.content)
                draft = _ScaleDraft.model_validate(raw)
            except json.JSONDecodeError as exc:
                issues.append(
                    {
                        "code": "FORM_SCALE_RESPONSE_NOT_JSON",
                        "path": "response.content",
                        "message": f"invalid JSON at line {exc.lineno}, column {exc.colno}",
                    }
                )
            except ValidationError as exc:
                issues.extend(
                    {
                        "code": "FORM_SCALE_SCHEMA_INVALID",
                        "path": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors(
                        include_url=False, include_context=False
                    )[:20]
                )
            else:
                if not (
                    constraints.auto_form_sections_min
                    <= draft.section_count
                    <= constraints.auto_form_sections_max
                ):
                    issues.append(
                        {
                            "code": "FORM_SCALE_COUNT_OUT_OF_RANGE",
                            "path": "section_count",
                            "message": (
                                "section_count must be between "
                                f"{constraints.auto_form_sections_min} and "
                                f"{constraints.auto_form_sections_max}"
                            ),
                        }
                    )
                else:
                    blueprint, blueprint_issues = _compile_blueprint(draft, request)
                    if blueprint_issues:
                        issues.extend(blueprint_issues)
                        continue
                    payload = request.model_dump(mode="json", by_alias=True)
                    effective_constraints = dict(payload["constraints"])
                    effective_constraints["target_form_sections"] = draft.section_count
                    effective_constraints["total_bars"] = (
                        draft.section_count * section_bars
                    )
                    effective_constraints["form_blueprint"] = [
                        item.model_dump(mode="json") for item in blueprint
                    ]
                    payload["constraints"] = effective_constraints
                    effective_request = StoryPlanRequest.model_validate(payload)
                    return (
                        effective_request,
                        FormScaleDecision(
                            strategy="story_llm",
                            section_count=draft.section_count,
                            section_bars=section_bars,
                            total_bars=draft.section_count * section_bars,
                            rationale=draft.rationale,
                            form_blueprint=blueprint,
                            provider=response.provider,
                            model=response.model,
                            request_id=response.request_id,
                            content_attempts=content_attempt,
                            network_attempts=network_attempts,
                        ),
                        network_attempts,
                    )

    raise ModelContentError(
        "FORM_SCALE_DECISION_ATTEMPTS_EXHAUSTED",
        (
            "story-driven form scale decision remained invalid after "
            f"{max_content_attempts} attempts"
        ),
        issues=issues,
        content_attempts=max_content_attempts,
        network_attempts=network_attempts,
    )
