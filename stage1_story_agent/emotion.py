"""Deterministic narrative-dimension to MuseCoco emotion derivation."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

from .models import EmotionQuadrant, FormRelation, LLMContentPlanDraft, NarrativeSegment


EMOTION_DERIVATION_VERSION = "valence-arousal-v1"
HIGH_AROUSAL_THRESHOLD = 0.55
POSITIVE_VALENCE_THRESHOLD = 0.15
NEGATIVE_VALENCE_THRESHOLD = -0.15


@dataclass(frozen=True)
class EmotionDerivation:
    quadrant: EmotionQuadrant
    weighted_valence: float
    peak_arousal: float
    source_segment_ids: tuple[str, ...]


def derive_emotion_quadrant(
    segments: Iterable[NarrativeSegment],
) -> EmotionDerivation:
    """Derive EM1 from introduction-segment valence and tension/arousal.

    High-arousal neutral material is treated as Q2 (uneasy); low-arousal neutral
    material is treated as Q4 (calm). Valence is tension-weighted so the most
    emotionally active material has more influence, while peak tension controls
    the high/low arousal split.
    """

    items = list(segments)
    if not items:
        raise ValueError("at least one narrative segment is required")
    weights = [max(item.tension, 0.1) for item in items]
    weighted_valence = sum(
        item.valence * weight for item, weight in zip(items, weights, strict=True)
    ) / sum(weights)
    peak_arousal = max(item.tension for item in items)
    if peak_arousal >= HIGH_AROUSAL_THRESHOLD:
        quadrant: EmotionQuadrant = (
            "Q1" if weighted_valence >= POSITIVE_VALENCE_THRESHOLD else "Q2"
        )
    else:
        quadrant = (
            "Q3" if weighted_valence <= NEGATIVE_VALENCE_THRESHOLD else "Q4"
        )
    return EmotionDerivation(
        quadrant=quadrant,
        weighted_valence=weighted_valence,
        peak_arousal=peak_arousal,
        source_segment_ids=tuple(item.segment_id for item in items),
    )


def derive_theme_emotions(
    draft: LLMContentPlanDraft,
) -> dict[str, EmotionDerivation]:
    """Derive one stable emotion target from each theme's introduction section."""

    segment_by_id = {
        segment.segment_id: segment
        for segment in draft.story_analysis.narrative_segments
    }
    introductions = {
        section.base_symbol: section
        for section in draft.form_sections
        if section.relation is FormRelation.INTRODUCE
    }
    result: dict[str, EmotionDerivation] = {}
    for family in draft.theme_families:
        introduction = introductions.get(family.base_symbol)
        if introduction is None:
            raise ValueError(
                f"theme family {family.base_symbol} has no introduction section"
            )
        segments = [
            segment_by_id[segment_id]
            for segment_id in introduction.narrative_segment_ids
            if segment_id in segment_by_id
        ]
        result[family.base_symbol] = derive_emotion_quadrant(segments)
    return result


def remove_deprecated_llm_em1(payload: Any) -> Any:
    """Ignore legacy model-selected EM1 while keeping raw_response.json unchanged."""

    if not isinstance(payload, dict):
        return payload
    normalized = copy.deepcopy(payload)
    families = normalized.get("theme_families")
    if not isinstance(families, list):
        return normalized
    for family in families:
        if not isinstance(family, dict):
            continue
        choices = family.get("musecoco_choices")
        if isinstance(choices, dict):
            choices.pop("EM1", None)
    return normalized
