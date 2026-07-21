#!/usr/bin/env python3
"""Mix two WAV files with resampling, channel matching, and peak protection."""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from math import gcd
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy import signal
from scipy.io import wavfile


@dataclass(frozen=True)
class MixResult:
    output_path: Path
    sample_rate: int
    channels: int
    frames: int
    duration_s: float
    source_peak: float
    output_peak: float
    peak_gain_db: float
    repeats_a: int
    repeats_b: int


def _read_wav(path: Path) -> tuple[int, np.ndarray]:
    if not path.is_file():
        raise FileNotFoundError(f"WAV file not found: {path}")

    try:
        sample_rate, raw = wavfile.read(path)
    except ValueError as exc:
        raise ValueError(f"Unsupported or invalid WAV file: {path}") from exc

    if sample_rate <= 0 or raw.size == 0:
        raise ValueError(f"WAV file is empty or has an invalid sample rate: {path}")
    if raw.ndim == 1:
        raw = raw[:, np.newaxis]
    elif raw.ndim != 2:
        raise ValueError(f"Unsupported WAV sample shape {raw.shape}: {path}")

    if np.issubdtype(raw.dtype, np.unsignedinteger):
        bits = raw.dtype.itemsize * 8
        midpoint = float(1 << (bits - 1))
        audio = (raw.astype(np.float64) - midpoint) / midpoint
    elif np.issubdtype(raw.dtype, np.signedinteger):
        scale = float(1 << (raw.dtype.itemsize * 8 - 1))
        audio = raw.astype(np.float64) / scale
    elif np.issubdtype(raw.dtype, np.floating):
        audio = raw.astype(np.float64)
    else:
        raise ValueError(f"Unsupported WAV sample type {raw.dtype}: {path}")

    if not np.all(np.isfinite(audio)):
        raise ValueError(f"WAV contains NaN or infinite samples: {path}")
    return int(sample_rate), audio


def _resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    divisor = gcd(source_rate, target_rate)
    output = signal.resample_poly(
        audio,
        target_rate // divisor,
        source_rate // divisor,
        axis=0,
    )
    expected_frames = int(round(len(audio) * target_rate / source_rate))
    if len(output) > expected_frames:
        output = output[:expected_frames]
    elif len(output) < expected_frames:
        output = np.pad(output, ((0, expected_frames - len(output)), (0, 0)))
    return output


def _match_channels(audio: np.ndarray, channels: int, path: Path) -> np.ndarray:
    source_channels = audio.shape[1]
    if source_channels == channels:
        return audio
    if source_channels == 1:
        return np.repeat(audio, channels, axis=1)
    raise ValueError(
        f"Cannot automatically match {source_channels} channels from {path} "
        f"to {channels} channels. Only mono-to-multichannel expansion is automatic."
    )


