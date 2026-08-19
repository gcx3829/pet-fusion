from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel

from app.graphs.reducers import CriticEvaluationBucket, merge_evaluations_by_candidate


class SearchState(TypedDict, total=False):
    schema_version: str
    search_id: str
    thread_id: str
    project_id: str
    status: Literal[
        "queued",
        "running",
        "waiting_for_human",
        "accepted",
        "failed",
        "cancelled",
    ]
    source_manifest: dict[str, object]
    source_manifest_hash: str
    # Project-bound Guidance Mask reference only; never raster bytes.
    guidance_mask_asset: dict[str, object] | None
    placement: dict[str, object]
    user_intent: str
    canonical_prompt: str
    canonical_prompt_hash: str
    # Prompt/visual-anchor lineage is kept as structured references.  These
    # fields are optional so checkpoints written before the prompt-contract
    # migration remain readable.
    prompt_version_id: str | None
    prompt_version_hash: str | None
    based_on_prompt_version_id: str | None
    prompt_schema_version: str | None
    prompt_template_version: str | None
    prompt_model: str | None
    refinement_mode: Literal["initial", "revision"] | None
    generation_mode: Literal["source_rebase", "candidate_anchored_rebase"] | None
    visual_anchor: dict[str, object] | None
    visual_anchor_candidate_id: str | None
    visual_anchor_raw_asset_sha256: str | None
    visual_anchor_asset: dict[str, object] | None
    professional_prompt_plan: dict[str, object] | None
    prompt_summary: str | None
    provider_proposal_hash: str | None
    # The nested Prompt Refiner subgraph communicates through checkpoint-safe
    # JSON values.  ``prompt_refiner_execution_mode`` is a local routing hint
    # and is never exposed as a provider prompt; the request/result contain
    # asset references and structured plans, never image bytes.
    prompt_refiner_execution_mode: Literal["initial", "revision", "local"] | None
    prompt_refiner_request: dict[str, object] | None
    prompt_refiner_result: dict[str, object] | None
    current_prompt_version: dict[str, object] | None
    canonical_template_version: str
    generation_prompt: str
    generation_prompt_hash: str
    prompt_history: list[dict[str, object]]
    human_feedback: str | None
    human_selected_candidate_id: str | None
    active_directives: list[dict[str, object]]
    active_directives_hash: str
    directive_policy_version: str
    directive_version: int
    attempted_directive_categories: list[str]
    planner_result: dict[str, object] | None
    planner_input: dict[str, object] | None
    selected_evaluation: dict[str, object] | None
    selected_blocking_issues: list[dict[str, object]]
    planner_proposal: dict[str, object] | None
    validated_planner_result: dict[str, object] | None
    planner_round_index: int | None
    planner_fallback_attempts: int
    critic_proxy_inputs: dict[str, dict[str, object]]
    evaluations_by_candidate: Annotated[
        CriticEvaluationBucket, merge_evaluations_by_candidate
    ]
    evaluations: list[dict[str, object]]
    round_history: list[dict[str, object]]
    round_index: int
    max_rounds: int
    review_each_round: bool
    candidate_count: int
    current_candidates: list[dict[str, object]]
    round_winner_id: str | None
    global_winner_id: str | None
    global_winner_score: float | None
    stop_action: str | None
    stop_reason: str | None
    interrupt_payload: dict[str, object] | None
    error: dict[str, object] | None


def assert_checkpoint_safe(value: object, *, path: str = "state") -> None:
    """Reject binary image payloads before they can enter a checkpoint."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"Binary checkpoint value is forbidden at {path}")
    if isinstance(value, str) and value.strip().lower().startswith("data:image/"):
        raise TypeError(f"Image data URL is forbidden at {path}")
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            assert_checkpoint_safe(
                getattr(value, field_name), path=f"{path}.{field_name}"
            )
    elif isinstance(value, Mapping):
        for key, child in value.items():
            assert_checkpoint_safe(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_checkpoint_safe(child, path=f"{path}[{index}]")
