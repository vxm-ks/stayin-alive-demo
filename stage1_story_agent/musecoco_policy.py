"""Project-level MuseCoco restrictions layered over the immutable official codebook."""

from __future__ import annotations

from collections.abc import Sequence


POLICY_VERSION = "musecoco-project-policy-v1"
BLOCKED_INSTRUMENTS = frozenset({"synthesizer"})
BLOCKED_ARTISTS = frozenset({"stravinsky"})


def validate_musecoco_policy(instruments: Sequence[str], artist: str) -> None:
    blocked_instruments = sorted(set(instruments).intersection(BLOCKED_INSTRUMENTS))
    if blocked_instruments:
        raise ValueError(
            "project policy forbids MuseCoco instruments: "
            + ", ".join(blocked_instruments)
        )
    if artist in BLOCKED_ARTISTS:
        raise ValueError(f"project policy forbids MuseCoco artist: {artist}")
