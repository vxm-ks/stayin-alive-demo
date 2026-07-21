#!/usr/bin/env python3
"""Generate reproducible waveform and short-time energy diagnostics for WAV audio.

The script is intentionally independent of the later music-generation pipeline.
It reads an integer-PCM or IEEE-float WAV file, converts it to a mono full-scale
signal, computes exact windowed peak and RMS energy values, and exports:

* a publication-ready PNG;
* a vector SVG;
* the numeric window statistics as CSV; and
* a JSON manifest containing parameters, versions, metrics, and a source hash.

No medical diagnosis is performed. See METHODS.md and THIRD_PARTY.md for the
method description, citations, licenses, and reproducibility notes.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import scipy
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from scipy.io import wavfile
from scipy.signal import welch


SCRIPT_VERSION = "1.3.0"


@dataclass(frozen=True)
class AudioData:
    sample_rate: int
    signal: np.ndarray
    channels: int
    original_dtype: str
    clipped_fraction: float

    @property
    def duration_s(self) -> float:
        return len(self.signal) / self.sample_rate


@dataclass(frozen=True)
class WindowStatistics:
    starts_s: np.ndarray
    ends_s: np.ndarray
    centers_s: np.ndarray
    peak_abs_fs: np.ndarray
    rms_fs: np.ndarray
    energy_dbfs: np.ndarray


@dataclass(frozen=True)
class PlotAnnotations:
    stable_start_s: float | None = None
    stable_end_s: float | None = None
    events: tuple[tuple[str, float], ...] = ()


def _normalise_samples(data: np.ndarray) -> tuple[np.ndarray, float]:
    """Return float64 full-scale samples and the exact clipping fraction."""
    if np.issubdtype(data.dtype, np.unsignedinteger):
        info = np.iinfo(data.dtype)
        midpoint = (info.max + 1) / 2.0
        normalised = (data.astype(np.float64) - midpoint) / midpoint
        clipped = (data == info.min) | (data == info.max)
    elif np.issubdtype(data.dtype, np.signedinteger):
        info = np.iinfo(data.dtype)
        scale = float(max(abs(info.min), info.max))
        normalised = data.astype(np.float64) / scale
        clipped = (data == info.min) | (data == info.max)
    elif np.issubdtype(data.dtype, np.floating):
        normalised = data.astype(np.float64)
        clipped = np.abs(normalised) >= 1.0
    else:
        raise TypeError(f"Unsupported WAV sample dtype: {data.dtype}")
    return normalised, float(np.mean(clipped))


def load_wav(path: Path, channel_mode: str = "mean") -> AudioData:
    """Load a WAV file and convert its channels to one full-scale signal."""
    sample_rate, raw = wavfile.read(path)
    if sample_rate <= 0 or raw.size == 0:
        raise ValueError("The WAV file is empty or has an invalid sample rate.")

    original_dtype = str(raw.dtype)
    normalised, clipped_fraction = _normalise_samples(raw)
    if normalised.ndim == 1:
        signal = normalised
        channels = 1
    elif normalised.ndim == 2:
        channels = int(normalised.shape[1])
        if channel_mode == "mean":
            signal = np.mean(normalised, axis=1)
        elif channel_mode == "first":
            signal = normalised[:, 0]
        else:
            raise ValueError(f"Unsupported channel mode: {channel_mode}")
    else:
        raise ValueError(f"Expected mono or multichannel audio, got shape {raw.shape}.")

    if not np.all(np.isfinite(signal)):
        raise ValueError("The WAV contains NaN or infinite samples.")
    return AudioData(
        sample_rate=int(sample_rate),
        signal=np.ascontiguousarray(signal, dtype=np.float64),
        channels=channels,
        original_dtype=original_dtype,
        clipped_fraction=clipped_fraction,
    )


def compute_window_statistics(
    signal: np.ndarray,
    sample_rate: int,
    window_ms: float,
    hop_ms: float,
    db_epsilon: float = 1e-12,
) -> WindowStatistics:
    """Compute peak, RMS, and dBFS for every analysis window."""
    if window_ms <= 0 or hop_ms <= 0:
        raise ValueError("window_ms and hop_ms must both be positive.")
    window_samples = max(1, int(round(window_ms * sample_rate / 1000.0)))
    hop_samples = max(1, int(round(hop_ms * sample_rate / 1000.0)))
    starts = np.arange(0, len(signal), hop_samples, dtype=np.int64)
    ends = np.minimum(starts + window_samples, len(signal))

    squared = signal * signal
    cumulative = np.concatenate(([0.0], np.cumsum(squared, dtype=np.float64)))
    sum_squares = cumulative[ends] - cumulative[starts]
    lengths = np.maximum(1, ends - starts)
    rms = np.sqrt(sum_squares / lengths)
    peak = np.fromiter(
        (np.max(np.abs(signal[start:end])) for start, end in zip(starts, ends)),
        dtype=np.float64,
        count=len(starts),
    )
    energy_dbfs = 20.0 * np.log10(np.maximum(rms, db_epsilon))
    return WindowStatistics(
        starts_s=starts / sample_rate,
        ends_s=ends / sample_rate,
        centers_s=(starts + ends) / (2.0 * sample_rate),
        peak_abs_fs=peak,
        rms_fs=rms,
        energy_dbfs=energy_dbfs,
    )


def minmax_waveform_envelope(
    signal: np.ndarray, sample_rate: int, max_bins: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Downsample a waveform without discarding narrow positive/negative peaks."""
    if max_bins < 100:
        raise ValueError("max_bins must be at least 100.")
    bin_count = min(max_bins, len(signal))
    edges = np.linspace(0, len(signal), bin_count + 1, dtype=np.int64)
    times = np.empty(bin_count, dtype=np.float64)
    lower = np.empty(bin_count, dtype=np.float64)
    upper = np.empty(bin_count, dtype=np.float64)
    for index, (start, end) in enumerate(zip(edges[:-1], edges[1:])):
        chunk = signal[start:end]
        times[index] = (start + end) / (2.0 * sample_rate)
        lower[index] = np.min(chunk)
        upper[index] = np.max(chunk)
    return times, lower, upper


