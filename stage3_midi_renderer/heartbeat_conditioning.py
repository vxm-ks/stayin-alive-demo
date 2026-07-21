"""Reusable Stage 3 conditioning for patient heartbeat timbre samples.

This component operates only on isolated heartbeat audio used to construct the
percussion SoundFont.  It never filters the complete music mix and never moves,
creates, or removes MIDI events.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfiltfilt


VERSION = "1.0.0"


class HeartbeatConditioningError(ValueError):
    """Raised when heartbeat conditioning inputs violate the component contract."""


@dataclass(frozen=True)
class HeartbeatConditioningProfile:
    name: str
    enabled: bool
    highpass_hz: float = 35.0
    lowpass_hz: float = 220.0
    filter_order: int = 4
    compressor_threshold_dbfs: float = -18.0
    compressor_ratio: float = 4.0
    compressor_attack_ms: float = 3.0
    compressor_release_ms: float = 80.0
    fade_ms: float = 5.0
    target_peak_dbfs: float = -3.0


PROFILE_REGISTRY: Mapping[str, HeartbeatConditioningProfile] = MappingProxyType({
    "none": HeartbeatConditioningProfile(name="none", enabled=False),
    "speaker_safe_loud_v1": HeartbeatConditioningProfile(
        name="speaker_safe_loud_v1",
        enabled=True,
    ),
})


@dataclass(frozen=True)
class HeartbeatConditioningAudit:
    component: str
    component_version: str
    profile: dict[str, Any]
    sample_rate_hz: int
    channels: int
    frames: int
    input_peak_dbfs: float
    input_rms_dbfs: float
    output_peak_dbfs: float
    output_rms_dbfs: float
    makeup_gain_db: float
    input_spectral_energy_percent: dict[str, float]
    output_spectral_energy_percent: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HeartbeatConditioningResult:
    output_dir: Path
    output_wav: Path
    manifest_json: Path
    profile_name: str
    sample_rate: int
    channels: int
    frames: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "output_wav": str(self.output_wav),
            "manifest_json": str(self.manifest_json),
            "profile": self.profile_name,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
        }


def available_conditioning_profiles() -> tuple[str, ...]:
    return tuple(PROFILE_REGISTRY)


def resolve_conditioning_profile(
    profile: str | HeartbeatConditioningProfile,
) -> HeartbeatConditioningProfile:
    if isinstance(profile, HeartbeatConditioningProfile):
        resolved = profile
    else:
        try:
            resolved = PROFILE_REGISTRY[profile]
        except KeyError as exc:
            raise HeartbeatConditioningError(
                f"unknown heartbeat conditioning profile {profile!r}; "
                f"available profiles: {', '.join(available_conditioning_profiles())}"
            ) from exc
    if not resolved.name.strip():
        raise HeartbeatConditioningError("conditioning profile name cannot be empty")
    if resolved.filter_order < 1 or resolved.filter_order > 12:
        raise HeartbeatConditioningError("filter_order must be between 1 and 12")
    if resolved.compressor_ratio < 1.0 or not math.isfinite(resolved.compressor_ratio):
        raise HeartbeatConditioningError("compressor_ratio must be finite and at least 1")
    for name, value in asdict(resolved).items():
        if isinstance(value, float) and not math.isfinite(value):
            raise HeartbeatConditioningError(f"{name} must be finite")
    if resolved.compressor_attack_ms <= 0 or resolved.compressor_release_ms <= 0:
        raise HeartbeatConditioningError("compressor attack and release must be positive")
    if resolved.fade_ms < 0:
        raise HeartbeatConditioningError("fade_ms cannot be negative")
    if not -60.0 <= resolved.target_peak_dbfs <= 0.0:
        raise HeartbeatConditioningError("target_peak_dbfs must be between -60 and 0")
    return resolved


def _as_audio_matrix(audio: np.ndarray) -> tuple[np.ndarray, bool]:
    signal = np.asarray(audio, dtype=np.float64)
    was_mono = signal.ndim == 1
    if was_mono:
        signal = signal[:, np.newaxis]
    if signal.ndim != 2 or signal.shape[0] == 0 or signal.shape[1] not in (1, 2):
        raise HeartbeatConditioningError(
            "heartbeat audio must be non-empty mono or stereo sample data"
        )
    if not np.all(np.isfinite(signal)):
        raise HeartbeatConditioningError("heartbeat audio contains NaN or infinite samples")
    return np.ascontiguousarray(signal), was_mono


def _level_dbfs(audio: np.ndarray) -> tuple[float, float]:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(np.asarray(audio, dtype=np.float64) ** 2))) if audio.size else 0.0
    return (
        float(20.0 * math.log10(max(peak, 1e-12))),
        float(20.0 * math.log10(max(rms, 1e-12))),
    )


def _spectral_energy(audio: np.ndarray, sample_rate: int) -> dict[str, float]:
    mono = np.mean(audio, axis=1)
    spectrum = np.abs(np.fft.rfft(mono)) ** 2
    frequencies = np.fft.rfftfreq(len(mono), 1.0 / sample_rate)
    total = float(np.sum(spectrum))
    if total <= 1e-24:
        return {"below_30_hz": 0.0, "30_to_250_hz": 0.0, "250_to_1000_hz": 0.0, "above_1000_hz": 0.0}
    return {
        "below_30_hz": float(100.0 * np.sum(spectrum[frequencies < 30.0]) / total),
        "30_to_250_hz": float(100.0 * np.sum(spectrum[(frequencies >= 30.0) & (frequencies < 250.0)]) / total),
        "250_to_1000_hz": float(100.0 * np.sum(spectrum[(frequencies >= 250.0) & (frequencies < 1000.0)]) / total),
        "above_1000_hz": float(100.0 * np.sum(spectrum[frequencies >= 1000.0]) / total),
    }


def condition_heartbeat_audio(
    audio: np.ndarray,
    sample_rate: int,
    *,
    profile: str | HeartbeatConditioningProfile = "speaker_safe_loud_v1",
) -> tuple[np.ndarray, HeartbeatConditioningAudit]:
    """Condition isolated heartbeat audio and return processed data plus audit.

    ``profile`` can be a registered profile name or an explicit immutable
    profile, allowing callers to use stable defaults or a fully audited custom
    configuration without changing this component's implementation.
    """
    if not 8_000 <= int(sample_rate) <= 384_000:
        raise HeartbeatConditioningError("sample_rate must be between 8000 and 384000")
    selected = resolve_conditioning_profile(profile)
    signal, was_mono = _as_audio_matrix(audio)
    input_peak_dbfs, input_rms_dbfs = _level_dbfs(signal)
    input_spectrum = _spectral_energy(signal, sample_rate)
    conditioned = signal.copy()
    makeup_gain = 1.0

    if selected.enabled:
        nyquist = sample_rate / 2.0
        if not 0.0 < selected.highpass_hz < selected.lowpass_hz < nyquist:
            raise HeartbeatConditioningError(
                "conditioning cutoffs must satisfy 0 < highpass < lowpass < Nyquist"
            )
        highpass_sos = butter(
            selected.filter_order,
            selected.highpass_hz,
            btype="highpass",
            fs=sample_rate,
            output="sos",
        )
        lowpass_sos = butter(
            selected.filter_order,
            selected.lowpass_hz,
            btype="lowpass",
            fs=sample_rate,
            output="sos",
        )
        try:
            conditioned = sosfiltfilt(highpass_sos, conditioned, axis=0)
            conditioned = sosfiltfilt(lowpass_sos, conditioned, axis=0)
        except ValueError as exc:
            raise HeartbeatConditioningError(
                "heartbeat sample is too short for the selected zero-phase filter"
            ) from exc

        attack = math.exp(-1.0 / max(1.0, selected.compressor_attack_ms * sample_rate / 1000.0))
        release = math.exp(-1.0 / max(1.0, selected.compressor_release_ms * sample_rate / 1000.0))
        detector = np.max(np.abs(conditioned), axis=1)
        envelope = np.empty_like(detector)
        level = 0.0
        for index, sample in enumerate(detector):
            coefficient = attack if sample > level else release
            level = coefficient * level + (1.0 - coefficient) * float(sample)
            envelope[index] = level
        threshold = 10.0 ** (selected.compressor_threshold_dbfs / 20.0)
        desired = np.where(
            envelope > threshold,
            threshold * np.power(envelope / threshold, 1.0 / selected.compressor_ratio),
            envelope,
        )
        gain_reduction = np.ones_like(envelope)
        active = envelope > 1e-12
        gain_reduction[active] = np.minimum(1.0, desired[active] / envelope[active])
        conditioned *= gain_reduction[:, np.newaxis]

        fade_samples = min(
            int(round(selected.fade_ms * sample_rate / 1000.0)),
            len(conditioned) // 4,
        )
        if fade_samples > 1:
            fade = np.sin(np.linspace(0.0, np.pi / 2.0, fade_samples)) ** 2
            conditioned[:fade_samples] *= fade[:, np.newaxis]
            conditioned[-fade_samples:] *= fade[::-1, np.newaxis]

        peak = float(np.max(np.abs(conditioned)))
        if peak > 1e-12:
            makeup_gain = (10.0 ** (selected.target_peak_dbfs / 20.0)) / peak
            conditioned *= makeup_gain

    output_peak_dbfs, output_rms_dbfs = _level_dbfs(conditioned)
    audit = HeartbeatConditioningAudit(
        component="stage3_midi_renderer.heartbeat_conditioning",
        component_version=VERSION,
        profile=asdict(selected),
        sample_rate_hz=int(sample_rate),
        channels=conditioned.shape[1],
        frames=conditioned.shape[0],
        input_peak_dbfs=input_peak_dbfs,
        input_rms_dbfs=input_rms_dbfs,
        output_peak_dbfs=output_peak_dbfs,
        output_rms_dbfs=output_rms_dbfs,
        makeup_gain_db=float(20.0 * math.log10(max(makeup_gain, 1e-12))),
        input_spectral_energy_percent=input_spectrum,
        output_spectral_energy_percent=_spectral_energy(conditioned, sample_rate),
    )
    output = conditioned[:, 0] if was_mono else conditioned
    return np.ascontiguousarray(output), audit


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_wav(path: Path) -> tuple[int, np.ndarray]:
    try:
        sample_rate, raw = wavfile.read(path)
    except (OSError, ValueError) as exc:
        raise HeartbeatConditioningError(f"could not read heartbeat WAV: {exc}") from exc
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        midpoint = float(1 << (raw.dtype.itemsize * 8 - 1))
        audio = (raw.astype(np.float64) - midpoint) / midpoint
    elif np.issubdtype(raw.dtype, np.signedinteger):
        audio = raw.astype(np.float64) / float(1 << (raw.dtype.itemsize * 8 - 1))
    elif np.issubdtype(raw.dtype, np.floating):
        audio = raw.astype(np.float64)
    else:
        raise HeartbeatConditioningError(f"unsupported WAV sample type {raw.dtype}")
    return int(sample_rate), audio


def _publish(staging: Path, destination: Path, force: bool) -> None:
    backup: Path | None = None
    try:
        if destination.exists():
            if not force:
                raise HeartbeatConditioningError(
                    f"conditioning output directory already exists: {destination}"
                )
            backup = destination.with_name(f".{destination.name}.backup-{uuid4().hex}")
            os.replace(destination, backup)
        os.replace(staging, destination)
        if backup is not None:
            shutil.rmtree(backup)
    except Exception:
        if not destination.exists() and backup is not None and backup.exists():
            os.replace(backup, destination)
        raise


def condition_heartbeat_wav(
    input_wav: str | Path,
    output_dir: str | Path,
    *,
    profile: str | HeartbeatConditioningProfile = "speaker_safe_loud_v1",
    force: bool = False,
) -> HeartbeatConditioningResult:
    """Run the component on a WAV and atomically publish WAV + JSON audit."""
    source = Path(input_wav).expanduser().resolve()
    if not source.is_file():
        raise HeartbeatConditioningError(f"heartbeat WAV not found: {source}")
    destination = Path(output_dir).expanduser().resolve()
    if source == destination or source.is_relative_to(destination):
        raise HeartbeatConditioningError("output directory cannot contain the input WAV")
    selected = resolve_conditioning_profile(profile)
    source_hash_before = _sha256(source)
    sample_rate, audio = _read_wav(source)
    conditioned, audit = condition_heartbeat_audio(audio, sample_rate, profile=selected)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=destination.parent))
    output_wav = staging / "heartbeat_conditioned.wav"
    manifest_json = staging / "heartbeat_conditioning_manifest.json"
    try:
        encoded = np.rint(np.clip(conditioned, -1.0, 1.0) * 32767.0).astype("<i2")
        wavfile.write(output_wav, sample_rate, encoded)
        if _sha256(source) != source_hash_before:
            raise HeartbeatConditioningError("input heartbeat WAV changed during conditioning")
        manifest = {
            "schema_version": "1.0",
            "stage": "stage3",
            "operation": "heartbeat_timbre_conditioning",
            "scope": "isolated_heartbeat_sample_before_soundfont_rendering",
            "complete_mix_filtered": False,
            "midi_events_modified": False,
            "generated_at": datetime.now(UTC).isoformat(),
            "input": {
                "wav": str(source),
                "sha256": source_hash_before,
            },
            "processing": audit.as_dict(),
            "output": {
                "file": output_wav.name,
                "sha256": _sha256(output_wav),
                "sample_rate_hz": sample_rate,
                "channels": audit.channels,
                "frames": audit.frames,
            },
        }
        manifest_json.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _publish(staging, destination, force)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return HeartbeatConditioningResult(
        output_dir=destination,
        output_wav=destination / output_wav.name,
        manifest_json=destination / manifest_json.name,
        profile_name=selected.name,
        sample_rate=sample_rate,
        channels=audit.channels,
        frames=audit.frames,
    )
