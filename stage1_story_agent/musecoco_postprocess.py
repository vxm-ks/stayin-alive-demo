"""Collect and normalize MuseCoco theme results for Stage 2 consumption."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from .bar_normalizer import BarNormalizationError, normalize_midi_bars
from .key_normalizer import KeyNormalizationError, normalize_midi_key
from .models import MuseCocoDelivery
from .tempo_normalizer import TempoNormalizationError, normalize_midi_tempo
from .utils import json_bytes


class MuseCocoPostprocessError(RuntimeError):
    """Raised when collected MuseCoco results cannot satisfy the Stage 1 contract."""


TonalityPolicy = Literal["loose", "strict"]


@dataclass(frozen=True)
class MuseCocoPostprocessResult:
    musecoco_dir: Path
    generated_themes_dir: Path
    manifest_path: Path
    theme_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "musecoco_dir": str(self.musecoco_dir.resolve()),
            "generated_themes_dir": str(self.generated_themes_dir.resolve()),
            "manifest_path": str(self.manifest_path.resolve()),
            "theme_count": self.theme_count,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MuseCocoPostprocessError(f"could not read JSON {path}: {exc}") from exc


def finalize_musecoco_results(
    musecoco_dir: str | Path,
    *,
    force: bool = False,
    tonality_policy: TonalityPolicy = "strict",
) -> MuseCocoPostprocessResult:
    if tonality_policy not in ("loose", "strict"):
        raise MuseCocoPostprocessError("tonality_policy must be 'loose' or 'strict'")
    root = Path(musecoco_dir).resolve()
    try:
        delivery = MuseCocoDelivery.model_validate(_read_json(root / "musecoco_plan.json"))
    except ValidationError as exc:
        raise MuseCocoPostprocessError(f"invalid musecoco_plan.json: {exc}") from exc
    if any(family.seed_bars != delivery.output_motif_bars for family in delivery.theme_families):
        raise MuseCocoPostprocessError(
            "every theme seed_bars must equal musecoco_plan.output_motif_bars"
        )

    raw_root = root / "raw_results"
    collection = _read_json(raw_root / "collection_manifest.json")
    if not isinstance(collection, dict) or collection.get("schema_version") != "musecoco-result-collection-v1":
        raise MuseCocoPostprocessError("raw result collection manifest has an invalid schema")
    items = collection.get("items")
    if not isinstance(items, list):
        raise MuseCocoPostprocessError("raw result collection manifest has no items")
    by_family = {
        item.get("theme_family_id"): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("theme_family_id"), str)
    }
    expected_families = {family.theme_family_id for family in delivery.theme_families}
    if set(by_family) != expected_families:
        raise MuseCocoPostprocessError(
            f"collected theme families mismatch: expected {sorted(expected_families)}, got {sorted(by_family)}"
        )

    destination = root / "generated_themes"
    if destination.exists() and not force:
        raise MuseCocoPostprocessError(
            f"generated themes already exist: {destination}; use force to replace them"
        )
    token = uuid4().hex
    staging = root / f".generated_themes.staging-{token}"
    backup = root / f".generated_themes.backup-{token}"
    manifest_items: list[dict[str, object]] = []
    try:
        staging.mkdir()
        for family in delivery.theme_families:
            item = by_family[family.theme_family_id]
            assert isinstance(item, dict)
            task_id = item.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise MuseCocoPostprocessError(
                    f"collection item has no task_id for {family.theme_family_id}"
                )
            source_dir = raw_root / task_id
            raw_midi = source_dir / "raw.mid"
            remi = source_dir / "result.remi.txt"
            source_audit = source_dir / "source_result_audit.json"
            recorded_files = item.get("files")
            if not isinstance(recorded_files, dict):
                raise MuseCocoPostprocessError(f"collection item has no hashes: {task_id}")
            for name, path in (
                ("raw.mid", raw_midi),
                ("result.remi.txt", remi),
                ("source_result_audit.json", source_audit),
            ):
                if not path.is_file() or path.stat().st_size == 0:
                    raise MuseCocoPostprocessError(f"collected result missing or empty: {path}")
                if recorded_files.get(name) != _sha256(path):
                    raise MuseCocoPostprocessError(f"collected result hash mismatch: {path}")

            theme_dir = staging / family.theme_family_id
            theme_dir.mkdir()
            copied_raw = theme_dir / "raw.mid"
            shutil.copy2(raw_midi, copied_raw)
            shutil.copy2(remi, theme_dir / "result.remi.txt")
            shutil.copy2(source_audit, theme_dir / "source_result_audit.json")
            bars_midi = theme_dir / "bars-normalized.mid"
            tempo_midi = theme_dir / "tempo-normalized.mid"
            final_midi = theme_dir / "final.mid"
            try:
                bars = normalize_midi_bars(
                    copied_raw,
                    bars_midi,
                    delivery.output_motif_bars,
                    delivery.time_signature,
                )
                tempo = normalize_midi_tempo(
                    bars_midi,
                    tempo_midi,
                    delivery.tempo_bpm,
                )
                if tonality_policy == "strict":
                    key = normalize_midi_key(
                        tempo_midi,
                        final_midi,
                        delivery.global_tonality.tonic,
                        delivery.global_tonality.mode,
                    )
                    key_audit = key.as_dict()
                    key_audit.update(
                        {
                            "tonality_policy": "strict",
                            "applied": True,
                            "input_midi": "tempo-normalized.mid",
                            "output_midi": "final.mid",
                        }
                    )
                else:
                    shutil.copy2(tempo_midi, final_midi)
                    unchanged_hash = _sha256(final_midi)
                    key_audit = {
                        "tonality_policy": "loose",
                        "applied": False,
                        "source_key": None,
                        "target_key": delivery.global_tonality.model_dump(mode="json"),
                        "reason": "tonality_detection_and_rewrite_disabled",
                        "input_midi": "tempo-normalized.mid",
                        "output_midi": "final.mid",
                        "input_sha256": unchanged_hash,
                        "output_sha256": unchanged_hash,
                    }
            except (BarNormalizationError, TempoNormalizationError, KeyNormalizationError) as exc:
                raise MuseCocoPostprocessError(
                    f"normalization failed for {family.theme_family_id}: {exc}"
                ) from exc
            bars_audit = bars.as_dict()
            bars_audit.update(
                {"input_midi": "raw.mid", "output_midi": "bars-normalized.mid"}
            )
            tempo_audit = tempo.as_dict()
            tempo_audit.update(
                {
                    "input_midi": "bars-normalized.mid",
                    "output_midi": "tempo-normalized.mid",
                }
            )
            normalization_audit = {
                "schema_version": "musecoco-theme-normalization-v1",
                "task_id": task_id,
                "theme_family_id": family.theme_family_id,
                "generation_target_bars": delivery.generation_target_bars,
                "output_motif_bars": delivery.output_motif_bars,
                "short_input_policy": "error",
                "long_input_policy": "trim_and_close_active_notes",
                "tonality_policy": tonality_policy,
                "bars": bars_audit,
                "tempo": tempo_audit,
                "key": key_audit,
                "final_sha256": _sha256(final_midi),
            }
            (theme_dir / "normalization_audit.json").write_bytes(
                json_bytes(normalization_audit)
            )
            manifest_items.append(
                {
                    "theme_family_id": family.theme_family_id,
                    "base_symbol": family.base_symbol,
                    "task_id": task_id,
                    "input_generation_target_bars": delivery.generation_target_bars,
                    "output_motif_bars": delivery.output_motif_bars,
                    "final_midi": f"{family.theme_family_id}/final.mid",
                    "final_sha256": normalization_audit["final_sha256"],
                }
            )
        manifest = {
            "schema_version": "musecoco-final-themes-v1",
            "story_id": delivery.story_id,
            "generation_target_bars": delivery.generation_target_bars,
            "output_motif_bars": delivery.output_motif_bars,
            "tempo_bpm": delivery.tempo_bpm,
            "time_signature": delivery.time_signature,
            "global_tonality": delivery.global_tonality.model_dump(mode="json"),
            "tonality_policy": tonality_policy,
            "items": manifest_items,
        }
        (staging / "theme_manifest.json").write_bytes(json_bytes(manifest))
        if destination.exists():
            os.replace(destination, backup)
        try:
            os.replace(staging, destination)
        except Exception:
            if backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except MuseCocoPostprocessError:
        raise
    except OSError as exc:
        raise MuseCocoPostprocessError(f"could not publish generated themes: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and destination.exists():
            shutil.rmtree(backup, ignore_errors=True)
    return MuseCocoPostprocessResult(
        musecoco_dir=root,
        generated_themes_dir=destination,
        manifest_path=destination / "theme_manifest.json",
        theme_count=len(manifest_items),
    )
