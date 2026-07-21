"""Optional runtime contract validation against an actual MIDI-GPT engine."""

from __future__ import annotations

from typing import Any, Mapping


def validate_with_engine(
    engine: Any,
    score_dict: Mapping[str, Any],
    request_dict: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse and validate compiled JSON with the loaded checkpoint.

    This performs MIDI-GPT's real ``from_dict`` and ``validate_request`` path
    without running model sampling.  It intentionally rejects piece-level
    controls because MIDI-GPT 0.3.2 drops them while normalising the request.
    """

    try:
        from midigpt import Score
        from midigpt.inference import GenerationRequest, validate_request
    except ImportError as exc:
        raise RuntimeError(
            "Install the pinned MIDI-GPT runtime before checkpoint validation"
        ) from exc

    score = Score.from_dict(dict(score_dict))
    request = GenerationRequest.from_dict(dict(request_dict))
    if request.controls:
        raise ValueError(
            "MIDI-GPT 0.3.2 loses piece-level controls during request validation"
        )
    validated = validate_request(
        request,
        score,
        engine._tokenizer._vocab.config(),
        engine._analyzer,
    )
    return {
        "valid": True,
        "score": score.to_dict(),
        "request": {
            "track_count": len(validated.tracks),
            "model_dim": validated.config.model_dim,
            "controls": dict(validated.controls),
        },
        "checkpoint": {
            "resolution": engine._tokenizer._vocab.config().resolution,
            "attributes": engine._analyzer.attribute_sizes(),
        },
    }
