"""Stage 3 unified whole-MIDI renderer."""

from .heartbeat_conditioning import (
    HeartbeatConditioningAudit,
    HeartbeatConditioningError,
    HeartbeatConditioningProfile,
    HeartbeatConditioningResult,
    available_conditioning_profiles,
    condition_heartbeat_audio,
    condition_heartbeat_wav,
    resolve_conditioning_profile,
)
from .renderer import (
    MidiInspection,
    RenderResult,
    Stage3RenderError,
    inspect_complete_midi,
    render_complete_midi,
    validate_render_inputs,
)
from .package_renderer import (
    PackageRenderResult,
    load_render_plan,
    parse_package_binding,
    render_with_packages,
)
from .batch_validator import (
    BatchValidationResult,
    LoudnessLimits,
    batch_validate_midis,
    integrated_loudness_lufs,
    true_peak_dbtp,
)

__all__ = [
    "HeartbeatConditioningAudit",
    "HeartbeatConditioningError",
    "HeartbeatConditioningProfile",
    "HeartbeatConditioningResult",
    "BatchValidationResult",
    "LoudnessLimits",
    "MidiInspection",
    "PackageRenderResult",
    "RenderResult",
    "Stage3RenderError",
    "available_conditioning_profiles",
    "batch_validate_midis",
    "condition_heartbeat_audio",
    "condition_heartbeat_wav",
    "inspect_complete_midi",
    "integrated_loudness_lufs",
    "load_render_plan",
    "parse_package_binding",
    "render_complete_midi",
    "render_with_packages",
    "resolve_conditioning_profile",
    "true_peak_dbtp",
    "validate_render_inputs",
]
