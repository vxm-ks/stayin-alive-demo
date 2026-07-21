"""Deterministic LegaSynth scaffold compiler for MIDI-GPT."""

from .builder import BuildResult, build_scaffold, build_scaffold_directory
from .contract import validate_with_engine
from .errors import ScaffoldValidationError
from .models import MusicPlan

__all__ = [
    "BuildResult",
    "MusicPlan",
    "ScaffoldValidationError",
    "build_scaffold",
    "build_scaffold_directory",
    "validate_with_engine",
]
