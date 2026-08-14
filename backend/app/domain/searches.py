from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.evaluations import CandidateEvaluation


class SearchStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_HUMAN = "waiting_for_human"
    ACCEPTED = "accepted"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_stream_terminal(self) -> bool:
        return self in {
            SearchStatus.WAITING_FOR_HUMAN,
            SearchStatus.ACCEPTED,
            SearchStatus.FAILED,
            SearchStatus.CANCELLED,
        }


class PlacementIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    coordinate_space: Literal["normalized"] = "normalized"
    pose: str = Field(min_length=1, max_length=100)
    facing: str = Field(min_length=1, max_length=100)
    contact_surface: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_bounds(self) -> PlacementIntent:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("placement rectangle must remain within normalized bounds")
        return self


class CreateSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement: PlacementIntent
    user_intent: str = Field(min_length=1, max_length=2000)
    candidate_count: int = Field(default=3, ge=1, le=4)
    max_rounds: int = Field(default=1, ge=1, le=3)
    budget_usd: float | None = Field(default=None, gt=0)
    review_each_round: bool = False


class PromptHistoryEntry(BaseModel):
    """The exact bounded prompts used by one generation round."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    round_index: int = Field(ge=0)
    canonical_prompt: str = Field(min_length=1, max_length=12_000)
    canonical_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_prompt: str = Field(min_length=1, max_length=16_000)
    generation_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_template_version: str = Field(min_length=1, max_length=120)
    active_directives: list[dict[str, object]] = Field(default_factory=list)
    active_directives_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_feedback: str | None = Field(default=None, max_length=2000)
    human_selected_candidate_id: str | None = Field(default=None, max_length=120)
    tuned: bool = False


class SearchRunRecord(BaseModel):
    search_id: str
    thread_id: str
    project_id: str
    status: SearchStatus
    source_manifest_hash: str
    placement: PlacementIntent
    user_intent: str
    candidate_count: int
    max_rounds: int
    budget_usd: float | None
    review_each_round: bool
    round_index: int = 0
    round_winner_id: str | None = None
    candidates: list[CandidateRecord] = Field(default_factory=list)
    global_winner_id: str | None = None
    global_winner_score: float | None = None
    round_history: list[dict[str, object]] = Field(default_factory=list)
    prompt_history: list[PromptHistoryEntry] = Field(default_factory=list)
    active_directives: list[dict[str, object]] = Field(default_factory=list)
    interrupt_payload: dict[str, object] | None = None
    stop_reason: str | None = None
    error: dict[str, object] | None = None
    created_at: datetime
    updated_at: datetime


class CreateSearchResponse(BaseModel):
    search_id: str
    thread_id: str
    project_id: str
    status: SearchStatus
    events_url: str
    created_at: datetime

    @classmethod
    def from_record(cls, search: SearchRunRecord) -> CreateSearchResponse:
        return cls(
            search_id=search.search_id,
            thread_id=search.thread_id,
            project_id=search.project_id,
            status=search.status,
            events_url=f"/api/v1/searches/{search.search_id}/events",
            created_at=search.created_at,
        )


class SearchResponse(BaseModel):
    search_id: str
    thread_id: str
    project_id: str
    status: SearchStatus
    round_index: int
    round_winner_id: str | None
    candidate_count: int
    candidates: list[CandidateResponse]
    global_winner_id: str | None
    global_winner_score: float | None
    round_history: list[dict[str, object]]
    prompt_history: list[PromptHistoryEntry]
    active_directives: list[dict[str, object]]
    interrupt_payload: dict[str, object] | None
    stop_reason: str | None
    error: dict[str, object] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(
        cls,
        search: SearchRunRecord,
        *,
        evaluations: Sequence[tuple[CandidateEvaluation, float | None]] = (),
    ) -> SearchResponse:
        evaluation_by_candidate = {
            evaluation.candidate_id: (evaluation, score)
            for evaluation, score in evaluations
        }
        response_candidates = [
            CandidateResponse.from_record(
                item,
                evaluation=(
                    evaluation_by_candidate[item.candidate_id][0]
                    if item.candidate_id in evaluation_by_candidate
                    else None
                ),
                score=(
                    evaluation_by_candidate[item.candidate_id][1]
                    if item.candidate_id in evaluation_by_candidate
                    else None
                ),
            )
            for item in search.candidates
        ]
        interrupt_payload = (
            dict(search.interrupt_payload) if search.interrupt_payload is not None else None
        )
        if search.status is SearchStatus.WAITING_FOR_HUMAN and response_candidates:
            interrupt_payload = interrupt_payload or {"type": "search_review"}
            raw_allowed_actions = interrupt_payload.get("allowed_actions", [])
            allowed_actions = (
                [str(item) for item in raw_allowed_actions]
                if isinstance(raw_allowed_actions, list)
                else []
            )
            if "accept_candidate" not in allowed_actions:
                allowed_actions.insert(0, "accept_candidate")
            if search.global_winner_id and "accept_global_winner" not in allowed_actions:
                allowed_actions.insert(0, "accept_global_winner")
            interrupt_payload["allowed_actions"] = allowed_actions
        return cls(
            search_id=search.search_id,
            thread_id=search.thread_id,
            project_id=search.project_id,
            status=search.status,
            round_index=search.round_index,
            round_winner_id=search.round_winner_id,
            candidate_count=search.candidate_count,
            candidates=response_candidates,
            global_winner_id=search.global_winner_id,
            global_winner_score=search.global_winner_score,
            round_history=search.round_history,
            prompt_history=search.prompt_history,
            active_directives=search.active_directives,
            interrupt_payload=interrupt_payload,
            stop_reason=search.stop_reason,
            error=search.error,
            created_at=search.created_at,
            updated_at=search.updated_at,
        )


class SearchEvent(BaseModel):
    id: int
    type: str
    search_id: str
    created_at: datetime
    payload: dict[str, object]

    def to_sse(self) -> str:
        return f"id: {self.id}\nevent: {self.type}\ndata: {self.model_dump_json()}\n\n"


class ResumeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "accept_global_winner",
        "accept_candidate",
        "continue_one_round",
        "update_user_intent",
        "cancel",
    ]
    updated_user_intent: str | None = Field(default=None, min_length=1, max_length=2000)
    human_feedback: str | None = Field(default=None, min_length=1, max_length=2000)
    selected_candidate_id: str | None = Field(default=None, min_length=1, max_length=120)
    reviewed_round_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_reviewed_round_for_continue(self) -> ResumeSearchRequest:
        if self.action == "continue_one_round" and self.reviewed_round_index is None:
            raise ValueError("reviewed_round_index is required for continue_one_round")
        return self
