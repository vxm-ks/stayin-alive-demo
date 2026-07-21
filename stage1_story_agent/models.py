"""Strict Pydantic contracts for the Stage 1 story planning agent."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .musecoco_policy import validate_musecoco_policy
from .musecoco_length_policy import DEFAULT_GENERATION_BARS, DEFAULT_OUTPUT_BARS

from .test_mode import (
    TEST_MODE_MODE,
    TEST_MODE_INPUT_MOTIF_BARS,
    TEST_MODE_SECTION_BARS,
    TEST_MODE_TEMPO_BPM,
    TEST_MODE_TIME_SIGNATURE,
    TEST_MODE_TONIC,
    TEST_MODE_TOTAL_BARS,
)


class StrictModel(BaseModel):
    """Shared configuration for every externally persisted model."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


StoryId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
LanguageTag = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", max_length=35),
]
SectionId = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]?$")]
NarrativeSegmentId = Annotated[str, StringConstraints(pattern=r"^N[1-9][0-9]?$")]
BaseSymbol = Annotated[str, StringConstraints(pattern=r"^[A-H]$")]
ThemeFamilyId = Annotated[str, StringConstraints(pattern=r"^theme-[A-H]$")]
SupportedTimeSignature = Literal["1/4", "2/4", "3/4", "4/4", "3/8", "6/8"]
Mode = Literal["major", "minor"]
Tonic = Literal["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


class FormRelation(str, Enum):
    INTRODUCE = "introduce"
    REPRISE = "reprise"
    VARIATION = "variation"
    DEVELOPMENT = "development"


class MaterialSource(str, Enum):
    MUSECOCO_SEED = "musecoco_seed"
    REUSE_THEME = "reuse_theme"
    MIDIGPT_VARIATION = "midigpt_variation"
    MIDIGPT_DEVELOPMENT = "midigpt_development"


class NarrativeRole(str, Enum):
    OPENING = "opening"
    BUILD = "build"
    TURN = "turn"
    CLIMAX = "climax"
    AFTERMATH = "aftermath"
    RESOLUTION = "resolution"
    OTHER = "other"


def _require_unique(values: list[Any], field_name: str) -> list[Any]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class StoryPlanConstraints(StrictModel):
    total_bars: int = Field(default=32, ge=4, le=256)
    target_form_sections: int | None = Field(default=None, ge=1, le=16)
    max_theme_families: int = Field(default=4, ge=1, le=8)
    max_variants_per_family: int = Field(default=2, ge=0, le=3)
    allowed_time_signatures: list[SupportedTimeSignature] = Field(
        default_factory=lambda: ["4/4"], min_length=1, max_length=6
    )
    tempo_bpm_min: float = Field(default=70.0, ge=30.0, le=240.0)
    tempo_bpm_max: float = Field(default=130.0, ge=30.0, le=240.0)
    allowed_modes: list[Mode] = Field(
        default_factory=lambda: ["major", "minor"], min_length=1, max_length=2
    )
    allowed_tonics: list[Tonic] | None = None
    musecoco_output_bars: int = Field(default=DEFAULT_OUTPUT_BARS, ge=1, le=16)
    musecoco_generation_bars: int = Field(default=DEFAULT_GENERATION_BARS, ge=1, le=16)

    @model_validator(mode="after")
    def validate_constraints(self) -> "StoryPlanConstraints":
        if self.tempo_bpm_min > self.tempo_bpm_max:
            raise ValueError("tempo_bpm_min must not exceed tempo_bpm_max")
        if self.musecoco_generation_bars < self.musecoco_output_bars:
            raise ValueError(
                "musecoco_generation_bars must not be less than musecoco_output_bars"
            )
        if self.total_bars < self.musecoco_output_bars:
            raise ValueError("total_bars must not be less than musecoco_output_bars")
        _require_unique(self.allowed_time_signatures, "allowed_time_signatures")
        _require_unique(self.allowed_modes, "allowed_modes")
        if self.allowed_tonics is not None:
            if not self.allowed_tonics:
                raise ValueError("allowed_tonics must be null or non-empty")
            _require_unique(self.allowed_tonics, "allowed_tonics")
        return self


class StoryPlanRequest(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    story_text: str = Field(min_length=1, max_length=20_000)
    language: LanguageTag = "zh-CN"
    test_mode: bool = False
    constraints: StoryPlanConstraints = Field(default_factory=StoryPlanConstraints)

    @model_validator(mode="before")
    @classmethod
    def normalize_test_mode(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not value.get("test_mode"):
            return value
        normalized = dict(value)
        constraints = dict(normalized.get("constraints") or {})
        constraints.update(
            {
                "total_bars": TEST_MODE_TOTAL_BARS,
                "target_form_sections": 3,
                "max_theme_families": 2,
                "max_variants_per_family": 0,
                "allowed_time_signatures": [TEST_MODE_TIME_SIGNATURE],
                "tempo_bpm_min": TEST_MODE_TEMPO_BPM,
                "tempo_bpm_max": TEST_MODE_TEMPO_BPM,
                "allowed_modes": [TEST_MODE_MODE],
                "allowed_tonics": [TEST_MODE_TONIC],
            }
        )
        constraints.setdefault("musecoco_output_bars", TEST_MODE_INPUT_MOTIF_BARS)
        constraints.setdefault("musecoco_generation_bars", DEFAULT_GENERATION_BARS)
        normalized["constraints"] = constraints
        return normalized

    @model_validator(mode="after")
    def validate_test_mode_lengths(self) -> "StoryPlanRequest":
        if self.test_mode and self.constraints.musecoco_output_bars > min(TEST_MODE_SECTION_BARS):
            raise ValueError("test-mode musecoco_output_bars must not exceed 8")
        return self


class GlobalTonality(StrictModel):
    tonic: Tonic
    mode: Mode
    rationale: str = Field(min_length=1, max_length=300)


class GlobalProposal(StrictModel):
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    time_signature: SupportedTimeSignature
    global_tonality: GlobalTonality


class NarrativeSegment(StrictModel):
    segment_id: NarrativeSegmentId
    summary: str = Field(min_length=1, max_length=500)
    narrative_role: NarrativeRole
    emotion: str = Field(min_length=1, max_length=80)
    valence: float = Field(ge=-1.0, le=1.0)
    tension: float = Field(ge=0.0, le=1.0)


class EmotionalArcPoint(StrictModel):
    position: float = Field(ge=0.0, le=1.0)
    emotion: str = Field(min_length=1, max_length=80)
    valence: float = Field(ge=-1.0, le=1.0)
    tension: float = Field(ge=0.0, le=1.0)


class LLMStoryAnalysis(StrictModel):
    summary: str = Field(min_length=1, max_length=1_000)
    emotional_arc: list[EmotionalArcPoint] = Field(min_length=2, max_length=20)
    narrative_segments: list[NarrativeSegment] = Field(min_length=1, max_length=99)


class LLMFormSection(StrictModel):
    """Only fields the LLM is authoritative for."""

    section_id: SectionId
    base_symbol: BaseSymbol
    variant_index: int = Field(ge=0, le=3)
    relation: FormRelation
    source_section_id: SectionId | None = None
    bar_count: int = Field(ge=1, le=64)
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    narrative_segment_ids: list[NarrativeSegmentId] = Field(min_length=1)
    musical_intent: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_local_shape(self) -> "LLMFormSection":
        _require_unique(self.narrative_segment_ids, "narrative_segment_ids")
        return self


Instrument = Literal[
    "piano", "keyboard", "percussion", "organ", "guitar", "bass", "violin",
    "viola", "cello", "harp", "strings", "voice", "trumpet", "trombone",
    "tuba", "horn", "brass", "sax", "oboe", "bassoon", "clarinet", "piccolo",
    "flute", "pipe", "synthesizer", "ethnic_instruments", "sound_effects", "drum",
]
Artist = Literal[
    "beethoven", "mozart", "chopin", "schubert", "schumann", "bach", "haydn",
    "brahms", "handel", "tchaikovsky", "mendelssohn", "dvorak", "liszt",
    "stravinsky", "mahler", "prokofiev", "shostakovich",
]
Genre = Literal[
    "new_age", "electronic", "rap", "religious", "international", "easy_listening",
    "avant_garde", "rnb", "latin", "children", "jazz", "classical", "comedy_spoken",
    "pop_rock", "reggae", "stage", "folk", "blues", "vocal", "holiday", "country",
    "symphony",
]
Danceability = Literal["danceable", "not_danceable"]
RhythmicIntensity = Literal["low", "medium", "high"]
EmotionQuadrant = Literal["Q1", "Q2", "Q3", "Q4"]
BarBucket = Literal["1-4", "5-8", "9-12", "13-16"]
TempoClass = Literal["slow", "moderate", "fast"]
DurationBucket = Literal["0-15", "15-30", "30-45", "45-60", "60+"]


class MuseCocoChoices(StrictModel):
    I1s2: list[Instrument] = Field(min_length=1, max_length=28)
    R1: Danceability
    R3: RhythmicIntensity
    S2s1: Artist
    S4: list[Genre] = Field(min_length=1, max_length=22)
    P4: int = Field(ge=0, le=11)

    @model_validator(mode="after")
    def validate_unique_lists(self) -> "MuseCocoChoices":
        _require_unique(self.I1s2, "I1s2")
        _require_unique(self.S4, "S4")
        validate_musecoco_policy(self.I1s2, self.S2s1)
        return self


class LLMThemeFamily(StrictModel):
    base_symbol: BaseSymbol
    role: str = Field(min_length=1, max_length=80)
    seed_bars: int = Field(ge=1, le=16)
    intent: str = Field(min_length=1, max_length=300)
    musecoco_choices: MuseCocoChoices


class LLMContentPlanDraft(StrictModel):
    global_proposal: GlobalProposal
    story_analysis: LLMStoryAnalysis
    form_sections: list[LLMFormSection] = Field(min_length=1, max_length=16)
    theme_families: list[LLMThemeFamily] = Field(min_length=1, max_length=8)


class CompiledFormSection(StrictModel):
    section_id: SectionId
    form_label: str = Field(pattern=r"^[A-H]'{0,3}$")
    theme_family_id: ThemeFamilyId
    relation: FormRelation
    source_section_id: SectionId | None = None
    material_source: MaterialSource
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    bar_count: int = Field(ge=1, le=64)
    narrative_segment_ids: list[NarrativeSegmentId] = Field(min_length=1)
    musical_intent: str = Field(min_length=1, max_length=300)


class BarRange(StrictModel):
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)

    @model_validator(mode="after")
    def validate_order(self) -> "BarRange":
        if self.bar_start > self.bar_end:
            raise ValueError("bar_start must not exceed bar_end")
        return self


DrumPatternName = Literal["single_pulse_per_bar", "pulse_each_beat"]


class DrumPatternInstruction(StrictModel):
    pattern: DrumPatternName
    time_signature: SupportedTimeSignature
    kick_beats: list[float] = Field(default_factory=list, max_length=32)
    snare_beats: list[float] = Field(default_factory=list, max_length=32)
    closed_hihat_beats: list[float] = Field(default_factory=list, max_length=64)


class Stage2SectionInstruction(StrictModel):
    section_id: SectionId
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    input_motif_bars: int = Field(ge=1, le=16)
    target_section_bars: int = Field(ge=1, le=64)
    extension_bars: int = Field(ge=0, le=63)
    extension_method: Literal[
        "none", "midigpt_extend", "midigpt_transform", "reuse_source_section"
    ]
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    source_tension: float = Field(ge=0.0, le=1.0)
    tension_level: Literal["low", "high"]
    drum_pattern: DrumPatternInstruction
    midigpt_access: Literal["fixed", "extension_only", "modifiable"]
    protected_bar_ranges: list[BarRange] = Field(default_factory=list, max_length=2)
    editable_bar_ranges: list[BarRange] = Field(default_factory=list, max_length=2)


class Stage2Handoff(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    bar_numbering: Literal["one_based_closed"] = "one_based_closed"
    drum_track_scope: Literal["instructions_only"] = "instructions_only"
    heart_sound_render_stage: Literal["after_midigpt"] = "after_midigpt"
    sections: list[Stage2SectionInstruction] = Field(min_length=1, max_length=16)


class Stage2Delivery(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    total_bars: int = Field(ge=4, le=256)
    form_string: str = Field(min_length=1)
    time_signature: SupportedTimeSignature
    bar_numbering: Literal["one_based_closed"] = "one_based_closed"
    drum_track_scope: Literal["instructions_only"] = "instructions_only"
    sections: list[Stage2SectionInstruction] = Field(min_length=1, max_length=16)


class HeartbeatProcessingSection(StrictModel):
    section_id: SectionId
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    source_tension: float = Field(ge=0.0, le=1.0)
    tension_level: Literal["low", "high"]
    trigger_mode: Literal["once_per_bar", "every_beat"]
    trigger_beats: list[float] = Field(min_length=1, max_length=32)
    heart_sounds: list[Literal["S1", "S2"]] = Field(min_length=1, max_length=2)
    source_drum_pattern: DrumPatternInstruction


class HeartbeatProcessingDelivery(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    render_stage: Literal["after_midigpt"] = "after_midigpt"
    input_source: Literal["final_midigpt_drum_track"] = "final_midigpt_drum_track"
    contains_heartbeat_audio: Literal[False] = False
    tension_threshold: Literal[0.55] = 0.55
    high_tension_every_beat_below_bpm: Literal[110.0] = 110.0
    sections: list[HeartbeatProcessingSection] = Field(min_length=1, max_length=16)


class FormPlan(StrictModel):
    form_string: str = Field(min_length=1)
    sections: list[CompiledFormSection] = Field(min_length=1, max_length=16)


class VariationTask(StrictModel):
    task_id: str = Field(pattern=r"^variation-S[1-9][0-9]?$")
    target_section_id: SectionId
    source_section_id: SectionId
    theme_family_id: ThemeFamilyId
    strategy: Literal["midigpt_variation", "midigpt_development"]
    target_bars: int = Field(ge=1, le=64)
    intent: str = Field(min_length=1, max_length=300)


class FormCompilation(StrictModel):
    form_plan: FormPlan
    theme_family_ids: list[ThemeFamilyId] = Field(min_length=1, max_length=8)
    variation_tasks: list[VariationTask]


class MuseCocoAttributeTargets(StrictModel):
    I1s2: list[Instrument] = Field(min_length=1, max_length=28)
    R1: Danceability
    R3: RhythmicIntensity
    S2s1: Artist
    S4: list[Genre] = Field(min_length=1, max_length=22)
    B1s1: BarBucket
    TS1s1: SupportedTimeSignature
    K1: Mode
    T1s1: TempoClass
    P4: int = Field(ge=0, le=11)
    EM1: EmotionQuadrant
    TM1: DurationBucket

    @model_validator(mode="after")
    def validate_project_policy(self) -> "MuseCocoAttributeTargets":
        validate_musecoco_policy(self.I1s2, self.S2s1)
        return self


class ThemeFamily(StrictModel):
    theme_family_id: ThemeFamilyId
    base_symbol: BaseSymbol
    introduced_in_section_id: SectionId
    role: str = Field(min_length=1, max_length=80)
    seed_bars: int = Field(ge=1, le=16)
    intent: str = Field(min_length=1, max_length=300)
    musecoco_text_style: Literal["official-template-aligned-v1"] = "official-template-aligned-v1"
    musecoco_attribute_targets: MuseCocoAttributeTargets
    musecoco_text: str = Field(min_length=1, max_length=2_000)


class MuseCocoDelivery(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    time_signature: SupportedTimeSignature
    global_tonality: GlobalTonality
    generation_target_bars: int = Field(default=DEFAULT_GENERATION_BARS, ge=1, le=16)
    output_motif_bars: int = Field(default=DEFAULT_OUTPUT_BARS, ge=1, le=16)
    theme_families: list[ThemeFamily] = Field(min_length=1, max_length=8)
    tempo_normalization_required: Literal[True] = True
    key_normalization_required: Literal[True] = True
    key_normalization_policy: Literal["transpose_tonic_reject_mode_mismatch"] = (
        "transpose_tonic_reject_mode_mismatch"
    )


class GlobalPlan(StrictModel):
    total_bars: int = Field(ge=4, le=256)
    tempo_bpm: float = Field(ge=30.0, le=240.0)
    time_signature: SupportedTimeSignature
    global_tonality: GlobalTonality


class Provenance(StrictModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    prompt_version: Literal["stage1-form-v7"] = "stage1-form-v7"
    request_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=80)
    story_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: str = Field(min_length=1, max_length=40)
    content_attempts: int = Field(ge=1, le=3)
    network_attempts: int = Field(ge=1)


class ContentPlan(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    test_mode: bool = False
    global_: GlobalPlan = Field(alias="global")
    story_analysis: LLMStoryAnalysis
    form_plan: FormPlan
    stage2_handoff: Stage2Handoff
    theme_families: list[ThemeFamily] = Field(min_length=1, max_length=8)
    variation_tasks: list[VariationTask]
    musecoco_requests: list[dict[str, Any]] = Field(default_factory=list, max_length=0)
    provenance: Provenance


class BackendRawResponse(StrictModel):
    provider: str
    model: str
    request_id: str
    finish_reason: str | None = None
    content: str
    usage: dict[str, int] = Field(default_factory=dict)


class RunManifest(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: StoryId
    run_id: str
    status: Literal["succeeded"] = "succeeded"
    generated_at: str
    story_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, str]


class PlanRun(StrictModel):
    content_plan: ContentPlan
    musecoco_delivery: MuseCocoDelivery
    heartbeat_delivery: HeartbeatProcessingDelivery
    stage2_delivery: Stage2Delivery
    raw_response: BackendRawResponse
    manifest: RunManifest


class ValidationIssue(StrictModel):
    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    path: str
    message: str


class FailureReport(StrictModel):
    schema_version: Literal["0.2-draft"] = "0.2-draft"
    story_id: str | None = None
    status: Literal["failed"] = "failed"
    category: Literal["input", "configuration", "backend", "content", "write", "internal"]
    error_code: str
    message: str
    generated_at: str
    content_attempts: int = Field(ge=0, le=3)
    network_attempts: int = Field(ge=0)
    issues: list[ValidationIssue] = Field(default_factory=list, max_length=20)
