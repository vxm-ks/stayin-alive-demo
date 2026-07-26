#!/usr/bin/env python3
"""External, fail-closed task-package queue for an immutable MuseCoco runtime.

Compatible with the Python 3.8 MuseCoco conda environment.  This wrapper never
edits official source files.  Every cleanup is preceded by a snapshot of any
state that is actually present, and cleanup targets are a fixed allowlist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


VERSION = "1.3.0"
DEFAULT_MAX_ATTEMPTS = 3
TASK_SCHEMA = "musecoco-task-package-v1"
REQUIRED_TASK_FILES = (
    "predict_attributes.json",
    "predict.json",
    "predict_index.json",
    "audit.json",
)

OFFICIAL_SHA256 = {
    "stage2_pre.py": "681907a52ee266835d6f98b6a96428310c48320775232cd7d3d73b52f3152a80",
    "att_key.json": "8be36050b25a123dcf4109e090b00c6abe3465638b9c2c9d971da24b6a9006b2",
    "interactive_1billion.sh": "181aaad8fe2b98b40f2f9e08695262a329c1539d8a561d589a9abca56b516918",
    "interactive_dict_v5_1billion.py": "939d687065b8009871164a9a777cfd8b40416ba79d8eff3e4e2b8f3d7454e1ee",
    "A2M_task_new.py": "13d69f27ba8f777a0566572a00722d03921c1c38581bd8716f7777df2adebc90",
}

SCALAR_VECTOR_SIZES = {
    "R1": 3,
    "R3": 4,
    "S2s1": 18,
    "B1s1": 5,
    "TS1s1": 8,
    "K1": 3,
    "T1s1": 4,
    "P4": 13,
    "EM1": 5,
    "TM1": 6,
}


class MuseCocoQueueError(RuntimeError):
    pass


REMI_TOKEN_RE = re.compile(r"^([a-z])-([0-9]+)$")
REMI_MUSIC_TYPES = frozenset(("b", "s", "o", "t", "i", "p", "d", "v"))


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    root: Path
    stage1: Path
    stage2: Path
    output_root: Path
    tools: Path
    queue: Path
    running: Path
    failed: Path
    done: Path
    lock: Path
    queue_logs: Path
    runtime_backups: Path
    conda_prefix: Path

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        home = Path(os.environ.get("HOME", str(Path.home()))).expanduser().resolve()
        root = Path(
            os.environ.get("MUSECOCO_RUNTIME_ROOT", str(home / "musecoco_runtime"))
        ).expanduser().resolve()
        output_root = Path(
            os.environ.get("MUSECOCO_OUTPUT_ROOT", str(home / "MuseCoco_outputs"))
        ).expanduser().resolve()
        tools = Path(
            os.environ.get("MUSECOCO_TOOLS_ROOT", str(home / "musecoco_tools"))
        ).expanduser().resolve()
        conda_prefix = Path(
            os.environ.get(
                "MUSECOCO_CONDA_PREFIX",
                str(home / "miniforge3" / "envs" / "MuseCoco"),
            )
        ).expanduser().resolve()
        return cls(
            home=home,
            root=root,
            stage1=root / "1-text2attribute_model",
            stage2=root / "2-attribute2music_model",
            output_root=output_root,
            tools=tools,
            queue=root / "task_queue",
            running=root / "task_running",
            failed=root / "task_failed",
            done=root / "task_done",
            lock=root / "task_queue.lock",
            queue_logs=output_root / "queue_logs",
            runtime_backups=output_root / "runtime_state_backups",
            conda_prefix=conda_prefix,
        )

    @property
    def stage1_tmp(self) -> Path:
        return self.stage1 / "tmp"

    @property
    def stage1_predict(self) -> Path:
        return self.stage1 / "data" / "predict.json"

    @property
    def stage1_attributes(self) -> Path:
        return self.stage1_tmp / "predict_attributes.json"

    @property
    def stage1_probs(self) -> Path:
        return self.stage1_tmp / "softmax_probs.json"

    @property
    def stage1_bin(self) -> Path:
        return self.stage1 / "infer_test.bin"

    @property
    def stage2_bin(self) -> Path:
        return self.stage2 / "data" / "infer_input" / "infer_test.bin"

    @property
    def generation_dir(self) -> Path:
        return (
            self.stage2
            / "generation"
            / "0505"
            / "linear_mask-1billion-checkpoint_2_280000"
            / "infer_test"
        )

    @property
    def generation_sample(self) -> Path:
        return self.generation_dir / "topk15-t1.0-ngram0" / "0"

    @property
    def remi_file(self) -> Path:
        return self.generation_sample / "remi" / "0.txt"

    @property
    def midi_file(self) -> Path:
        return self.generation_sample / "midi" / "0.mid"

    @property
    def official_log_1(self) -> Path:
        return self.stage2 / "log" / "0505" / "infer_test"

    @property
    def official_log_2(self) -> Path:
        return self.stage2 / "log" / "0505" / "linear_mask-1billion"

    def official_files(self) -> Dict[str, Path]:
        return {
            "stage2_pre.py": self.stage1 / "stage2_pre.py",
            "att_key.json": self.stage1 / "data" / "att_key.json",
            "interactive_1billion.sh": self.stage2 / "interactive_1billion.sh",
            "interactive_dict_v5_1billion.py": (
                self.stage2 / "linear_mask" / "interactive_dict_v5_1billion.py"
            ),
            "A2M_task_new.py": self.stage2 / "linear_mask" / "A2M_task_new.py",
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")


def read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise MuseCocoQueueError("invalid JSON {}: {}".format(path, exc))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.tmp-{}".format(path.name, uuid.uuid4().hex))
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_directories(paths: RuntimePaths) -> None:
    for directory in (
        paths.queue,
        paths.running,
        paths.failed,
        paths.done,
        paths.queue_logs,
        paths.runtime_backups,
        paths.stage1_tmp,
        paths.stage2_bin.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def validate_official_installation(
    paths: RuntimePaths,
    expected_hashes: Mapping[str, str] = OFFICIAL_SHA256,
    enforce_environment: bool = True,
) -> Dict[str, str]:
    if enforce_environment:
        expected_python = paths.conda_prefix / "bin" / "python"
        try:
            same_python = expected_python.is_file() and os.path.samefile(
                str(expected_python), sys.executable
            )
        except OSError:
            same_python = False
        if not same_python:
            raise MuseCocoQueueError(
                "wrapper must run with the MuseCoco environment Python: {}".format(
                    expected_python
                )
            )
    observed = {}
    for name, path in paths.official_files().items():
        if not path.is_file() or path.is_symlink():
            raise MuseCocoQueueError("official file missing or symlinked: {}".format(path))
        observed[name] = sha256_file(path)
        expected = expected_hashes.get(name)
        if expected is None or observed[name].lower() != expected.lower():
            raise MuseCocoQueueError(
                "official file hash mismatch; wrapper refuses to continue: {}".format(path)
            )
    return observed


def _expected_vector_size(key: str) -> int:
    if key.startswith("I1s2_") or key.startswith("S4_"):
        return 3
    try:
        return SCALAR_VECTOR_SIZES[key]
    except KeyError:
        raise MuseCocoQueueError("no vector-size contract for attribute {}".format(key))


def _validate_one_hot(key: str, vector) -> None:
    size = _expected_vector_size(key)
    if not isinstance(vector, list) or len(vector) != size:
        raise MuseCocoQueueError(
            "{} must be a vector of length {}, got {}".format(
                key, size, len(vector) if isinstance(vector, list) else "non-list"
            )
        )
    if any(value not in (0, 1) for value in vector) or sum(vector) != 1:
        raise MuseCocoQueueError("{} must be strict one-hot".format(key))


def validate_task_package(
    task_dir: Path,
    paths: RuntimePaths,
    expected_task_id: Optional[str] = None,
) -> Dict[str, object]:
    task_dir = task_dir.resolve()
    if not task_dir.is_dir() or task_dir.is_symlink():
        raise MuseCocoQueueError("task package must be a real directory: {}".format(task_dir))
    entries = sorted(item.name for item in task_dir.iterdir())
    if entries != sorted(REQUIRED_TASK_FILES):
        raise MuseCocoQueueError(
            "task package files mismatch; expected {}, got {}".format(
                sorted(REQUIRED_TASK_FILES), entries
            )
        )
    for name in REQUIRED_TASK_FILES:
        path = task_dir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise MuseCocoQueueError("task file missing, empty, or symlinked: {}".format(path))

    attributes = read_json(task_dir / "predict_attributes.json")
    att_keys = read_json(paths.official_files()["att_key.json"])
    if not isinstance(att_keys, list) or len(att_keys) != 60:
        raise MuseCocoQueueError("official att_key.json must contain exactly 60 keys")
    if not isinstance(attributes, dict) or list(attributes.keys()) != att_keys:
        raise MuseCocoQueueError(
            "predict_attributes.json keys/order must exactly match official att_key.json"
        )
    for key in att_keys:
        batch = attributes[key]
        if not isinstance(batch, list) or len(batch) != 1:
            raise MuseCocoQueueError("{} must contain exactly one sample".format(key))
        _validate_one_hot(key, batch[0])

    predict = read_json(task_dir / "predict.json")
    if (
        not isinstance(predict, list)
        or len(predict) != 1
        or not isinstance(predict[0], dict)
        or not isinstance(predict[0].get("text"), str)
        or not predict[0]["text"].strip()
    ):
        raise MuseCocoQueueError("predict.json must contain one non-empty text item")

    index = read_json(task_dir / "predict_index.json")
    task_id = expected_task_id or task_dir.name
    if (
        not isinstance(index, dict)
        or index.get("schema_version") != TASK_SCHEMA
        or index.get("task_id") != task_id
        or not isinstance(index.get("samples"), list)
        or len(index["samples"]) != 1
    ):
        raise MuseCocoQueueError("predict_index.json does not describe this one-sample task")

    audit = read_json(task_dir / "audit.json")
    required_audit = {
        "schema_version": TASK_SCHEMA,
        "task_id": task_id,
        "sample_count": 1,
        "official_head_count": 60,
        "probability_source": "deterministic_one_hot",
        "softmax_probs_rule": "exact_same_shape_copy_of_predict_attributes",
        "uses_stage1_model": False,
        "uses_official_stage2_pre": True,
        "uses_official_stage2_generation": True,
        "queue_item_type": "atomic_task_directory",
    }
    if not isinstance(audit, dict):
        raise MuseCocoQueueError("audit.json must be an object")
    for key, expected in required_audit.items():
        if audit.get(key) != expected:
            raise MuseCocoQueueError(
                "audit.json {} must be {!r}".format(key, expected)
            )
    recorded_files = audit.get("files")
    if not isinstance(recorded_files, dict):
        raise MuseCocoQueueError("audit.json must contain input file hashes")
    for name in ("predict_attributes.json", "predict.json", "predict_index.json"):
        if recorded_files.get(name) != sha256_file(task_dir / name):
            raise MuseCocoQueueError("task input hash mismatch for {}".format(name))
    return {
        "attributes": attributes,
        "predict": predict,
        "index": index,
        "audit": audit,
    }


def _is_nonempty_state(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir() and not path.is_symlink():
        return next(path.iterdir(), None) is not None
    return True


def _state_targets(paths: RuntimePaths) -> Tuple[Path, ...]:
    return (
        paths.stage1_bin,
        paths.stage2_bin,
        paths.stage1_predict,
        paths.stage1_tmp,
        paths.generation_dir,
        paths.official_log_1,
        paths.official_log_2,
    )


def _generation_targets(paths: RuntimePaths) -> Tuple[Path, ...]:
    return (paths.generation_dir, paths.official_log_1, paths.official_log_2)


def _relative_runtime_path(paths: RuntimePaths, target: Path) -> Path:
    try:
        return target.relative_to(paths.root)
    except ValueError:
        raise MuseCocoQueueError("cleanup target escapes MuseCoco runtime: {}".format(target))


def snapshot_runtime_state(
    paths: RuntimePaths,
    snapshot_parent: Path,
    reason: str,
    targets: Optional[Iterable[Path]] = None,
) -> Optional[Path]:
    present = [path for path in (targets or _state_targets(paths)) if _is_nonempty_state(path)]
    if not present:
        return None
    snapshot_dir = snapshot_parent / "runtime_snapshot_{}_{}".format(reason, timestamp())
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for source in present:
        relative = _relative_runtime_path(paths, source)
        destination = snapshot_dir / "musecoco_runtime" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            raise MuseCocoQueueError(
                "refusing to snapshot or clean symlinked runtime state: {}".format(source)
            )
        if source.is_dir():
            shutil.copytree(str(source), str(destination), copy_function=shutil.copy2)
            for file_path in destination.rglob("*"):
                if file_path.is_file():
                    hashes[str(file_path.relative_to(snapshot_dir))] = sha256_file(file_path)
        else:
            shutil.copy2(str(source), str(destination))
            hashes[str(destination.relative_to(snapshot_dir))] = sha256_file(destination)
    write_json(
        snapshot_dir / "snapshot_manifest.json",
        {
            "schema_version": "musecoco-runtime-snapshot-v1",
            "created_at": datetime.now().astimezone().isoformat(),
            "reason": reason,
            "source_runtime": str(paths.root),
            "source_paths": [str(path) for path in present],
            "files": hashes,
        },
    )
    return snapshot_dir


def _remove_allowed(paths: RuntimePaths, target: Path) -> None:
    _relative_runtime_path(paths, target)
    if target.is_symlink():
        raise MuseCocoQueueError("refusing to clean symlink: {}".format(target))
    if target.is_dir():
        shutil.rmtree(str(target))
    elif target.exists():
        target.unlink()


def verify_clean_state(paths: RuntimePaths) -> None:
    for file_path in (paths.stage1_bin, paths.stage2_bin, paths.stage1_predict):
        if file_path.exists() or file_path.is_symlink():
            raise MuseCocoQueueError("runtime cleanup verification failed: {}".format(file_path))
    if not paths.stage1_tmp.is_dir() or next(paths.stage1_tmp.iterdir(), None) is not None:
        raise MuseCocoQueueError("Stage1 tmp must exist and be empty after cleanup")
    for directory in (paths.generation_dir, paths.official_log_1, paths.official_log_2):
        if directory.exists() or directory.is_symlink():
            raise MuseCocoQueueError("runtime cleanup verification failed: {}".format(directory))
    if not paths.stage2_bin.parent.is_dir():
        raise MuseCocoQueueError("Stage2 infer_input directory is missing after cleanup")


def cleanup_runtime_state(paths: RuntimePaths, snapshot_parent: Path, reason: str) -> Optional[Path]:
    snapshot = snapshot_runtime_state(paths, snapshot_parent, reason)
    for target in _state_targets(paths):
        if target == paths.stage1_tmp:
            if target.exists() or target.is_symlink():
                _remove_allowed(paths, target)
            target.mkdir(parents=True, exist_ok=True)
        elif target.exists() or target.is_symlink():
            _remove_allowed(paths, target)
    paths.stage2_bin.parent.mkdir(parents=True, exist_ok=True)
    verify_clean_state(paths)
    return snapshot


def clear_generation_state(paths: RuntimePaths, snapshot_parent: Path, reason: str) -> Optional[Path]:
    snapshot = snapshot_runtime_state(
        paths, snapshot_parent, reason, targets=_generation_targets(paths)
    )
    for target in _generation_targets(paths):
        if target.exists() or target.is_symlink():
            _remove_allowed(paths, target)
    for target in _generation_targets(paths):
        if target.exists() or target.is_symlink():
            raise MuseCocoQueueError("generation cleanup verification failed: {}".format(target))
    return snapshot


def _run_logged(
    command: Sequence[str], cwd: Path, log_path: Path, environment: Mapping[str, str]
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        process.stdout.close()
        return process.wait()


def _runtime_environment(paths: RuntimePaths) -> Dict[str, str]:
    environment = dict(os.environ)
    bin_dir = str(paths.conda_prefix / "bin")
    environment["PATH"] = bin_dir + os.pathsep + environment.get("PATH", "")
    environment["CONDA_PREFIX"] = str(paths.conda_prefix)
    environment["CONDA_DEFAULT_ENV"] = "MuseCoco"
    return environment


def _write_hash_list(path: Path, named_paths: Mapping[str, Path]) -> Dict[str, str]:
    values = {name: sha256_file(file_path) for name, file_path in named_paths.items()}
    with path.open("w", encoding="utf-8") as handle:
        for name in sorted(values):
            handle.write("{}  {}\n".format(values[name], name))
    return values


def validate_remi_output(path: Path) -> Dict[str, int]:
    """Validate the generated REMIGEN2 music suffix without editing it."""

    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise MuseCocoQueueError(
            "expected non-empty, non-symlinked official REMI output not found: {}".format(
                path
            )
        )
    try:
        tokens = path.read_text(encoding="utf-8").split()
    except (OSError, UnicodeError) as exc:
        raise MuseCocoQueueError("could not read official REMI output {}: {}".format(path, exc))
    if tokens.count("<sep>") != 1:
        raise MuseCocoQueueError("official REMI output must contain exactly one <sep> token")
    music_tokens = tokens[tokens.index("<sep>") + 1 :]
    if not music_tokens:
        raise MuseCocoQueueError("official REMI output has no music tokens after <sep>")

    previous_type = None
    bar_count = 0
    note_count = 0
    for index, token in enumerate(music_tokens):
        match = REMI_TOKEN_RE.fullmatch(token)
        if match is None:
            raise MuseCocoQueueError(
                "invalid REMI music token at index {}: {!r}".format(index, token)
            )
        item_type = match.group(1)
        if item_type not in REMI_MUSIC_TYPES:
            raise MuseCocoQueueError(
                "unknown REMI music token type at index {}: {!r}".format(index, token)
            )
        if item_type == "d" and previous_type != "p":
            raise MuseCocoQueueError(
                "invalid REMI transition at index {}: duration follows {!r}, not pitch".format(
                    index, previous_type
                )
            )
        if item_type == "v" and previous_type != "d":
            raise MuseCocoQueueError(
                "invalid REMI transition at index {}: velocity follows {!r}, not duration".format(
                    index, previous_type
                )
            )
        if item_type == "b":
            bar_count += 1
        elif item_type == "v":
            note_count += 1
        previous_type = item_type

    if previous_type not in ("b", "v"):
        raise MuseCocoQueueError(
            "official REMI output ends with incomplete token type {!r}".format(previous_type)
        )
    if bar_count < 1 or note_count < 1:
        raise MuseCocoQueueError(
            "official REMI output contains no complete musical material"
        )
    return {
        "token_count": len(music_tokens),
        "bar_token_count": bar_count,
        "complete_note_count": note_count,
    }


def validate_midi_output(path: Path) -> Dict[str, int]:
    """Perform a dependency-free structural check of an official MIDI result."""

    if not path.is_file() or path.is_symlink() or path.stat().st_size < 14:
        raise MuseCocoQueueError(
            "expected non-empty, non-symlinked official MIDI output not found: {}".format(
                path
            )
        )
    with path.open("rb") as handle:
        header = handle.read(14)
    if header[:4] != b"MThd" or int.from_bytes(header[4:8], "big") != 6:
        raise MuseCocoQueueError("official MIDI output has an invalid MThd header: {}".format(path))
    track_count = int.from_bytes(header[10:12], "big")
    ticks_per_beat = int.from_bytes(header[12:14], "big")
    if track_count < 1 or ticks_per_beat == 0 or ticks_per_beat & 0x8000:
        raise MuseCocoQueueError(
            "official MIDI output has invalid track/division fields: {}".format(path)
        )
    return {
        "byte_count": path.stat().st_size,
        "track_count": track_count,
        "ticks_per_beat": ticks_per_beat,
    }


def truncate_remi_to_complete_bars(
    source: Path,
    destination: Path,
    target_bars: int,
) -> Dict[str, int]:
    """Keep exactly the requested complete REMIGEN2 bar prefix."""

    if target_bars < 1:
        raise MuseCocoQueueError("REMI truncation target_bars must be positive")
    try:
        tokens = source.read_text(encoding="utf-8").split()
    except (OSError, UnicodeError) as exc:
        raise MuseCocoQueueError("could not read REMI for truncation {}: {}".format(source, exc))
    if tokens.count("<sep>") != 1:
        raise MuseCocoQueueError("REMI truncation requires exactly one <sep> token")
    separator = tokens.index("<sep>")
    music_tokens = tokens[separator + 1 :]
    bar_indices = [
        index for index, token in enumerate(music_tokens) if token.startswith("b-")
    ]
    if len(bar_indices) < target_bars:
        raise MuseCocoQueueError(
            "REMI has only {} complete bar boundaries; {} required".format(
                len(bar_indices), target_bars
            )
        )
    cutoff = bar_indices[target_bars - 1] + 1
    trimmed_music = music_tokens[:cutoff]
    destination.write_text(
        " ".join(tokens[: separator + 1] + trimmed_music),
        encoding="utf-8",
    )
    validation = validate_remi_output(destination)
    return {
        "target_bars": target_bars,
        "raw_music_token_count": len(music_tokens),
        "kept_music_token_count": len(trimmed_music),
        "discarded_music_token_count": len(music_tokens) - len(trimmed_music),
        **validation,
    }


def _decode_remi_to_midi(remi_path: Path, midi_path: Path, paths: RuntimePaths) -> None:
    """Decode a validated prefix with MuseCoco's installed decoder library."""

    module_root = str(paths.stage2)
    added_to_path = module_root not in sys.path
    if added_to_path:
        sys.path.insert(0, module_root)
    try:
        from midiprocessor import MidiDecoder

        tokens = remi_path.read_text(encoding="utf-8").split()
        music_tokens = tokens[tokens.index("<sep>") + 1 :]
        midi_object = MidiDecoder("REMIGEN2").decode_from_token_str_list(music_tokens)
        midi_object.dump(str(midi_path))
    except Exception as exc:
        raise MuseCocoQueueError(
            "target-bar REMI fallback could not be decoded: {}: {}".format(
                type(exc).__name__, exc
            )
        )
    finally:
        if added_to_path:
            sys.path.remove(module_root)


