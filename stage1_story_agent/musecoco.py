"""Deterministic MuseCoco attribute derivation and English rendering."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    GlobalProposal,
    EmotionQuadrant,
    LLMThemeFamily,
    MuseCocoAttributeTargets,
    ThemeFamily,
)


MUSECOCO_ATTRIBUTE_KEYS = frozenset(
    {"I1s2", "R1", "R3", "S2s1", "S4", "B1s1", "TS1s1", "K1", "T1s1", "P4", "EM1", "TM1"}
)

MUSECOCO_TEXT_STYLE_VERSION = "official-template-aligned-v1"

EMOTION_LABELS = {
    "Q1": "happiness, excitement, and positivity",
    "Q2": "tension, unease, and anxiety",
    "Q3": "sadness, dejection, and melancholy",
    "Q4": "calmness, relaxation, and serenity",
}

BAR_TEXT = {
    "1-4": "1 ~ 4",
    "5-8": "5 ~ 8",
    "9-12": "9 ~ 12",
    "13-16": "13 ~ 16",
}

DURATION_TEXT = {
    "0-15": "1 ~ 15",
    "15-30": "16 ~ 30",
    "30-45": "31 ~ 45",
    "45-60": "46 ~ 60",
    "60+": "over 60",
}

RHYTHM_TEXT = {
    "low": "a very peaceful beat",
    "medium": "a moderate beat",
    "high": "an extremely strong beat",
}


@dataclass(frozen=True)
class RenderedMuseCocoText:
    text: str
    fragments_by_attribute: dict[str, str]


def bar_bucket(seed_bars: int) -> str:
    if not 1 <= seed_bars <= 16:
        raise ValueError("seed_bars must be between 1 and 16")
    if seed_bars <= 4:
        return "1-4"
    if seed_bars <= 8:
        return "5-8"
    if seed_bars <= 12:
        return "9-12"
    return "13-16"


def tempo_class(tempo_bpm: float) -> str:
    if tempo_bpm <= 0:
        raise ValueError("tempo_bpm must be positive")
    if tempo_bpm <= 76:
        return "slow"
    if tempo_bpm < 120:
        return "moderate"
    return "fast"


def duration_seconds(seed_bars: int, time_signature: str, tempo_bpm: float) -> float:
    numerator_text, denominator_text = time_signature.split("/", 1)
    numerator, denominator = int(numerator_text), int(denominator_text)
    if seed_bars <= 0 or numerator <= 0 or denominator <= 0 or tempo_bpm <= 0:
        raise ValueError("duration inputs must be positive")
    return seed_bars * numerator * 4 / denominator * 60 / tempo_bpm


def duration_bucket(seconds: float) -> str:
    if seconds <= 0:
        raise ValueError("duration must be positive")
    if seconds <= 15:
        return "0-15"
    if seconds <= 30:
        return "15-30"
    if seconds <= 45:
        return "30-45"
    if seconds <= 60:
        return "45-60"
    return "60+"


def derive_targets(
    family: LLMThemeFamily,
    global_proposal: GlobalProposal,
    emotion_quadrant: EmotionQuadrant,
    *,
    generation_bars: int | None = None,
) -> MuseCocoAttributeTargets:
    choices = family.musecoco_choices
    attribute_bars = generation_bars or family.seed_bars
    seconds = duration_seconds(
        attribute_bars, global_proposal.time_signature, global_proposal.tempo_bpm
    )
    return MuseCocoAttributeTargets(
        I1s2=choices.I1s2,
        R1=choices.R1,
        R3=choices.R3,
        S2s1=choices.S2s1,
        S4=choices.S4,
        B1s1=bar_bucket(attribute_bars),
        TS1s1=global_proposal.time_signature,
        K1=global_proposal.global_tonality.mode,
        T1s1=tempo_class(global_proposal.tempo_bpm),
        P4=choices.P4,
        EM1=emotion_quadrant,
        TM1=duration_bucket(seconds),
    )


def _display(value: str) -> str:
    return value.replace("_", " ")


def _english_list(values: list[str]) -> str:
    displayed = [_display(value) for value in values]
    if len(displayed) == 1:
        return displayed[0]
    if len(displayed) == 2:
        return " and ".join(displayed)
    return f"{', '.join(displayed[:-1])}, and {displayed[-1]}"


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def render_musecoco_text(
    family: LLMThemeFamily,
    global_proposal: GlobalProposal,
    targets: MuseCocoAttributeTargets | None = None,
    *,
    emotion_quadrant: EmotionQuadrant | None = None,
) -> RenderedMuseCocoText:
    if targets is None:
        if emotion_quadrant is None:
            raise ValueError("emotion_quadrant is required when targets are omitted")
        targets = derive_targets(family, global_proposal, emotion_quadrant)
    instruments = _english_list(targets.I1s2)
    genres = _english_list(targets.S4)
    dance_phrase = (
        "suitable for dancing"
        if targets.R1 == "danceable"
        else "not suitable for dancing"
    )
    artist = _display(targets.S2s1).title()
    bar_text = BAR_TEXT[targets.B1s1]
    duration_text = DURATION_TEXT[targets.TM1]
    rhythm_text = RHYTHM_TEXT[targets.R3]
    fragments = {
        "I1s2": f"The music is brought to life through the use of {instruments}.",
        "R1": f"This music is {dance_phrase}.",
        "R3": f"The song has {rhythm_text}.",
        "S2s1": f"The music is in the vein of {artist}.",
        "S4": f"The song belongs to the {genres} genre.",
        "B1s1": f"The song spans approximately {bar_text} bars.",
        "TS1s1": f"The music is in {targets.TS1s1}.",
        "K1": f"This music is composed in the {targets.K1} key.",
        "T1s1": f"The tempo of this song is {targets.T1s1}.",
        "P4": f"Its pitch range is within {targets.P4} octaves.",
        "EM1": f"The music conveys {EMOTION_LABELS[targets.EM1]}.",
        "TM1": f"This song has a duration of {duration_text} seconds.",
    }
    text = (
        f"The musical piece is a representative example of the {genres} style and "
        f"is in the vein of {artist}. This music is composed in the {targets.K1} key "
        f"and follows a {targets.TS1s1} meter. The music is brought to life through "
        f"the use of {instruments}. Its pitch range is within {targets.P4} octaves. "
        f"The tempo of this song is {targets.T1s1}. This music is {dance_phrase}, and "
        f"the song has {rhythm_text}. The music conveys "
        f"{EMOTION_LABELS[targets.EM1]}. The song spans approximately {bar_text} bars "
        f"and has a duration of {duration_text} seconds."
    )
    if set(fragments) != MUSECOCO_ATTRIBUTE_KEYS:
        raise AssertionError("MuseCoco rendering coverage invariant failed")
    return RenderedMuseCocoText(text=text, fragments_by_attribute=fragments)


def enrich_theme_family(
    family: LLMThemeFamily,
    global_proposal: GlobalProposal,
    introduced_in_section_id: str,
    emotion_quadrant: EmotionQuadrant,
    *,
    generation_bars: int | None = None,
) -> ThemeFamily:
    targets = derive_targets(
        family,
        global_proposal,
        emotion_quadrant,
        generation_bars=generation_bars,
    )
    rendered = render_musecoco_text(family, global_proposal, targets)
    return ThemeFamily(
        theme_family_id=f"theme-{family.base_symbol}",
        base_symbol=family.base_symbol,
        introduced_in_section_id=introduced_in_section_id,
        role=family.role,
        seed_bars=family.seed_bars,
        intent=family.intent,
        musecoco_text_style=MUSECOCO_TEXT_STYLE_VERSION,
        musecoco_attribute_targets=targets,
        musecoco_text=rendered.text,
    )
