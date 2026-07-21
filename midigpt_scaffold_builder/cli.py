"""Command-line interface for the offline scaffold compiler."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .builder import build_scaffold_directory
from .errors import ScaffoldValidationError
from .models import ContentPlan, HeartbeatManifest, MotifManifest, MusicPlan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compile content/music plans plus heartbeat and motif manifests "
            "into an auditable MIDI-GPT Score and GenerationRequest."
        )
    )
    parser.add_argument("--content-plan", type=Path)
    parser.add_argument("--music-plan", type=Path)
    parser.add_argument("--heartbeat-manifest", type=Path)
    parser.add_argument("--motif-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--schema-dir",
        type=Path,
        help="Write music-plan and motif-manifest JSON Schemas, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.schema_dir is not None:
        args.schema_dir.mkdir(parents=True, exist_ok=True)
        schemas = {
            "content_plan.consumed.schema.json": ContentPlan.model_json_schema(),
            "music_plan.schema.json": MusicPlan.model_json_schema(),
            "heartbeat_manifest.schema.json": HeartbeatManifest.model_json_schema(),
            "motif_manifest.schema.json": MotifManifest.model_json_schema(),
        }
        for name, schema in schemas.items():
            (args.schema_dir / name).write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(args.schema_dir.resolve())
        return 0
    required = {
        "--content-plan": args.content_plan,
        "--music-plan": args.music_plan,
        "--heartbeat-manifest": args.heartbeat_manifest,
        "--motif-manifest": args.motif_manifest,
        "--output-dir": args.output_dir,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        build_parser().error(f"required arguments: {' '.join(missing)}")
    try:
        result = build_scaffold_directory(
            args.content_plan,
            args.music_plan,
            args.heartbeat_manifest,
            args.motif_manifest,
            args.output_dir,
        )
    except ScaffoldValidationError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "path": exc.path, "error": exc.message},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output_directory": str(result.output_directory),
                "tracks": len(result.score["tracks"]),
                "bars": len(result.score["tracks"][0]["bars"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
