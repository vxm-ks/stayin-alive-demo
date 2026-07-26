"""Dependency-free policy for accepting or retrying one MIDI-GPT block."""

from __future__ import annotations


# A first unsuitable candidate may be resampled twice.
MAX_BLOCK_RETRIES = 2
MAX_BLOCK_ATTEMPTS = 1 + MAX_BLOCK_RETRIES


def candidate_gate_action(
    *,
    attempt_number: int,
    total_attempts: int,
    has_notes: bool,
    melody_ok: bool,
) -> str:
    """Return the soft quality-gate action for one generated block."""

    if has_notes and melody_ok:
        return "accept"
    if attempt_number < total_attempts:
        return "retry"
    if has_notes:
        return "soft_accept"
    return "exhausted"
