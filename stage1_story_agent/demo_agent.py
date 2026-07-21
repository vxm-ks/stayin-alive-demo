"""Run the complete Stage 1 pipeline offline with the checked-in draft."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import Stage1StoryAgent
from .artifacts import write_plan_run
from .backends import FakeBackend
from .models import StoryPlanRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("stage1_story_agent/outputs/offline-demo"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    examples = Path(__file__).with_name("examples")
    request = StoryPlanRequest.model_validate_json((examples / "story_input.example.json").read_text(encoding="utf-8"))
    draft_text = (examples / "llm_draft.example.json").read_text(encoding="utf-8")
    run = Stage1StoryAgent(FakeBackend([draft_text])).plan(request)
    destinations = write_plan_run(run, args.output_dir, force=args.force)
    print(json.dumps({
        "output_dirs": {name: str(path) for name, path in destinations.items()},
        "form": run.content_plan.form_plan.form_string,
        "sections": len(run.content_plan.form_plan.sections),
        "theme_families": len(run.content_plan.theme_families),
        "variation_tasks": len(run.content_plan.variation_tasks),
        "musecoco_requests": len(run.content_plan.musecoco_requests),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
