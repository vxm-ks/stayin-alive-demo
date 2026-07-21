"""Strict project schemas used before compiling official MIDI-GPT objects."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Identifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
]
SectionId = Annotated[str, StringConstraints(pattern=r"^S[1-9][0-9]?$")]
ThemeFamilyId = Annotated[str, StringConstraints(pattern=r"^theme-[A-H]$")]
TimeSignature = Literal["1/4", "2/4", "3/4", "4/4", "3/8", "6/8"]


class ContentGlobal(StrictModel):
    total_bars: int = Field(ge=1, le=256)
    tempo_bpm: float = Field(gt=0, le=300)
    time_signature: TimeSignature
    global_tonality: dict[str, Any] | None = None


class ContentSection(StrictModel):
    section_id: SectionId
    form_label: str = Field(min_length=1, max_length=8)
    theme_family_id: ThemeFamilyId
    relation: Literal["introduce", "reprise", "variation", "development"]
    source_section_id: SectionId | None = None
    material_source: Literal[
        "musecoco_seed",
        "reuse_theme",
        "midigpt_variation",
        "midigpt_development",
    ]
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    bar_count: int = Field(ge=1, le=64)
    narrative_segment_ids: list[str] = Field(default_factory=list)
    musical_intent: str = ""


class ContentFormPlan(StrictModel):
    form_string: str = Field(min_length=1)
    sections: list[ContentSection] = Field(min_length=1, max_length=16)


class ContentVariationTask(StrictModel):
    task_id: Identifier
    target_section_id: SectionId
    source_section_id: SectionId
    theme_family_id: ThemeFamilyId
    strategy: Literal["midigpt_variation", "midigpt_development"]
    target_bars: int = Field(ge=1, le=64)
    intent: str | None = None


class ContentPlan(StrictModel):
    """The Stage-1 fields consumed by this compiler.

    Story analysis and MuseCoco attributes are deliberately opaque here, but
    top-level names remain strict so a misspelled contract field cannot vanish.
    """

    schema_version: str
    story_id: Identifier
    test_mode: bool = False
    global_: ContentGlobal = Field(alias="global")
    story_analysis: dict[str, Any] = Field(default_factory=dict)
    form_plan: ContentFormPlan
    stage2_handoff: dict[str, Any] | None = None
    theme_families: list[dict[str, Any]] = Field(default_factory=list)
    variation_tasks: list[ContentVariationTask] = Field(default_factory=list)
    musecoco_requests: list[Any] = Field(default_factory=list)
    provenance: dict[str, Any] | None = None

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class TrackPlan(StrictModel):
    track_id: Identifier
    role: Literal[
        "heartbeat",
        "motif",
        "melody",
        "harmony",
        "bass",
        "accompaniment",
        "transition",
        "auxiliary",
    ]
    instrument: int = Field(ge=0, le=127)
    track_type: Literal["melodic", "drum"] = "melodic"
    behavior: Literal["context", "generate", "ignore"]


class HitsPerBarPattern(StrictModel):
    mode: Literal["hits_per_bar"]
    hit_unit: Literal["S1", "S2", "s1_s2_pair"]
    hits_per_bar: int = Field(ge=1, le=32)
    beat_positions: list[float] = Field(min_length=1, max_length=32)
    velocity_scale: float = Field(gt=0, le=2)

    @model_validator(mode="after")
    def validate_count(self) -> "HitsPerBarPattern":
        if self.hits_per_bar != len(self.beat_positions):
            raise ValueError("hits_per_bar must equal the number of beat_positions")
        if len(set(self.beat_positions)) != len(self.beat_positions):
            raise ValueError("beat_positions must not contain duplicates")
        return self


class EventSequencePattern(StrictModel):
    mode: Literal["event_sequence"]
    event_sequence: list[Literal["S1", "S2"]] = Field(min_length=1, max_length=16)
    event_spacing_beats: float = Field(gt=0, le=16)
    start_beat: float = Field(ge=1, le=32)
    fill_to_bar_end: bool = True
    velocity_scale: float = Field(gt=0, le=2)


HeartbeatPattern = Annotated[
    HitsPerBarPattern | EventSequencePattern, Field(discriminator="mode")
]


class HeartbeatSectionPlan(StrictModel):
    section_id: SectionId
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    intent: str = Field(min_length=1, max_length=300)
    pattern: HeartbeatPattern


class HeartbeatArrangement(StrictModel):
    role: Literal["protected_rhythm_anchor"] = "protected_rhythm_anchor"
    preserve_complete_events: Literal[True] = True
    sections: list[HeartbeatSectionPlan] = Field(min_length=1, max_length=16)
    unplanned_bars_policy: Literal["error"] = "error"


class MotifPlacement(StrictModel):
    placement_id: Identifier
    motif_id: Identifier
    target_track_id: Identifier
    target_section_id: SectionId
    target_bar: int = Field(ge=1, le=256)
    repeat_count: int = Field(default=1, ge=1, le=64)
    transpose_semitones: int = Field(default=0, ge=-36, le=36)
    velocity_scale: float = Field(default=1.0, gt=0, le=2)
    protected: Literal[True] = True


class GenerationRegion(StrictModel):
    track_id: Identifier
    bar_start: int = Field(ge=1, le=256)
    bar_end: int = Field(ge=1, le=256)
    mode: Literal["infill", "autoregressive"] = "infill"


class GenerationConfig(StrictModel):
    temperature: float = Field(default=1.0, gt=0)
    seed: int = -1
    max_attempts: int = Field(default=3, ge=1)
    novelty_check: bool = True
    silence_check: bool = True
    temperature_escalation: float = Field(default=1.0, ge=1.0)
    bars_per_step: int = Field(default=1, ge=1)
    tracks_per_step: int = Field(default=1, ge=1)
    shuffle: bool = False
    mask_mode: Literal["attention"] = "attention"
    polyphony_hard_limit: int = Field(default=0, ge=0)
    density_hard_limit: int = Field(default=0, ge=0)
    top_p: float = Field(default=0.95, gt=0, le=1)
    top_k: int = Field(default=0, ge=0)
    mask_p: float = Field(default=0.0, ge=0, lt=1)
    mask_k: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_sampling(self) -> "GenerationConfig":
        if self.mask_p and self.top_p < 1 and self.mask_p >= self.top_p:
            raise ValueError("mask_p must be smaller than top_p")
        if self.mask_k and self.top_k and self.mask_k >= self.top_k:
            raise ValueError("mask_k must be smaller than top_k")
        return self


class MidiGptPlan(StrictModel):
    checkpoint: str = Field(default="yellow", min_length=1, max_length=100)
    model_dim_bars: int = Field(default=4, ge=1, le=16)
    fill_regions: list[GenerationRegion] = Field(min_length=1)
    generation_config: GenerationConfig = Field(default_factory=GenerationConfig)


class MusicPlan(StrictModel):
    schema_version: Literal["0.1-draft"] = "0.1-draft"
    story_id: Identifier
    ppq: int = Field(default=480, ge=24, le=9600)
    tracks: list[TrackPlan] = Field(min_length=1, max_length=32)
    motif_placements: list[MotifPlacement] = Field(default_factory=list)
    heartbeat_arrangement: HeartbeatArrangement
    midigpt: MidiGptPlan

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "MusicPlan":
        track_ids = [item.track_id for item in self.tracks]
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("tracks.track_id must be unique")
        placement_ids = [item.placement_id for item in self.motif_placements]
        if len(placement_ids) != len(set(placement_ids)):
            raise ValueError("motif_placements.placement_id must be unique")
        return self


class HeartbeatEvent(StrictModel):
    event_type: Literal["S1", "S2"]
    tick: int = Field(ge=0)
    duration_ticks: int = Field(gt=0)
    midi_note: int = Field(ge=0, le=127)
    velocity: int = Field(ge=1, le=127)
    source_event_time_s: float | None = None
    time_s: float | None = None


class HeartbeatMidiOutput(StrictModel):
    file: str
    sha256: str | None = None
    events: list[HeartbeatEvent] = Field(min_length=1)


class HeartbeatMidiData(StrictModel):
    target_bpm: float = Field(gt=0)
    ppq: int = Field(gt=0)
    outputs: dict[TimeSignature, HeartbeatMidiOutput]
    format: int | None = None
    channel_human_number: int | None = None
    channel_zero_based: int | None = None
    equal_note_value: str | None = None
    velocity_mode: str | None = None
    mapping: dict[str, Any] | None = None


class HeartbeatManifest(StrictModel):
    midi: HeartbeatMidiData
    exporter: dict[str, Any] | None = None
    created_utc: str | None = None
    inputs: dict[str, Any] | None = None
    soundfont: dict[str, Any] | None = None
    environment: dict[str, Any] | None = None
    standards_and_software: list[dict[str, Any]] | None = None


class MotifAsset(StrictModel):
    motif_id: Identifier
    theme_family_id: ThemeFamilyId
    midi_file: str
    sha256: str
    bars: int = Field(ge=1, le=64)
    ppq: int = Field(gt=0)
    time_signature: TimeSignature
    source_track_index: int | None = Field(default=None, ge=0)


class MotifManifest(StrictModel):
    schema_version: Literal["0.1-draft"] = "0.1-draft"
    story_id: Identifier
    motifs: list[MotifAsset] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_ids(self) -> "MotifManifest":
        ids = [item.motif_id for item in self.motifs]
        if len(ids) != len(set(ids)):
            raise ValueError("motifs.motif_id must be unique")
        return self
