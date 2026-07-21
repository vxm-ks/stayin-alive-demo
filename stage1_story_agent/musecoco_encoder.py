"""Deterministic Stage 1 attributes-to-MuseCoco encoding.

This module mirrors the audited MuseCoco contract without importing its WSL
runtime.  Stage 1 fields are all known, so the multi-label instrument and genre
heads use closed-world semantics: selected values are yes and every unselected
value is an explicit no, never NA.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TypeAlias

from .models import MuseCocoAttributeTargets, MuseCocoDelivery
from .musecoco_policy import POLICY_VERSION, validate_musecoco_policy
from .utils import json_bytes, sha256_hex


Vector: TypeAlias = list[int]
CombinedValue: TypeAlias = Vector | list[Vector]
CombinedLabels: TypeAlias = dict[str, CombinedValue]

SCHEMA_VERSION = "musecoco-direct-encoding-v1"
TASK_PACKAGE_SCHEMA_VERSION = "musecoco-task-package-v1"
ENCODING_SOURCE = "deterministic_agent"
PROBABILITY_SOURCE = "deterministic_one_hot"

# Hashes of the exact local MuseCoco files audited on 2026-07-20.
ATT_KEY_SHA256 = "8BE36050B25A123DCF4109E090B00C6ABE3465638B9C2C9D971DA24B6A9006B2"
NUM_LABELS_SHA256 = "758A06181BD3CEC223F5D72A5686FFE91959C1F9BD5EE426BA0D936D2BFFC105"
ATTRIBUTE_DICTIONARY_SHA256 = (
    "9EFBF5CFEB4B0E10D586034277AEC0BE3E4CEBDE755D6C28D82BEBD41A722445"
)

INSTRUMENTS = (
    "piano", "keyboard", "percussion", "organ", "guitar", "bass", "violin",
    "viola", "cello", "harp", "strings", "voice", "trumpet", "trombone",
    "tuba", "horn", "brass", "sax", "oboe", "bassoon", "clarinet", "piccolo",
    "flute", "pipe", "synthesizer", "ethnic_instruments", "sound_effects", "drum",
)

ARTISTS = (
    "beethoven", "mozart", "chopin", "schubert", "schumann", "bach-js", "haydn",
    "brahms", "Handel", "tchaikovsky", "mendelssohn", "dvorak", "liszt",
    "stravinsky", "mahler", "prokofiev", "shostakovich",
)

ARTIST_ALIASES = {
    "bach": "bach-js",
    "handel": "Handel",
}

GENRES = (
    "new_age", "electronic", "rap", "religious", "international", "easy_listening",
    "avant_garde", "rnb", "latin", "children", "jazz", "classical", "comedy_spoken",
    "pop_rock", "reggae", "stage", "folk", "blues", "vocal", "holiday", "country",
    "symphony",
)

SCALAR_CODEBOOKS: dict[str, tuple[object, ...]] = {
    "R1": ("danceable", "not_danceable"),
    "R3": ("low", "medium", "high"),
    "S2s1": ARTISTS,
    "B1s1": ("1-4", "5-8", "9-12", "13-16"),
    "TS1s1": ("4/4", "2/4", "3/4", "1/4", "6/8", "3/8", "other"),
    "K1": ("major", "minor"),
    "T1s1": ("slow", "moderate", "fast"),
    "P4": tuple(range(12)),
    "EM1": ("Q1", "Q2", "Q3", "Q4"),
    "TM1": ("0-15", "15-30", "30-45", "45-60", "60+"),
}

# stage2_pre.py leaves scalar heads in this order and appends the grouped heads.
COMBINED_KEY_ORDER = (
    "R1", "R3", "S2s1", "B1s1", "TS1s1", "K1", "T1s1", "P4", "EM1",
    "TM1", "I1s2", "S4",
)

ATT_KEY = (
    *(f"I1s2_{name}" for name in INSTRUMENTS),
    "R1", "R3", "S2s1",
    *(f"S4_{name}" for name in GENRES),
    "B1s1", "TS1s1", "K1", "T1s1", "P4", "EM1", "TM1",
)

NUM_LABELS = {
    **{f"I1s2_{name}": 3 for name in INSTRUMENTS},
    "R1": 3,
    "R3": 4,
    "S2s1": 18,
    **{f"S4_{name}": 3 for name in GENRES},
    "B1s1": 5,
    "TS1s1": 8,
    "K1": 3,
    "T1s1": 4,
    "P4": 13,
    "EM1": 5,
    "TM1": 6,
}

AUTOMATIC_NA_TOKENS = ("I4_28", "C1_4", "ST1_14")
TOKENS_PER_SAMPLE = len(INSTRUMENTS) + len(GENRES) + len(SCALAR_CODEBOOKS) + 3


class MuseCocoEncodingError(ValueError):
    """Raised when a value cannot satisfy the frozen MuseCoco contract."""


def _one_hot(size: int, index: int) -> Vector:
    vector = [0] * size
    vector[index] = 1
    return vector


def _scalar_vector(key: str, value: object) -> Vector:
    values = SCALAR_CODEBOOKS[key]
    try:
        index = values.index(value)
    except ValueError as exc:
        raise MuseCocoEncodingError(f"unsupported {key} value: {value!r}") from exc
    # The final position is NA. Stage 1 values are known, so it remains zero.
    return _one_hot(len(values) + 1, index)


def _closed_world_vectors(
    selected: Sequence[str],
    codebook: Sequence[str],
    *,
    key: str,
) -> list[Vector]:
    selected_set = set(selected)
    if not selected_set:
        raise MuseCocoEncodingError(f"{key} must contain at least one selected value")
    unknown = selected_set.difference(codebook)
    if unknown:
        raise MuseCocoEncodingError(
            f"unsupported {key} values: {', '.join(sorted(unknown))}"
        )
    if len(selected_set) != len(selected):
        raise MuseCocoEncodingError(f"{key} must not contain duplicate values")
    return [
        [1, 0, 0] if value in selected_set else [0, 1, 0]
        for value in codebook
    ]


def encode_targets(targets: MuseCocoAttributeTargets) -> CombinedLabels:
    """Encode one Stage 1 target object into MuseCoco's 12 grouped labels."""

    try:
        validate_musecoco_policy(targets.I1s2, targets.S2s1)
    except ValueError as exc:
        raise MuseCocoEncodingError(str(exc)) from exc
    artist = ARTIST_ALIASES.get(targets.S2s1, targets.S2s1)
    labels: CombinedLabels = {
        "R1": _scalar_vector("R1", targets.R1),
        "R3": _scalar_vector("R3", targets.R3),
        "S2s1": _scalar_vector("S2s1", artist),
        "B1s1": _scalar_vector("B1s1", targets.B1s1),
        "TS1s1": _scalar_vector("TS1s1", targets.TS1s1),
        "K1": _scalar_vector("K1", targets.K1),
        "T1s1": _scalar_vector("T1s1", targets.T1s1),
        "P4": _scalar_vector("P4", targets.P4),
        "EM1": _scalar_vector("EM1", targets.EM1),
        "TM1": _scalar_vector("TM1", targets.TM1),
        "I1s2": _closed_world_vectors(targets.I1s2, INSTRUMENTS, key="I1s2"),
        "S4": _closed_world_vectors(targets.S4, GENRES, key="S4"),
    }
    validate_combined_labels(labels)
    return labels