def read_annotations(path: Path | None) -> PlotAnnotations:
    """Read optional stable-region and S1/S2 annotations from analysis JSON."""
    if path is None:
        return PlotAnnotations()
    payload = json.loads(path.read_text(encoding="utf-8"))
    stable = payload.get("stable_region") or {}
    start = stable.get("start_s", stable.get("start"))
    end = stable.get("end_s", stable.get("end"))
    events: list[tuple[str, float]] = []
    for event in payload.get("events", []):
        event_type = str(event.get("type", "")).upper()
        event_time = event.get("time_s", event.get("time", event.get("time_raw")))
        if event_type in {"S1", "S2"} and event_time is not None:
            events.append((event_type, float(event_time)))
    return PlotAnnotations(
        stable_start_s=float(start) if start is not None else None,
        stable_end_s=float(end) if end is not None else None,
        events=tuple(sorted(events, key=lambda item: item[1])),
    )


def validate_annotations(annotations: PlotAnnotations, duration_s: float) -> None:
    has_start = annotations.stable_start_s is not None
    has_end = annotations.stable_end_s is not None
    if has_start != has_end:
        raise ValueError("Stable-region start and end must be supplied together.")
    if has_start:
        assert annotations.stable_start_s is not None
        assert annotations.stable_end_s is not None
        if not 0 <= annotations.stable_start_s < annotations.stable_end_s <= duration_s:
            raise ValueError("Stable region must lie inside the audio duration.")
    for event_type, event_time in annotations.events:
        if event_type not in {"S1", "S2"} or not 0 <= event_time <= duration_s:
            raise ValueError(f"Invalid event annotation: {(event_type, event_time)}")


def merge_annotation_overrides(
    annotations: PlotAnnotations,
    stable_start_s: float | None,
    stable_end_s: float | None,
) -> PlotAnnotations:
    if stable_start_s is None and stable_end_s is None:
        return annotations
    if stable_start_s is None or stable_end_s is None:
        raise ValueError("--stable-start and --stable-end must be used together.")
    return PlotAnnotations(stable_start_s, stable_end_s, annotations.events)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nice_time_ticks(duration_s: float, target_count: int = 7) -> list[float]:
    raw_step = duration_s / max(1, target_count)
    exponent = 10 ** math.floor(math.log10(max(raw_step, 1e-12)))
    scaled = raw_step / exponent
    step_base = 1 if scaled <= 1 else 2 if scaled <= 2 else 5 if scaled <= 5 else 10
    step = step_base * exponent
    ticks = list(np.arange(0.0, duration_s + step * 0.5, step))
    if not ticks or ticks[-1] < duration_s * 0.98:
        ticks.append(duration_s)
    return [float(min(value, duration_s)) for value in ticks]


def _plot_geometry(width: int, height: int) -> dict[str, float]:
    return {
        "left": width * 0.075,
        "right": width * 0.025,
        "wave_top": height * 0.14,
        "wave_bottom": height * 0.47,
        "energy_top": height * 0.61,
        "energy_bottom": height * 0.90,
    }


def _plot_scales(
    duration_s: float,
    peak_abs: float,
    energy_dbfs: np.ndarray,
    energy_floor_db: float,
    geometry: dict[str, float],
    width: int,
):
    amp_limit = max(1.0, math.ceil(peak_abs * 10.0) / 10.0)
    energy_top_db = max(0.0, math.ceil(float(np.max(energy_dbfs)) / 10.0) * 10.0)
    energy_bottom_db = min(energy_floor_db, math.floor(float(np.min(energy_dbfs)) / 10.0) * 10.0)
    plot_width = width - geometry["left"] - geometry["right"]

    def x_scale(value: float) -> float:
        return geometry["left"] + value / duration_s * plot_width

    def wave_y(value: float) -> float:
        span = geometry["wave_bottom"] - geometry["wave_top"]
        return geometry["wave_top"] + (amp_limit - value) / (2.0 * amp_limit) * span

    def energy_y(value: float) -> float:
        span = geometry["energy_bottom"] - geometry["energy_top"]
        clipped = min(energy_top_db, max(energy_bottom_db, value))
        return geometry["energy_top"] + (energy_top_db - clipped) / (
            energy_top_db - energy_bottom_db
        ) * span

    return x_scale, wave_y, energy_y, amp_limit, energy_bottom_db, energy_top_db


