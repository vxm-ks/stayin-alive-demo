#!/usr/bin/env python3
"""Automatic normal-PCG selection, segmentation, extraction, and looping.

This module is the primary entry point for LegaSynth Track B heart-sound
processing. Plotting is a diagnostic by-product, not the analysis itself.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import re
import sys
from datetime import datetime
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import scipy
from scipy.io import wavfile
from scipy.ndimage import median_filter, uniform_filter1d
from scipy.signal import (
    butter,
    find_peaks,
    hilbert,
    istft,
    resample_poly,
    sosfiltfilt,
    stft,
)

try:  # Package import when called by another LegaSynth module.
    from . import audio_diagnostics as diagnostics
except ImportError:  # Direct script execution: python heart_sound_processor.py ...
    import audio_diagnostics as diagnostics


VERSION = "1.3.0"


def source_identity(input_path: Path) -> dict[str, str]:
    """Derive a filesystem-safe sample id and acquisition-time naming prefix.

    Recorder names shaped as YYYYMMDDhhmmss<ID> are parsed directly. Other
    names retain their full stem as the sample id and use file mtime as an
    explicitly recorded fallback because acquisition time is unavailable.
    """
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", input_path.stem).strip("_")
    safe_stem = safe_stem or "sample"
    match = re.fullmatch(r"(?P<timestamp>\d{14})(?P<sample_id>\d+)", input_path.stem)
    if match:
        try:
            acquired = datetime.strptime(match.group("timestamp"), "%Y%m%d%H%M%S")
            acquisition_time = acquired.strftime("%Y%m%dT%H%M%S")
            sample_id = match.group("sample_id")
            return {
                "sample_id": sample_id,
                "acquisition_time": acquisition_time,
                "time_source": "input_filename_YYYYMMDDhhmmss",
                "prefix": f"{sample_id}_{acquisition_time}",
            }
        except ValueError:
            pass

    if input_path.is_file():
        modified = datetime.fromtimestamp(input_path.stat().st_mtime).astimezone()
        time_source = "filesystem_mtime_fallback"
    else:
        modified = datetime.now().astimezone()
        time_source = "processing_time_fallback_missing_source"
    acquisition_time = modified.strftime("%Y%m%dT%H%M%S")
    return {
        "sample_id": safe_stem,
        "acquisition_time": acquisition_time,
        "time_source": time_source,
        "prefix": f"{safe_stem}_{acquisition_time}",
    }


@dataclass(frozen=True)
class Config:
    analysis_sample_rate: int = 2000
    bandpass_low_hz: float = 20.0
    bandpass_high_hz: float = 200.0
    filter_order: int = 4
    envelope_smoothing_ms: float = 45.0
    min_bpm: float = 40.0
    max_bpm: float = 160.0
    min_s1_s2_s: float = 0.18
    max_s1_s2_s: float = 0.45
    event_search_ms: float = 85.0
    stable_min_s: float = 8.0
    stable_target_s: float = 10.0
    stable_max_s: float = 12.0
    stable_min_cycles: int = 8
    loop_duration_s: float = 30.0
    min_confidence: float = 0.50
    denoise_reduction_db: float = 12.0
    spectral_oversubtraction: float = 1.15
    background_attenuation_db: float = 8.0
    compressor_threshold_dbfs: float = -22.0
    compressor_ratio: float = 3.0
    enhanced_target_rms_dbfs: float = -14.0
    enhanced_peak_dbfs: float = -1.0
    enhancement_high_hz: float = 450.0


@dataclass
class Event:
    type: str
    time_s: float
    predicted_time_s: float
    amplitude: float
    residual_s: float
    confidence: float = 0.0
    cycle_index: int = -1


@dataclass
class Cycle:
    index: int
    start_s: float
    s2_s: float
    end_s: float
    period_s: float
    s1_s2_s: float
    s1_amplitude: float
    s2_amplitude: float
    valid: bool = False
    quality: float = 0.0
    rejection_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class StableRegion:
    first_cycle: int
    last_cycle: int
    start_s: float
    end_s: float
    duration_s: float
    score: float
    valid_fraction: float
    period_cv: float
    s1_s2_cv: float


@dataclass
class ProcessingResult:
    status: str
    confidence: float
    bpm: float
    period_s: float
    s1_s2_s: float
    periodicity: float
    events: list[Event]
    cycles: list[Cycle]
    stable_region: StableRegion
    warnings: list[str]
    period_candidates: list[dict[str, float]]


def _robust_scale(values: np.ndarray) -> np.ndarray:
    low, high = np.percentile(values, [5.0, 95.0])
    if high <= low + 1e-12:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low), 0.0, 3.0)


def preprocess(
    signal: np.ndarray, source_rate: int, config: Config
) -> tuple[np.ndarray, np.ndarray, int]:
    """Return centred source signal, filtered analysis signal, and analysis rate."""
    centred = signal - np.median(signal)
    target_rate = min(config.analysis_sample_rate, source_rate)
    divisor = math.gcd(source_rate, target_rate)
    analysis = resample_poly(centred, target_rate // divisor, source_rate // divisor)
    nyquist = target_rate / 2.0
    high = min(config.bandpass_high_hz, nyquist * 0.92)
    if not 0 < config.bandpass_low_hz < high:
        raise ValueError("Band-pass limits are invalid for this sample rate.")
    sos = butter(
        config.filter_order,
        [config.bandpass_low_hz, high],
        btype="bandpass",
        fs=target_rate,
        output="sos",
    )
    filtered = sosfiltfilt(sos, analysis)
    return centred, filtered, target_rate


def extract_envelope(
    filtered: np.ndarray, sample_rate: int, smoothing_ms: float
) -> np.ndarray:
    """Fuse robustly normalised Hilbert amplitude and Shannon energy envelopes."""
    amplitude = np.abs(hilbert(filtered))
    reference = max(float(np.percentile(np.abs(filtered), 99.5)), 1e-12)
    normalised = np.clip(np.abs(filtered) / reference, 0.0, 1.0)
    squared = normalised * normalised
    shannon = -(squared * np.log(squared + 1e-12))
    fused = 0.60 * _robust_scale(amplitude) + 0.40 * _robust_scale(shannon)
    window = max(1, int(round(smoothing_ms * sample_rate / 1000.0)))
    return uniform_filter1d(fused, size=window, mode="nearest")


def normalised_autocorrelation(values: np.ndarray) -> np.ndarray:
    centred = values - np.mean(values)
    scale = np.std(centred)
    if scale <= 1e-12:
        raise ValueError("No usable periodic variation was found in the envelope.")
    centred /= scale
    count = len(centred)
    fft_size = 1 << (2 * count - 1).bit_length()
    spectrum = np.fft.rfft(centred, fft_size)
    correlation = np.fft.irfft(np.abs(spectrum) ** 2, fft_size)[:count]
    correlation /= np.arange(count, 0, -1)
    correlation /= correlation[0] + 1e-12
    return correlation


def estimate_period(
    envelope: np.ndarray, sample_rate: int, config: Config
) -> tuple[float, float, list[dict[str, float]]]:
    """Estimate a complete S1-to-S1 period and retain competing candidates."""
    analysis_rate = min(200, sample_rate)
    step = max(1, int(round(sample_rate / analysis_rate)))
    reduced = envelope[::step]
    reduced_rate = sample_rate / step
    correlation = normalised_autocorrelation(reduced)
    min_lag = max(1, int(math.floor(reduced_rate * 60.0 / config.max_bpm)))
    max_lag = min(len(correlation) - 1, int(math.ceil(reduced_rate * 60.0 / config.min_bpm)))
    peaks, _ = find_peaks(
        correlation[min_lag : max_lag + 1],
        distance=max(1, int(0.08 * reduced_rate)),
    )
    peaks += min_lag
    if len(peaks) == 0:
        peaks = np.array([min_lag + int(np.argmax(correlation[min_lag : max_lag + 1]))])
    ranked = sorted(peaks, key=lambda index: correlation[index], reverse=True)
    candidates = [
        {
            "period_s": float(index / reduced_rate),
            "bpm": float(60.0 * reduced_rate / index),
            "autocorrelation": float(correlation[index]),
        }
        for index in ranked[:8]
    ]
    best = ranked[0]
    period = float(best / reduced_rate)

    # Prefer a plausible double-length candidate when it has nearly equal
    # periodic support. This explicitly guards against S1/S2 half-period errors.
    doubled = 2 * best
    if doubled <= max_lag and correlation[doubled] >= 0.88 * correlation[best]:
        period = float(doubled / reduced_rate)
        best = doubled
    return period, float(max(0.0, correlation[best])), candidates


def estimate_cycle_phase(
    envelope: np.ndarray,
    sample_rate: int,
    period_s: float,
    config: Config,
) -> tuple[float, float]:
    """Estimate S1 phase and the shorter forward S1-to-S2 duration."""
    phase_bins = max(80, int(round(period_s * 240)))
    times = np.arange(len(envelope)) / sample_rate
    indices = np.floor(np.mod(times, period_s) / period_s * phase_bins).astype(int)
    sums = np.bincount(indices, weights=envelope, minlength=phase_bins)
    counts = np.bincount(indices, minlength=phase_bins)
    profile = sums / np.maximum(counts, 1)
    profile = uniform_filter1d(profile, size=max(3, phase_bins // 35), mode="wrap")
    baseline = float(np.median(profile))

    min_gap = max(1, int(round(config.min_s1_s2_s / period_s * phase_bins)))
    max_gap = min(
        phase_bins - 1,
        int(round(min(config.max_s1_s2_s, period_s * 0.48) / period_s * phase_bins)),
    )
    if max_gap <= min_gap:
        raise ValueError("Estimated cardiac period is too short for S1/S2 separation.")
    best_score = -np.inf
    best_pair = (0, min_gap)
    for first in range(phase_bins):
        for gap in range(min_gap, max_gap + 1):
            second = (first + gap) % phase_bins
            between = (first + gap // 2) % phase_bins
            score = (
                profile[first]
                + profile[second]
                - 0.45 * profile[between]
                - 0.55 * baseline
            )
            if score > best_score:
                best_score = score
                best_pair = (first, second)
    s1_phase = best_pair[0] / phase_bins * period_s
    s1_s2 = ((best_pair[1] - best_pair[0]) % phase_bins) / phase_bins * period_s
    return float(s1_phase), float(s1_s2)


def _refine_time(
    envelope: np.ndarray,
    sample_rate: int,
    predicted_s: float,
    radius_s: float,
) -> tuple[float, float]:
    centre = int(round(predicted_s * sample_rate))
    radius = max(1, int(round(radius_s * sample_rate)))
    start = max(0, centre - radius)
    end = min(len(envelope), centre + radius + 1)
    local = envelope[start:end]
    index = start + int(np.argmax(local))
    return index / sample_rate, float(envelope[index])


def detect_events(
    envelope: np.ndarray,
    sample_rate: int,
    period_s: float,
    phase_s: float,
    s1_s2_s: float,
    config: Config,
) -> tuple[list[Event], list[Event]]:
    radius_s = min(config.event_search_ms / 1000.0, s1_s2_s * 0.28)
    duration_s = len(envelope) / sample_rate
    first_index = math.floor(-phase_s / period_s) - 1
    predicted_s1 = phase_s + first_index * period_s
    s1_events: list[Event] = []
    s2_events: list[Event] = []
    while predicted_s1 < duration_s:
        predicted_s2 = predicted_s1 + s1_s2_s
        if radius_s <= predicted_s1 < duration_s - radius_s:
            time_s, amplitude = _refine_time(
                envelope, sample_rate, predicted_s1, radius_s
            )
            s1_events.append(
                Event("S1", time_s, predicted_s1, amplitude, time_s - predicted_s1)
            )
        if radius_s <= predicted_s2 < duration_s - radius_s:
            time_s, amplitude = _refine_time(
                envelope, sample_rate, predicted_s2, radius_s
            )
            s2_events.append(
                Event("S2", time_s, predicted_s2, amplitude, time_s - predicted_s2)
            )
        predicted_s1 += period_s
    return s1_events, s2_events


def build_cycles(
    s1_events: list[Event],
    s2_events: list[Event],
    expected_s1_s2_s: float,
    config: Config,
) -> list[Cycle]:
    s1_events.sort(key=lambda event: event.time_s)
    s2_events.sort(key=lambda event: event.time_s)
    cycles: list[Cycle] = []
    for index in range(len(s1_events) - 1):
        start = s1_events[index]
        end = s1_events[index + 1]
        inside = [event for event in s2_events if start.time_s < event.time_s < end.time_s]
        if not inside:
            continue
        s2 = min(
            inside,
            key=lambda event: abs(event.time_s - (start.time_s + expected_s1_s2_s)),
        )
        start.cycle_index = index
        s2.cycle_index = index
        period = end.time_s - start.time_s
        delta = s2.time_s - start.time_s
        cycles.append(
            Cycle(
                index=index,
                start_s=start.time_s,
                s2_s=s2.time_s,
                end_s=end.time_s,
                period_s=period,
                s1_s2_s=delta,
                s1_amplitude=start.amplitude,
                s2_amplitude=s2.amplitude,
            )
        )
    if len(cycles) < config.stable_min_cycles:
        raise ValueError(
            f"Only {len(cycles)} complete cycles were detected; "
            f"at least {config.stable_min_cycles} are required."
        )

    periods = np.asarray([cycle.period_s for cycle in cycles])
    deltas = np.asarray([cycle.s1_s2_s for cycle in cycles])
    s1_amplitudes = np.asarray([cycle.s1_amplitude for cycle in cycles])
    s2_amplitudes = np.asarray([cycle.s2_amplitude for cycle in cycles])
    med_period = float(np.median(periods))
    med_delta = float(np.median(deltas))
    med_s1 = max(float(np.median(s1_amplitudes)), 1e-12)
    med_s2 = max(float(np.median(s2_amplitudes)), 1e-12)

    for cycle in cycles:
        reasons: list[str] = []
        period_error = abs(cycle.period_s - med_period) / med_period
        delta_error = abs(cycle.s1_s2_s - med_delta) / med_delta
        s1_log_error = abs(math.log(max(cycle.s1_amplitude, 1e-12) / med_s1))
        s2_log_error = abs(math.log(max(cycle.s2_amplitude, 1e-12) / med_s2))
        if period_error > 0.18:
            reasons.append("period_outlier")
        if delta_error > 0.25:
            reasons.append("s1_s2_outlier")
        if not config.min_s1_s2_s * 0.75 <= cycle.s1_s2_s <= config.max_s1_s2_s * 1.15:
            reasons.append("physiological_duration_constraint")
        if s1_log_error > math.log(3.5):
            reasons.append("s1_amplitude_outlier")
        if s2_log_error > math.log(3.5):
            reasons.append("s2_amplitude_outlier")
        cycle.quality = float(
            np.exp(-3.0 * period_error)
            * np.exp(-2.0 * delta_error)
            * np.exp(-0.20 * (s1_log_error + s2_log_error))
        )
        cycle.rejection_reasons = tuple(reasons)
        cycle.valid = not reasons
    return cycles


def choose_stable_region(cycles: list[Cycle], config: Config) -> StableRegion:
    candidates: list[StableRegion] = []
    for start in range(len(cycles)):
        for end in range(start, len(cycles)):
            selected = cycles[start : end + 1]
            duration = selected[-1].end_s - selected[0].start_s
            if duration < config.stable_min_s:
                continue
            if duration > config.stable_max_s:
                break
            if len(selected) < config.stable_min_cycles:
                continue
            periods = np.asarray([cycle.period_s for cycle in selected])
            deltas = np.asarray([cycle.s1_s2_s for cycle in selected])
            valid_fraction = float(np.mean([cycle.valid for cycle in selected]))
            period_cv = float(np.std(periods) / max(np.mean(periods), 1e-12))
            delta_cv = float(np.std(deltas) / max(np.mean(deltas), 1e-12))
            mean_quality = float(np.mean([cycle.quality for cycle in selected]))
            duration_reward = max(
                0.0,
                1.0
                - abs(duration - config.stable_target_s)
                / max(config.stable_max_s - config.stable_min_s, 1e-12),
            )
            score = (
                2.0 * valid_fraction
                + 0.8 * mean_quality
                + 0.25 * duration_reward
                - 3.0 * period_cv
                - 2.0 * delta_cv
            )
            candidates.append(
                StableRegion(
                    first_cycle=start,
                    last_cycle=end,
                    start_s=selected[0].start_s,
                    end_s=selected[-1].end_s,
                    duration_s=duration,
                    score=float(score),
                    valid_fraction=valid_fraction,
                    period_cv=period_cv,
                    s1_s2_cv=delta_cv,
                )
            )
    if not candidates:
        raise ValueError(
            "No complete-cycle window satisfies the stable-region duration and cycle-count constraints."
        )
    return max(candidates, key=lambda candidate: candidate.score)


def assign_event_confidence(
    events: list[Event], cycles: list[Cycle], search_radius_s: float
) -> None:
    amplitudes = np.asarray([event.amplitude for event in events])
    median_amplitude = max(float(np.median(amplitudes)), 1e-12)
    quality_by_cycle = {cycle.index: cycle.quality for cycle in cycles}
    for event in events:
        timing = math.exp(-abs(event.residual_s) / max(search_radius_s, 1e-12))
        amplitude = min(1.0, event.amplitude / median_amplitude)
        cycle_quality = quality_by_cycle.get(event.cycle_index, 0.5)
        event.confidence = float(0.45 * timing + 0.25 * amplitude + 0.30 * cycle_quality)


def analyse(signal: np.ndarray, sample_rate: int, config: Config) -> tuple[
    ProcessingResult, np.ndarray, np.ndarray, int
]:
    centred, filtered, analysis_rate = preprocess(signal, sample_rate, config)
    envelope = extract_envelope(
        filtered, analysis_rate, config.envelope_smoothing_ms
    )
    period, periodicity, candidates = estimate_period(envelope, analysis_rate, config)
    phase, initial_delta = estimate_cycle_phase(
        envelope, analysis_rate, period, config
    )
    s1_events, s2_events = detect_events(
        envelope, analysis_rate, period, phase, initial_delta, config
    )
    cycles = build_cycles(s1_events, s2_events, initial_delta, config)
    stable = choose_stable_region(cycles, config)
    events = sorted(s1_events + s2_events, key=lambda event: event.time_s)
    assign_event_confidence(
        events, cycles, min(config.event_search_ms / 1000.0, initial_delta * 0.28)
    )

    selected = cycles[stable.first_cycle : stable.last_cycle + 1]
    valid_selected = [cycle for cycle in selected if cycle.valid]
    if not valid_selected:
        raise ValueError("The selected stable region contains no valid cycles.")
    final_period = float(np.median([cycle.period_s for cycle in valid_selected]))
    final_delta = float(np.median([cycle.s1_s2_s for cycle in valid_selected]))
    mean_cycle_quality = float(np.mean([cycle.quality for cycle in selected]))
    confidence = float(
        np.clip(
            0.25 * min(1.0, periodicity / 0.45)
            + 0.35 * stable.valid_fraction
            + 0.40 * mean_cycle_quality,
            0.0,
            1.0,
        )
    )
    warnings: list[str] = []
    if stable.valid_fraction < 0.80:
        warnings.append("stable_region_contains_many_rejected_cycles")
    if confidence < config.min_confidence:
        warnings.append("low_overall_confidence")
    if confidence < config.min_confidence or stable.valid_fraction < 0.60:
        status = "REJECT"
    else:
        status = "PASS" if not warnings else "PASS_WITH_WARNING"
    result = ProcessingResult(
        status=status,
        confidence=confidence,
        bpm=60.0 / final_period,
        period_s=final_period,
        s1_s2_s=final_delta,
        periodicity=periodicity,
        events=events,
        cycles=cycles,
        stable_region=stable,
        warnings=warnings,
        period_candidates=candidates,
    )
    return result, centred, filtered, analysis_rate


def _float_to_int16(signal: np.ndarray) -> np.ndarray:
    return np.rint(np.clip(signal, -1.0, 1.0) * 32767.0).astype(np.int16)


def write_wav(path: Path, sample_rate: int, signal: np.ndarray) -> None:
    wavfile.write(path, sample_rate, _float_to_int16(signal))


def _stable_events(result: ProcessingResult) -> list[Event]:
    start = result.stable_region.start_s
    end = result.stable_region.end_s
    return [event for event in result.events if start <= event.time_s < end]


def build_event_timelines(result: ProcessingResult) -> list[dict[str, float | str | int]]:
    events = _stable_events(result)
    s1 = [event for event in events if event.type == "S1"]
    s2 = [event for event in events if event.type == "S2"]
    pair_count = min(len(s1), len(s2))
    if pair_count == 0:
        return []
    raw_s1 = np.asarray([event.time_s for event in s1[:pair_count]])
    raw_s2 = np.asarray([event.time_s for event in s2[:pair_count]])
    raw_periods = np.diff(np.append(raw_s1, result.stable_region.end_s))
    smoothed_periods = median_filter(raw_periods, size=3, mode="nearest")
    smoothed_s1 = np.empty(pair_count)
    smoothed_s1[0] = raw_s1[0]
    for index in range(1, pair_count):
        smoothed_s1[index] = smoothed_s1[index - 1] + smoothed_periods[index - 1]
    raw_delta = raw_s2 - raw_s1
    smoothed_delta = median_filter(raw_delta, size=3, mode="nearest")
    regular_s1 = raw_s1[0] + np.arange(pair_count) * result.period_s
    rows: list[dict[str, float | str | int]] = []
    for index in range(pair_count):
        rows.append(
            {
                "cycle_index": index,
                "type": "S1",
                "raw_time_s": float(raw_s1[index]),
                "smoothed_time_s": float(smoothed_s1[index]),
                "regularized_time_s": float(regular_s1[index]),
            }
        )
        rows.append(
            {
                "cycle_index": index,
                "type": "S2",
                "raw_time_s": float(raw_s2[index]),
                "smoothed_time_s": float(smoothed_s1[index] + smoothed_delta[index]),
                "regularized_time_s": float(regular_s1[index] + result.s1_s2_s),
            }
        )
    return rows


def _event_clip(
    signal: np.ndarray,
    sample_rate: int,
    event_time_s: float,
    event_type: str,
) -> tuple[np.ndarray, int]:
    pre_s = 0.030
    post_s = 0.180 if event_type == "S1" else 0.150
    centre = int(round(event_time_s * sample_rate))
    pre = int(round(pre_s * sample_rate))
    post = int(round(post_s * sample_rate))
    start = max(0, centre - pre)
    end = min(len(signal), centre + post)
    clip = signal[start:end].copy()
    fade = min(int(round(0.006 * sample_rate)), len(clip) // 4)
    if fade > 1:
        ramp = np.sin(np.linspace(0, np.pi / 2, fade)) ** 2
        clip[:fade] *= ramp
        clip[-fade:] *= ramp[::-1]
    event_offset = centre - start
    return clip, event_offset


def select_reference_events(result: ProcessingResult) -> dict[str, list[Event]]:
    """Select real S1/S2 exemplars from valid, sufficiently strong cycles."""
    cycle_by_index = {cycle.index: cycle for cycle in result.cycles}
    selected: dict[str, list[Event]] = {}
    for event_type in ("S1", "S2"):
        typed = [event for event in _stable_events(result) if event.type == event_type]
        valid = [
            event
            for event in typed
            if cycle_by_index.get(event.cycle_index) is not None
            and cycle_by_index[event.cycle_index].valid
        ]
        pool = valid if len(valid) >= 2 else typed
        if not pool:
            raise ValueError(f"No stable {event_type} events are available.")
        median_amplitude = max(float(np.median([event.amplitude for event in pool])), 1e-12)
        accepted = [
            event
            for event in pool
            if event.amplitude >= 0.55 * median_amplitude and event.confidence >= 0.45
        ]
        if len(accepted) < min(3, len(pool)):
            accepted = sorted(
                pool,
                key=lambda event: (
                    event.confidence,
                    cycle_by_index.get(event.cycle_index, Cycle(0, 0, 0, 0, 0, 0, 0, 0)).quality,
                    event.amplitude,
                ),
                reverse=True,
            )[: min(3, len(pool))]
        selected[event_type] = sorted(accepted, key=lambda event: event.time_s)
    return selected


def build_event_clip_bank(
    signal: np.ndarray,
    sample_rate: int,
    result: ProcessingResult,
) -> dict[str, list[tuple[np.ndarray, int, Event]]]:
    references = select_reference_events(result)
    return {
        event_type: [
            (*_event_clip(signal, sample_rate, event.time_s, event_type), event)
            for event in events
        ]
        for event_type, events in references.items()
    }


def repair_authentic_segment(
    stable_signal: np.ndarray,
    source_signal: np.ndarray,
    sample_rate: int,
    result: ProcessingResult,
    crop_start_s: float,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Replace rejected/weak stable events with vetted real events at original times."""
    repaired = stable_signal.copy()
    references = select_reference_events(result)
    bank = build_event_clip_bank(source_signal, sample_rate, result)
    accepted_ids = {
        event_type: {id(event) for event in events}
        for event_type, events in references.items()
    }
    replacements: list[dict[str, Any]] = []
    fade_samples = max(2, int(round(0.012 * sample_rate)))
    replacement_index = {"S1": 0, "S2": 0}
    for target in _stable_events(result):
        if id(target) in accepted_ids[target.type]:
            continue
        candidates = bank[target.type]
        source_clip, source_offset, source_event = candidates[
            replacement_index[target.type] % len(candidates)
        ]
        replacement_index[target.type] += 1
        target_centre = int(round((target.time_s - crop_start_s) * sample_rate))
        destination_start = target_centre - source_offset
        source_start = max(0, -destination_start)
        destination_start = max(0, destination_start)
        length = min(
            len(source_clip) - source_start,
            len(repaired) - destination_start,
        )
        if length <= 2:
            continue
        source_part = source_clip[source_start : source_start + length]
        blend = np.ones(length, dtype=np.float64)
        fade = min(fade_samples, length // 3)
        if fade > 1:
            ramp = np.sin(np.linspace(0.0, np.pi / 2.0, fade)) ** 2
            blend[:fade] = ramp
            blend[-fade:] = ramp[::-1]
        destination = repaired[destination_start : destination_start + length]
        repaired[destination_start : destination_start + length] = (
            destination * (1.0 - blend) + source_part * blend
        )
        replacements.append(
            {
                "type": target.type,
                "target_cycle_index": target.cycle_index,
                "target_time_s": target.time_s,
                "source_cycle_index": source_event.cycle_index,
                "source_time_s": source_event.time_s,
                "reason": "target event excluded from valid reference bank",
            }
        )
    return repaired, replacements


def build_authentic_schedule(
    result: ProcessingResult,
    loop_duration_s: float,
    maximum_period_deviation: float = 0.06,
    maximum_s1_s2_deviation: float = 0.10,
) -> list[Event]:
    """Build a bounded, naturally varying rhythm with outlier timing removed."""
    stable_cycles = result.cycles[
        result.stable_region.first_cycle : result.stable_region.last_cycle + 1
    ]
    valid_cycles = [cycle for cycle in stable_cycles if cycle.valid]
    pattern_cycles = valid_cycles if len(valid_cycles) >= 3 else stable_cycles
    if not pattern_cycles:
        raise ValueError("No stable cycles are available for authentic scheduling.")

    raw_periods = np.asarray([cycle.period_s for cycle in pattern_cycles])
    raw_deltas = np.asarray([cycle.s1_s2_s for cycle in pattern_cycles])
    periods = median_filter(raw_periods, size=3, mode="nearest")
    deltas = median_filter(raw_deltas, size=3, mode="nearest")
    period_centre = float(np.median(periods))
    delta_centre = float(np.median(deltas))
    periods = np.clip(
        periods,
        period_centre * (1.0 - maximum_period_deviation),
        period_centre * (1.0 + maximum_period_deviation),
    )
    deltas = np.clip(
        deltas,
        max(0.18, delta_centre * (1.0 - maximum_s1_s2_deviation)),
        min(0.45, delta_centre * (1.0 + maximum_s1_s2_deviation)),
    )

    schedule: list[Event] = []
    cycle_number = 0
    time_s = 0.0
    while time_s < loop_duration_s:
        pattern_index = cycle_number % len(periods)
        source_cycle = pattern_cycles[pattern_index]
        schedule.append(
            Event("S1", time_s, time_s, 1.0, 0.0, 1.0, source_cycle.index)
        )
        s2_time = time_s + float(deltas[pattern_index])
        if s2_time < loop_duration_s:
            schedule.append(
                Event("S2", s2_time, s2_time, 1.0, 0.0, 1.0, source_cycle.index)
            )
        time_s += float(periods[pattern_index])
        cycle_number += 1
    return schedule


def build_regularized_schedule(
    result: ProcessingResult, loop_duration_s: float
) -> list[Event]:
    schedule: list[Event] = []
    cycle_number = 0
    while cycle_number * result.period_s < loop_duration_s:
        s1_time = cycle_number * result.period_s
        schedule.append(Event("S1", s1_time, s1_time, 1.0, 0.0))
        s2_time = s1_time + result.s1_s2_s
        if s2_time < loop_duration_s:
            schedule.append(Event("S2", s2_time, s2_time, 1.0, 0.0))
        cycle_number += 1
    return schedule


def schedule_rhythm_metrics(schedule: Sequence[Event]) -> dict[str, float | int]:
    s1_times = np.asarray([event.time_s for event in schedule if event.type == "S1"])
    periods = np.diff(s1_times)
    if len(periods) == 0:
        return {"cycle_count": len(s1_times), "period_count": 0}
    mean_period = float(np.mean(periods))
    return {
        "cycle_count": len(s1_times),
        "period_count": len(periods),
        "mean_period_s": mean_period,
        "minimum_period_s": float(np.min(periods)),
        "maximum_period_s": float(np.max(periods)),
        "period_cv": float(np.std(periods) / max(mean_period, 1e-12)),
        "equivalent_bpm": 60.0 / max(mean_period, 1e-12),
    }


def measure_loop_event_energy(
    sample_rate: int,
    loop_signals: Sequence[tuple[str, str, np.ndarray, Sequence[Event]]],
    minimum_relative_rms: float = 0.45,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Measure every expected S event and flag abnormally weak local energy."""
    rows: list[dict[str, Any]] = []
    for loop_name, variant, signal, schedule in loop_signals:
        for event_index, event in enumerate(schedule):
            start = max(0, int(round(event.time_s * sample_rate)))
            post_s = 0.180 if event.type == "S1" else 0.150
            end = min(len(signal), int(round((event.time_s + post_s) * sample_rate)))
            window = signal[start:end]
            if len(window) == 0:
                peak = 0.0
                rms = 0.0
            else:
                peak = float(np.max(np.abs(window)))
                rms = float(np.sqrt(np.mean(window * window)))
            rows.append(
                {
                    "loop": loop_name,
                    "variant": variant,
                    "event_index": event_index,
                    "type": event.type,
                    "time_s": event.time_s,
                    "peak_abs_fs": peak,
                    "rms_fs": rms,
                    "rms_dbfs": 20.0 * math.log10(max(rms, 1e-12)),
                }
            )

    medians: dict[tuple[str, str, str], float] = {}
    for row in rows:
        key = (row["loop"], row["variant"], row["type"])
        if key not in medians:
            values = [
                candidate["rms_fs"]
                for candidate in rows
                if (
                    candidate["loop"],
                    candidate["variant"],
                    candidate["type"],
                )
                == key
            ]
            medians[key] = max(float(np.median(values)), 1e-12)
        row["relative_to_same_type_median"] = row["rms_fs"] / medians[key]
        row["preservation_status"] = (
            "OK"
            if row["relative_to_same_type_median"] >= minimum_relative_rms
            else "WEAK"
        )

    summary: dict[str, Any] = {
        "minimum_relative_rms": minimum_relative_rms,
        "window_definition": "event time through +180 ms (S1) or +150 ms (S2)",
        "groups": {},
    }
    for loop_name, variant, _, _ in loop_signals:
        key = f"{loop_name}_{variant}"
        subset = [
            row
            for row in rows
            if row["loop"] == loop_name and row["variant"] == variant
        ]
        summary["groups"][key] = {
            "event_count": len(subset),
            "weak_event_count": sum(
                row["preservation_status"] == "WEAK" for row in subset
            ),
            "minimum_relative_rms": min(
                (row["relative_to_same_type_median"] for row in subset),
                default=0.0,
            ),
        }
    return rows, summary


def repeat_exact(segment: np.ndarray, target_samples: int) -> np.ndarray:
    repetitions = max(1, math.ceil(target_samples / len(segment)))
    return np.tile(segment, repetitions)[:target_samples]


def choose_equal_phase_loop_boundaries(
    signal: np.ndarray,
    sample_rate: int,
    stable_start_s: float,
    stable_end_s: float,
    search_before_s: float = 0.10,
) -> tuple[int, int, float]:
    """Find a shared pre-S1 offset with low local energy at both loop edges.

    Applying the same offset to both boundaries preserves the exact stable-region
    duration and therefore does not change its average tempo.
    """
    start_centre = int(round(stable_start_s * sample_rate))
    end_centre = int(round(stable_end_s * sample_rate))
    search = max(1, int(round(search_before_s * sample_rate)))
    radius = max(1, int(round(0.004 * sample_rate)))
    best_offset = 0
    best_score = np.inf
    for offset in range(-search, 1):
        first = start_centre + offset
        second = end_centre + offset
        if first - radius < 0 or second + radius >= len(signal):
            continue
        first_window = signal[first - radius : first + radius + 1]
        second_window = signal[second - radius : second + radius + 1]
        score = float(
            np.mean(first_window * first_window)
            + np.mean(second_window * second_window)
        )
        if score < best_score:
            best_score = score
            best_offset = offset
    start = start_centre + best_offset
    end = end_centre + best_offset
    if not 0 <= start < end <= len(signal):
        raise ValueError("Unable to place valid equal-phase loop boundaries.")
    return start, end, best_offset / sample_rate


def _render_bandpass(
    signal: np.ndarray, sample_rate: int, config: Config
) -> np.ndarray:
    high = min(config.enhancement_high_hz, sample_rate * 0.46)
    low = min(config.bandpass_low_hz, high * 0.5)
    sos = butter(
        4,
        [low, high],
        btype="bandpass",
        fs=sample_rate,
        output="sos",
    )
    return sosfiltfilt(sos, signal)


def build_event_activity_mask(
    length: int,
    sample_rate: int,
    events: Sequence[Event],
    origin_s: float,
) -> np.ndarray:
    """Build a smooth 0..1 mask around S1/S2 intervals."""
    activity = np.zeros(length, dtype=np.float64)
    fade = max(1, int(round(0.025 * sample_rate)))
    for event in events:
        pre_s = 0.050
        post_s = 0.220 if event.type == "S1" else 0.180
        centre = int(round((event.time_s - origin_s) * sample_rate))
        core_start = max(0, centre - int(round(pre_s * sample_rate)))
        core_end = min(length, centre + int(round(post_s * sample_rate)))
        if core_end <= core_start:
            continue
        activity[core_start:core_end] = 1.0
        left_start = max(0, core_start - fade)
        if core_start > left_start:
            ramp = np.sin(
                np.linspace(0.0, np.pi / 2.0, core_start - left_start)
            ) ** 2
            activity[left_start:core_start] = np.maximum(
                activity[left_start:core_start], ramp
            )
        right_end = min(length, core_end + fade)
        if right_end > core_end:
            ramp = np.cos(
                np.linspace(0.0, np.pi / 2.0, right_end - core_end)
            ) ** 2
            activity[core_end:right_end] = np.maximum(
                activity[core_end:right_end], ramp
            )
    return activity


def soft_spectral_denoise(
    signal: np.ndarray,
    sample_rate: int,
    activity: np.ndarray,
    config: Config,
) -> tuple[np.ndarray, np.ndarray, dict[str, float | int]]:
    """Apply spectral subtraction using non-event frames as the noise profile."""
    target = max(128, int(round(0.046 * sample_rate)))
    nperseg = int(2 ** round(math.log2(target)))
    nperseg = min(1024, max(128, nperseg), len(signal))
    noverlap = min(nperseg - 1, int(round(nperseg * 0.75)))
    _, frame_times, spectrum = stft(
        signal,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=noverlap,
        boundary="zeros",
        padded=True,
    )
    power = np.abs(spectrum) ** 2
    frame_indices = np.clip(
        np.rint(frame_times * sample_rate).astype(int), 0, len(activity) - 1
    )
    noise_frames = activity[frame_indices] < 0.10
    if int(np.sum(noise_frames)) < 3:
        frame_power = np.mean(power, axis=0)
        keep = max(3, int(math.ceil(len(frame_power) * 0.25)))
        lowest = np.argsort(frame_power)[:keep]
        noise_frames = np.zeros(len(frame_power), dtype=bool)
        noise_frames[lowest] = True
    noise_power = np.median(power[:, noise_frames], axis=1, keepdims=True)
    floor_gain = 10.0 ** (-config.denoise_reduction_db / 20.0)
    gain_power = 1.0 - config.spectral_oversubtraction * noise_power / (
        power + 1e-15
    )
    gain = np.sqrt(np.maximum(floor_gain * floor_gain, gain_power))
    gain = uniform_filter1d(gain, size=3, axis=0, mode="nearest")
    gain = uniform_filter1d(gain, size=3, axis=1, mode="nearest")
    _, cleaned = istft(
        spectrum * gain,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=noverlap,
        input_onesided=True,
        boundary=True,
    )
    if len(cleaned) < len(signal):
        cleaned = np.pad(cleaned, (0, len(signal) - len(cleaned)))
    cleaned = cleaned[: len(signal)]
    removed = signal - cleaned
    details: dict[str, float | int] = {
        "stft_window_samples": nperseg,
        "stft_overlap_samples": noverlap,
        "noise_frame_count": int(np.sum(noise_frames)),
        "noise_frame_fraction": float(np.mean(noise_frames)),
        "spectral_floor_gain": floor_gain,
    }
    return cleaned, removed, details


def _compress_and_raise_loudness(
    signal: np.ndarray, sample_rate: int, config: Config
) -> tuple[np.ndarray, dict[str, float]]:
    """Reduce crest factor with a smooth envelope compressor, then add makeup."""
    envelope = uniform_filter1d(
        np.abs(signal), size=max(1, int(round(0.010 * sample_rate))), mode="nearest"
    )
    envelope_db = 20.0 * np.log10(np.maximum(envelope, 1e-12))
    over = np.maximum(0.0, envelope_db - config.compressor_threshold_dbfs)
    desired_gain_db = -(1.0 - 1.0 / config.compressor_ratio) * over
    attack = math.exp(-1.0 / max(1.0, 0.005 * sample_rate))
    release = math.exp(-1.0 / max(1.0, 0.100 * sample_rate))
    smoothed_gain_db = np.empty_like(desired_gain_db)
    state = 0.0
    for index, desired in enumerate(desired_gain_db):
        coefficient = attack if desired < state else release
        state = coefficient * state + (1.0 - coefficient) * desired
        smoothed_gain_db[index] = state
    compressed = signal * (10.0 ** (smoothed_gain_db / 20.0))
    rms_before_makeup = float(np.sqrt(np.mean(compressed * compressed)))
    rms_db = 20.0 * math.log10(max(rms_before_makeup, 1e-12))
    makeup_db = min(12.0, max(0.0, config.enhanced_target_rms_dbfs - rms_db))
    enhanced = compressed * (10.0 ** (makeup_db / 20.0))
    peak_target = 10.0 ** (config.enhanced_peak_dbfs / 20.0)
    peak_before_limiter = float(np.max(np.abs(enhanced)))
    # A continuous tanh limiter preserves small-signal slope while containing
    # transient peaks. Raw and clean outputs remain available for exact A/B.
    enhanced = peak_target * np.tanh(enhanced / peak_target)
    final_peak = float(np.max(np.abs(enhanced)))
    if final_peak > 1e-12:
        enhanced *= peak_target / final_peak
    return enhanced, {
        "makeup_gain_db": makeup_db,
        "peak_before_soft_limiter_dbfs": 20.0
        * math.log10(max(peak_before_limiter, 1e-12)),
        "soft_limiter": "tanh",
        "mean_compressor_gain_reduction_db": float(-np.mean(smoothed_gain_db)),
    }


def _peak_normalise(signal: np.ndarray, target_dbfs: float = -1.0) -> np.ndarray:
    target = 10.0 ** (target_dbfs / 20.0)
    peak = float(np.max(np.abs(signal)))
    if peak <= 1e-12:
        return signal.copy()
    return signal * (target / peak)


def enhance_heartbeat_audio(
    signal: np.ndarray,
    sample_rate: int,
    events: Sequence[Event],
    origin_s: float,
    config: Config,
) -> dict[str, Any]:
    """Return clean, enhanced, event/background, and removed-noise stems."""
    render_filtered = _render_bandpass(signal, sample_rate, config)
    activity = build_event_activity_mask(
        len(render_filtered), sample_rate, events, origin_s
    )
    clean, removed, denoise_details = soft_spectral_denoise(
        render_filtered, sample_rate, activity, config
    )
    background_gain = 10.0 ** (-config.background_attenuation_db / 20.0)
    focus_gain = background_gain + (1.0 - background_gain) * activity
    focused = clean * focus_gain
    enhanced, compressor_details = _compress_and_raise_loudness(
        focused, sample_rate, config
    )
    events_only = clean * activity
    background_only = clean * (1.0 - activity)
    def rms_dbfs(values: np.ndarray) -> float:
        return 20.0 * math.log10(
            max(float(np.sqrt(np.mean(values * values))), 1e-12)
        )

    active_weight = max(float(np.sum(activity)), 1e-12)
    background_weight = max(float(np.sum(1.0 - activity)), 1e-12)
    removed_active_rms = math.sqrt(
        float(np.sum(removed * removed * activity)) / active_weight
    )
    removed_background_rms = math.sqrt(
        float(np.sum(removed * removed * (1.0 - activity))) / background_weight
    )
    return {
        "clean": clean,
        "enhanced": enhanced,
        "events_only_monitor": _peak_normalise(events_only),
        "background_only_monitor": _peak_normalise(background_only),
        "removed_noise_monitor": _peak_normalise(removed),
        "activity": activity,
        "details": {
            **denoise_details,
            **compressor_details,
            "input_rms_dbfs": rms_dbfs(signal),
            "clean_rms_dbfs": rms_dbfs(clean),
            "enhanced_rms_dbfs": rms_dbfs(enhanced),
            "enhanced_rms_change_db": rms_dbfs(enhanced) - rms_dbfs(signal),
            "removed_active_rms_dbfs": 20.0
            * math.log10(max(removed_active_rms, 1e-12)),
            "removed_background_rms_dbfs": 20.0
            * math.log10(max(removed_background_rms, 1e-12)),
        },
    }


def render_scheduled_events(
    signal: np.ndarray,
    sample_rate: int,
    result: ProcessingResult,
    schedule: Sequence[Event],
    duration_s: float,
) -> np.ndarray:
    """Render vetted real-event clips at an arbitrary S1/S2 schedule."""
    bank = build_event_clip_bank(signal, sample_rate, result)
    clips = {
        event_type: [(clip, offset) for clip, offset, _ in typed_bank]
        for event_type, typed_bank in bank.items()
    }
    if not clips["S1"] or not clips["S2"]:
        raise ValueError("Not enough stable S1/S2 clips for regularized rendering.")
    output = np.zeros(int(round(duration_s * sample_rate)), dtype=np.float64)

    def place(clip: np.ndarray, offset: int, event_time_s: float) -> None:
        start = int(round(event_time_s * sample_rate)) - offset
        source_start = max(0, -start)
        destination_start = max(0, start)
        length = min(len(clip) - source_start, len(output) - destination_start)
        if length > 0:
            output[destination_start : destination_start + length] += clip[
                source_start : source_start + length
            ]

    type_counts = {"S1": 0, "S2": 0}
    for event in schedule:
        typed_clips = clips[event.type]
        clip, offset = typed_clips[type_counts[event.type] % len(typed_clips)]
        type_counts[event.type] += 1
        place(clip, offset, event.time_s)
    peak = float(np.max(np.abs(output)))
    if peak > 0.98:
        output *= 0.98 / peak
    return output


def render_regularized_events(
    signal: np.ndarray,
    sample_rate: int,
    result: ProcessingResult,
    duration_s: float,
) -> np.ndarray:
    schedule = build_regularized_schedule(result, duration_s)
    return render_scheduled_events(signal, sample_rate, result, schedule, duration_s)


def write_outputs(
    input_path: Path,
    output_dir: Path,
    audio: diagnostics.AudioData,
    result: ProcessingResult,
    centred: np.ndarray,
    filtered: np.ndarray,
    analysis_rate: int,
    config: Config,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = source_identity(input_path)
    prefix = identity["prefix"]
    group_dir = output_dir / prefix
    group_dir.mkdir(parents=True, exist_ok=True)

    def named(name: str) -> Path:
        return group_dir / f"{prefix}_{name}"

    samples_dir = named("event_samples")
    samples_dir.mkdir(exist_ok=True)
    created: list[Path] = []

    start_sample, end_sample, loop_boundary_offset_s = choose_equal_phase_loop_boundaries(
        centred,
        audio.sample_rate,
        result.stable_region.start_s,
        result.stable_region.end_s,
    )
    audio_crop_start_s = start_sample / audio.sample_rate
    audio_crop_end_s = end_sample / audio.sample_rate
    stable_raw = centred[start_sample:end_sample]
    stable_raw_path = named("stable_segment_raw.wav")
    write_wav(stable_raw_path, audio.sample_rate, stable_raw)
    created.append(stable_raw_path)
    stable_events = _stable_events(result)

    enhanced_stable = enhance_heartbeat_audio(
        stable_raw,
        audio.sample_rate,
        stable_events,
        audio_crop_start_s,
        config,
    )
    stable_clean_path = named("stable_segment_clean.wav")
    stable_enhanced_path = named("stable_segment_enhanced.wav")
    events_monitor_path = named("events_only_monitor.wav")
    background_monitor_path = named("background_only_monitor.wav")
    removed_noise_path = named("removed_noise_monitor.wav")
    write_wav(stable_clean_path, audio.sample_rate, enhanced_stable["clean"])
    write_wav(stable_enhanced_path, audio.sample_rate, enhanced_stable["enhanced"])
    write_wav(
        events_monitor_path,
        audio.sample_rate,
        enhanced_stable["events_only_monitor"],
    )
    write_wav(
        background_monitor_path,
        audio.sample_rate,
        enhanced_stable["background_only_monitor"],
    )
    write_wav(
        removed_noise_path,
        audio.sample_rate,
        enhanced_stable["removed_noise_monitor"],
    )
    created.extend(
        [
            stable_clean_path,
            stable_enhanced_path,
            events_monitor_path,
            background_monitor_path,
            removed_noise_path,
        ]
    )

    stable_filtered_start = int(round(audio_crop_start_s * analysis_rate))
    stable_filtered_end = int(round(audio_crop_end_s * analysis_rate))
    stable_filtered_path = named("stable_segment_filtered.wav")
    write_wav(
        stable_filtered_path,
        analysis_rate,
        filtered[stable_filtered_start:stable_filtered_end],
    )
    created.append(stable_filtered_path)

    authentic_segment, authentic_replacements = repair_authentic_segment(
        stable_raw,
        centred,
        audio.sample_rate,
        result,
        audio_crop_start_s,
    )
    authentic_source_path = named("stable_segment_event_repaired.wav")
    write_wav(authentic_source_path, audio.sample_rate, authentic_segment)
    created.append(authentic_source_path)
    authentic_schedule = build_authentic_schedule(result, config.loop_duration_s)
    authentic_raw = render_scheduled_events(
        centred,
        audio.sample_rate,
        result,
        authentic_schedule,
        config.loop_duration_s,
    )
    enhanced_authentic = enhance_heartbeat_audio(
        authentic_raw,
        audio.sample_rate,
        authentic_schedule,
        0.0,
        config,
    )

    authentic_path = named("authentic_loop.wav")
    authentic_clean = enhanced_authentic["clean"]
    authentic_enhanced = enhanced_authentic["enhanced"]
    write_wav(authentic_path, audio.sample_rate, authentic_raw)
    created.append(authentic_path)
    authentic_clean_path = named("authentic_loop_clean.wav")
    authentic_enhanced_path = named("authentic_loop_enhanced.wav")
    write_wav(
        authentic_clean_path,
        audio.sample_rate,
        authentic_clean,
    )
    write_wav(
        authentic_enhanced_path,
        audio.sample_rate,
        authentic_enhanced,
    )
    created.extend([authentic_clean_path, authentic_enhanced_path])

    regularized_path = named("regularized_events.wav")
    regularized_signal = render_regularized_events(
        centred, audio.sample_rate, result, config.loop_duration_s
    )
    write_wav(
        regularized_path,
        audio.sample_rate,
        regularized_signal,
    )
    created.append(regularized_path)

    scheduled_events = build_regularized_schedule(result, config.loop_duration_s)
    enhanced_regularized = enhance_heartbeat_audio(
        regularized_signal,
        audio.sample_rate,
        scheduled_events,
        0.0,
        config,
    )
    regularized_clean_path = named("regularized_events_clean.wav")
    regularized_enhanced_path = named("regularized_events_enhanced.wav")
    write_wav(
        regularized_clean_path,
        audio.sample_rate,
        enhanced_regularized["clean"],
    )
    write_wav(
        regularized_enhanced_path,
        audio.sample_rate,
        enhanced_regularized["enhanced"],
    )
    created.extend([regularized_clean_path, regularized_enhanced_path])

    event_energy_rows, event_preservation = measure_loop_event_energy(
        audio.sample_rate,
        [
            ("authentic", "raw", authentic_raw, authentic_schedule),
            ("authentic", "clean", authentic_clean, authentic_schedule),
            ("authentic", "enhanced", authentic_enhanced, authentic_schedule),
            ("regularized", "raw", regularized_signal, scheduled_events),
            (
                "regularized",
                "clean",
                enhanced_regularized["clean"],
                scheduled_events,
            ),
            (
                "regularized",
                "enhanced",
                enhanced_regularized["enhanced"],
                scheduled_events,
            ),
        ],
    )
    event_energy_path = named("loop_event_energy.csv")
    with event_energy_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(event_energy_rows[0].keys()))
        writer.writeheader()
        writer.writerows(event_energy_rows)
    created.append(event_energy_path)

    comparison_stems = {
        "stable_comparison": named("stable_comparison"),
        "authentic_loop_comparison": named("authentic_loop_comparison"),
        "regularized_comparison": named("regularized_comparison"),
        "noise_diagnostics": named("noise_diagnostics"),
    }
    time_energy_stems = {
        "stable_raw": named("stable_segment_raw_time_energy"),
        "stable_clean": named("stable_segment_clean_time_energy"),
        "stable_enhanced": named("stable_segment_enhanced_time_energy"),
        "authentic_base": named("authentic_loop_time_energy"),
        "authentic_clean": named("authentic_loop_clean_time_energy"),
        "authentic_enhanced": named("authentic_loop_enhanced_time_energy"),
        "regularized_base": named("regularized_events_time_energy"),
        "regularized_clean": named("regularized_events_clean_time_energy"),
        "regularized_enhanced": named("regularized_events_enhanced_time_energy"),
    }

    type_counts = {"S1": 0, "S2": 0}
    for event in stable_events:
        type_counts[event.type] += 1
        clip, _ = _event_clip(centred, audio.sample_rate, event.time_s, event.type)
        path = samples_dir / (
            f"{prefix}_{event.type.lower()}_{type_counts[event.type]:03d}.wav"
        )
        write_wav(path, audio.sample_rate, clip)

    cycles_path = named("cycles.csv")
    with cycles_path.open("w", newline="", encoding="utf-8") as stream:
        fieldnames = list(asdict(result.cycles[0]).keys())
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for cycle in result.cycles:
            row = asdict(cycle)
            row["rejection_reasons"] = ";".join(cycle.rejection_reasons)
            writer.writerow(row)
    created.append(cycles_path)

    timeline = build_event_timelines(result)
    events_path = named("heartbeat_events.csv")
    with events_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "cycle_index",
                "type",
                "raw_time_s",
                "smoothed_time_s",
                "regularized_time_s",
            ],
        )
        writer.writeheader()
        writer.writerows(timeline)
    created.append(events_path)

    window_stats = diagnostics.compute_window_statistics(
        audio.signal, audio.sample_rate, window_ms=50.0, hop_ms=10.0
    )
    energy_path = named("window_energy.csv")
    diagnostics.write_csv(energy_path, window_stats)
    created.append(energy_path)

    analysis_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "processor": {"name": "heart_sound_processor.py", "version": VERSION},
        "source": {
            "path": str(input_path.resolve()),
            "sha256": diagnostics.file_sha256(input_path),
            "sample_id": identity["sample_id"],
            "acquisition_time": identity["acquisition_time"],
            "time_source": identity["time_source"],
            "output_prefix": prefix,
            "analysis_group_directory": group_dir.name,
            "sample_rate_hz": audio.sample_rate,
            "channels": audio.channels,
            "dtype": audio.original_dtype,
            "duration_s": audio.duration_s,
        },
        "status": result.status,
        "confidence": result.confidence,
        "bpm": result.bpm,
        "period_s": result.period_s,
        "s1_s2_s": result.s1_s2_s,
        "periodicity": result.periodicity,
        "stable_region": asdict(result.stable_region),
        "audio_crop": {
            "start_s": audio_crop_start_s,
            "end_s": audio_crop_end_s,
            "shared_offset_from_s1_s": loop_boundary_offset_s,
            "duration_s": audio_crop_end_s - audio_crop_start_s,
            "selection": "minimum joint 8-ms local energy within 100 ms before both S1 boundaries",
        },
        "period_candidates": result.period_candidates,
        "warnings": result.warnings,
        "config": asdict(config),
        "cycles": [asdict(cycle) for cycle in result.cycles],
        "events": [asdict(event) for event in result.events],
        "timelines": timeline,
        "quality": {
            "peak_abs_fs": float(np.max(np.abs(audio.signal))),
            "rms_fs": float(np.sqrt(np.mean(audio.signal * audio.signal))),
            "dc_offset_fs": float(np.mean(audio.signal)),
            "clipped_fraction": audio.clipped_fraction,
        },
        "enhancement": {
            "method": "event-aware spectral subtraction, background attenuation, and envelope compression",
            "parameters": {
                "render_band_hz": [
                    config.bandpass_low_hz,
                    config.enhancement_high_hz,
                ],
                "maximum_spectral_reduction_db": config.denoise_reduction_db,
                "spectral_oversubtraction": config.spectral_oversubtraction,
                "background_attenuation_db": config.background_attenuation_db,
                "compressor_threshold_dbfs": config.compressor_threshold_dbfs,
                "compressor_ratio": config.compressor_ratio,
                "target_rms_dbfs": config.enhanced_target_rms_dbfs,
                "peak_target_dbfs": config.enhanced_peak_dbfs,
            },
            "stable_segment_details": enhanced_stable["details"],
            "regularized_details": enhanced_regularized["details"],
            "reference": {
                "doi": "10.1109/TASSP.1979.1163209",
                "note": "Spectral subtraction inspiration; this implementation adds event-derived noise-frame selection, smoothing, and a gain floor.",
            },
        },
        "event_preservation": {
            **event_preservation,
            "reference_event_cycles": {
                event_type: [event.cycle_index for event in events]
                for event_type, events in select_reference_events(result).items()
            },
            "stable_event_repair_replacement_count": len(authentic_replacements),
            "stable_event_repair_replacements": authentic_replacements,
            "authentic_rhythm": {
                **schedule_rhythm_metrics(authentic_schedule),
                "construction": "median-filtered valid-cycle periods, bounded to +/-6%; S1-S2 intervals bounded to +/-10%",
            },
            "regularized_rhythm": {
                **schedule_rhythm_metrics(scheduled_events),
                "construction": "constant median period and constant median S1-S2 interval",
            },
            "policy": (
                "both loops use vetted valid-cycle events; authentic uses bounded natural "
                "timing variation, while regularized uses a constant timing grid"
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "method_references": [
            {"doi": "10.1088/0967-3334/31/4/004", "role": "duration-constrained PCG segmentation"},
            {"doi": "10.1109/TBME.2015.2475278", "role": "S1-systole-S2-diastole state sequence"},
            {"doi": "10.1109/ACCESS.2024.3351570", "role": "Shannon-energy envelope and lobe rejection"},
        ],
        "outputs": {
            "stable_segment_raw": stable_raw_path.name,
            "stable_segment_filtered": stable_filtered_path.name,
            "stable_segment_clean": stable_clean_path.name,
            "stable_segment_enhanced": stable_enhanced_path.name,
            "stable_segment_event_repaired": authentic_source_path.name,
            "authentic_loop": authentic_path.name,
            "authentic_loop_clean": authentic_clean_path.name,
            "authentic_loop_enhanced": authentic_enhanced_path.name,
            "regularized_events": regularized_path.name,
            "regularized_events_clean": regularized_clean_path.name,
            "regularized_events_enhanced": regularized_enhanced_path.name,
            "events_only_monitor": events_monitor_path.name,
            "background_only_monitor": background_monitor_path.name,
            "removed_noise_monitor": removed_noise_path.name,
            "loop_event_energy": event_energy_path.name,
            "event_samples": f"{samples_dir.name}/",
            "stable_comparison_png": comparison_stems["stable_comparison"].with_suffix(".png").name,
            "stable_comparison_svg": comparison_stems["stable_comparison"].with_suffix(".svg").name,
            "authentic_loop_comparison_png": comparison_stems["authentic_loop_comparison"].with_suffix(".png").name,
            "authentic_loop_comparison_svg": comparison_stems["authentic_loop_comparison"].with_suffix(".svg").name,
            "regularized_comparison_png": comparison_stems["regularized_comparison"].with_suffix(".png").name,
            "regularized_comparison_svg": comparison_stems["regularized_comparison"].with_suffix(".svg").name,
            "noise_diagnostics_png": comparison_stems["noise_diagnostics"].with_suffix(".png").name,
            "noise_diagnostics_svg": comparison_stems["noise_diagnostics"].with_suffix(".svg").name,
            "time_energy_figures": {
                key: {
                    "png": stem.with_suffix(".png").name,
                    "svg": stem.with_suffix(".svg").name,
                }
                for key, stem in time_energy_stems.items()
            },
        },
    }
    analysis_path = named("analysis.json")
    analysis_path.write_text(
        json.dumps(analysis_payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    created.append(analysis_path)

    plot_annotations = diagnostics.PlotAnnotations(
        stable_start_s=result.stable_region.start_s,
        stable_end_s=result.stable_region.end_s,
        events=tuple(
            (event.type, event.time_s)
            for event in stable_events
            if event.confidence >= 0.35
        ),
    )
    waveform = diagnostics.minmax_waveform_envelope(
        audio.signal, audio.sample_rate, 5000
    )
    diagnostic_svg = named("diagnostic.svg")
    diagnostic_png = named("diagnostic.png")
    diagnostics.render_svg(
        diagnostic_svg,
        input_path.name,
        audio,
        window_stats,
        waveform,
        plot_annotations,
        -60.0,
        1600,
        900,
        analysis_payload,
    )
    diagnostics.render_png(
        diagnostic_png,
        input_path.name,
        audio,
        window_stats,
        waveform,
        plot_annotations,
        -60.0,
        1600,
        900,
        200,
        analysis_payload,
    )
    created.extend([diagnostic_svg, diagnostic_png])

    relative_stable_events = tuple(
        (event.type, event.time_s - audio_crop_start_s)
        for event in stable_events
        if event.confidence >= 0.35
    )
    figure_metadata = {
        "processor": {"name": "heart_sound_processor.py", "version": VERSION},
        "diagnostics": {
            "name": "audio_diagnostics.py",
            "version": diagnostics.SCRIPT_VERSION,
        },
        "source_sha256": analysis_payload["source"]["sha256"],
        "analysis_manifest": analysis_path.name,
        "event_energy_table": event_energy_path.name,
        "rms_window_ms": 50.0,
        "rms_hop_ms": 10.0,
        "spectrum": "Welch PSD, Hann window, 50% overlap",
    }
    comparison_specs = [
        (
            comparison_stems["stable_comparison"],
            "Stable segment: raw / clean / enhanced",
            [
                ("Raw", stable_raw),
                ("Clean", enhanced_stable["clean"]),
                ("Enhanced", enhanced_stable["enhanced"]),
            ],
            relative_stable_events,
        ),
        (
            comparison_stems["authentic_loop_comparison"],
            "Authentic loop: event-preserved / clean / enhanced",
            [
                ("Event-preserved", authentic_raw),
                ("Clean", authentic_clean),
                ("Enhanced", authentic_enhanced),
            ],
            tuple((event.type, event.time_s) for event in authentic_schedule),
        ),
        (
            comparison_stems["regularized_comparison"],
            "Regularized loop: vetted events / clean / enhanced",
            [
                ("Vetted events", regularized_signal),
                ("Clean", enhanced_regularized["clean"]),
                ("Enhanced", enhanced_regularized["enhanced"]),
            ],
            tuple((event.type, event.time_s) for event in scheduled_events),
        ),
        (
            comparison_stems["noise_diagnostics"],
            "Noise inspection stems (monitoring gain applied)",
            [
                ("Events only monitor", enhanced_stable["events_only_monitor"]),
                ("Background only monitor", enhanced_stable["background_only_monitor"]),
                ("Removed noise monitor", enhanced_stable["removed_noise_monitor"]),
            ],
            relative_stable_events,
        ),
    ]
    for stem, title, plot_series, plot_events in comparison_specs:
        png_path, svg_path = diagnostics.render_comparison_pair(
            stem,
            title,
            audio.sample_rate,
            plot_series,
            plot_events,
            figure_metadata,
        )
        created.extend([png_path, svg_path])

    authentic_plot_events = tuple(
        (event.type, event.time_s) for event in authentic_schedule
    )
    regularized_plot_events = tuple(
        (event.type, event.time_s) for event in scheduled_events
    )
    time_energy_specs = [
        ("stable_raw", "Stable segment — Raw", stable_raw, relative_stable_events),
        (
            "stable_clean",
            "Stable segment — Clean",
            enhanced_stable["clean"],
            relative_stable_events,
        ),
        (
            "stable_enhanced",
            "Stable segment — Enhanced",
            enhanced_stable["enhanced"],
            relative_stable_events,
        ),
        (
            "authentic_base",
            "Authentic loop — Bounded natural rhythm",
            authentic_raw,
            authentic_plot_events,
        ),
        (
            "authentic_clean",
            "Authentic loop — Clean",
            authentic_clean,
            authentic_plot_events,
        ),
        (
            "authentic_enhanced",
            "Authentic loop — Enhanced",
            authentic_enhanced,
            authentic_plot_events,
        ),
        (
            "regularized_base",
            "Regularized loop — Fixed rhythm",
            regularized_signal,
            regularized_plot_events,
        ),
        (
            "regularized_clean",
            "Regularized loop — Clean",
            enhanced_regularized["clean"],
            regularized_plot_events,
        ),
        (
            "regularized_enhanced",
            "Regularized loop — Enhanced",
            enhanced_regularized["enhanced"],
            regularized_plot_events,
        ),
    ]
    for key, title, plot_signal, plot_events in time_energy_specs:
        png_path, svg_path = diagnostics.render_time_energy_pair(
            time_energy_stems[key],
            title,
            audio.sample_rate,
            plot_signal,
            plot_events,
            figure_metadata,
        )
        created.extend([png_path, svg_path])
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Automatically analyse, select, and extract stable normal heart sounds."
    )
    parser.add_argument("input", help="Input WAV recording.")
    parser.add_argument("--output-dir", default="processed_heartbeat")
    parser.add_argument("--analysis-sample-rate", type=int, default=2000)
    parser.add_argument("--bandpass-low-hz", type=float, default=20.0)
    parser.add_argument("--bandpass-high-hz", type=float, default=200.0)
    parser.add_argument("--min-bpm", type=float, default=40.0)
    parser.add_argument("--max-bpm", type=float, default=160.0)
    parser.add_argument("--stable-min-s", type=float, default=8.0)
    parser.add_argument("--stable-target-s", type=float, default=10.0)
    parser.add_argument("--stable-max-s", type=float, default=12.0)
    parser.add_argument("--stable-min-cycles", type=int, default=8)
    parser.add_argument("--loop-duration-s", type=float, default=30.0)
    parser.add_argument("--denoise-reduction-db", type=float, default=12.0)
    parser.add_argument("--background-attenuation-db", type=float, default=8.0)
    parser.add_argument("--compressor-threshold-dbfs", type=float, default=-22.0)
    parser.add_argument("--compressor-ratio", type=float, default=3.0)
    parser.add_argument("--enhanced-target-rms-dbfs", type=float, default=-14.0)
    parser.add_argument("--enhanced-peak-dbfs", type=float, default=-1.0)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def write_rejection_report(
    output_dir: Path,
    input_path: Path,
    config: Config,
    reason: str,
    audio: diagnostics.AudioData | None,
) -> Path:
    """Persist a machine-readable failure instead of silently producing output."""
    output_dir.mkdir(parents=True, exist_ok=True)
    identity = source_identity(input_path)
    prefix = identity["prefix"]
    group_dir = output_dir / prefix
    group_dir.mkdir(parents=True, exist_ok=True)
    source: dict[str, Any] = {
        "path": str(input_path),
        "sample_id": identity["sample_id"],
        "acquisition_time": identity["acquisition_time"],
        "time_source": identity["time_source"],
        "output_prefix": prefix,
        "analysis_group_directory": group_dir.name,
    }
    if input_path.is_file():
        source["sha256"] = diagnostics.file_sha256(input_path)
    if audio is not None:
        source.update(
            {
                "sample_rate_hz": audio.sample_rate,
                "channels": audio.channels,
                "dtype": audio.original_dtype,
                "duration_s": audio.duration_s,
                "clipped_fraction": audio.clipped_fraction,
            }
        )
    payload = {
        "schema_version": "1.0",
        "processor": {"name": "heart_sound_processor.py", "version": VERSION},
        "status": "REJECT",
        "rejection_reasons": [reason],
        "source": source,
        "config": asdict(config),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    path = group_dir / f"{prefix}_analysis.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config(
        analysis_sample_rate=args.analysis_sample_rate,
        bandpass_low_hz=args.bandpass_low_hz,
        bandpass_high_hz=args.bandpass_high_hz,
        min_bpm=args.min_bpm,
        max_bpm=args.max_bpm,
        stable_min_s=args.stable_min_s,
        stable_target_s=args.stable_target_s,
        stable_max_s=args.stable_max_s,
        stable_min_cycles=args.stable_min_cycles,
        loop_duration_s=args.loop_duration_s,
        denoise_reduction_db=args.denoise_reduction_db,
        background_attenuation_db=args.background_attenuation_db,
        compressor_threshold_dbfs=args.compressor_threshold_dbfs,
        compressor_ratio=args.compressor_ratio,
        enhanced_target_rms_dbfs=args.enhanced_target_rms_dbfs,
        enhanced_peak_dbfs=args.enhanced_peak_dbfs,
    )
    if not (
        0 < config.stable_min_s <= config.stable_target_s <= config.stable_max_s
    ):
        parser.error("Stable duration must satisfy min <= target <= max.")
    if not 0 < config.min_bpm < config.max_bpm:
        parser.error("BPM bounds are invalid.")
    if config.denoise_reduction_db < 0 or config.background_attenuation_db < 0:
        parser.error("Denoise and background attenuation must be non-negative.")
    if config.compressor_ratio < 1.0:
        parser.error("Compressor ratio must be at least 1.0.")
    if config.enhanced_peak_dbfs > 0:
        parser.error("Enhanced peak target must not exceed 0 dBFS.")
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    audio: diagnostics.AudioData | None = None
    try:
        audio = diagnostics.load_wav(input_path)
        if audio.duration_s < config.stable_min_s:
            raise ValueError(
                f"Audio duration {audio.duration_s:.3f}s is shorter than the "
                f"minimum stable duration {config.stable_min_s:.3f}s."
            )
        if float(np.max(np.abs(audio.signal))) < 1e-5:
            raise ValueError("The input is silent or below the usable amplitude floor.")
        result, centred, filtered, analysis_rate = analyse(
            audio.signal, audio.sample_rate, config
        )
        if audio.clipped_fraction > 0.01:
            result.warnings.append("more_than_1_percent_samples_are_clipped")
            if result.status == "PASS":
                result.status = "PASS_WITH_WARNING"
        if audio.clipped_fraction > 0.10:
            result.status = "REJECT"
        created = write_outputs(
            input_path,
            output_dir,
            audio,
            result,
            centred,
            filtered,
            analysis_rate,
            config,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        report = write_rejection_report(
            output_dir, input_path, config, str(exc), audio
        )
        print("status=REJECT", file=sys.stderr)
        print(f"reason={exc}", file=sys.stderr)
        print(report, file=sys.stderr)
        return 2
    print(f"status={result.status}")
    print(f"confidence={result.confidence:.4f}")
    print(f"bpm={result.bpm:.3f}")
    print(
        f"stable={result.stable_region.start_s:.3f}-"
        f"{result.stable_region.end_s:.3f}s"
    )
    for path in created:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
