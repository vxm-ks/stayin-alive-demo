#!/usr/bin/env python3
"""Inspect one processed heartbeat WAV, then render bars at a chosen BPM.

The input is any one of the nine WAV variants written by ``heart_extraction``.
This module reads that file without changing it, estimates its current heart
rhythm, prints a conservative target-BPM range, and asks the user to choose a
target. Complete real S1/S2 events are repositioned without time stretching.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import scipy
from scipy import signal as scipy_signal

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from heart_extraction import audio_diagnostics as diagnostics
from heart_extraction import heart_sound_processor as extraction


VERSION = "1.6.0"
METERS: tuple[tuple[str, int, int], ...] = (
    ("4-4", 4, 4),
    ("3-4", 3, 4),
    ("2-4", 2, 4),
)
INPUT_VARIANTS: tuple[str, ...] = (
    "stable_segment_raw",
    "stable_segment_clean",
    "stable_segment_enhanced",
    "authentic_loop",
    "authentic_loop_clean",
    "authentic_loop_enhanced",
    "regularized_events",
    "regularized_events_clean",
    "regularized_events_enhanced",
)


@dataclass(frozen=True)
class ClipExemplar:
    event_type: str
    cycle_index: int
    source_path: Path
    source_time_s: float
    sample_rate: int
    raw: np.ndarray
    alignment_offset_samples: int
    detected_peak_offset_samples: int
    refined_onset_offset_samples: int
    onset_shift_ms: float
    boundary_jump_abs: float
    clip_peak: float
    clip_rms: float
    quality_score: float
    confidence: float
    amplitude: float


@dataclass(frozen=True)
class BpmRecommendation:
    detected_bpm: float
    recommended_min_bpm: float
    recommended_max_bpm: float
    technical_min_bpm: float
    technical_max_bpm: float
    rule: str


@dataclass
class PreparedInput:
    input_path: Path
    input_sha256: str
    prefix: str
    input_variant: str
    sample_rate: int
    duration_s: float
    result: extraction.ProcessingResult
    bank: dict[str, list[ClipExemplar]]
    recommendation: BpmRecommendation
    period_correction: dict[str, Any]


def _bpm_tag(value: float) -> str:
    rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    return rendered.replace(".", "p")


def _linear_peak_normalise(
    signal: np.ndarray, target_dbfs: float = -1.0
) -> tuple[np.ndarray, float]:
    """Apply gain only; return the signal and applied gain in dB."""
    peak = float(np.max(np.abs(signal))) if len(signal) else 0.0
    if peak <= 1e-12:
        raise ValueError("Rendered bar is silent.")
    target = 10.0 ** (target_dbfs / 20.0)
    gain = target / peak
    return signal * gain, 20.0 * math.log10(max(gain, 1e-12))


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-12))


def _true_peak(signal: np.ndarray, oversample: int = 4) -> float:
    """Estimate inter-sample peak with polyphase oversampling."""
    if not len(signal):
        return 0.0
    if oversample <= 1:
        return float(np.max(np.abs(signal)))
    oversampled = scipy_signal.resample_poly(
        np.asarray(signal, dtype=np.float64), oversample, 1
    )
    return float(np.max(np.abs(oversampled)))


def _true_peak_normalise(
    signal: np.ndarray, target_dbtp: float = -1.0, oversample: int = 4
) -> tuple[np.ndarray, float, float]:
    measured = _true_peak(signal, oversample)
    if measured <= 1e-12:
        raise ValueError("Rendered bar is silent.")
    target = 10.0 ** (target_dbtp / 20.0)
    gain = target / measured
    output = np.asarray(signal, dtype=np.float64) * gain
    return output, 20.0 * math.log10(max(gain, 1e-12)), _true_peak(
        output, oversample
    )


def _signal_metrics(signal: np.ndarray, sample_rate: int) -> dict[str, float]:
    values = np.asarray(signal, dtype=np.float64)
    peak = float(np.max(np.abs(values))) if len(values) else 0.0
    rms = float(np.sqrt(np.mean(values * values))) if len(values) else 0.0
    if len(values):
        window = min(len(values), max(1, int(round(0.020 * sample_rate))))
        envelope = np.sqrt(
            np.convolve(
                values * values,
                np.ones(window, dtype=np.float64) / window,
                mode="same",
            )
        )
        threshold = max(10.0 ** (-45.0 / 20.0), 0.05 * peak)
        active = envelope >= threshold
        active_rms = (
            float(np.sqrt(np.mean(values[active] ** 2)))
            if np.any(active)
            else rms
        )
        active_fraction = float(np.mean(active))
    else:
        active_rms = 0.0
        active_fraction = 0.0
    return {
        "sample_peak_dbfs": _dbfs(peak),
        "estimated_true_peak_dbtp": _dbfs(_true_peak(values)),
        "whole_bar_rms_dbfs": _dbfs(rms),
        "active_rms_dbfs": _dbfs(active_rms),
        "active_fraction": active_fraction,
        "active_crest_factor_db": _dbfs(peak) - _dbfs(active_rms),
    }


def _compressor(
    signal: np.ndarray,
    sample_rate: int,
    threshold_dbfs: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    makeup_db: float,
) -> np.ndarray:
    """Feed-forward envelope compressor with attack/release memory."""
    values = np.asarray(signal, dtype=np.float64)
    attack = math.exp(-1.0 / max(1.0, 0.001 * attack_ms * sample_rate))
    release = math.exp(-1.0 / max(1.0, 0.001 * release_ms * sample_rate))
    envelope = 0.0
    gain = np.empty_like(values)
    slope = 1.0 - 1.0 / ratio
    for index, absolute in enumerate(np.abs(values)):
        coefficient = attack if absolute > envelope else release
        envelope = coefficient * envelope + (1.0 - coefficient) * float(absolute)
        level_db = _dbfs(envelope)
        reduction_db = -slope * max(0.0, level_db - threshold_dbfs)
        gain[index] = 10.0 ** ((reduction_db + makeup_db) / 20.0)
    return values * gain


def _parallel_compress(
    signal: np.ndarray,
    sample_rate: int,
    wet: float,
    *,
    threshold_dbfs: float = -18.0,
    ratio: float = 2.0,
    attack_ms: float = 12.0,
    release_ms: float = 100.0,
    makeup_db: float = 5.0,
) -> np.ndarray:
    compressed = _compressor(
        signal,
        sample_rate,
        threshold_dbfs,
        ratio,
        attack_ms,
        release_ms,
        makeup_db,
    )
    return (1.0 - wet) * signal + wet * compressed


def _bandpass(
    signal: np.ndarray, sample_rate: int, low_hz: float, high_hz: float
) -> np.ndarray:
    nyquist = 0.5 * sample_rate
    low = max(5.0, min(low_hz, nyquist * 0.70))
    high = max(low + 5.0, min(high_hz, nyquist * 0.92))
    sos = scipy_signal.butter(3, (low, high), btype="bandpass", fs=sample_rate, output="sos")
    values = np.asarray(signal, dtype=np.float64)
    try:
        return scipy_signal.sosfiltfilt(sos, values)
    except ValueError:
        return scipy_signal.sosfilt(sos, values)


def _rms_match(layer: np.ndarray, reference: np.ndarray) -> tuple[np.ndarray, float]:
    layer_rms = float(np.sqrt(np.mean(layer * layer)))
    reference_rms = float(np.sqrt(np.mean(reference * reference)))
    if layer_rms <= 1e-12 or reference_rms <= 1e-12:
        return np.zeros_like(reference), 0.0
    gain = min(reference_rms / layer_rms, 10.0 ** (18.0 / 20.0))
    return layer * gain, 20.0 * math.log10(max(gain, 1e-12))


def _apply_loudness_mode(
    rendered_raw: np.ndarray,
    sample_rate: int,
    beat_peak: str,
    loudness_mode: str,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Apply an auditable post-render loudness/presence profile."""
    if loudness_mode not in {"safe", "loud", "cinematic"}:
        raise ValueError("loudness_mode must be one of: safe, loud, cinematic")

    legacy_target = -3.0 if beat_peak in {"s1", "s2"} else -1.0
    base, base_gain_db = _linear_peak_normalise(rendered_raw, legacy_target)
    before = _signal_metrics(base, sample_rate)
    common: dict[str, Any] = {
        "mode": loudness_mode,
        "source_derivation_policy": "all layers derived only from the selected heartbeat",
        "initial_sample_peak_target_dbfs": legacy_target,
        "initial_linear_gain_db": base_gain_db,
        "metrics_before_mode": before,
    }
    if loudness_mode == "safe":
        output = base
        common.update(
            {
                "dynamic_compression_applied": False,
                "cinematic_layers_applied": False,
                "final_control": "legacy sample-peak normalisation",
                "final_target_dbfs": legacy_target,
            }
        )
    else:
        compressor = {
            "topology": "parallel feed-forward",
            "threshold_dbfs": -18.0,
            "ratio": 4.0,
            "attack_ms": 0.2,
            "release_ms": 100.0,
            "makeup_db": 9.0 if loudness_mode == "loud" else 8.0,
            "wet_fraction": 0.30 if loudness_mode == "loud" else 0.25,
            "transient_policy": (
                "fast compressed branch; uncompressed dry branch preserves attack"
            ),
        }
        if loudness_mode == "loud":
            enhanced = _parallel_compress(
                base,
                sample_rate,
                float(compressor["wet_fraction"]),
                threshold_dbfs=float(compressor["threshold_dbfs"]),
                ratio=float(compressor["ratio"]),
                attack_ms=float(compressor["attack_ms"]),
                release_ms=float(compressor["release_ms"]),
                makeup_db=float(compressor["makeup_db"]),
            )
            layers: dict[str, Any] = {
                "dry_fraction": 1.0,
                "body_fraction": 0.0,
                "harmonic_fraction": 0.0,
            }
        else:
            body, body_match_db = _rms_match(
                _bandpass(base, sample_rate, 45.0, 120.0), base
            )
            saturated = np.tanh(2.2 * base) / math.tanh(2.2)
            harmonic, harmonic_match_db = _rms_match(
                _bandpass(saturated - base, sample_rate, 120.0, 900.0), base
            )
            layered = 0.70 * base + 0.20 * body + 0.10 * harmonic
            enhanced = _parallel_compress(
                layered,
                sample_rate,
                float(compressor["wet_fraction"]),
                threshold_dbfs=float(compressor["threshold_dbfs"]),
                ratio=float(compressor["ratio"]),
                attack_ms=float(compressor["attack_ms"]),
                release_ms=float(compressor["release_ms"]),
                makeup_db=float(compressor["makeup_db"]),
            )
            layers = {
                "dry_fraction": 0.70,
                "body_fraction": 0.20,
                "body_band_hz": [45.0, 120.0],
                "body_rms_match_gain_db": body_match_db,
                "harmonic_fraction": 0.10,
                "harmonic_band_hz": [120.0, 900.0],
                "harmonic_drive": 2.2,
                "harmonic_rms_match_gain_db": harmonic_match_db,
            }
        output, final_gain_db, final_true_peak = _true_peak_normalise(
            enhanced, -1.0, 4
        )
        common.update(
            {
                "dynamic_compression_applied": True,
                "cinematic_layers_applied": loudness_mode == "cinematic",
                "parallel_compressor": compressor,
                "layers": layers,
                "final_control": "4x oversampled true-peak normalisation",
                "final_target_dbtp": -1.0,
                "final_gain_db": final_gain_db,
                "measured_final_true_peak_dbtp": _dbfs(final_true_peak),
            }
        )
    common["metrics_after_mode"] = _signal_metrics(output, sample_rate)
    return np.ascontiguousarray(output), common