def _selected_index(vector: Sequence[int], *, key: str, expected_size: int) -> int:
    if len(vector) != expected_size:
        raise MuseCocoEncodingError(
            f"{key} vector must have length {expected_size}, got {len(vector)}"
        )
    if any(value not in (0, 1) for value in vector) or sum(vector) != 1:
        raise MuseCocoEncodingError(f"{key} vector must be one-hot")
    return vector.index(1)


def validate_combined_labels(labels: Mapping[str, CombinedValue]) -> None:
    """Validate grouped labels against the current known-field Stage 1 contract."""

    if set(labels) != set(COMBINED_KEY_ORDER):
        missing = sorted(set(COMBINED_KEY_ORDER).difference(labels))
        extra = sorted(set(labels).difference(COMBINED_KEY_ORDER))
        raise MuseCocoEncodingError(
            f"combined label keys mismatch; missing={missing}, extra={extra}"
        )

    for key, values in (("I1s2", INSTRUMENTS), ("S4", GENRES)):
        vectors = labels[key]
        if not isinstance(vectors, list) or len(vectors) != len(values):
            raise MuseCocoEncodingError(
                f"{key} must contain {len(values)} three-state vectors"
            )
        yes_count = 0
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list):
                raise MuseCocoEncodingError(f"{key}[{index}] must be a vector")
            state = _selected_index(vector, key=f"{key}[{index}]", expected_size=3)
            if state == 2:
                raise MuseCocoEncodingError(f"known Stage 1 field {key} must not contain NA")
            yes_count += state == 0
        if yes_count == 0:
            raise MuseCocoEncodingError(f"known Stage 1 field {key} must select a value")

    for key, codebook in SCALAR_CODEBOOKS.items():
        vector = labels[key]
        if not isinstance(vector, list) or any(isinstance(item, list) for item in vector):
            raise MuseCocoEncodingError(f"{key} must be a single vector")
        state = _selected_index(vector, key=key, expected_size=len(codebook) + 1)
        if state == len(codebook):
            raise MuseCocoEncodingError(f"known Stage 1 field {key} must not be NA")


