"""Checkpoint-safe contracts for narrow, versioned search directives."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlannerAction(StrEnum):
    """The only actions a feedback planner may request."""

    CONTINUE = "continue"
    STOP = "stop"
    HUMAN_REVIEW = "human_review"


class DirectiveCategory(StrEnum):
    """Small fixed taxonomy accepted by the generation prompt compiler."""

    IDENTITY = "identity"
    POSE_GEOMETRY = "pose_geometry"
    PERSPECTIVE_SCALE = "perspective_scale"
    LIGHTING_COLOR = "lighting_color"
    OPTICAL_CONSISTENCY = "optical_consistency"
    PHYSICAL_INTEGRATION = "physical_integration"
    SCENE_PRESERVATION = "scene_preservation"
    ASSET_INTEGRITY = "asset_integrity"


class DirectivePolicy(BaseModel):
    """Deterministic guardrails shared by fake and future live planner providers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = Field(default="directive-policy/v1", min_length=1, max_length=80)
    max_directives: int = Field(default=3, ge=1, le=3)
    max_instruction_chars: int = Field(default=240, ge=40, le=240)
    max_expected_effect_chars: int = Field(default=200, ge=40, le=200)


class ActionableBlockingIssue(BaseModel):
    """A deliberately short projection of a selected candidate's blocking issue.

    Critic evidence and other free-form rationale are deliberately excluded so this
    model is safe to persist in a LangGraph checkpoint and to pass to a planner.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str = Field(min_length=1, max_length=120)
    category: DirectiveCategory
    region: str | None = Field(default=None, max_length=120)
    suggested_fix: str | None = Field(default=None, max_length=240)
    confidence: float = Field(ge=0.75, le=1)


class PlannerDirective(BaseModel):
    """One bounded correction that may be appended to the next generation prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directive_id: str = Field(min_length=8, max_length=120)
    category: DirectiveCategory
    instruction: str = Field(min_length=1, max_length=240)
    replaces_category: DirectiveCategory | None = None
    priority: int = Field(ge=1, le=3)
    expected_effect: str = Field(min_length=1, max_length=200)


class PlannerInput(BaseModel):
    """The complete checkpoint-safe input contract for the feedback planner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["planner-input/v1"] = "planner-input/v1"
    search_id: str = Field(min_length=1, max_length=120)
    round_index: int = Field(ge=0)
    selected_candidate_id: str = Field(min_length=1, max_length=120)
    global_winner_id: str | None = Field(default=None, max_length=120)
    canonical_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_prompt_summary: str = Field(min_length=1, max_length=400)
    blocking_issues: tuple[ActionableBlockingIssue, ...] = Field(
        default_factory=tuple, max_length=20
    )
    active_directives: tuple[PlannerDirective, ...] = Field(default_factory=tuple, max_length=3)
    attempted_categories: tuple[DirectiveCategory, ...] = Field(
        default_factory=tuple, max_length=12
    )
    directive_policy: DirectivePolicy = Field(default_factory=DirectivePolicy)
    directive_version: int = Field(default=0, ge=0)
    fallback_attempts: int = Field(default=0, ge=0, le=1)

    @property
    def is_checkpoint_safe(self) -> bool:
        """All fields are structured IDs, hashes, enums, or bounded short strings."""

        return True


class PlannerProposal(BaseModel):
    """Untrusted provider output before deterministic policy fields are applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["planner-proposal/v1"] = "planner-proposal/v1"
    action: PlannerAction
    directives: tuple[PlannerDirective, ...] = Field(default_factory=tuple, max_length=3)
    stop_reason: str | None = Field(default=None, max_length=160)
    plan_summary: str = Field(min_length=1, max_length=400)
    fallback_used: bool = False


class PlannerResult(BaseModel):
    """Validated planner output; ``directives`` is the replacement active set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["planner-result/v1"] = "planner-result/v1"
    action: PlannerAction
    directives: tuple[PlannerDirective, ...] = Field(default_factory=tuple, max_length=3)
    stop_reason: str | None = Field(default=None, max_length=160)
    plan_summary: str = Field(min_length=1, max_length=400)
    directive_policy_version: str = Field(min_length=1, max_length=80)
    directive_version: int = Field(default=0, ge=0)
    active_directives_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fallback_used: bool = False

    @property
    def is_checkpoint_safe(self) -> bool:
        return True


def stable_directive_id(
    *, category: DirectiveCategory, instruction: str, policy_version: str
) -> str:
    """Derive an idempotent directive ID without embedding user or image data."""

    digest = hashlib.sha256(
        f"{policy_version}\0{category.value}\0{instruction}".encode()
    ).hexdigest()
    return f"directive-{digest[:24]}"


def stable_directives_hash(directives: tuple[PlannerDirective, ...]) -> str:
    """Hash the canonical active set so prompt lineage remains auditable."""

    payload = [
        directive.model_dump(mode="json")
        for directive in sorted(
            directives,
            key=lambda item: (item.priority, item.category.value, item.directive_id),
        )
    ]
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