def run_worker(
    task_dir: Path,
    paths: RuntimePaths,
    expected_hashes: Mapping[str, str] = OFFICIAL_SHA256,
    enforce_environment: bool = True,
) -> Path:
    official_hashes = validate_official_installation(
        paths, expected_hashes, enforce_environment=enforce_environment
    )
    task = validate_task_package(task_dir, paths)
    ensure_directories(paths)
    save_dir = paths.output_root / "from_task_{}_{}".format(timestamp(), task_dir.name)
    save_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(str(task_dir), str(save_dir / "input_task"), copy_function=shutil.copy2)
    success = False
    softmax_copy_verified = False
    primary_error = None
    output_hashes = {}
    output_validation = {"status": "not_started"}
    try:
        cleanup_runtime_state(paths, save_dir, "before_install")
        shutil.copy2(str(task_dir / "predict_attributes.json"), str(paths.stage1_attributes))
        shutil.copy2(str(task_dir / "predict_attributes.json"), str(paths.stage1_probs))
        shutil.copy2(str(task_dir / "predict.json"), str(paths.stage1_predict))

        shutil.copy2(str(paths.stage1_predict), str(save_dir / "predict.json"))
        shutil.copy2(str(paths.stage1_attributes), str(save_dir / "predict_attributes.json"))
        shutil.copy2(str(paths.stage1_probs), str(save_dir / "softmax_probs.json"))
        if sha256_file(paths.stage1_attributes) != sha256_file(paths.stage1_probs):
            raise MuseCocoQueueError(
                "deterministic softmax_probs.json is not an exact copy of predict_attributes.json"
            )
        softmax_copy_verified = True

        environment = _runtime_environment(paths)
        stage2_pre_status = _run_logged(
            [sys.executable, "stage2_pre.py"],
            paths.stage1,
            save_dir / "stage2_pre.log",
            environment,
        )
        if stage2_pre_status != 0 or not paths.stage1_bin.is_file() or paths.stage1_bin.stat().st_size == 0:
            raise MuseCocoQueueError("official stage2_pre.py failed or produced no infer_test.bin")
        shutil.copy2(str(paths.stage1_bin), str(save_dir / "infer_test.stage1.bin"))
        paths.stage2_bin.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(paths.stage1_bin), str(paths.stage2_bin))
        shutil.copy2(str(paths.stage2_bin), str(save_dir / "infer_test.stage2.bin"))
        infer_hashes = _write_hash_list(
            save_dir / "infer_test.sha256.txt",
            {
                "infer_test.stage1.bin": save_dir / "infer_test.stage1.bin",
                "infer_test.stage2.bin": save_dir / "infer_test.stage2.bin",
            },
        )
        if len(set(infer_hashes.values())) != 1:
            raise MuseCocoQueueError("Stage1 and Stage2 infer_test.bin hashes differ")

        clear_generation_state(paths, save_dir, "before_stage2_generation")
        generation_status = _run_logged(
            ["bash", "interactive_1billion.sh", "0", "1"],
            paths.stage2,
            save_dir / "stage2_generate.log",
            environment,
        )
        if generation_status != 0:
            raise MuseCocoQueueError("official interactive_1billion.sh returned non-zero")
        output_validation["status"] = "in_progress"
        if (
            not paths.remi_file.is_file()
            or paths.remi_file.is_symlink()
            or paths.remi_file.stat().st_size == 0
        ):
            raise MuseCocoQueueError(
                "expected non-empty, non-symlinked official REMI output not found: {}".format(
                    paths.remi_file
                )
            )
        raw_remi = save_dir / "result.remi.raw.txt"
        final_remi = save_dir / "result.remi.txt"
        final_midi = save_dir / "result.mid"
        shutil.copy2(str(paths.remi_file), str(raw_remi))
        try:
            output_validation["remi"] = validate_remi_output(raw_remi)
            if (
                not paths.midi_file.is_file()
                or paths.midi_file.is_symlink()
                or paths.midi_file.stat().st_size == 0
            ):
                raise MuseCocoQueueError(
                    "official REMI was generated but no MIDI was decoded: {}".format(
                        paths.midi_file
                    )
                )
            validate_midi_output(paths.midi_file)
            shutil.copy2(str(raw_remi), str(final_remi))
            shutil.copy2(str(paths.midi_file), str(final_midi))
            output_validation["mode"] = "official_full_output"
        except MuseCocoQueueError as official_output_error:
            target_bars = task["audit"].get("generation_target_bars")
            if not isinstance(target_bars, int):
                raise MuseCocoQueueError(
                    "official output rejected ({}); task has no integer generation_target_bars "
                    "for safe fallback".format(official_output_error)
                )
            try:
                output_validation["truncation"] = truncate_remi_to_complete_bars(
                    raw_remi,
                    final_remi,
                    target_bars,
                )
                _decode_remi_to_midi(final_remi, final_midi, paths)
                output_validation["remi"] = validate_remi_output(final_remi)
                output_validation["mode"] = "target_bar_truncation_fallback"
                output_validation["official_output_error"] = str(official_output_error)
            except Exception as fallback_error:
                raise MuseCocoQueueError(
                    "official output rejected ({}); target-bar fallback failed ({})".format(
                        official_output_error, fallback_error
                    )
                )
        output_validation["midi"] = validate_midi_output(save_dir / "result.mid")
        output_validation["status"] = "passed"
        result_files = {
            "result.remi.raw.txt": raw_remi,
            "result.remi.txt": final_remi,
            "result.mid": final_midi,
        }
        output_hashes = _write_hash_list(save_dir / "result.sha256.txt", result_files)
        success = True
    except Exception as exc:
        primary_error = "{}: {}".format(type(exc).__name__, exc)
        if output_validation["status"] == "in_progress":
            output_validation["status"] = "failed"
            output_validation["error"] = primary_error
        with (save_dir / "worker_error.log").open("w", encoding="utf-8") as handle:
            traceback.print_exc(file=handle)
    finally:
        cleanup_error = None
        try:
            cleanup_runtime_state(
                paths,
                save_dir,
                "after_success" if success else "after_failure",
            )
        except Exception as exc:
            cleanup_error = "{}: {}".format(type(exc).__name__, exc)
            success = False
        write_json(
            save_dir / "result_audit.json",
            {
                "schema_version": "musecoco-task-result-v1",
                "wrapper_version": VERSION,
                "task_id": task_dir.name,
                "status": "success" if success else "failed",
                "probability_source": "deterministic_one_hot",
                "softmax_probs_exact_input_copy": softmax_copy_verified,
                "uses_stage1_model": False,
                "uses_official_stage2_pre": True,
                "uses_official_stage2_generation": True,
                "official_files_immutable": True,
                "official_file_sha256": official_hashes,
                "task_audit": task["audit"],
                "output_sha256": output_hashes,
                "output_validation": output_validation,
                "primary_error": primary_error,
                "cleanup_error": cleanup_error,
                "runtime_clean_verified": cleanup_error is None,
            },
        )
    if not success:
        raise MuseCocoQueueError(
            "worker failed; debug output retained in {}: {}".format(
                save_dir, primary_error or "strict cleanup failed"
            )
        )
    return save_dir