def flatten_labels(labels: Mapping[str, CombinedValue]) -> dict[str, Vector]:
    """Expand 12 grouped labels to the official 60 Text-to-Attribute heads."""

    validate_combined_labels(labels)
    flat: dict[str, Vector] = {}
    for index, name in enumerate(INSTRUMENTS):
        flat[f"I1s2_{name}"] = list(labels["I1s2"][index])  # type: ignore[index]
    for key in ("R1", "R3", "S2s1"):
        flat[key] = list(labels[key])  # type: ignore[arg-type]
    for index, name in enumerate(GENRES):
        flat[f"S4_{name}"] = list(labels["S4"][index])  # type: ignore[index]
    for key in ("B1s1", "TS1s1", "K1", "T1s1", "P4", "EM1", "TM1"):
        flat[key] = list(labels[key])  # type: ignore[arg-type]
    if tuple(flat) != ATT_KEY:
        raise MuseCocoEncodingError("internal flat head order does not match ATT_KEY")
    return flat


def labels_to_tokens(labels: Mapping[str, CombinedValue]) -> list[str]:
    """Mirror MuseCoco convert_vector_to_token for the 12-label input."""

    validate_combined_labels(labels)
    instruments = labels["I1s2"]
    genres = labels["S4"]
    tokens = [
        f"I1s2_{index}_{_selected_index(vector, key=f'I1s2[{index}]', expected_size=3)}"
        for index, vector in enumerate(instruments)  # type: ignore[arg-type]
    ]
    tokens.extend(("I4_28", "C1_4"))
    for key in ("R1", "R3", "S2s1"):
        tokens.append(
            f"{key}_{_selected_index(labels[key], key=key, expected_size=len(SCALAR_CODEBOOKS[key]) + 1)}"  # type: ignore[arg-type]
        )
    tokens.extend(
        f"S4_{index}_{_selected_index(vector, key=f'S4[{index}]', expected_size=3)}"
        for index, vector in enumerate(genres)  # type: ignore[arg-type]
    )
    for key in ("B1s1", "TS1s1", "K1", "T1s1", "P4"):
        tokens.append(
            f"{key}_{_selected_index(labels[key], key=key, expected_size=len(SCALAR_CODEBOOKS[key]) + 1)}"  # type: ignore[arg-type]
        )
    tokens.append("ST1_14")
    for key in ("EM1", "TM1"):
        tokens.append(
            f"{key}_{_selected_index(labels[key], key=key, expected_size=len(SCALAR_CODEBOOKS[key]) + 1)}"  # type: ignore[arg-type]
        )
    if len(tokens) != TOKENS_PER_SAMPLE:
        raise MuseCocoEncodingError(
            f"internal token count mismatch: expected {TOKENS_PER_SAMPLE}, got {len(tokens)}"
        )
    return tokens


def _ordered_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def build_encoding_payloads(delivery: MuseCocoDelivery) -> dict[str, object]:
    """Build all JSON-compatible direct-encoding payloads for a delivery."""

    predict: list[dict[str, str]] = []
    index_samples: list[dict[str, object]] = []
    direct_samples: list[dict[str, object]] = []
    flat_batch: dict[str, list[Vector]] = {key: [] for key in ATT_KEY}

    for index, family in enumerate(delivery.theme_families):
        labels = encode_targets(family.musecoco_attribute_targets)
        flat = flatten_labels(labels)
        predict.append({"text": family.musecoco_text})
        index_samples.append(
            {
                "index": index,
                "theme_family_id": family.theme_family_id,
                "base_symbol": family.base_symbol,
                "introduced_in_section_id": family.introduced_in_section_id,
            }
        )
        direct_samples.append(
            {
                "index": index,
                "theme_family_id": family.theme_family_id,
                "text": family.musecoco_text,
                "pred_labels": labels,
                "attribute_tokens": labels_to_tokens(labels),
            }
        )
        for key in ATT_KEY:
            flat_batch[key].append(flat[key])

    return {
        "predict.json": predict,
        "predict_index.json": {
            "schema_version": SCHEMA_VERSION,
            "story_id": delivery.story_id,
            "samples": index_samples,
        },
        "direct_attribute_labels.json": {
            "schema_version": SCHEMA_VERSION,
            "story_id": delivery.story_id,
            "encoding_source": ENCODING_SOURCE,
            "closed_world_multilabel": True,
            "probability_source": PROBABILITY_SOURCE,
            "project_policy_version": POLICY_VERSION,
            "softmax_materialization": "worker_copies_predict_attributes_shape",
            "samples": direct_samples,
        },
        "predict_attributes.json": flat_batch,
}


def _task_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_").lower()
    return slug or "theme"


