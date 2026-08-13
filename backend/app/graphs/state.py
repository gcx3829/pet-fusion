from __future__ import annotations

from typing import Literal, TypedDict


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
    active_directives: list[dict[str, object]]
    round_index: int
    max_rounds: int
    candidate_count: int
    current_candidates: list[dict[str, object]]
    global_winner_id: str | None
    stop_reason: str | None
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
