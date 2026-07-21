"""Stage 1 story-to-form planning agent."""

from .agent import Stage1StoryAgent
from .backends import DeepSeekBackend, FakeBackend
from .form import compile_form, make_form_label
from .models import (
    ContentPlan,
    FormCompilation,
    FormPlan,
    FormRelation,
    LLMFormSection,
    MaterialSource,
    StoryPlanConstraints,
    StoryPlanRequest,
    VariationTask,
)
from .musecoco_encoder import (
    MuseCocoEncodingError,
    build_encoding_files,
    build_encoding_payloads,
    encode_targets,
    flatten_labels,
    labels_to_tokens,
)
from .key_normalizer import (
    DetectedKey,
    KeyNormalizationError,
    KeyNormalizationResult,
    detect_key_from_profile,
    normalize_midi_key,
)
from .bar_normalizer import BarNormalizationError, BarNormalizationResult, normalize_midi_bars
from .musecoco_postprocess import (
    MuseCocoPostprocessError,
    MuseCocoPostprocessResult,
    finalize_musecoco_results,
)

__all__ = [
    "ContentPlan",
    "BarNormalizationError",
    "BarNormalizationResult",
    "DeepSeekBackend",
    "DetectedKey",
    "FakeBackend",
    "FormCompilation",
    "FormPlan",
    "FormRelation",
    "LLMFormSection",
    "KeyNormalizationError",
    "KeyNormalizationResult",
    "MaterialSource",
    "MuseCocoEncodingError",
    "MuseCocoPostprocessError",
    "MuseCocoPostprocessResult",
    "StoryPlanConstraints",
    "StoryPlanRequest",
    "Stage1StoryAgent",
    "VariationTask",
    "build_encoding_files",
    "build_encoding_payloads",
    "compile_form",
    "detect_key_from_profile",
    "encode_targets",
    "flatten_labels",
    "finalize_musecoco_results",
    "labels_to_tokens",
    "make_form_label",
    "normalize_midi_key",
    "normalize_midi_bars",
]
