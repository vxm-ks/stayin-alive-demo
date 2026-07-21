"""Stage 1 orchestration: model response to validated executable content plan."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from .backends import BackendResponse, LLMBackend
from .config import Stage1Config
from .errors import ModelContentError, PlanValidationError
from .emotion import derive_theme_emotions, remove_deprecated_llm_em1
from .handoff import compile_stage2_handoff, make_deliveries
from .models import (
    BackendRawResponse,
    ContentPlan,
    FailureReport,
    GlobalPlan,
    LLMContentPlanDraft,
    PlanRun,
    Provenance,
    RunManifest,
    StoryPlanRequest,
    ValidationIssue,
)
from .musecoco import enrich_theme_family
from .musecoco_length_policy import apply_musecoco_output_bars
from .prompts import PROMPT_VERSION, build_initial_request, build_repair_request
from .test_mode import apply_test_mode_melodic_profile
from .utils import canonical_json_bytes, json_bytes, sha256_hex
from .validators import validate_and_compile_draft, validate_content_plan


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _pydantic_issues(exc: ValidationError) -> list[dict[str, str]]:
    issues = []
    for error in exc.errors(include_url=False, include_context=False)[:20]:
        path = ".".join(str(part) for part in error["loc"])
        issues.append({"code": "DRAFT_SCHEMA_INVALID", "path": path, "message": error["msg"]})
    return issues


def _response_issue(response: BackendResponse) -> list[dict[str, str]] | None:
    if not response.content.strip():
        return [{"code": "MODEL_RESPONSE_EMPTY", "path": "response.content", "message": "model response content is empty"}]
    if response.finish_reason == "length":
        return [{"code": "MODEL_RESPONSE_TRUNCATED", "path": "response.finish_reason", "message": "model response was truncated at the output token limit"}]
    return None


class Stage1StoryAgent:
    def __init__(self, backend: LLMBackend, config: Stage1Config | None = None) -> None:
        self.backend = backend
        self.config = config or Stage1Config()

    def plan(self, request: StoryPlanRequest | dict) -> PlanRun:
        if not isinstance(request, StoryPlanRequest):
            request = StoryPlanRequest.model_validate(request)
        story_hash = sha256_hex(request.story_text.encode("utf-8"))
        request_hash = sha256_hex(canonical_json_bytes(request))
        run_id = str(uuid4())
        previous_content = ""
        issues: list[dict[str, str]] = []
        total_network_attempts = 0

        for content_attempt in range(1, self.config.max_content_attempts + 1):
            completion = (
                build_initial_request(request)
                if content_attempt == 1
                else build_repair_request(request, previous_content, issues)
            )
            response = self.backend.complete(completion)
            total_network_attempts += response.network_attempts
            previous_content = response.content
            issues = _response_issue(response) or []
            draft: LLMContentPlanDraft | None = None
            compilation = None
            if not issues:
                try:
                    raw = json.loads(response.content)
                except json.JSONDecodeError as exc:
                    issues = [{"code": "MODEL_RESPONSE_NOT_JSON", "path": "response.content", "message": f"invalid JSON at line {exc.lineno}, column {exc.colno}"}]
                else:
                    raw = apply_musecoco_output_bars(
                        raw, request.constraints.musecoco_output_bars
                    )
                    if request.test_mode:
                        raw = apply_test_mode_melodic_profile(
                            raw, request.constraints.musecoco_output_bars
                        )
                    raw = remove_deprecated_llm_em1(raw)
                    try:
                        draft = LLMContentPlanDraft.model_validate(raw)
                    except ValidationError as exc:
                        issues = _pydantic_issues(exc)
            if draft is not None and not issues:
                try:
                    compilation = validate_and_compile_draft(request, draft)
                except PlanValidationError as exc:
                    issues = exc.issues
            if draft is not None and compilation is not None and not issues:
                generated_at = utc_now()
                introduce_by_symbol = {
                    section.form_label: section.section_id
                    for section in compilation.form_plan.sections
                    if section.relation.value == "introduce"
                }
                emotion_by_symbol = derive_theme_emotions(draft)
                families = [
                    enrich_theme_family(
                        family,
                        draft.global_proposal,
                        introduce_by_symbol[family.base_symbol],
                        emotion_by_symbol[family.base_symbol].quadrant,
                        generation_bars=request.constraints.musecoco_generation_bars,
                    )
                    for family in draft.theme_families
                ]
                stage2_handoff = compile_stage2_handoff(
                    draft,
                    compilation.form_plan,
                    test_mode=request.test_mode,
                )
                provenance = Provenance(
                    provider=response.provider,
                    model=response.model,
                    prompt_version=PROMPT_VERSION,
                    request_id=response.request_id,
                    run_id=run_id,
                    story_sha256=story_hash,
                    request_sha256=request_hash,
                    generated_at=generated_at,
                    content_attempts=content_attempt,
                    network_attempts=total_network_attempts,
                )
                content_plan = ContentPlan(
                    schema_version=request.schema_version,
                    story_id=request.story_id,
                    test_mode=request.test_mode,
                    global_=GlobalPlan(
                        total_bars=request.constraints.total_bars,
                        tempo_bpm=draft.global_proposal.tempo_bpm,
                        time_signature=draft.global_proposal.time_signature,
                        global_tonality=draft.global_proposal.global_tonality,
                    ),
                    story_analysis=draft.story_analysis,
                    form_plan=compilation.form_plan,
                    stage2_handoff=stage2_handoff,
                    theme_families=families,
                    variation_tasks=compilation.variation_tasks,
                    musecoco_requests=[],
                    provenance=provenance,
                )
                validate_content_plan(request, content_plan)
                raw_response = BackendRawResponse(
                    provider=response.provider,
                    model=response.model,
                    request_id=response.request_id,
                    finish_reason=response.finish_reason,
                    content=response.content,
                    usage=response.usage,
                )
                musecoco_delivery, heartbeat_delivery, stage2_delivery = make_deliveries(
                    story_id=request.story_id,
                    draft=draft,
                    form_plan=compilation.form_plan,
                    families=families,
                    handoff=stage2_handoff,
                    generation_target_bars=request.constraints.musecoco_generation_bars,
                    output_motif_bars=request.constraints.musecoco_output_bars,
                )
                files = {
                    "content_plan.json": sha256_hex(json_bytes(content_plan)),
                    "musecoco_plan.json": sha256_hex(json_bytes(musecoco_delivery)),
                    "heartbeat_processing_plan.json": sha256_hex(json_bytes(heartbeat_delivery)),
                    "stage2_plan.json": sha256_hex(json_bytes(stage2_delivery)),
                    "raw_response.json": sha256_hex(json_bytes(raw_response)),
                }
                return PlanRun(
                    content_plan=content_plan,
                    musecoco_delivery=musecoco_delivery,
                    heartbeat_delivery=heartbeat_delivery,
                    stage2_delivery=stage2_delivery,
                    raw_response=raw_response,
                    manifest=RunManifest(
                        story_id=request.story_id,
                        run_id=run_id,
                        generated_at=generated_at,
                        story_sha256=story_hash,
                        request_sha256=request_hash,
                        files=files,
                    ),
                )

        raise ModelContentError(
            "MODEL_CONTENT_ATTEMPTS_EXHAUSTED",
            f"model content remained invalid after {self.config.max_content_attempts} attempts",
            issues=issues,
            content_attempts=self.config.max_content_attempts,
            network_attempts=total_network_attempts,
        )


def failure_report_for(
    error: ModelContentError,
    *,
    story_id: str | None,
) -> FailureReport:
    return FailureReport(
        story_id=story_id,
        category="content",
        error_code=error.code,
        message=error.public_message,
        generated_at=utc_now(),
        content_attempts=error.content_attempts,
        network_attempts=error.network_attempts,
        issues=[ValidationIssue.model_validate(item) for item in error.issues],
    )