def build_task_package_files(delivery: MuseCocoDelivery) -> dict[str, bytes]:
    """Split a batch delivery into atomic one-sample MuseCoco task packages."""

    payloads = build_encoding_payloads(delivery)
    predict = payloads["predict.json"]
    index_payload = payloads["predict_index.json"]
    flat_batch = payloads["predict_attributes.json"]
    if not isinstance(predict, list) or not isinstance(flat_batch, dict):
        raise MuseCocoEncodingError("internal task package payload shape is invalid")
    samples = index_payload.get("samples") if isinstance(index_payload, dict) else None
    if not isinstance(samples, list) or len(samples) != len(predict):
        raise MuseCocoEncodingError("predict_index sample count does not match predict.json")

    files: dict[str, bytes] = {}
    for sample_index, (family, predict_item, index_item) in enumerate(
        zip(delivery.theme_families, predict, samples)
    ):
        task_attributes = {
            key: [list(flat_batch[key][sample_index])]  # type: ignore[index]
            for key in ATT_KEY
        }
        task_predict = [dict(predict_item)]
        content_fingerprint = sha256_hex(
            _ordered_json_bytes(task_attributes) + _ordered_json_bytes(task_predict)
        )[:10]
        task_id = (
            f"task_{sample_index + 1:03d}_{_task_slug(delivery.story_id)}_"
            f"{_task_slug(family.theme_family_id)}_{content_fingerprint}"
        )
        task_root = f"task_packages/{task_id}"
        task_index = {
            "schema_version": TASK_PACKAGE_SCHEMA_VERSION,
            "story_id": delivery.story_id,
            "task_id": task_id,
            "samples": [
                {
                    **dict(index_item),
                    "source_batch_index": sample_index,
                }
            ],
        }
        serialized = {
            "predict_attributes.json": _ordered_json_bytes(task_attributes),
            "predict.json": _ordered_json_bytes(task_predict),
            "predict_index.json": _ordered_json_bytes(task_index),
        }
        audit = {
            "schema_version": TASK_PACKAGE_SCHEMA_VERSION,
            "task_id": task_id,
            "story_id": delivery.story_id,
            "theme_family_id": family.theme_family_id,
            "base_symbol": family.base_symbol,
            "introduced_in_section_id": family.introduced_in_section_id,
            "source_batch_index": sample_index,
            "sample_count": 1,
            "generation_target_bars": delivery.generation_target_bars,
            "output_motif_bars": delivery.output_motif_bars,
            "official_head_count": len(ATT_KEY),
            "probability_source": PROBABILITY_SOURCE,
            "project_policy_version": POLICY_VERSION,
            "softmax_probs_rule": "exact_same_shape_copy_of_predict_attributes",
            "uses_stage1_model": False,
            "uses_official_stage2_pre": True,
            "uses_official_stage2_generation": True,
            "queue_item_type": "atomic_task_directory",
            "files": {
                name: sha256_hex(data)
                for name, data in serialized.items()
            },
        }
        serialized["audit.json"] = _ordered_json_bytes(audit)
        for name, data in serialized.items():
            files[f"{task_root}/{name}"] = data
    return files


def build_encoding_files(delivery: MuseCocoDelivery) -> dict[str, bytes]:
    """Serialize direct-encoding files and add their auditable manifest."""

    payloads = build_encoding_payloads(delivery)
    files: dict[str, bytes] = {}
    for name, payload in payloads.items():
        # This file must retain the exact official att_key insertion order.
        files[name] = (
            _ordered_json_bytes(payload)
            if name == "predict_attributes.json"
            else json_bytes(payload)
        )
    task_files = build_task_package_files(delivery)
    files.update(task_files)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "story_id": delivery.story_id,
        "encoding_source": ENCODING_SOURCE,
        "closed_world_multilabel": True,
        "probability_source": PROBABILITY_SOURCE,
        "project_policy_version": POLICY_VERSION,
        "softmax_materialization": "worker_exact_shape_copy",
        "task_package_schema_version": TASK_PACKAGE_SCHEMA_VERSION,
        "task_package_count": len(delivery.theme_families),
        "sample_count": len(delivery.theme_families),
        "theme_family_ids": [family.theme_family_id for family in delivery.theme_families],
        "official_head_count": len(ATT_KEY),
        "tokens_per_sample": TOKENS_PER_SAMPLE,
        "att_key_sha256": ATT_KEY_SHA256,
        "num_labels_sha256": NUM_LABELS_SHA256,
        "attribute_dictionary_sha256": ATTRIBUTE_DICTIONARY_SHA256,
        "p4_verification_semantics": "ceil-semitone-span-over-12",
        "source_musecoco_plan_sha256": sha256_hex(json_bytes(delivery)),
        "files": {name: sha256_hex(data) for name, data in files.items()},
    }
    files["encoding_manifest.json"] = json_bytes(manifest)
    return files
