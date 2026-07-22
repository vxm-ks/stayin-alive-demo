"""Command-line entry point for the Stage 1 story agent."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .agent import Stage1StoryAgent, failure_report_for, utc_now
from .artifacts import write_failure_report, write_plan_run
from .backends import DeepSeekBackend
from .config import Stage1Config
from .errors import ArtifactWriteError, ModelContentError, Stage1AgentError
from .key_normalizer import (
    KeyNormalizationError,
    default_output_path as default_key_output_path,
    normalize_midi_key,
)
from .models import FailureReport, StoryPlanRequest, ValidationIssue
from .musecoco_queue_bridge import (
    MuseCocoQueueBridgeError,
    enqueue_musecoco_tasks,
)
from .musecoco_postprocess import (
    MuseCocoPostprocessError,
    finalize_musecoco_results,
)
from .tempo_normalizer import (
    TempoNormalizationError,
    default_output_path,
    normalize_midi_tempo,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stage1_story_agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser(
        "plan",
        help="compile a story into separated MuseCoco, heartbeat, Stage 2, and audit plans",
    )
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="StoryPlanRequest JSON file")
    source.add_argument("--story-text", help="natural-language story text; other request fields use defaults")
    plan.add_argument("--story-id", default="cli-story", help="story ID for --story-text (default: cli-story)")
    plan.add_argument("--language", default="zh-CN", help="language tag for --story-text (default: zh-CN)")
    plan.add_argument("--test-mode", action="store_true", help="force deterministic C-minor/96-BPM/4-4 A-B-A test profile")
    plan.add_argument("--output-dir", type=Path, help="base prefix for four sibling delivery directories (default: stage1_story_agent/outputs/<timestamp>)")
    plan.add_argument("--force", action="store_true", help="replace existing delivery directories with backup/restore protection")
    plan.add_argument(
        "--enqueue-musecoco",
        action="store_true",
        help="publish generated one-sample task packages to the WSL MuseCoco queue",
    )
    plan.add_argument(
        "--run-musecoco-queue",
        action="store_true",
        help="run the WSL queue after enqueueing (requires --enqueue-musecoco)",
    )
    plan.add_argument(
        "--musecoco-max-attempts",
        type=int,
        default=3,
        help="maximum attempts for each MuseCoco task (default: 3)",
    )
    plan.add_argument("--wsl-distro", default="Ubuntu")
    plan.add_argument(
        "--tonality-policy", choices=["loose", "strict"], default="strict",
        help="loose preserves generated tonality; strict forces planned tonic and mode",
    )
    plan.add_argument(
        "--musecoco-output-bars",
        type=int,
        choices=[8],
        help="delivered MuseCoco motif length (fixed: 8 bars)",
    )
    plan.add_argument(
        "--musecoco-generation-bars",
        type=int,
        help="length encouraged in MuseCoco attributes (default from request: 12 bars)",
    )

    enqueue = subparsers.add_parser(
        "enqueue-musecoco",
        help="enqueue task packages from an existing *-musecoco output directory",
    )
    enqueue.add_argument("--musecoco-dir", type=Path, required=True)
    enqueue.add_argument("--run", action="store_true", help="run the queue after enqueueing")
    enqueue.add_argument("--max-tasks", type=int)
    enqueue.add_argument("--max-attempts", type=int, default=3)
    enqueue.add_argument("--wsl-distro", default="Ubuntu")
    enqueue.add_argument(
        "--tonality-policy", choices=["loose", "strict"], default="strict",
        help="loose preserves generated tonality; strict forces planned tonic and mode",
    )
    enqueue.add_argument(
        "--force-results",
        action="store_true",
        help="replace existing raw_results and generated_themes after a successful queue run",
    )

    normalize = subparsers.add_parser(
        "normalize-tempo",
        help="set a MuseCoco MIDI file to the planned constant BPM",
    )
    normalize.add_argument("--input-midi", type=Path, required=True, help="MuseCoco Standard MIDI File")
    tempo_source = normalize.add_mutually_exclusive_group(required=True)
    tempo_source.add_argument(
        "--content-plan",
        type=Path,
        help="read target BPM from content_plan.json global.tempo_bpm",
    )
    tempo_source.add_argument("--target-bpm", type=float, help="explicit target BPM (30-240)")
    normalize.add_argument(
        "--output-midi",
        type=Path,
        help="output MIDI (default: <input>.tempo-normalized.mid)",
    )
    normalize.add_argument("--force", action="store_true", help="replace an existing output MIDI")

    normalize_key = subparsers.add_parser(
        "normalize-key",
        help="detect and force a MuseCoco MIDI file to the planned global key",
    )
    normalize_key.add_argument("--input-midi", type=Path, required=True, help="MuseCoco Standard MIDI File")
    normalize_key.add_argument(
        "--content-plan",
        type=Path,
        help="read target tonic and mode from content_plan.json",
    )
    normalize_key.add_argument(
        "--target-tonic",
        choices=["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"],
        help="explicit target tonic; requires --target-mode when no content plan is used",
    )
    normalize_key.add_argument(
        "--target-mode",
        choices=["major", "minor"],
        help="explicit target mode; requires --target-tonic when no content plan is used",
    )
    normalize_key.add_argument(
        "--source-tonic",
        choices=["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"],
        help="audited override for ambiguous/missing source key metadata",
    )
    normalize_key.add_argument(
        "--source-mode",
        choices=["major", "minor"],
        help="source mode override; must be supplied with --source-tonic",
    )
    normalize_key.add_argument(
        "--output-midi",
        type=Path,
        help="output MIDI (default: <input>.key-normalized.mid)",
    )
    normalize_key.add_argument("--force", action="store_true", help="replace an existing output MIDI")

    return parser


def build_request_from_text(
    story_text: str,
    *,
    story_id: str = "cli-story",
    language: str = "zh-CN",
    test_mode: bool = False,
    musecoco_output_bars: int | None = None,
    musecoco_generation_bars: int | None = None,
) -> StoryPlanRequest:
    """Convert command-line natural text into a strict request with model defaults."""

    payload: dict[str, object] = {
        "story_id": story_id,
        "story_text": story_text,
        "language": language,
        "test_mode": test_mode,
    }
    constraints: dict[str, int] = {}
    if musecoco_output_bars is not None:
        constraints["musecoco_output_bars"] = musecoco_output_bars
    if musecoco_generation_bars is not None:
        constraints["musecoco_generation_bars"] = musecoco_generation_bars
    if constraints:
        payload["constraints"] = constraints
    return StoryPlanRequest.model_validate(payload)


def _override_musecoco_bars(
    request: StoryPlanRequest,
    *,
    output_bars: int | None,
    generation_bars: int | None,
) -> StoryPlanRequest:
    if output_bars is None and generation_bars is None:
        return request
    payload = request.model_dump(mode="json", by_alias=True)
    constraints = dict(payload["constraints"])
    if output_bars is not None:
        constraints["musecoco_output_bars"] = output_bars
    if generation_bars is not None:
        constraints["musecoco_generation_bars"] = generation_bars
    payload["constraints"] = constraints
    return StoryPlanRequest.model_validate(payload)


def timestamped_output_dir(now: datetime | None = None) -> Path:
    """Return a collision-resistant, Windows-safe delivery directory prefix."""

    current = now or datetime.now().astimezone()
    folder = current.strftime("%Y%m%d-%H%M%S-%f")
    return Path("stage1_story_agent") / "outputs" / folder


def _validation_issues(exc: ValidationError) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            code="INPUT_SCHEMA_INVALID",
            path=".".join(str(part) for part in error["loc"]),
            message=error["msg"],
        )
        for error in exc.errors(include_url=False, include_context=False)[:20]
    ]


def _try_write_failure(report: FailureReport, output_dir: Path, force: bool) -> None:
    try:
        write_failure_report(report, output_dir, force=force)
    except ArtifactWriteError:
        pass


def _run_tempo_normalizer(args: argparse.Namespace) -> int:
    try:
        if args.content_plan is not None:
            try:
                plan = json.loads(args.content_plan.read_text(encoding="utf-8"))
                target_bpm = float(plan["global"]["tempo_bpm"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise TempoNormalizationError(
                    f"could not read global.tempo_bpm from content_plan.json: {exc}"
                ) from exc
        else:
            target_bpm = args.target_bpm

        output_midi = args.output_midi or default_output_path(args.input_midi)
        result = normalize_midi_tempo(
            args.input_midi,
            output_midi,
            target_bpm,
            force=args.force,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except TempoNormalizationError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _run_key_normalizer(args: argparse.Namespace) -> int:
    try:
        if args.content_plan is not None:
            if args.target_tonic is not None or args.target_mode is not None:
                raise KeyNormalizationError(
                    "--content-plan cannot be combined with --target-tonic or --target-mode"
                )
            try:
                plan = json.loads(args.content_plan.read_text(encoding="utf-8"))
                tonality = plan["global"]["global_tonality"]
                target_tonic = str(tonality["tonic"])
                target_mode = str(tonality["mode"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise KeyNormalizationError(
                    f"could not read global.global_tonality from content_plan.json: {exc}"
                ) from exc
        else:
            if args.target_tonic is None or args.target_mode is None:
                raise KeyNormalizationError(
                    "use --content-plan or provide both --target-tonic and --target-mode"
                )
            target_tonic = args.target_tonic
            target_mode = args.target_mode

        output_midi = args.output_midi or default_key_output_path(args.input_midi)
        result = normalize_midi_key(
            args.input_midi,
            output_midi,
            target_tonic,
            target_mode,
            source_tonic=args.source_tonic,
            source_mode=args.source_mode,
            force=args.force,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0
    except KeyNormalizationError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _run_musecoco_enqueue(args: argparse.Namespace) -> int:
    try:
        result = enqueue_musecoco_tasks(
            args.musecoco_dir,
            wsl_distro=args.wsl_distro,
            run_queue=args.run,
            max_tasks=args.max_tasks,
            max_attempts=args.max_attempts,
            force_collect=args.force_results,
        )
        payload = result.as_dict()
        if args.run:
            postprocess = finalize_musecoco_results(
                args.musecoco_dir,
                force=args.force_results,
                tonality_policy=args.tonality_policy,
            )
            payload["postprocess"] = postprocess.as_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (MuseCocoQueueBridgeError, MuseCocoPostprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 6


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "normalize-tempo":
        return _run_tempo_normalizer(args)
    if args.command == "normalize-key":
        return _run_key_normalizer(args)
    if args.command == "enqueue-musecoco":
        return _run_musecoco_enqueue(args)
    if args.command != "plan":
        return 2
    story_id: str | None = None
    output_dir = args.output_dir or timestamped_output_dir()
    try:
        if args.run_musecoco_queue and not args.enqueue_musecoco:
            raise MuseCocoQueueBridgeError(
                "--run-musecoco-queue requires --enqueue-musecoco"
            )
        if args.story_text is not None:
            story_id = args.story_id
            try:
                request = build_request_from_text(
                    args.story_text,
                    story_id=args.story_id,
                    language=args.language,
                    test_mode=args.test_mode,
                    musecoco_output_bars=args.musecoco_output_bars,
                    musecoco_generation_bars=args.musecoco_generation_bars,
                )
            except ValidationError as exc:
                report = FailureReport(
                    story_id=story_id,
                    category="input",
                    error_code="INPUT_SCHEMA_INVALID",
                    message="natural story text could not be converted to StoryPlanRequest",
                    generated_at=utc_now(),
                    content_attempts=0,
                    network_attempts=0,
                    issues=_validation_issues(exc),
                )
                _try_write_failure(report, output_dir, args.force)
                print(report.message, file=sys.stderr)
                return 2
        else:
            try:
                raw_request = json.loads(args.input.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report = FailureReport(
                    story_id=None,
                    category="input",
                    error_code="INPUT_FILE_INVALID",
                    message="input file could not be read as UTF-8 JSON",
                    generated_at=utc_now(),
                    content_attempts=0,
                    network_attempts=0,
                )
                _try_write_failure(report, output_dir, args.force)
                print(report.message, file=sys.stderr)
                return 2
            story_id = raw_request.get("story_id") if isinstance(raw_request, dict) else None
            try:
                request = StoryPlanRequest.model_validate(raw_request)
                request = _override_musecoco_bars(
                    request,
                    output_bars=args.musecoco_output_bars,
                    generation_bars=args.musecoco_generation_bars,
                )
            except ValidationError as exc:
                report = FailureReport(
                    story_id=story_id,
                    category="input",
                    error_code="INPUT_SCHEMA_INVALID",
                    message="input JSON does not match StoryPlanRequest",
                    generated_at=utc_now(),
                    content_attempts=0,
                    network_attempts=0,
                    issues=_validation_issues(exc),
                )
                _try_write_failure(report, output_dir, args.force)
                print(report.message, file=sys.stderr)
                return 2
            if args.test_mode and not request.test_mode:
                normalized = request.model_dump(mode="json", by_alias=True)
                normalized["test_mode"] = True
                request = StoryPlanRequest.model_validate(normalized)

        config = Stage1Config.from_env(require_api_key=True)
        backend = DeepSeekBackend(config)
        try:
            run = Stage1StoryAgent(backend, config).plan(request)
        finally:
            backend.close()
        destinations = write_plan_run(run, output_dir, force=args.force)
        result: dict[str, object] = {
            name: str(path) for name, path in destinations.items()
        }
        if args.enqueue_musecoco:
            queue_result = enqueue_musecoco_tasks(
                destinations["musecoco"],
                wsl_distro=args.wsl_distro,
                run_queue=args.run_musecoco_queue,
                max_attempts=args.musecoco_max_attempts,
                force_collect=args.force,
            )
            result["musecoco_queue"] = queue_result.as_dict()
            if args.run_musecoco_queue:
                postprocess = finalize_musecoco_results(
                    destinations["musecoco"],
                    force=args.force,
                    tonality_policy=args.tonality_policy,
                )
                result["musecoco_postprocess"] = postprocess.as_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MuseCocoQueueBridgeError, MuseCocoPostprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 6
    except ModelContentError as exc:
        report = failure_report_for(exc, story_id=story_id)
        _try_write_failure(report, output_dir, args.force)
        print(exc.public_message, file=sys.stderr)
        return exc.exit_code
    except Stage1AgentError as exc:
        report = FailureReport(
            story_id=story_id,
            category=exc.category,
            error_code=exc.code,
            message=exc.public_message,
            generated_at=utc_now(),
            content_attempts=0,
            network_attempts=0,
        )
        _try_write_failure(report, output_dir, args.force)
        print(exc.public_message, file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
