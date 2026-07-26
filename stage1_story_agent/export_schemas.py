"""Regenerate checked-in JSON Schema snapshots from Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    ContentPlan,
    FailureReport,
    FormScaleDecision,
    HeartbeatProcessingDelivery,
    LLMContentPlanDraft,
    MuseCocoDelivery,
    Stage2Delivery,
    StoryPlanRequest,
)


SCHEMAS = {
    "story_plan_request.schema.json": StoryPlanRequest,
    "llm_content_plan_draft.schema.json": LLMContentPlanDraft,
    "content_plan.schema.json": ContentPlan,
    "musecoco_plan.schema.json": MuseCocoDelivery,
    "heartbeat_processing_plan.schema.json": HeartbeatProcessingDelivery,
    "stage2_plan.schema.json": Stage2Delivery,
    "failure_report.schema.json": FailureReport,
    "form_scale_decision.schema.json": FormScaleDecision,
}


def main() -> None:
    directory = Path(__file__).with_name("schemas")
    directory.mkdir(exist_ok=True)
    for filename, model in SCHEMAS.items():
        text = json.dumps(model.model_json_schema(by_alias=True), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (directory / filename).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