def enqueue_task_packages(source: Path, paths: RuntimePaths) -> Dict[str, List[str]]:
    ensure_directories(paths)
    source = source.resolve()
    if not source.is_dir():
        raise MuseCocoQueueError("task package source directory not found: {}".format(source))
    task_dirs = sorted(path for path in source.iterdir() if path.is_dir() and not path.name.startswith("."))
    if not task_dirs:
        raise MuseCocoQueueError("no task package directories found in {}".format(source))
    enqueued = []
    skipped_done = []
    for task_dir in task_dirs:
        validate_task_package(task_dir, paths)
        existing = [directory / task_dir.name for directory in (paths.queue, paths.running, paths.failed, paths.done)]
        matches = [path for path in existing if path.exists()]
        if matches:
            done_match = paths.done / task_dir.name
            if len(matches) == 1 and done_match in matches:
                source_audit = sha256_file(task_dir / "audit.json")
                done_audit = sha256_file(done_match / "audit.json")
                if source_audit == done_audit:
                    skipped_done.append(task_dir.name)
                    continue
            raise MuseCocoQueueError(
                "task ID already exists in queue state: {} -> {}".format(
                    task_dir.name, [str(path) for path in matches]
                )
            )
        staging = paths.queue / ".incoming-{}-{}".format(task_dir.name, uuid.uuid4().hex)
        destination = paths.queue / task_dir.name
        try:
            shutil.copytree(str(task_dir), str(staging), copy_function=shutil.copy2)
            validate_task_package(staging, paths, expected_task_id=task_dir.name)
            os.replace(str(staging), str(destination))
        finally:
            if staging.exists():
                shutil.rmtree(str(staging), ignore_errors=True)
        enqueued.append(task_dir.name)
    return {"enqueued": enqueued, "skipped_already_done": skipped_done}


