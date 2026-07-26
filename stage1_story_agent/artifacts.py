"""Atomic publication of successful and failed Stage 1 runs."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from .errors import ArtifactWriteError
from .models import FailureReport, PlanRun
from .musecoco_encoder import MuseCocoEncodingError, build_encoding_files
from .utils import json_bytes, sha256_hex


def _publish(files: dict[str, bytes], output_dir: Path, *, force: bool) -> None:
    output_dir = output_dir.resolve()
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staging = parent / f".{output_dir.name}.staging-{token}"
    backup = parent / f".{output_dir.name}.backup-{token}"
    try:
        staging.mkdir()
        for name, data in files.items():
            (staging / name).write_bytes(data)
        if output_dir.exists():
            if not force:
                raise ArtifactWriteError("OUTPUT_EXISTS", f"output directory already exists: {output_dir}")
            os.replace(output_dir, backup)
        try:
            os.replace(staging, output_dir)
        except Exception:
            if backup.exists() and not output_dir.exists():
                os.replace(backup, output_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except ArtifactWriteError:
        raise
    except Exception as exc:
        raise ArtifactWriteError("ATOMIC_PUBLISH_FAILED", f"could not atomically publish output directory: {output_dir}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup.exists() and output_dir.exists():
            shutil.rmtree(backup, ignore_errors=True)


def delivery_directories(output_base: str | Path) -> dict[str, Path]:
    """Return separate sibling directories for each consumer and audit data."""

    base = Path(output_base).resolve()
    return {
        "musecoco": base.with_name(f"{base.name}-musecoco"),
        "heartbeat": base.with_name(f"{base.name}-heartbeat"),
        "stage2": base.with_name(f"{base.name}-stage2"),
        "audit": base.with_name(f"{base.name}-audit"),
    }


def _publish_deliveries(
    bundles: dict[Path, dict[str, bytes]],
    *,
    force: bool,
) -> None:
    """Atomically publish coordinated sibling delivery directories."""

    token = uuid4().hex
    staging: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        for target in bundles:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and not force:
                raise ArtifactWriteError(
                    "OUTPUT_EXISTS", f"output directory already exists: {target}"
                )
        for target, files in bundles.items():
            stage = target.parent / f".{target.name}.staging-{token}"
            stage.mkdir()
            staging[target] = stage
            for name, data in files.items():
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        if force:
            for target in bundles:
                if target.exists():
                    backup = target.parent / f".{target.name}.backup-{token}"
                    os.replace(target, backup)
                    backups[target] = backup
        for target, stage in staging.items():
            os.replace(stage, target)
            published.append(target)
        for backup in backups.values():
            shutil.rmtree(backup, ignore_errors=True)
    except ArtifactWriteError:
        raise
    except Exception as exc:
        for target in published:
            shutil.rmtree(target, ignore_errors=True)
        for target, backup in backups.items():
            if backup.exists() and not target.exists():
                os.replace(backup, target)
        raise ArtifactWriteError(
            "ATOMIC_PUBLISH_FAILED",
            "could not atomically publish separated consumer directories",
        ) from exc
    finally:
        for stage in staging.values():
            shutil.rmtree(stage, ignore_errors=True)
        for target, backup in backups.items():
            if backup.exists() and target.exists():
                shutil.rmtree(backup, ignore_errors=True)


def write_plan_run(
    run: PlanRun,
    output_dir: str | Path,
    *,
    force: bool = False,
) -> dict[str, Path]:
    content_plan = json_bytes(run.content_plan)
    musecoco_plan = json_bytes(run.musecoco_delivery)
    heartbeat_plan = json_bytes(run.heartbeat_delivery)
    stage2_plan = json_bytes(run.stage2_delivery)
    raw_response = json_bytes(run.raw_response)
    form_scale_decision = json_bytes(run.form_scale_decision)
    expected = run.manifest.files
    actual = {
        "content_plan.json": sha256_hex(content_plan),
        "musecoco_plan.json": sha256_hex(musecoco_plan),
        "heartbeat_processing_plan.json": sha256_hex(heartbeat_plan),
        "stage2_plan.json": sha256_hex(stage2_plan),
        "raw_response.json": sha256_hex(raw_response),
        "form_scale_decision.json": sha256_hex(form_scale_decision),
    }
    if expected != actual:
        raise ArtifactWriteError("MANIFEST_HASH_MISMATCH", "run manifest hashes do not match serialized artifacts")
    destinations = delivery_directories(output_dir)
    try:
        encoded_musecoco = build_encoding_files(run.musecoco_delivery)
    except MuseCocoEncodingError as exc:
        raise ArtifactWriteError(
            "MUSECOCO_ENCODING_FAILED",
            f"could not encode MuseCoco attributes: {exc}",
        ) from exc
    musecoco_files = {"musecoco_plan.json": musecoco_plan, **encoded_musecoco}
    bundles = {
        destinations["musecoco"]: musecoco_files,
        destinations["heartbeat"]: {
            "heartbeat_processing_plan.json": heartbeat_plan
        },
        destinations["stage2"]: {"stage2_plan.json": stage2_plan},
        destinations["audit"]: {
            "content_plan.json": content_plan,
            "raw_response.json": raw_response,
            "form_scale_decision.json": form_scale_decision,
            "run_manifest.json": json_bytes(run.manifest),
        },
    }
    _publish_deliveries(bundles, force=force)
    return destinations


def write_failure_report(
    report: FailureReport,
    output_dir: str | Path,
    *,
    force: bool = False,
) -> Path:
    destination = delivery_directories(output_dir)["audit"]
    _publish({"failure_report.json": json_bytes(report)}, destination, force=force)
    return destination.resolve()
