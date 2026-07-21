"""Deterministic separation of MuseCoco requested and delivered bar lengths."""

from __future__ import annotations

import copy
from typing import Any


DEFAULT_OUTPUT_BARS = 8
DEFAULT_GENERATION_BARS = 12


def apply_musecoco_output_bars(payload: Any, output_bars: int) -> Any:
    """Force every LLM theme seed to the configured delivered motif length."""

    if not isinstance(payload, dict):
        return payload
    normalized = copy.deepcopy(payload)
    families = normalized.get("theme_families")
    if not isinstance(families, list):
        return normalized
    for family in families:
        if isinstance(family, dict):
            family["seed_bars"] = output_bars
    return normalized