def _nearest_zero_crossing(
    signal: np.ndarray, index: int, radius: int
) -> int:
    """Return the lowest-amplitude zero crossing near an energy onset."""
    left = max(0, index - radius)
    right = min(len(signal) - 1, index + radius)
    if right <= left:
        return min(max(index, 0), len(signal) - 1)
    local = signal[left : right + 1]
    crossings = np.flatnonzero(local[:-1] * local[1:] <= 0.0)
    if len(crossings):
        candidates: list[int] = []
        for crossing in crossings:
            a = left + int(crossing)
            b = min(a + 1, len(signal) - 1)
            candidates.append(a if abs(signal[a]) <= abs(signal[b]) else b)
        return min(candidates, key=lambda item: (abs(item - index), abs(signal[item])))
    return left + int(np.argmin(np.abs(local)))


def _prepare_event_clip(
    clip: np.ndarray,
    detected_peak_offset: int,
    sample_rate: int,
) -> tuple[np.ndarray, int, dict[str, float | int]]:
    """Find the attack before the detected peak and taper only its boundaries."""
    values = np.ascontiguousarray(clip, dtype=np.float64)
    peak_offset = min(max(1, detected_peak_offset), len(values) - 1)
    envelope_window = max(1, int(round(0.002 * sample_rate)))
    envelope = np.convolve(
        np.abs(values),
        np.ones(envelope_window, dtype=np.float64) / envelope_window,
        mode="same",
    )
    search_envelope = envelope[: peak_offset + 1]
    noise_count = min(
        len(search_envelope), max(2, int(round(0.008 * sample_rate)))
    )
    noise_floor = float(np.median(search_envelope[:noise_count]))
    peak_envelope = max(float(np.max(search_envelope)), 1e-12)
    onset_threshold = max(2.0 * noise_floor, 0.15 * peak_envelope)
    above = search_envelope >= onset_threshold
    sustain = max(1, int(round(0.0015 * sample_rate)))
    sustained = np.convolve(
        above.astype(np.int16), np.ones(sustain, dtype=np.int16), mode="same"
    ) >= max(1, sustain // 2)
    candidates = np.flatnonzero(sustained)
    energy_onset = int(candidates[0]) if len(candidates) else max(0, peak_offset - sustain)
    zero_radius = max(1, int(round(0.002 * sample_rate)))
    refined_onset = _nearest_zero_crossing(values, energy_onset, zero_radius)

    pre_roll = max(1, int(round(0.005 * sample_rate)))
    start = max(0, refined_onset - pre_roll)
    prepared = values[start:].copy()
    alignment = refined_onset - start

    fade_in = min(len(prepared), max(2, alignment + 1))
    if fade_in > 1:
        prepared[:fade_in] *= np.sin(
            np.linspace(0.0, np.pi / 2.0, fade_in)
        ) ** 2
    fade_out = min(len(prepared), max(2, int(round(0.010 * sample_rate))))
    if fade_out > 1:
        prepared[-fade_out:] *= np.cos(
            np.linspace(0.0, np.pi / 2.0, fade_out)
        ) ** 2
    prepared[0] = 0.0
    prepared[-1] = 0.0
    peak = float(np.max(np.abs(prepared)))
    rms = float(np.sqrt(np.mean(prepared * prepared)))
    details: dict[str, float | int] = {
        "detected_peak_offset_samples": peak_offset,
        "energy_onset_offset_samples": energy_onset,
        "refined_onset_offset_samples": refined_onset,
        "alignment_offset_samples": alignment,
        "onset_shift_ms": 1000.0 * (refined_onset - peak_offset) / sample_rate,
        "onset_threshold": onset_threshold,
        "noise_floor": noise_floor,
        "boundary_jump_abs": float(abs(prepared[0])),
        "clip_peak": peak,
        "clip_rms": rms,
        "fade_in_ms": 1000.0 * fade_in / sample_rate,
        "fade_out_ms": 1000.0 * fade_out / sample_rate,
    }
    return np.ascontiguousarray(prepared), alignment, details


def _score_exemplars(exemplars: list[ClipExemplar]) -> list[ClipExemplar]:
    median_peak = max(float(np.median([item.clip_peak for item in exemplars])), 1e-12)
    median_rms = max(float(np.median([item.clip_rms for item in exemplars])), 1e-12)
    scored: list[ClipExemplar] = []
    for item in exemplars:
        peak_similarity = math.exp(-abs(math.log(max(item.clip_peak, 1e-12) / median_peak)))
        rms_similarity = math.exp(-abs(math.log(max(item.clip_rms, 1e-12) / median_rms)))
        score = 0.50 * item.confidence + 0.25 * peak_similarity + 0.25 * rms_similarity
        scored.append(replace(item, quality_score=float(score)))
    return sorted(scored, key=lambda item: item.source_time_s)


def _analysis_config(duration_s: float) -> extraction.Config:
    if duration_s < 4.0:
        raise ValueError(
            "The selected WAV is shorter than 4 seconds. Choose one of the nine "
            "Stable/Authentic/Regularized outputs from heart_extraction."
        )
    stable_max = min(12.0, duration_s)
    stable_target = min(10.0, stable_max)
    stable_min = min(8.0, stable_target)
    return extraction.Config(
        stable_min_s=stable_min,
        stable_target_s=stable_target,
        stable_max_s=stable_max,
        stable_min_cycles=4,
    )


def _effective_s2_phase(
    source_s2_phase: float,
    beat_peak: str,
    pair_rhythm: str,
) -> float:
    if pair_rhythm not in {"natural", "even"}:
        raise ValueError("pair_rhythm must be one of: natural, even")
    if pair_rhythm == "even":
        if beat_peak != "both":
            raise ValueError(
                "pair_rhythm=even requires beat_peak=both because both S1 and S2 "
                "are needed for equal spacing."
            )
        return 0.5
    return source_s2_phase


def _recommend_bpm(
    detected_bpm: float,
    s2_phase: float,
    beat_peak: str = "both",
    pair_rhythm: str = "natural",
) -> BpmRecommendation:
    rendered_s2_phase = _effective_s2_phase(s2_phase, beat_peak, pair_rhythm)
    technical_min = 30.0
    if beat_peak == "both":
        technical_max = min(
            300.0,
            60.0 * min(rendered_s2_phase, 1.0 - rendered_s2_phase) / 0.080,
        )
        spacing_rule = "S1/S2 onset-spacing safety limit"
    elif beat_peak in {"s1", "s2"}:
        technical_max = 300.0
        spacing_rule = "single-peak 300 BPM project ceiling"
    else:
        raise ValueError("beat_peak must be one of: both, s1, s2")
    recommended_min = max(40.0, detected_bpm * 0.80)
    recommended_max = min(180.0, detected_bpm * 1.25, technical_max * 0.95)
    if recommended_max < recommended_min:
        recommended_min = max(technical_min, min(detected_bpm, technical_max * 0.80))
        recommended_max = technical_max * 0.95
    return BpmRecommendation(
        detected_bpm=detected_bpm,
        recommended_min_bpm=round(recommended_min, 1),
        recommended_max_bpm=round(recommended_max, 1),
        technical_min_bpm=technical_min,
        technical_max_bpm=round(technical_max, 1),
        rule=(
            "project heuristic: 80%-125% of detected BPM, bounded to 40-180 BPM "
            f"and 95% of the {spacing_rule}; beat_peak={beat_peak}; "
            f"pair_rhythm={pair_rhythm}"
        ),
    )


def _analyse_with_period_correction(
    signal: np.ndarray,
    sample_rate: int,
    config: extraction.Config,
) -> tuple[
    extraction.ProcessingResult,
    np.ndarray,
    dict[str, Any],
]:
    """Rerun in a narrow range when cycle fitting selects an octave error."""
    result, centred, _, _ = extraction.analyse(signal, sample_rate, config)
    correction: dict[str, Any] = {
        "applied": False,
        "initial_bpm": result.bpm,
        "candidate_bpm": result.bpm,
        "reason": "final cycle period agrees with strongest autocorrelation candidate",
    }
    if not result.period_candidates:
        return result, centred, correction
    strongest = result.period_candidates[0]
    candidate_bpm = float(strongest["bpm"])
    candidate_strength = float(strongest["autocorrelation"])
    disagreement = abs(candidate_bpm - result.bpm) / max(candidate_bpm, 1e-12)
    correction.update(
        {
            "candidate_bpm": candidate_bpm,
            "candidate_autocorrelation": candidate_strength,
            "relative_disagreement": disagreement,
        }
    )
    if candidate_strength < 0.45 or disagreement <= 0.18:
        return result, centred, correction
    narrowed = replace(
        config,
        min_bpm=max(30.0, candidate_bpm * 0.75),
        max_bpm=min(240.0, candidate_bpm * 1.25),
    )
    corrected, corrected_centred, _, _ = extraction.analyse(
        signal, sample_rate, narrowed
    )
    correction.update(
        {
            "applied": True,
            "corrected_bpm": corrected.bpm,
            "reason": (
                "cycle fitting disagreed with a strong autocorrelation candidate; "
                "analysis rerun around the strongest candidate to reject half/double tempo"
            ),
        }
    )
    corrected.warnings.append("half_or_double_tempo_candidate_corrected")
    return corrected, corrected_centred, correction


def inspect_wav(input_path: Path) -> PreparedInput:
    """Read and analyse one processed WAV without writing beside the input."""
    resolved = input_path.resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".wav":
        raise ValueError("Input must be one WAV file produced by heart_extraction.")
    matching_variants = [
        variant
        for variant in INPUT_VARIANTS
        if resolved.stem == variant or resolved.stem.endswith(f"_{variant}")
    ]
    if not matching_variants:
        raise ValueError(
            "The WAV filename does not match one of the nine supported "
            "Stable/Authentic/Regularized outputs."
        )
    input_variant = max(matching_variants, key=len)
    audio = diagnostics.load_wav(resolved)
    config = _analysis_config(audio.duration_s)
    result, centred, period_correction = _analyse_with_period_correction(
        audio.signal, audio.sample_rate, config
    )
    if result.status == "REJECT":
        raise ValueError(
            f"Heartbeat analysis rejected the selected WAV (confidence={result.confidence:.3f})."
        )
    source_bank = extraction.build_event_clip_bank(
        centred, audio.sample_rate, result
    )
    bank: dict[str, list[ClipExemplar]] = {"S1": [], "S2": []}
    for event_type in ("S1", "S2"):
        if not source_bank[event_type]:
            raise ValueError(f"No vetted {event_type} exemplar is available.")
        for clip, onset, event in source_bank[event_type]:
            prepared_clip, alignment, clip_details = _prepare_event_clip(
                clip, onset, audio.sample_rate
            )
            bank[event_type].append(
                ClipExemplar(
                    event_type=event_type,
                    cycle_index=event.cycle_index,
                    source_path=resolved,
                    source_time_s=event.time_s,
                    sample_rate=audio.sample_rate,
                    raw=prepared_clip,
                    alignment_offset_samples=alignment,
                    detected_peak_offset_samples=int(
                        clip_details["detected_peak_offset_samples"]
                    ),
                    refined_onset_offset_samples=int(
                        clip_details["refined_onset_offset_samples"]
                    ),
                    onset_shift_ms=float(clip_details["onset_shift_ms"]),
                    boundary_jump_abs=float(clip_details["boundary_jump_abs"]),
                    clip_peak=float(clip_details["clip_peak"]),
                    clip_rms=float(clip_details["clip_rms"]),
                    quality_score=0.0,
                    confidence=event.confidence,
                    amplitude=event.amplitude,
                )
            )
        bank[event_type] = _score_exemplars(bank[event_type])
    phase = result.s1_s2_s / result.period_s
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", resolved.stem).strip("_")
    return PreparedInput(
        input_path=resolved,
        input_sha256=diagnostics.file_sha256(resolved),
        prefix=safe_prefix or "processed_heartbeat",
        input_variant=input_variant,
        sample_rate=audio.sample_rate,
        duration_s=audio.duration_s,
        result=result,
        bank=bank,
        recommendation=_recommend_bpm(result.bpm, phase),
        period_correction=period_correction,
    )


def _quality_policy(
    target_bpm: float,
    s2_phase: float,
    beat_peak: str = "both",
    pair_rhythm: str = "natural",
) -> dict[str, Any]:
    if not 20.0 <= target_bpm <= 300.0:
        raise ValueError("Target BPM must be between 20 and 300.")
    rendered_s2_phase = _effective_s2_phase(s2_phase, beat_peak, pair_rhythm)
    beat_s = 60.0 / target_bpm
    if beat_peak == "both":
        s1_to_s2_s: float | None = beat_s * rendered_s2_phase
        s2_to_next_s1_s: float | None = beat_s - s1_to_s2_s
        minimum_spacing = min(s1_to_s2_s, s2_to_next_s1_s)
    elif beat_peak in {"s1", "s2"}:
        s1_to_s2_s = None
        s2_to_next_s1_s = None
        minimum_spacing = beat_s
    else:
        raise ValueError("beat_peak must be one of: both, s1, s2")
    hard_minimum = 0.080
    if minimum_spacing < hard_minimum:
        if beat_peak == "both":
            max_safe_bpm = (
                60.0
                * min(rendered_s2_phase, 1.0 - rendered_s2_phase)
                / hard_minimum
            )
        else:
            max_safe_bpm = 60.0 / hard_minimum
        raise ValueError(
            f"Target BPM compresses S1/S2 onset spacing below {hard_minimum:.3f}s. "
            f"For this recording phase, use at most about {max_safe_bpm:.1f} BPM."
        )
    warnings: list[str] = []
    if minimum_spacing < 0.120:
        warnings.append("short_inter_event_spacing_possible_tail_overlap")
    return {
        "beat_duration_s": beat_s,
        "beat_peak": beat_peak,
        "pair_rhythm": pair_rhythm,
        "rendered_s2_phase_ratio": rendered_s2_phase,
        "equal_inter_onset_spacing": pair_rhythm == "even",
        "s1_to_s2_s": s1_to_s2_s,
        "s2_to_next_s1_s": s2_to_next_s1_s,
        "minimum_onset_spacing_s": minimum_spacing,
        "hard_minimum_onset_spacing_s": hard_minimum,
        "warnings": warnings,
    }


def _place_clip(
    output: np.ndarray, start: int, clip: np.ndarray, sample_rate: int
) -> bool:
    if start >= len(output):
        return True
    source_start = max(0, -start)
    destination_start = max(0, start)
    available = len(output) - destination_start
    used = min(available, len(clip) - source_start)
    if used <= 0:
        return True
    placed = clip[source_start : source_start + used].copy()
    head_truncated = source_start > 0
    if head_truncated and used > 2:
        fade = min(used // 3, max(2, int(round(0.005 * sample_rate))))
        placed[:fade] *= np.sin(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
        placed[0] = 0.0
    tail_truncated = source_start + used < len(clip)
    if tail_truncated and used > 2:
        fade = min(used // 3, max(2, int(round(0.006 * sample_rate))))
        placed[-fade:] *= np.cos(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
    output[destination_start : destination_start + used] += placed
    return tail_truncated


def _render_bar(
    bank: dict[str, list[ClipExemplar]],
    sample_rate: int,
    beats: int,
    beat_s: float,
    s2_phase: float,
    meter: str,
    beat_peak: str = "both",
    event_bank_mode: str = "auto",
    pair_rhythm: str = "natural",
) -> tuple[np.ndarray, list[dict[str, Any]], int]:
    length = int(round(beats * beat_s * sample_rate))
    output = np.zeros(max(1, length), dtype=np.float64)
    rows: list[dict[str, Any]] = []
    truncated_count = 0
    sequence_index = {"S1": 0, "S2": 0}
    rendered_s2_phase = _effective_s2_phase(s2_phase, beat_peak, pair_rhythm)
    if beat_peak == "both":
        event_offsets = (("S1", 0.0), ("S2", beat_s * rendered_s2_phase))
    elif beat_peak in {"s1", "s2"}:
        event_offsets = ((beat_peak.upper(), 0.0),)
    else:
        raise ValueError("beat_peak must be one of: both, s1, s2")
    if event_bank_mode == "auto":
        effective_bank_mode = "best" if beat_peak in {"s1", "s2"} else "rotate"
    elif event_bank_mode in {"best", "rotate"}:
        effective_bank_mode = event_bank_mode
    else:
        raise ValueError("event_bank_mode must be one of: auto, best, rotate")
    for beat_index in range(beats):
        for event_type, offset in event_offsets:
            exemplars = bank[event_type]
            if effective_bank_mode == "best":
                exemplar = max(exemplars, key=lambda item: item.quality_score)
            else:
                exemplar = exemplars[sequence_index[event_type] % len(exemplars)]
            sequence_index[event_type] += 1
            clip = exemplar.raw
            time_s = beat_index * beat_s + offset
            sample = int(round(time_s * sample_rate))
            placement_start = sample - exemplar.alignment_offset_samples
            truncated = _place_clip(output, placement_start, clip, sample_rate)
            truncated_count += int(truncated)
            rows.append(
                {
                    "meter": meter.replace("-", "/"),
                    "beat_peak_mode": beat_peak,
                    "pair_rhythm": pair_rhythm,
                    "event_bank_mode": effective_bank_mode,
                    "beat_index": beat_index + 1,
                    "event_type": event_type,
                    "time_s": time_s,
                    "sample_index": sample,
                    "placement_start_sample": placement_start,
                    "head_preroll_truncated_at_bar_start": placement_start < 0,
                    "source_cycle_index": exemplar.cycle_index,
                    "source_clip": exemplar.source_path.name,
                    "source_event_time_s": exemplar.source_time_s,
                    "source_confidence": exemplar.confidence,
                    "source_amplitude": exemplar.amplitude,
                    "exemplar_quality_score": exemplar.quality_score,
                    "exemplar_onset_shift_ms": exemplar.onset_shift_ms,
                    "exemplar_alignment_offset_samples": exemplar.alignment_offset_samples,
                    "exemplar_boundary_jump_abs": exemplar.boundary_jump_abs,
                    "tail_truncated_at_bar_end": truncated,
                }
            )
    return output, rows, truncated_count


def _measure_boundary_quality(
    signal: np.ndarray, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    placement_jumps: list[float] = []
    onset_jumps: list[float] = []
    for row in rows:
        placement = max(0, int(row["placement_start_sample"]))
        onset = max(0, int(row["sample_index"]))
        placement_jump = (
            float(abs(signal[placement]))
            if placement == 0
            else float(abs(signal[placement] - signal[placement - 1]))
        )
        onset_jump = (
            float(abs(signal[onset]))
            if onset == 0
            else float(abs(signal[onset] - signal[onset - 1]))
        )
        placement_jumps.append(placement_jump)
        onset_jumps.append(onset_jump)
        row["placement_boundary_jump_abs"] = placement_jump
        row["aligned_onset_jump_abs"] = onset_jump
    maximum_boundary_jump = max(placement_jumps + onset_jumps, default=0.0)
    threshold = 0.030
    return {
        "maximum_placement_boundary_jump_abs": max(placement_jumps, default=0.0),
        "maximum_aligned_onset_jump_abs": max(onset_jumps, default=0.0),
        "maximum_checked_boundary_jump_abs": maximum_boundary_jump,
        "acceptance_threshold_abs": threshold,
        "status": "PASS" if maximum_boundary_jump <= threshold else "WARNING",
        "maximum_sample_to_sample_jump_abs": (
            float(np.max(np.abs(np.diff(signal)))) if len(signal) > 1 else 0.0
        ),
    }


def render_prepared(
    prepared: PreparedInput,
    target_bpm: float,
    output_root: Path,
    beat_peak: str = "both",
    event_bank_mode: str = "auto",
    loudness_mode: str = "safe",
    pair_rhythm: str = "natural",
) -> tuple[Path, list[Path]]:
    period_s = prepared.result.period_s
    s1_s2_s = prepared.result.s1_s2_s
    if period_s <= 0 or not 0 < s1_s2_s < period_s:
        raise ValueError("Selected WAV contains an invalid heart-cycle timing model.")
    source_s2_phase = s1_s2_s / period_s
    if not 0.15 <= source_s2_phase <= 0.75:
        raise ValueError(f"Implausible S2 phase ratio: {source_s2_phase:.4f}")
    rendered_s2_phase = _effective_s2_phase(
        source_s2_phase, beat_peak, pair_rhythm
    )
    quality = _quality_policy(
        target_bpm, source_s2_phase, beat_peak, pair_rhythm
    )
    sample_rate = prepared.sample_rate
    bank = prepared.bank
    if event_bank_mode == "auto":
        effective_bank_mode = "best" if beat_peak in {"s1", "s2"} else "rotate"
    elif event_bank_mode in {"best", "rotate"}:
        effective_bank_mode = event_bank_mode
    else:
        raise ValueError("event_bank_mode must be one of: auto, best, rotate")
    prefix = prepared.prefix
    tag = _bpm_tag(target_bpm)
    peak_tag = "" if beat_peak == "both" else f"_{beat_peak}"
    rhythm_tag = "_even" if pair_rhythm == "even" else ""
    bank_tag = f"_{effective_bank_mode}"
    loudness_tag = "" if loudness_mode == "safe" else f"_{loudness_mode}"
    group_dir = output_root.resolve() / (
        f"{prefix}_bpm{tag}{peak_tag}{rhythm_tag}{bank_tag}{loudness_tag}_bars"
    )
    group_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    meter_reports: dict[str, Any] = {}
    all_rows: list[dict[str, Any]] = []
    beat_s = float(quality["beat_duration_s"])

    for meter_name, beats, denominator in METERS:
        rendered_raw, rows, truncated_count = _render_bar(
            bank,
            sample_rate,
            beats,
            beat_s,
            source_s2_phase,
            meter_name,
            beat_peak,
            effective_bank_mode,
            pair_rhythm,
        )
        rendered, loudness_report = _apply_loudness_mode(
            rendered_raw, sample_rate, beat_peak, loudness_mode
        )
        boundary_quality = _measure_boundary_quality(rendered, rows)
        if boundary_quality["status"] != "PASS":
            warning = f"{meter_name}_event_boundary_jump_exceeds_threshold"
            if warning not in quality["warnings"]:
                quality["warnings"].append(warning)
        annotations = [(row["event_type"], float(row["time_s"])) for row in rows]
        for row in rows:
            row["loudness_mode"] = loudness_mode
        stem = group_dir / (
            f"{prefix}_bpm{tag}{peak_tag}{rhythm_tag}{bank_tag}"
            f"{loudness_tag}_{meter_name}"
        )
        wav_path = stem.with_suffix(".wav")
        extraction.write_wav(wav_path, sample_rate, rendered)
        created.append(wav_path)
        energy_png, energy_svg = diagnostics.render_time_energy_pair(
            Path(f"{stem}_time_energy"),
            f"{prefix} | {target_bpm:g} BPM | {meter_name.replace('-', '/')}",
            sample_rate,
            rendered,
            annotations,
            metadata={
                "Target BPM": f"{target_bpm:g}",
                "Meter": meter_name.replace("-", "/"),
                "Input variant": prepared.input_variant,
                "Beat peak": beat_peak.upper() if beat_peak != "both" else "S1+S2",
                "Pair rhythm": pair_rhythm,
                "Event bank": effective_bank_mode,
                "Loudness mode": loudness_mode,
                "Method": "S1/S2 event repositioning; no time stretching",
            },
        )
        created.extend((energy_png, energy_svg))
        all_rows.extend(rows)
        meter_reports[meter_name.replace("-", "/")] = {
            "numerator": beats,
            "denominator": denominator,
            "nominal_duration_s": beats * beat_s,
            "rendered_duration_s": len(rendered) / sample_rate,
            "sample_count": len(rendered),
            "event_count": len(rows),
            "tail_truncation_count": truncated_count,
            "loudness": loudness_report,
            "boundary_quality": boundary_quality,
            "output": {
                "wav": wav_path.name,
                "wav_sha256": diagnostics.file_sha256(wav_path),
                "time_energy_png": energy_png.name,
                "time_energy_svg": energy_svg.name,
            },
        }

    events_path = group_dir / (
        f"{prefix}_bpm{tag}{peak_tag}{rhythm_tag}{bank_tag}"
        f"{loudness_tag}_bar_events.csv"
    )
    with events_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(all_rows[0].keys()))
        writer.writeheader()
        writer.writerows(all_rows)
    created.append(events_path)

    report = {
        "schema_version": "1.4",
        "renderer": {"name": "tempo_bar_renderer.py", "version": VERSION},
        "source": {
            "selected_wav": str(prepared.input_path),
            "selected_wav_sha256": prepared.input_sha256,
            "selected_wav_duration_s": prepared.duration_s,
            "selected_wav_sample_rate_hz": prepared.sample_rate,
            "selected_variant": prepared.input_variant,
            "output_prefix": prefix,
            "read_only_input_policy": True,
        },
        "timing": {
            "target_bpm": target_bpm,
            "beat_peak": beat_peak,
            "pair_rhythm": pair_rhythm,
            "beat_unit": "quarter note",
            "mapping": (
                "S1 on beat and S2 at half-beat; equal alternating intervals"
                if beat_peak == "both" and pair_rhythm == "even"
                else "S1 on beat and S2 at preserved physiological phase"
                if beat_peak == "both" and pair_rhythm == "natural"
                else f"only {beat_peak.upper()} retained and aligned to each beat"
            ),
            "source_period_s": period_s,
            "source_s1_s2_s": s1_s2_s,
            "source_s2_phase_ratio": source_s2_phase,
            "rendered_s2_phase_ratio": rendered_s2_phase,
            "source_s2_phase_preserved": pair_rhythm == "natural",
            "preserved_s2_phase_ratio": (
                source_s2_phase if pair_rhythm == "natural" else None
            ),
            "equal_inter_onset_spacing": pair_rhythm == "even",
            "time_stretching_applied": False,
            "event_waveform_resampling_applied": False,
        },
        "quality": {
            **quality,
            "warnings": list(quality["warnings"]) + list(prepared.result.warnings),
            "exemplar_counts": {key: len(value) for key, value in bank.items()},
            "detected_confidence": prepared.result.confidence,
            "detected_periodicity": prepared.result.periodicity,
            "period_correction": prepared.period_correction,
            "detected_stable_region": asdict(prepared.result.stable_region),
            "bpm_recommendation": asdict(
                _recommend_bpm(
                    prepared.result.bpm,
                    source_s2_phase,
                    beat_peak,
                    pair_rhythm,
                )
            ),
        },
        "processing": {
            "input_variant_inherited": prepared.input_variant,
            "event_bank_mode": effective_bank_mode,
            "loudness_mode": loudness_mode,
            "pair_rhythm": pair_rhythm,
            "event_waveform_duration_adjustment_applied": False,
            "single_peak_default_event_bank": "best",
            "event_attack_refinement": (
                "2 ms smoothed energy onset, nearest zero crossing within 2 ms, "
                "5 ms pre-roll, raised-cosine boundary tapers"
            ),
            "additional_denoising_applied": False,
            "additional_dynamic_compression_applied": loudness_mode in {"loud", "cinematic"},
            "cinematic_derived_layers_applied": loudness_mode == "cinematic",
            "post_render_processing": (
                "legacy linear sample-peak gain"
                if loudness_mode == "safe"
                else "parallel compression and 4x oversampled true-peak normalisation"
            ),
            "best_exemplars": {
                event_type: {
                    "cycle_index": max(items, key=lambda item: item.quality_score).cycle_index,
                    "source_time_s": max(items, key=lambda item: item.quality_score).source_time_s,
                    "quality_score": max(items, key=lambda item: item.quality_score).quality_score,
                    "onset_shift_ms": max(items, key=lambda item: item.quality_score).onset_shift_ms,
                }
                for event_type, items in bank.items()
            },
        },
        "meters": meter_reports,
        "event_table": events_path.name,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    report_path = group_dir / (
        f"{prefix}_bpm{tag}{peak_tag}{rhythm_tag}{bank_tag}"
        f"{loudness_tag}_bar_analysis.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    created.append(report_path)
    if diagnostics.file_sha256(prepared.input_path) != prepared.input_sha256:
        raise RuntimeError("The selected input WAV changed during rendering.")
    return group_dir, created


def render(
    input_path: Path,
    target_bpm: float,
    output_root: Path,
    beat_peak: str = "both",
    event_bank_mode: str = "auto",
    loudness_mode: str = "safe",
    pair_rhythm: str = "natural",
) -> tuple[Path, list[Path]]:
    """Convenience API for non-interactive callers."""
    return render_prepared(
        inspect_wav(input_path), target_bpm, output_root, beat_peak, event_bank_mode,
        loudness_mode, pair_rhythm
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect one of heart_extraction's nine WAV outputs, recommend a BPM "
            "range, then render 4/4, 3/4, and 2/4 bars."
        )
    )
    parser.add_argument(
        "input_wav",
        help="One Stable/Authentic/Regularized Base/Clean/Enhanced WAV",
    )
    parser.add_argument(
        "--target-bpm",
        type=float,
        help="Skip the interactive prompt and use this BPM",
    )
    parser.add_argument(
        "--beat-peak",
        choices=("both", "s1", "s2"),
        help="Keep both peaks, only S1, or only S2 (interactive default: ask)",
    )
    parser.add_argument(
        "--event-bank",
        choices=("auto", "best", "rotate"),
        default="auto",
        help="auto uses best for single peak and rotate for both (default: auto)",
    )
    parser.add_argument(
        "--loudness-mode",
        choices=("safe", "loud", "cinematic"),
        default="safe",
        help=(
            "safe preserves legacy gain; loud adds parallel compression; "
            "cinematic also adds heartbeat-derived body/harmonic layers"
        ),
    )
    parser.add_argument(
        "--pair-rhythm",
        choices=("natural", "even"),
        default="natural",
        help=(
            "natural preserves physiological S2 phase; even places S2 at the "
            "half-beat without stretching either event"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "outputs"),
        help="Parent directory for one source/time/BPM output group",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prepared = inspect_wav(Path(args.input_wav))
        print("status=READY")
        print(f"selected_wav={prepared.input_path}")
        beat_peak = args.beat_peak
        if beat_peak is None and args.target_bpm is None:
            try:
                beat_peak = input(
                    "Select beat peak [both/s1/s2] (default both): "
                ).strip().lower() or "both"
            except EOFError:
                print(
                    "No peak mode was entered. Rerun with --beat-peak both|s1|s2.",
                    file=sys.stderr,
                )
                return 1
            if beat_peak not in {"both", "s1", "s2"}:
                raise ValueError("Beat peak must be one of: both, s1, s2.")
        elif beat_peak is None:
            beat_peak = "both"
        s2_phase = prepared.result.s1_s2_s / prepared.result.period_s
        recommendation = _recommend_bpm(
            prepared.result.bpm, s2_phase, beat_peak, args.pair_rhythm
        )
        print(f"beat_peak_mode={beat_peak}")
        print(f"pair_rhythm={args.pair_rhythm}")
        print(f"loudness_mode={args.loudness_mode}")
        print(f"detected_bpm={recommendation.detected_bpm:.2f}")
        print(
            "recommended_bpm_range="
            f"{recommendation.recommended_min_bpm:.1f}-"
            f"{recommendation.recommended_max_bpm:.1f}"
        )
        print(
            "technical_bpm_range="
            f"{recommendation.technical_min_bpm:.1f}-"
            f"{recommendation.technical_max_bpm:.1f}"
        )
        target_bpm = args.target_bpm
        if target_bpm is None:
            prompt = (
                "Enter target BPM "
                f"[{recommendation.recommended_min_bpm:.1f}-"
                f"{recommendation.recommended_max_bpm:.1f}]: "
            )
            try:
                target_bpm = float(input(prompt).strip())
            except EOFError:
                print(
                    "No BPM was entered. Rerun with --target-bpm <value>.",
                    file=sys.stderr,
                )
                return 1
            except ValueError as exc:
                raise ValueError("Target BPM must be a number.") from exc
        group_dir, created = render_prepared(
            prepared,
            target_bpm,
            Path(args.output_dir),
            beat_peak,
            args.event_bank,
            args.loudness_mode,
            args.pair_rhythm,
        )
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"status=REJECT\nreason={exc}", file=sys.stderr)
        return 2
    print("status=PASS")
    print(f"output_group={group_dir}")
    for path in created:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