def recover_running_tasks(paths: RuntimePaths) -> List[str]:
    recovered = []
    for task in sorted(path for path in paths.running.iterdir() if path.is_dir()):
        destination = paths.failed / "recovered_{}_{}".format(timestamp(), task.name)
        os.replace(str(task), str(destination))
        recovered.append(destination.name)
    return recovered


def _stream_worker(command: Sequence[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            log.write(line)
            log.flush()
        process.stdout.close()
        return process.wait()


def run_queue(
    paths: RuntimePaths,
    max_tasks: Optional[int] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Dict[str, object]:
    if max_attempts < 1:
        raise MuseCocoQueueError("max_attempts must be positive")
    validate_official_installation(paths)
    ensure_directories(paths)
    try:
        paths.lock.mkdir()
    except FileExistsError:
        raise MuseCocoQueueError("another MuseCoco task queue is already running")
    successes = []
    failures = []
    attempts_by_task = {}
    try:
        recovered = recover_running_tasks(paths)
        cleanup_runtime_state(paths, paths.runtime_backups, "queue_start")
        processed = 0
        while max_tasks is None or processed < max_tasks:
            pending = sorted(path for path in paths.queue.iterdir() if path.is_dir() and not path.name.startswith("."))
            if not pending:
                break
            source = pending[0]
            original_name = source.name
            running_task = paths.running / original_name
            os.replace(str(source), str(running_task))
            execution_id = timestamp()
            attempt_records = []
            status = 1
            for attempt in range(1, max_attempts + 1):
                item_log = paths.queue_logs / "{}_{}_attempt_{:02d}.log".format(
                    execution_id, original_name, attempt
                )
                status = _stream_worker(
                    [sys.executable, str(Path(__file__).resolve()), "worker", str(running_task)],
                    item_log,
                )
                attempt_records.append(
                    {
                        "attempt": attempt,
                        "return_code": status,
                        "status": "success" if status == 0 else "failed",
                        "log": str(item_log),
                    }
                )
                write_json(
                    paths.queue_logs / "{}_{}.attempts.json".format(execution_id, original_name),
                    {
                        "schema_version": "musecoco-task-attempts-v1",
                        "task_id": original_name,
                        "max_attempts": max_attempts,
                        "attempts": attempt_records,
                    },
                )
                cleanup_runtime_state(
                    paths,
                    paths.runtime_backups,
                    "queue_after_attempt_{:02d}".format(attempt),
                )
                if status == 0:
                    break
            attempts_by_task[original_name] = len(attempt_records)
            if status == 0:
                destination = paths.done / original_name
                if destination.exists():
                    raise MuseCocoQueueError("done task collision: {}".format(destination))
                os.replace(str(running_task), str(destination))
                successes.append(original_name)
            else:
                destination = paths.failed / "failed_{}_{}".format(timestamp(), original_name)
                os.replace(str(running_task), str(destination))
                failures.append(original_name)
            processed += 1
        verify_clean_state(paths)
        return {
            "status": "PASS" if not failures else "PARTIAL_FAILURE",
            "recovered_to_failed": recovered,
            "successes": successes,
            "failures": failures,
            "attempts_by_task": attempts_by_task,
            "remaining": sorted(path.name for path in paths.queue.iterdir() if path.is_dir()),
        }
    finally:
        if paths.lock.exists() and paths.lock.is_dir():
            try:
                paths.lock.rmdir()
            except OSError:
                raise MuseCocoQueueError("could not release queue lock {}".format(paths.lock))


def queue_status(paths: RuntimePaths) -> Dict[str, object]:
    def directory_names(directory: Path) -> List[str]:
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.iterdir() if path.is_dir())

    return {
        "queue": directory_names(paths.queue),
        "running": directory_names(paths.running),
        "failed": directory_names(paths.failed),
        "done": directory_names(paths.done),
        "locked": paths.lock.exists(),
    }


def _successful_output_for_task(
    paths: RuntimePaths, task_id: str, task_audit: Mapping[str, object]
) -> Tuple[Path, Dict[str, object]]:
    candidates = sorted(
        paths.output_root.glob("from_task_*_{}".format(task_id)), reverse=True
    )
    for candidate in candidates:
        audit_path = candidate / "result_audit.json"
        if not audit_path.is_file() or audit_path.is_symlink():
            continue
        audit = read_json(audit_path)
        if (
            isinstance(audit, dict)
            and audit.get("status") == "success"
            and audit.get("task_id") == task_id
            and audit.get("task_audit") == task_audit
        ):
            output_hashes = audit.get("output_sha256")
            validation = audit.get("output_validation")
            if (
                not isinstance(output_hashes, dict)
                or not isinstance(validation, dict)
                or validation.get("status") != "passed"
            ):
                continue
            valid = True
            for name in ("result.remi.txt", "result.mid"):
                result_path = candidate / name
                if (
                    not result_path.is_file()
                    or result_path.is_symlink()
                    or result_path.stat().st_size == 0
                    or output_hashes.get(name) != sha256_file(result_path)
                ):
                    valid = False
                    break
            if valid:
                return candidate, audit
    raise MuseCocoQueueError(
        "no successful, input-matching output found for task {}".format(task_id)
    )


def collect_task_results(
    source: Path,
    destination: Path,
    paths: RuntimePaths,
    force: bool = False,
) -> Dict[str, object]:
    """Atomically copy audited successful results into a Stage 1 delivery directory."""

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir() or source.is_symlink():
        raise MuseCocoQueueError("task source must be a real directory: {}".format(source))
    task_dirs = sorted(
        item for item in source.iterdir() if item.is_dir() and not item.name.startswith(".")
    )
    if not task_dirs:
        raise MuseCocoQueueError("no task packages found in {}".format(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not force:
        raise MuseCocoQueueError(
            "collection destination already exists: {}; use --force to replace it".format(
                destination
            )
        )
    token = uuid.uuid4().hex
    staging = destination.parent / ".{}.staging-{}".format(destination.name, token)
    backup = destination.parent / ".{}.backup-{}".format(destination.name, token)
    collected = []
    try:
        staging.mkdir()
        for task_dir in task_dirs:
            task = validate_task_package(task_dir, paths)
            task_id = task_dir.name
            done_task = paths.done / task_id
            if not done_task.is_dir():
                raise MuseCocoQueueError("task is not in done state: {}".format(task_id))
            if sha256_file(done_task / "audit.json") != sha256_file(task_dir / "audit.json"):
                raise MuseCocoQueueError("done task audit differs from source: {}".format(task_id))
            task_audit = task["audit"]
            assert isinstance(task_audit, dict)
            output_dir, result_audit = _successful_output_for_task(
                paths, task_id, task_audit
            )
            output_hashes = result_audit.get("output_sha256")
            if not isinstance(output_hashes, dict):
                raise MuseCocoQueueError("result audit has no output hashes: {}".format(task_id))
            task_destination = staging / task_id
            task_destination.mkdir()
            copied_hashes = {}
            for source_name, destination_name in (
                ("result.mid", "raw.mid"),
                ("result.remi.txt", "result.remi.txt"),
                ("result_audit.json", "source_result_audit.json"),
            ):
                source_file = output_dir / source_name
                if not source_file.is_file() or source_file.is_symlink() or source_file.stat().st_size == 0:
                    raise MuseCocoQueueError(
                        "required result missing, empty, or symlinked: {}".format(source_file)
                    )
                destination_file = task_destination / destination_name
                shutil.copy2(str(source_file), str(destination_file))
                copied_hashes[destination_name] = sha256_file(destination_file)
            if output_hashes.get("result.mid") != copied_hashes["raw.mid"]:
                raise MuseCocoQueueError("result.mid hash mismatch for {}".format(task_id))
            if output_hashes.get("result.remi.txt") != copied_hashes["result.remi.txt"]:
                raise MuseCocoQueueError("result.remi.txt hash mismatch for {}".format(task_id))
            collected.append(
                {
                    "task_id": task_id,
                    "theme_family_id": task_audit.get("theme_family_id"),
                    "source_output_dir": str(output_dir),
                    "files": copied_hashes,
                }
            )
        write_json(
            staging / "collection_manifest.json",
            {
                "schema_version": "musecoco-result-collection-v1",
                "wrapper_version": VERSION,
                "source_task_packages": str(source),
                "items": collected,
            },
        )
        if destination.exists():
            os.replace(str(destination), str(backup))
        try:
            os.replace(str(staging), str(destination))
        except Exception:
            if backup.exists() and not destination.exists():
                os.replace(str(backup), str(destination))
            raise
        if backup.exists():
            shutil.rmtree(str(backup))
        return {
            "status": "PASS",
            "destination": str(destination),
            "collected": collected,
        }
    finally:
        if staging.exists():
            shutil.rmtree(str(staging), ignore_errors=True)
        if backup.exists() and destination.exists():
            shutil.rmtree(str(backup), ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="musecoco_task_queue")
    parser.add_argument("--version", action="version", version="%(prog)s {}".format(VERSION))
    subparsers = parser.add_subparsers(dest="command", required=True)
    enqueue = subparsers.add_parser("enqueue", help="atomically enqueue task package directories")
    enqueue.add_argument("--source", type=Path, required=True)
    worker = subparsers.add_parser("worker", help="process exactly one running task package")
    worker.add_argument("task_dir", type=Path)
    run = subparsers.add_parser("run", help="process queued task packages serially")
    run.add_argument("--max-tasks", type=int)
    run.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    subparsers.add_parser("status", help="show queue state without modifying it")
    validate = subparsers.add_parser("validate-task", help="validate one task package")
    validate.add_argument("task_dir", type=Path)
    collect = subparsers.add_parser("collect", help="copy audited successful results")
    collect.add_argument("--source", type=Path, required=True)
    collect.add_argument("--destination", type=Path, required=True)
    collect.add_argument("--force", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    paths = RuntimePaths.from_environment()
    try:
        if args.command == "enqueue":
            validate_official_installation(paths)
            result = enqueue_task_packages(args.source, paths)
        elif args.command == "worker":
            output = run_worker(args.task_dir, paths)
            result = {"status": "PASS", "output_dir": str(output)}
        elif args.command == "run":
            if args.max_tasks is not None and args.max_tasks < 1:
                raise MuseCocoQueueError("--max-tasks must be positive")
            if args.max_attempts < 1:
                raise MuseCocoQueueError("--max-attempts must be positive")
            result = run_queue(
                paths,
                max_tasks=args.max_tasks,
                max_attempts=args.max_attempts,
            )
        elif args.command == "status":
            result = queue_status(paths)
        elif args.command == "validate-task":
            validate_official_installation(paths)
            validate_task_package(args.task_dir, paths)
            result = {"status": "PASS", "task_dir": str(args.task_dir.resolve())}
        elif args.command == "collect":
            validate_official_installation(paths)
            result = collect_task_results(
                args.source,
                args.destination,
                paths,
                force=args.force,
            )
        else:
            raise MuseCocoQueueError("unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 3 if result.get("status") == "PARTIAL_FAILURE" else 0
    except (MuseCocoQueueError, OSError, subprocess.SubprocessError) as exc:
        print("MuseCoco task queue failed: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