def _validate_number(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")


def _repeat_to_frames(audio: np.ndarray, frame_count: int) -> tuple[np.ndarray, int]:
    """Repeat a non-empty track and trim it to an exact frame count."""
    repeats = max(1, math.ceil(frame_count / len(audio)))
    if repeats == 1:
        return audio[:frame_count], repeats
    return np.tile(audio, (repeats, 1))[:frame_count], repeats


def mix_wav_tracks(
    first_wav: str | Path,
    second_wav: str | Path,
    output_wav: str | Path,
    *,
    gain_a_db: float = 0.0,
    gain_b_db: float = 0.0,
    offset_b_s: float = 0.0,
    sample_rate: int | None = None,
    ceiling_dbfs: float = -1.0,
    normalize: bool = False,
    loop_shorter: bool = True,
) -> MixResult:
    """Mix two WAVs from time zero and write a 16-bit PCM WAV.

    The second track can be delayed with ``offset_b_s``. The output lasts until
    the end of the longer track. By default, a shorter track repeats so both
    tracks remain present. A shared linear gain prevents clipping; with
    ``normalize=True`` the same operation also raises quiet mixes to the ceiling.
    """
    first_path = Path(first_wav).expanduser().resolve()
    second_path = Path(second_wav).expanduser().resolve()
    output_path = Path(output_wav).expanduser().resolve()
    if output_path in (first_path, second_path):
        raise ValueError("Output path must not overwrite either input WAV.")

    for name, value in (
        ("gain_a_db", gain_a_db),
        ("gain_b_db", gain_b_db),
        ("offset_b_s", offset_b_s),
        ("ceiling_dbfs", ceiling_dbfs),
    ):
        _validate_number(name, float(value))
    if offset_b_s < 0:
        raise ValueError("offset_b_s must be zero or greater.")
    if ceiling_dbfs > 0 or ceiling_dbfs < -60:
        raise ValueError("ceiling_dbfs must be between -60 and 0 dBFS.")

    rate_a, audio_a = _read_wav(first_path)
    rate_b, audio_b = _read_wav(second_path)
    target_rate = max(rate_a, rate_b) if sample_rate is None else int(sample_rate)
    if target_rate < 1000 or target_rate > 384000:
        raise ValueError("sample_rate must be between 1000 and 384000 Hz.")

    channels = max(audio_a.shape[1], audio_b.shape[1])
    audio_a = _match_channels(
        _resample(audio_a, rate_a, target_rate), channels, first_path
    )
    audio_b = _match_channels(
        _resample(audio_b, rate_b, target_rate), channels, second_path
    )
    audio_a *= 10.0 ** (gain_a_db / 20.0)
    audio_b *= 10.0 ** (gain_b_db / 20.0)

    offset_frames = int(round(offset_b_s * target_rate))
    frame_count = max(len(audio_a), offset_frames + len(audio_b))
    mixed = np.zeros((frame_count, channels), dtype=np.float64)
    if loop_shorter:
        track_a, repeats_a = _repeat_to_frames(audio_a, frame_count)
        available_b_frames = frame_count - offset_frames
        track_b, repeats_b = _repeat_to_frames(audio_b, available_b_frames)
    else:
        track_a, repeats_a = audio_a, 1
        track_b, repeats_b = audio_b, 1
    mixed[: len(track_a)] += track_a
    mixed[offset_frames : offset_frames + len(track_b)] += track_b

    source_peak = float(np.max(np.abs(mixed)))
    target_peak = 10.0 ** (ceiling_dbfs / 20.0)
    peak_gain = 1.0
    if source_peak > target_peak or (normalize and source_peak > 0.0):
        peak_gain = target_peak / source_peak
        mixed *= peak_gain

    mixed = np.clip(mixed, -1.0, 1.0)
    output_peak = float(np.max(np.abs(mixed)))
    pcm = np.round(mixed * 32767.0).astype(np.int16)
    if channels == 1:
        pcm = pcm[:, 0]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(output_path, target_rate, pcm)

    peak_gain_db = 20.0 * math.log10(peak_gain) if peak_gain > 0 else -math.inf
    return MixResult(
        output_path=output_path,
        sample_rate=target_rate,
        channels=channels,
        frames=frame_count,
        duration_s=frame_count / target_rate,
        source_peak=source_peak,
        output_peak=output_peak,
        peak_gain_db=peak_gain_db,
        repeats_a=repeats_a,
        repeats_b=repeats_b,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mix two WAV files into one 16-bit PCM WAV track."
    )
    parser.add_argument("first_wav", type=Path, help="first input WAV")
    parser.add_argument("second_wav", type=Path, help="second input WAV")
    parser.add_argument("output_wav", type=Path, help="output WAV")
    parser.add_argument("--gain-a-db", type=float, default=0.0)
    parser.add_argument("--gain-b-db", type=float, default=0.0)
    parser.add_argument(
        "--offset-b", type=float, default=0.0, metavar="SECONDS",
        help="delay the second track (default: 0)",
    )
    parser.add_argument(
        "--sample-rate", type=int, help="output rate (default: higher input rate)"
    )
    parser.add_argument("--ceiling-dbfs", type=float, default=-1.0)
    parser.add_argument(
        "--normalize", action="store_true",
        help="also raise a quiet mix to the selected peak ceiling",
    )
    parser.add_argument(
        "--no-loop-shorter",
        action="store_false",
        dest="loop_shorter",
        help="play each track once instead of repeating a shorter track",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = mix_wav_tracks(
            args.first_wav,
            args.second_wav,
            args.output_wav,
            gain_a_db=args.gain_a_db,
            gain_b_db=args.gain_b_db,
            offset_b_s=args.offset_b,
            sample_rate=args.sample_rate,
            ceiling_dbfs=args.ceiling_dbfs,
            normalize=args.normalize,
            loop_shorter=args.loop_shorter,
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("status=PASS")
    print(f"output={result.output_path}")
    print(f"sample_rate={result.sample_rate}")
    print(f"channels={result.channels}")
    print(f"duration_s={result.duration_s:.3f}")
    print(f"track_a_repeats={result.repeats_a}")
    print(f"track_b_repeats={result.repeats_b}")
    print(f"peak_protection_gain_db={result.peak_gain_db:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