def _xml_points(points: Sequence[tuple[float, float]]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def render_svg(
    path: Path,
    title: str,
    audio: AudioData,
    statistics: WindowStatistics,
    waveform: tuple[np.ndarray, np.ndarray, np.ndarray],
    annotations: PlotAnnotations,
    energy_floor_db: float,
    width: int,
    height: int,
    metadata: dict[str, Any],
) -> None:
    geometry = _plot_geometry(width, height)
    times, lower, upper = waveform
    x_scale, wave_y, energy_y, amp_limit, energy_bottom_db, energy_top_db = _plot_scales(
        audio.duration_s,
        float(np.max(np.abs(audio.signal))),
        statistics.energy_dbfs,
        energy_floor_db,
        geometry,
        width,
    )
    colors = {
        "background": "#ffffff",
        "foreground": "#172033",
        "muted": "#64748b",
        "grid": "#dbe3ee",
        "wave": "#2563a5",
        "wave_fill": "#bfdbfe",
        "energy": "#0f766e",
        "stable": "#fef3c7",
        "s1": "#c2410c",
        "s2": "#7c3aed",
    }
    upper_points = [(x_scale(t), wave_y(v)) for t, v in zip(times, upper)]
    lower_points = [(x_scale(t), wave_y(v)) for t, v in zip(times[::-1], lower[::-1])]
    energy_points = [
        (x_scale(t), energy_y(v))
        for t, v in zip(statistics.centers_s, statistics.energy_dbfs)
    ]
    subtitle = (
        f"{audio.sample_rate} Hz | {audio.channels} channel(s) | "
        f"{audio.duration_s:.3f} s | {audio.original_dtype}"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f"<title id=\"title\">{html.escape(title)}</title>",
        "<desc id=\"desc\">Waveform and short-time RMS energy versus time.</desc>",
        f"<metadata>{html.escape(json.dumps(metadata, ensure_ascii=False, sort_keys=True))}</metadata>",
        f'<rect width="100%" height="100%" fill="{colors["background"]}"/>',
        f'<text x="{geometry["left"]:.1f}" y="{height * 0.055:.1f}" '
        f'font-family="Arial, sans-serif" font-size="{height * 0.030:.1f}" '
        f'font-weight="600" fill="{colors["foreground"]}">{html.escape(title)}</text>',
        f'<text x="{geometry["left"]:.1f}" y="{height * 0.090:.1f}" '
        f'font-family="Arial, sans-serif" font-size="{height * 0.017:.1f}" '
        f'fill="{colors["muted"]}">{html.escape(subtitle)}</text>',
    ]

    if annotations.stable_start_s is not None and annotations.stable_end_s is not None:
        stable_x = x_scale(annotations.stable_start_s)
        stable_width = x_scale(annotations.stable_end_s) - stable_x
        parts.append(
            f'<rect x="{stable_x:.2f}" y="{geometry["wave_top"]:.2f}" '
            f'width="{stable_width:.2f}" height="{geometry["energy_bottom"] - geometry["wave_top"]:.2f}" '
            f'fill="{colors["stable"]}"/>'
        )

    time_ticks = _nice_time_ticks(audio.duration_s)
    amplitude_ticks = [-amp_limit, 0.0, amp_limit]
    energy_ticks = list(np.linspace(energy_bottom_db, energy_top_db, 4))
    for value in amplitude_ticks:
        y = wave_y(value)
        parts.append(
            f'<line x1="{geometry["left"]:.2f}" y1="{y:.2f}" x2="{width - geometry["right"]:.2f}" '
            f'y2="{y:.2f}" stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{geometry["left"] - 12:.2f}" y="{y + 5:.2f}" text-anchor="end" '
            f'font-family="Arial, sans-serif" font-size="{height * 0.015:.1f}" fill="{colors["muted"]}">{value:.1f}</text>'
        )
    for value in energy_ticks:
        y = energy_y(float(value))
        parts.append(
            f'<line x1="{geometry["left"]:.2f}" y1="{y:.2f}" x2="{width - geometry["right"]:.2f}" '
            f'y2="{y:.2f}" stroke="{colors["grid"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{geometry["left"] - 12:.2f}" y="{y + 5:.2f}" text-anchor="end" '
            f'font-family="Arial, sans-serif" font-size="{height * 0.015:.1f}" fill="{colors["muted"]}">{value:.0f}</text>'
        )
    for value in time_ticks:
        x = x_scale(value)
        parts.append(
            f'<line x1="{x:.2f}" y1="{geometry["energy_bottom"]:.2f}" x2="{x:.2f}" '
            f'y2="{geometry["energy_bottom"] + 7:.2f}" stroke="{colors["muted"]}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.2f}" y="{geometry["energy_bottom"] + height * 0.030:.2f}" text-anchor="middle" '
            f'font-family="Arial, sans-serif" font-size="{height * 0.015:.1f}" fill="{colors["muted"]}">{value:g}</text>'
        )

    parts.extend(
        [
            f'<text x="{geometry["left"]:.2f}" y="{geometry["wave_top"] - height * 0.018:.2f}" '
            f'font-family="Arial, sans-serif" font-size="{height * 0.019:.1f}" fill="{colors["foreground"]}">Waveform amplitude (FS)</text>',
            f'<text x="{geometry["left"]:.2f}" y="{geometry["energy_top"] - height * 0.018:.2f}" '
            f'font-family="Arial, sans-serif" font-size="{height * 0.019:.1f}" fill="{colors["foreground"]}">Short-time RMS energy (dBFS)</text>',
            f'<polygon points="{_xml_points(upper_points + lower_points)}" fill="{colors["wave_fill"]}"/>',
            f'<polyline points="{_xml_points(upper_points)}" fill="none" stroke="{colors["wave"]}" stroke-width="1.5"/>',
            f'<polyline points="{_xml_points([(x_scale(t), wave_y(v)) for t, v in zip(times, lower)])}" '
            f'fill="none" stroke="{colors["wave"]}" stroke-width="1.5"/>',
            f'<polyline points="{_xml_points(energy_points)}" fill="none" stroke="{colors["energy"]}" '
            f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>',
        ]
    )
    for event_type, event_time in annotations.events:
        color = colors["s1"] if event_type == "S1" else colors["s2"]
        event_x = x_scale(event_time)
        parts.append(
            f'<line x1="{event_x:.2f}" y1="{geometry["wave_top"]:.2f}" x2="{event_x:.2f}" '
            f'y2="{geometry["energy_bottom"]:.2f}" stroke="{color}" stroke-width="1.5" stroke-dasharray="5 5"/>'
        )
        parts.append(
            f'<circle cx="{event_x:.2f}" cy="{geometry["wave_top"]:.2f}" r="4" fill="{color}"/>'
        )
    parts.append(
        f'<text x="{geometry["left"] + (width - geometry["right"] - geometry["left"]) / 2:.2f}" '
        f'y="{height * 0.972:.2f}" text-anchor="middle" font-family="Arial, sans-serif" '
        f'font-size="{height * 0.017:.1f}" fill="{colors["muted"]}">Time (s)</text>'
    )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf"),
        Path("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_png(
    path: Path,
    title: str,
    audio: AudioData,
    statistics: WindowStatistics,
    waveform: tuple[np.ndarray, np.ndarray, np.ndarray],
    annotations: PlotAnnotations,
    energy_floor_db: float,
    width: int,
    height: int,
    dpi: int,
    metadata: dict[str, Any],
) -> None:
    geometry = _plot_geometry(width, height)
    times, lower, upper = waveform
    x_scale, wave_y, energy_y, amp_limit, energy_bottom_db, energy_top_db = _plot_scales(
        audio.duration_s,
        float(np.max(np.abs(audio.signal))),
        statistics.energy_dbfs,
        energy_floor_db,
        geometry,
        width,
    )
    colors = {
        "background": (255, 255, 255, 255),
        "foreground": (23, 32, 51, 255),
        "muted": (100, 116, 139, 255),
        "grid": (219, 227, 238, 255),
        "wave": (37, 99, 165, 255),
        "wave_fill": (191, 219, 254, 255),
        "energy": (15, 118, 110, 255),
        "stable": (254, 243, 199, 255),
        "s1": (194, 65, 12, 255),
        "s2": (124, 58, 237, 255),
    }
    image = Image.new("RGBA", (width, height), colors["background"])
    draw = ImageDraw.Draw(image)
    title_font = _load_font(max(18, int(height * 0.030)), bold=True)
    label_font = _load_font(max(13, int(height * 0.019)))
    small_font = _load_font(max(11, int(height * 0.015)))

    if annotations.stable_start_s is not None and annotations.stable_end_s is not None:
        draw.rectangle(
            [
                (x_scale(annotations.stable_start_s), geometry["wave_top"]),
                (x_scale(annotations.stable_end_s), geometry["energy_bottom"]),
            ],
            fill=colors["stable"],
        )

    for value in [-amp_limit, 0.0, amp_limit]:
        y = wave_y(value)
        draw.line(
            [(geometry["left"], y), (width - geometry["right"], y)],
            fill=colors["grid"],
            width=1,
        )
        draw.text(
            (geometry["left"] - 12, y),
            f"{value:.1f}",
            fill=colors["muted"],
            font=small_font,
            anchor="rm",
        )
    for value in np.linspace(energy_bottom_db, energy_top_db, 4):
        y = energy_y(float(value))
        draw.line(
            [(geometry["left"], y), (width - geometry["right"], y)],
            fill=colors["grid"],
            width=1,
        )
        draw.text(
            (geometry["left"] - 12, y),
            f"{value:.0f}",
            fill=colors["muted"],
            font=small_font,
            anchor="rm",
        )
    for value in _nice_time_ticks(audio.duration_s):
        x = x_scale(value)
        draw.line(
            [(x, geometry["energy_bottom"]), (x, geometry["energy_bottom"] + 7)],
            fill=colors["muted"],
            width=1,
        )
        draw.text(
            (x, geometry["energy_bottom"] + height * 0.030),
            f"{value:g}",
            fill=colors["muted"],
            font=small_font,
            anchor="mm",
        )

    wave_polygon = [(x_scale(t), wave_y(v)) for t, v in zip(times, upper)]
    wave_polygon += [(x_scale(t), wave_y(v)) for t, v in zip(times[::-1], lower[::-1])]
    draw.polygon(wave_polygon, fill=colors["wave_fill"])
    draw.line(
        [(x_scale(t), wave_y(v)) for t, v in zip(times, upper)],
        fill=colors["wave"],
        width=max(1, width // 1000),
        joint="curve",
    )
    draw.line(
        [(x_scale(t), wave_y(v)) for t, v in zip(times, lower)],
        fill=colors["wave"],
        width=max(1, width // 1000),
        joint="curve",
    )
    draw.line(
        [
            (x_scale(t), energy_y(v))
            for t, v in zip(statistics.centers_s, statistics.energy_dbfs)
        ],
        fill=colors["energy"],
        width=max(2, width // 650),
        joint="curve",
    )
    for event_type, event_time in annotations.events:
        color = colors["s1"] if event_type == "S1" else colors["s2"]
        event_x = x_scale(event_time)
        draw.line(
            [(event_x, geometry["wave_top"]), (event_x, geometry["energy_bottom"])],
            fill=color,
            width=max(1, width // 1200),
        )
        radius = max(3, width // 500)
        draw.ellipse(
            [
                (event_x - radius, geometry["wave_top"] - radius),
                (event_x + radius, geometry["wave_top"] + radius),
            ],
            fill=color,
        )

    subtitle = (
        f"{audio.sample_rate} Hz | {audio.channels} channel(s) | "
        f"{audio.duration_s:.3f} s | {audio.original_dtype}"
    )
    draw.text(
        (geometry["left"], height * 0.055),
        title,
        fill=colors["foreground"],
        font=title_font,
        anchor="lm",
    )
    draw.text(
        (geometry["left"], height * 0.090),
        subtitle,
        fill=colors["muted"],
        font=small_font,
        anchor="lm",
    )
    draw.text(
        (geometry["left"], geometry["wave_top"] - height * 0.018),
        "Waveform amplitude (FS)",
        fill=colors["foreground"],
        font=label_font,
        anchor="ls",
    )
    draw.text(
        (geometry["left"], geometry["energy_top"] - height * 0.018),
        "Short-time RMS energy (dBFS)",
        fill=colors["foreground"],
        font=label_font,
        anchor="ls",
    )
    draw.text(
        ((geometry["left"] + width - geometry["right"]) / 2, height * 0.972),
        "Time (s)",
        fill=colors["muted"],
        font=label_font,
        anchor="mm",
    )

    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Software", f"audio_diagnostics.py {SCRIPT_VERSION}")
    png_info.add_text("AnalysisMetadata", json.dumps(metadata, ensure_ascii=False, sort_keys=True))
    image.convert("RGB").save(path, dpi=(dpi, dpi), pnginfo=png_info)


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(abs(value), 1e-12))


def _comparison_data(
    sample_rate: int,
    series: Sequence[tuple[str, np.ndarray]],
    max_waveform_bins: int = 5000,
) -> dict[str, Any]:
    """Prepare shared-scale waveform, RMS, spectrum, and level metrics."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive.")
    if len(series) < 2:
        raise ValueError("At least two signals are required for a comparison.")
    lengths = {len(np.asarray(signal)) for _, signal in series}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise ValueError("Comparison signals must be non-empty and equally long.")

    prepared: list[dict[str, Any]] = []
    duration_s = next(iter(lengths)) / sample_rate
    for label, source in series:
        signal = np.ascontiguousarray(source, dtype=np.float64)
        if not np.all(np.isfinite(signal)):
            raise ValueError(f"Comparison signal {label!r} contains non-finite samples.")
        statistics = compute_window_statistics(signal, sample_rate, 50.0, 10.0)
        peak = float(np.max(np.abs(signal)))
        rms = float(np.sqrt(np.mean(signal * signal)))
        nperseg = min(4096, len(signal))
        frequencies, density = welch(
            signal,
            fs=sample_rate,
            window="hann",
            nperseg=nperseg,
            noverlap=nperseg // 2,
            detrend="constant",
            scaling="density",
        )
        spectrum_db = 10.0 * np.log10(np.maximum(density, 1e-15))
        prepared.append(
            {
                "label": label,
                "signal": signal,
                "waveform": minmax_waveform_envelope(
                    signal, sample_rate, max_waveform_bins
                ),
                "statistics": statistics,
                "frequencies": frequencies,
                "spectrum_db": spectrum_db,
                "peak_dbfs": _dbfs(peak),
                "rms_dbfs": _dbfs(rms),
                "crest_db": _dbfs(peak / max(rms, 1e-12)),
            }
        )

    spectral_high_hz = min(500.0, sample_rate / 2.0)
    visible_spectra = [
        item["spectrum_db"][
            (item["frequencies"] >= 20.0)
            & (item["frequencies"] <= spectral_high_hz)
        ]
        for item in prepared
    ]
    spectral_top_db = min(10.0, math.ceil(max(float(np.max(x)) for x in visible_spectra) / 10.0) * 10.0)
    spectral_bottom_db = max(
        -140.0,
        math.floor(min(float(np.percentile(x, 5.0)) for x in visible_spectra) / 10.0) * 10.0,
    )
    if spectral_top_db - spectral_bottom_db < 50.0:
        spectral_bottom_db = spectral_top_db - 50.0
    return {
        "sample_rate": sample_rate,
        "duration_s": duration_s,
        "series": prepared,
        "amplitude_limit": max(0.05, max(float(np.max(np.abs(item["signal"]))) for item in prepared) * 1.05),
        "energy_bottom_db": -60.0,
        "energy_top_db": 0.0,
        "spectral_low_hz": 20.0,
        "spectral_high_hz": spectral_high_hz,
        "spectral_bottom_db": spectral_bottom_db,
        "spectral_top_db": spectral_top_db,
    }


def _comparison_layout(width: int, height: int, row_count: int) -> dict[str, Any]:
    left = width * 0.085
    right = width * 0.965
    first_row_top = height * 0.145
    row_step = height * 0.135
    row_height = height * 0.090
    rms_top = first_row_top + row_count * row_step + height * 0.025
    rms_bottom = rms_top + height * 0.135
    spectrum_top = rms_bottom + height * 0.070
    spectrum_bottom = spectrum_top + height * 0.135
    return {
        "left": left,
        "right": right,
        "rows": [
            (first_row_top + index * row_step, first_row_top + index * row_step + row_height)
            for index in range(row_count)
        ],
        "rms_top": rms_top,
        "rms_bottom": rms_bottom,
        "spectrum_top": spectrum_top,
        "spectrum_bottom": spectrum_bottom,
    }


def render_comparison_png(
    path: Path,
    title: str,
    sample_rate: int,
    series: Sequence[tuple[str, np.ndarray]],
    events: Sequence[tuple[str, float]] = (),
    width: int = 1600,
    height: int = 1200,
    dpi: int = 200,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Render an aligned multi-signal waveform/RMS/spectrum comparison."""
    data = _comparison_data(sample_rate, series)
    layout = _comparison_layout(width, height, len(series))
    colors = [(37, 99, 165), (15, 118, 110), (194, 65, 12), (124, 58, 237)]
    foreground = (23, 32, 51)
    muted = (100, 116, 139)
    grid = (219, 227, 238)
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = _load_font(max(20, int(height * 0.026)), bold=True)
    label_font = _load_font(max(14, int(height * 0.016)), bold=True)
    small_font = _load_font(max(11, int(height * 0.012)))
    left, right = layout["left"], layout["right"]
    duration_s = data["duration_s"]
    x_time = lambda t: left + (float(t) / duration_s) * (right - left)

    draw.text((left, height * 0.045), title, fill=foreground, font=title_font, anchor="lm")
    draw.text(
        (left, height * 0.080),
        (
            f"Aligned comparison | {sample_rate} Hz | {duration_s:.3f} s | "
            f"shared waveform scale | {len(events)} expected S1/S2 markers"
        ),
        fill=muted,
        font=small_font,
        anchor="lm",
    )

    amplitude_limit = data["amplitude_limit"]
    for index, (item, (top, bottom)) in enumerate(zip(data["series"], layout["rows"])):
        color = colors[index % len(colors)]
        wave_y = lambda value: (top + bottom) / 2.0 - float(value) / amplitude_limit * (bottom - top) / 2.0
        draw.rectangle((left, top, right, bottom), outline=grid, width=1)
        draw.line((left, (top + bottom) / 2.0, right, (top + bottom) / 2.0), fill=grid, width=1)
        for event_type, event_time in events:
            if 0.0 <= event_time <= duration_s:
                x = x_time(event_time)
                marker = (253, 186, 116) if event_type == "S1" else (196, 181, 253)
                draw.line((x, top, x, bottom), fill=marker, width=1)
        times, lower, upper = item["waveform"]
        polygon = [(x_time(t), wave_y(v)) for t, v in zip(times, upper)]
        polygon += [(x_time(t), wave_y(v)) for t, v in zip(times[::-1], lower[::-1])]
        fill = tuple(int(channel + (255 - channel) * 0.78) for channel in color)
        draw.polygon(polygon, fill=fill)
        draw.line([(x_time(t), wave_y(v)) for t, v in zip(times, upper)], fill=color, width=2)
        draw.line([(x_time(t), wave_y(v)) for t, v in zip(times, lower)], fill=color, width=2)
        metrics = (
            f"{item['label']}   Peak {item['peak_dbfs']:.2f} dBFS   "
            f"RMS {item['rms_dbfs']:.2f} dBFS   Crest {item['crest_db']:.2f} dB"
        )
        draw.text((left, top - height * 0.012), metrics, fill=color, font=label_font, anchor="ls")

    rms_top, rms_bottom = layout["rms_top"], layout["rms_bottom"]
    energy_y = lambda value: rms_bottom - (float(value) - data["energy_bottom_db"]) / (
        data["energy_top_db"] - data["energy_bottom_db"]
    ) * (rms_bottom - rms_top)
    draw.text((left, rms_top - height * 0.014), "Short-time RMS energy (dBFS)", fill=foreground, font=label_font, anchor="ls")
    draw.rectangle((left, rms_top, right, rms_bottom), outline=grid, width=1)
    for event_type, event_time in events:
        if 0.0 <= event_time <= duration_s:
            x = x_time(event_time)
            marker = (253, 186, 116) if event_type == "S1" else (196, 181, 253)
            draw.line((x, rms_top, x, rms_bottom), fill=marker, width=1)
    for value in (-60, -40, -20, 0):
        y = energy_y(value)
        draw.line((left, y, right, y), fill=grid, width=1)
        draw.text((left - 10, y), str(value), fill=muted, font=small_font, anchor="rm")
    for index, item in enumerate(data["series"]):
        stats = item["statistics"]
        points = [
            (x_time(t), energy_y(np.clip(v, data["energy_bottom_db"], data["energy_top_db"])))
            for t, v in zip(stats.centers_s, stats.energy_dbfs)
        ]
        draw.line(points, fill=colors[index % len(colors)], width=3, joint="curve")

    spec_top, spec_bottom = layout["spectrum_top"], layout["spectrum_bottom"]
    x_freq = lambda value: left + (float(value) - data["spectral_low_hz"]) / (
        data["spectral_high_hz"] - data["spectral_low_hz"]
    ) * (right - left)
    spec_y = lambda value: spec_bottom - (float(value) - data["spectral_bottom_db"]) / (
        data["spectral_top_db"] - data["spectral_bottom_db"]
    ) * (spec_bottom - spec_top)
    draw.text((left, spec_top - height * 0.014), "Welch power spectral density (dBFS/Hz)", fill=foreground, font=label_font, anchor="ls")
    draw.rectangle((left, spec_top, right, spec_bottom), outline=grid, width=1)
    for value in np.linspace(data["spectral_bottom_db"], data["spectral_top_db"], 4):
        y = spec_y(value)
        draw.line((left, y, right, y), fill=grid, width=1)
        draw.text((left - 10, y), f"{value:.0f}", fill=muted, font=small_font, anchor="rm")
    for index, item in enumerate(data["series"]):
        mask = (item["frequencies"] >= data["spectral_low_hz"]) & (item["frequencies"] <= data["spectral_high_hz"])
        points = [
            (x_freq(frequency), spec_y(np.clip(value, data["spectral_bottom_db"], data["spectral_top_db"])))
            for frequency, value in zip(item["frequencies"][mask], item["spectrum_db"][mask])
        ]
        draw.line(points, fill=colors[index % len(colors)], width=3, joint="curve")
    for frequency in np.linspace(data["spectral_low_hz"], data["spectral_high_hz"], 6):
        x = x_freq(frequency)
        draw.text((x, spec_bottom + height * 0.020), f"{frequency:.0f}", fill=muted, font=small_font, anchor="mm")
    draw.text(((left + right) / 2.0, spec_bottom + height * 0.045), "Frequency (Hz)", fill=muted, font=small_font, anchor="mm")

    for value in _nice_time_ticks(duration_s):
        x = x_time(value)
        draw.text((x, rms_bottom + height * 0.020), f"{value:g}", fill=muted, font=small_font, anchor="mm")
    draw.text(((left + right) / 2.0, rms_bottom + height * 0.045), "Time (s)", fill=muted, font=small_font, anchor="mm")

    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("Software", f"audio_diagnostics.py {SCRIPT_VERSION}")
    png_info.add_text("AnalysisMetadata", json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True))
    image.save(path, dpi=(dpi, dpi), pnginfo=png_info)


def render_comparison_svg(
    path: Path,
    title: str,
    sample_rate: int,
    series: Sequence[tuple[str, np.ndarray]],
    events: Sequence[tuple[str, float]] = (),
    width: int = 1600,
    height: int = 1200,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Render the vector equivalent of :func:`render_comparison_png`."""
    data = _comparison_data(sample_rate, series)
    layout = _comparison_layout(width, height, len(series))
    colors = ["#2563a5", "#0f766e", "#c2410c", "#7c3aed"]
    left, right = layout["left"], layout["right"]
    duration_s = data["duration_s"]
    x_time = lambda t: left + (float(t) / duration_s) * (right - left)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">',
        f'<title>{html.escape(title)}</title>',
        '<desc>Aligned waveform, short-time RMS energy, and Welch spectrum comparison.</desc>',
        f'<metadata>{html.escape(json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True))}</metadata>',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{left:.1f}" y="{height * 0.050:.1f}" font-family="Arial, sans-serif" font-size="32" font-weight="600" fill="#172033">{html.escape(title)}</text>',
        f'<text x="{left:.1f}" y="{height * 0.082:.1f}" font-family="Arial, sans-serif" font-size="15" fill="#64748b">Aligned comparison | {sample_rate} Hz | {duration_s:.3f} s | shared waveform scale | {len(events)} expected S1/S2 markers</text>',
    ]
    amplitude_limit = data["amplitude_limit"]
    for index, (item, (top, bottom)) in enumerate(zip(data["series"], layout["rows"])):
        color = colors[index % len(colors)]
        wave_y = lambda value: (top + bottom) / 2.0 - float(value) / amplitude_limit * (bottom - top) / 2.0
        parts.extend([
            f'<rect x="{left:.1f}" y="{top:.1f}" width="{right-left:.1f}" height="{bottom-top:.1f}" fill="none" stroke="#dbe3ee"/>',
            f'<line x1="{left:.1f}" y1="{(top+bottom)/2:.1f}" x2="{right:.1f}" y2="{(top+bottom)/2:.1f}" stroke="#dbe3ee"/>',
        ])
        for event_type, event_time in events:
            if 0.0 <= event_time <= duration_s:
                event_color = "#c2410c" if event_type == "S1" else "#7c3aed"
                x = x_time(event_time)
                parts.append(f'<line x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" y2="{bottom:.1f}" stroke="{event_color}" stroke-opacity="0.25"/>')
        times, lower, upper = item["waveform"]
        upper_points = [(x_time(t), wave_y(v)) for t, v in zip(times, upper)]
        lower_points = [(x_time(t), wave_y(v)) for t, v in zip(times[::-1], lower[::-1])]
        parts.extend([
            f'<polygon points="{_xml_points(upper_points + lower_points)}" fill="{color}" fill-opacity="0.20"/>',
            f'<polyline points="{_xml_points(upper_points)}" fill="none" stroke="{color}" stroke-width="1.7"/>',
            f'<polyline points="{_xml_points([(x_time(t), wave_y(v)) for t, v in zip(times, lower)])}" fill="none" stroke="{color}" stroke-width="1.7"/>',
            f'<text x="{left:.1f}" y="{top-height*0.012:.1f}" font-family="Arial, sans-serif" font-size="18" font-weight="600" fill="{color}">{html.escape(item["label"])}   Peak {item["peak_dbfs"]:.2f} dBFS   RMS {item["rms_dbfs"]:.2f} dBFS   Crest {item["crest_db"]:.2f} dB</text>',
        ])

    rms_top, rms_bottom = layout["rms_top"], layout["rms_bottom"]
    energy_y = lambda value: rms_bottom - (float(value) - data["energy_bottom_db"]) / (data["energy_top_db"] - data["energy_bottom_db"]) * (rms_bottom - rms_top)
    parts.extend([
        f'<text x="{left:.1f}" y="{rms_top-height*0.014:.1f}" font-family="Arial, sans-serif" font-size="18" font-weight="600" fill="#172033">Short-time RMS energy (dBFS)</text>',
        f'<rect x="{left:.1f}" y="{rms_top:.1f}" width="{right-left:.1f}" height="{rms_bottom-rms_top:.1f}" fill="none" stroke="#dbe3ee"/>',
    ])
    for event_type, event_time in events:
        if 0.0 <= event_time <= duration_s:
            event_color = "#c2410c" if event_type == "S1" else "#7c3aed"
            x = x_time(event_time)
            parts.append(
                f'<line x1="{x:.1f}" y1="{rms_top:.1f}" x2="{x:.1f}" '
                f'y2="{rms_bottom:.1f}" stroke="{event_color}" stroke-opacity="0.25"/>'
            )
    for value in (-60, -40, -20, 0):
        y = energy_y(value)
        parts.extend([
            f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" stroke="#dbe3ee"/>',
            f'<text x="{left-10:.1f}" y="{y+5:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="14" fill="#64748b">{value}</text>',
        ])
    for index, item in enumerate(data["series"]):
        stats = item["statistics"]
        points = [(x_time(t), energy_y(np.clip(v, data["energy_bottom_db"], data["energy_top_db"]))) for t, v in zip(stats.centers_s, stats.energy_dbfs)]
        parts.append(f'<polyline points="{_xml_points(points)}" fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2.5" stroke-linejoin="round"/>')
    for value in _nice_time_ticks(duration_s):
        x = x_time(value)
        parts.append(f'<text x="{x:.1f}" y="{rms_bottom+height*0.020:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="14" fill="#64748b">{value:g}</text>')
    parts.append(f'<text x="{(left+right)/2:.1f}" y="{rms_bottom+height*0.045:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="14" fill="#64748b">Time (s)</text>')

    spec_top, spec_bottom = layout["spectrum_top"], layout["spectrum_bottom"]
    x_freq = lambda value: left + (float(value) - data["spectral_low_hz"]) / (data["spectral_high_hz"] - data["spectral_low_hz"]) * (right - left)
    spec_y = lambda value: spec_bottom - (float(value) - data["spectral_bottom_db"]) / (data["spectral_top_db"] - data["spectral_bottom_db"]) * (spec_bottom - spec_top)
    parts.extend([
        f'<text x="{left:.1f}" y="{spec_top-height*0.014:.1f}" font-family="Arial, sans-serif" font-size="18" font-weight="600" fill="#172033">Welch power spectral density (dBFS/Hz)</text>',
        f'<rect x="{left:.1f}" y="{spec_top:.1f}" width="{right-left:.1f}" height="{spec_bottom-spec_top:.1f}" fill="none" stroke="#dbe3ee"/>',
    ])
    for value in np.linspace(data["spectral_bottom_db"], data["spectral_top_db"], 4):
        y = spec_y(value)
        parts.extend([
            f'<line x1="{left:.1f}" y1="{y:.1f}" x2="{right:.1f}" y2="{y:.1f}" stroke="#dbe3ee"/>',
            f'<text x="{left-10:.1f}" y="{y+5:.1f}" text-anchor="end" font-family="Arial, sans-serif" font-size="14" fill="#64748b">{value:.0f}</text>',
        ])
    for index, item in enumerate(data["series"]):
        mask = (item["frequencies"] >= data["spectral_low_hz"]) & (item["frequencies"] <= data["spectral_high_hz"])
        points = [(x_freq(frequency), spec_y(np.clip(value, data["spectral_bottom_db"], data["spectral_top_db"]))) for frequency, value in zip(item["frequencies"][mask], item["spectrum_db"][mask])]
        parts.append(f'<polyline points="{_xml_points(points)}" fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2.5" stroke-linejoin="round"/>')
    for frequency in np.linspace(data["spectral_low_hz"], data["spectral_high_hz"], 6):
        x = x_freq(frequency)
        parts.append(f'<text x="{x:.1f}" y="{spec_bottom+height*0.020:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="14" fill="#64748b">{frequency:.0f}</text>')
    parts.extend([
        f'<text x="{(left+right)/2:.1f}" y="{spec_bottom+height*0.045:.1f}" text-anchor="middle" font-family="Arial, sans-serif" font-size="14" fill="#64748b">Frequency (Hz)</text>',
        '</svg>',
    ])
    path.write_text("\n".join(parts), encoding="utf-8")


def render_comparison_pair(
    output_stem: Path,
    title: str,
    sample_rate: int,
    series: Sequence[tuple[str, np.ndarray]],
    events: Sequence[tuple[str, float]] = (),
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write matching PNG and SVG comparison figures and return their paths."""
    png_path = output_stem.with_suffix(".png")
    svg_path = output_stem.with_suffix(".svg")
    render_comparison_png(png_path, title, sample_rate, series, events, metadata=metadata)
    render_comparison_svg(svg_path, title, sample_rate, series, events, metadata=metadata)
    return png_path, svg_path


def render_time_energy_pair(
    output_stem: Path,
    title: str,
    sample_rate: int,
    signal: np.ndarray,
    events: Sequence[tuple[str, float]] = (),
    metadata: dict[str, Any] | None = None,
    width: int = 1600,
    height: int = 900,
    dpi: int = 200,
) -> tuple[Path, Path]:
    """Write a standalone waveform/time-energy PNG and SVG for one signal."""
    values = np.ascontiguousarray(signal, dtype=np.float64)
    if len(values) == 0 or not np.all(np.isfinite(values)):
        raise ValueError("The time-energy signal must be non-empty and finite.")
    audio = AudioData(
        sample_rate=sample_rate,
        signal=values,
        channels=1,
        original_dtype="float64 processing buffer",
        clipped_fraction=float(np.mean(np.abs(values) >= 1.0)),
    )
    statistics = compute_window_statistics(
        values, sample_rate, window_ms=50.0, hop_ms=10.0
    )
    waveform = minmax_waveform_envelope(values, sample_rate, 5000)
    annotations = PlotAnnotations(events=tuple(events))
    png_path = output_stem.with_suffix(".png")
    svg_path = output_stem.with_suffix(".svg")
    render_svg(
        svg_path,
        title,
        audio,
        statistics,
        waveform,
        annotations,
        -60.0,
        width,
        height,
        metadata or {},
    )
    render_png(
        png_path,
        title,
        audio,
        statistics,
        waveform,
        annotations,
        -60.0,
        width,
        height,
        dpi,
        metadata or {},
    )
    return png_path, svg_path


def write_csv(path: Path, statistics: WindowStatistics) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "window_start_s",
                "window_end_s",
                "window_center_s",
                "peak_abs_fs",
                "rms_fs",
                "energy_dbfs",
            ]
        )
        for row in zip(
            statistics.starts_s,
            statistics.ends_s,
            statistics.centers_s,
            statistics.peak_abs_fs,
            statistics.rms_fs,
            statistics.energy_dbfs,
        ):
            writer.writerow([f"{value:.10g}" for value in row])


def build_metadata(
    input_path: Path,
    audio: AudioData,
    statistics: WindowStatistics,
    annotations: PlotAnnotations,
    args: argparse.Namespace,
) -> dict[str, Any]:
    signal = audio.signal
    window_samples = max(1, int(round(args.window_ms * audio.sample_rate / 1000.0)))
    hop_samples = max(1, int(round(args.hop_ms * audio.sample_rate / 1000.0)))
    return {
        "schema_version": "1.0",
        "script": {"name": "audio_diagnostics.py", "version": SCRIPT_VERSION},
        "source": {
            "path": str(input_path.resolve()),
            "sha256": file_sha256(input_path),
            "sample_rate_hz": audio.sample_rate,
            "channels": audio.channels,
            "dtype": audio.original_dtype,
            "duration_s": audio.duration_s,
        },
        "parameters": {
            "channel_mode": args.channel_mode,
            "requested_window_ms": args.window_ms,
            "requested_hop_ms": args.hop_ms,
            "window_samples": window_samples,
            "hop_samples": hop_samples,
            "effective_window_ms": window_samples / audio.sample_rate * 1000.0,
            "effective_hop_ms": hop_samples / audio.sample_rate * 1000.0,
            "energy_floor_db": args.energy_floor_db,
            "max_waveform_bins": args.max_waveform_bins,
            "width": args.width,
            "height": args.height,
            "dpi": args.dpi,
            "formats": list(args.formats),
        },
        "metrics": {
            "peak_abs_fs": float(np.max(np.abs(signal))),
            "overall_rms_fs": float(np.sqrt(np.mean(signal * signal))),
            "overall_rms_dbfs": float(
                20.0 * np.log10(max(float(np.sqrt(np.mean(signal * signal))), 1e-12))
            ),
            "dc_offset_fs": float(np.mean(signal)),
            "clipped_fraction": audio.clipped_fraction,
            "analysis_window_count": int(len(statistics.centers_s)),
        },
        "annotations": {
            "stable_start_s": annotations.stable_start_s,
            "stable_end_s": annotations.stable_end_s,
            "events": [
                {"type": event_type, "time_s": event_time}
                for event_type, event_time in annotations.events
            ],
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "pillow": Image.__version__,
            "platform": platform.platform(),
        },
        "method": {
            "waveform_display": "Per-bin minimum/maximum envelope",
            "energy": "Short-time root mean square (RMS)",
            "db_conversion": "20*log10(max(RMS, 1e-12))",
        },
        "citations": [
            {
                "software": "NumPy",
                "doi": "10.1038/s41586-020-2649-2",
            },
            {
                "software": "SciPy",
                "doi": "10.1038/s41592-019-0686-2",
            },
            {
                "software": "Pillow",
                "url": "https://python-pillow.github.io/",
            },
        ],
    }


def run(args: argparse.Namespace) -> list[Path]:
    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Input WAV does not exist: {input_path}")
    if args.width < 640 or args.height < 360:
        raise ValueError("Figure dimensions must be at least 640 x 360.")
    if args.dpi <= 0:
        raise ValueError("DPI must be positive.")
    if args.energy_floor_db >= 0:
        raise ValueError("--energy-floor-db must be below 0 dBFS.")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    audio = load_wav(input_path, channel_mode=args.channel_mode)
    statistics = compute_window_statistics(
        audio.signal,
        audio.sample_rate,
        window_ms=args.window_ms,
        hop_ms=args.hop_ms,
    )
    waveform = minmax_waveform_envelope(
        audio.signal, audio.sample_rate, args.max_waveform_bins
    )
    annotations = read_annotations(Path(args.analysis_json) if args.analysis_json else None)
    annotations = merge_annotation_overrides(
        annotations, args.stable_start, args.stable_end
    )
    validate_annotations(annotations, audio.duration_s)

    prefix = args.output_prefix or input_path.stem
    title = args.title or input_path.name
    metadata = build_metadata(input_path, audio, statistics, annotations, args)
    created: list[Path] = []
    if "svg" in args.formats:
        svg_path = output_dir / f"{prefix}.time_energy.svg"
        render_svg(
            svg_path,
            title,
            audio,
            statistics,
            waveform,
            annotations,
            args.energy_floor_db,
            args.width,
            args.height,
            metadata,
        )
        created.append(svg_path)
    if "png" in args.formats:
        png_path = output_dir / f"{prefix}.time_energy.png"
        render_png(
            png_path,
            title,
            audio,
            statistics,
            waveform,
            annotations,
            args.energy_floor_db,
            args.width,
            args.height,
            args.dpi,
            metadata,
        )
        created.append(png_path)
    if not args.no_csv:
        csv_path = output_dir / f"{prefix}.time_energy.csv"
        write_csv(csv_path, statistics)
        created.append(csv_path)
    if not args.no_json:
        json_path = output_dir / f"{prefix}.time_energy.json"
        json_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        created.append(json_path)
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate reproducible waveform and short-time RMS energy diagnostics."
    )
    parser.add_argument("input", help="Input integer-PCM or IEEE-float WAV file.")
    parser.add_argument(
        "--output-dir",
        default="diagnostics",
        help="Output directory (default: ./diagnostics).",
    )
    parser.add_argument("--output-prefix", help="Output filename prefix.")
    parser.add_argument("--title", help="Figure title; defaults to the WAV filename.")
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=("png", "svg"),
        default=("png", "svg"),
        help="Figure formats (default: png svg).",
    )
    parser.add_argument("--window-ms", type=float, default=50.0)
    parser.add_argument("--hop-ms", type=float, default=10.0)
    parser.add_argument("--energy-floor-db", type=float, default=-60.0)
    parser.add_argument("--max-waveform-bins", type=int, default=5000)
    parser.add_argument("--channel-mode", choices=("mean", "first"), default="mean")
    parser.add_argument("--analysis-json", help="Optional S1/S2 and stable-region JSON.")
    parser.add_argument("--stable-start", type=float, help="Stable-region start in seconds.")
    parser.add_argument("--stable-end", type=float, help="Stable-region end in seconds.")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument("--no-json", action="store_true")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {SCRIPT_VERSION}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        created = run(args)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    for path in created:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
