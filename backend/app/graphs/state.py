from __future__ import annotations

from typing import Annotated, Literal, TypedDict

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
    placement: dict[str, object]
    user_intent: str
    canonical_prompt: str
    canonical_prompt_hash: str
    canonical_template_version: str
    generation_prompt: str
    generation_prompt_hash: str
    prompt_history: list[dict[str, object]]
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
    if isinstance(value, str) and value.startswith("data:image/"):
        raise TypeError(f"Image data URL is forbidden at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            assert_checkpoint_safe(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            assert_checkpoint_safe(child, path=f"{path}[{index}]")
