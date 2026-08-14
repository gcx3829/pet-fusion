"""Structured candidate evaluations shared by Critic, Ranker, and the search graph."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Severity(StrEnum):
    """Normalized issue severity used by deterministic policy code."""

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class CriticCategory(StrEnum):
    """Closed issue taxonomy normalized before policy code sees provider output."""

    CAT_IDENTITY = "cat_identity"
    POSE_GEOMETRY = "pose_geometry"
    PERSPECTIVE_SCALE = "perspective_scale"
    LIGHTING_COLOR = "lighting_color"
    OPTICAL_CONSISTENCY = "optical_consistency"
    PHYSICAL_INTEGRATION = "physical_integration"
    SCENE_PRESERVATION = "scene_preservation"
    ASSET_INTEGRITY = "asset_integrity"
    UNCLASSIFIED = "unclassified"


_CRITIC_CATEGORY_ALIASES: dict[str, CriticCategory] = {
    "identity": CriticCategory.CAT_IDENTITY,
    "cat_identity": CriticCategory.CAT_IDENTITY,
    "coat_pattern": CriticCategory.CAT_IDENTITY,
    "fur_identity": CriticCategory.CAT_IDENTITY,
    "pose": CriticCategory.POSE_GEOMETRY,
    "pose_geometry": CriticCategory.POSE_GEOMETRY,
    "anatomy": CriticCategory.POSE_GEOMETRY,
    "perspective": CriticCategory.PERSPECTIVE_SCALE,
    "perspective_scale": CriticCategory.PERSPECTIVE_SCALE,
    "scale": CriticCategory.PERSPECTIVE_SCALE,
    "lighting": CriticCategory.LIGHTING_COLOR,
    "lighting_color": CriticCategory.LIGHTING_COLOR,
    "color": CriticCategory.LIGHTING_COLOR,
    "optics": CriticCategory.OPTICAL_CONSISTENCY,
    "optical_consistency": CriticCategory.OPTICAL_CONSISTENCY,
    "sharpness": CriticCategory.OPTICAL_CONSISTENCY,
    "physical_integration": CriticCategory.PHYSICAL_INTEGRATION,
    "integration": CriticCategory.PHYSICAL_INTEGRATION,
    "contact_shadow": CriticCategory.PHYSICAL_INTEGRATION,
    "scene_preservation": CriticCategory.SCENE_PRESERVATION,
    "background": CriticCategory.SCENE_PRESERVATION,
    "asset_integrity": CriticCategory.ASSET_INTEGRITY,
}


MIN_BLOCKING_CONFIDENCE = 0.75
MAX_CRITIC_ISSUES = 20
EvaluationAction = Literal["accept", "regenerate", "review", "none"]


class CriticIssue(BaseModel):
    """One observable issue reported for a candidate image."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_id: str = Field(min_length=1, max_length=120)
    category: CriticCategory
    severity: Severity
    region: str | None = Field(default=None, max_length=120)
    evidence: str = Field(min_length=1, max_length=500)
    suggested_fix: str | None = Field(default=None, max_length=300)
    confidence: float = Field(ge=0, le=1)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: object) -> CriticCategory:
        if isinstance(value, CriticCategory):
            return value
        if not isinstance(value, str):
            return CriticCategory.UNCLASSIFIED
        normalized = "_".join(value.strip().casefold().replace("-", " ").split())
        return _CRITIC_CATEGORY_ALIASES.get(normalized, CriticCategory.UNCLASSIFIED)


class DimensionScores(BaseModel):
    """The fixed rubric dimensions consumed by the deterministic ranker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cat_identity: float = Field(ge=0, le=100)
    pose_geometry: float = Field(ge=0, le=100)
    perspective_scale: float = Field(ge=0, le=100)
    lighting_color: float = Field(ge=0, le=100)
    optical_consistency: float = Field(ge=0, le=100)
    physical_integration: float = Field(ge=0, le=100)
    scene_preservation: float = Field(ge=0, le=100)
    overall_photographic_naturalness: float = Field(ge=0, le=100)

    @model_validator(mode="before")
    @classmethod
    def normalize_unit_interval_scores(cls, value: Any) -> Any:
        """Accept relays that return rubric scores as 0..1 fractions.

        The public rubric is 0..100, but a few OpenAI-compatible relays coerce
        numeric fields into unit-interval probabilities. Normalizing that shape
        at the domain boundary keeps the ranker and UI on one scale without
        changing the documented response contract.
        """

        if not isinstance(value, Mapping):
            return value
        dimension_names = tuple(cls.model_fields)
        raw_scores = [value.get(name) for name in dimension_names]
        numeric_scores = [
            item
            for item in raw_scores
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        if (
            numeric_scores
            and len(numeric_scores) == len(dimension_names)
            and max(numeric_scores) <= 1
        ):
            return {
                **value,
                **{
                    name: float(value[name]) * 100
                    for name in dimension_names
                },
            }
        return value

    def as_mapping(self) -> dict[str, float]:
        return {
            "cat_identity": self.cat_identity,
            "pose_geometry": self.pose_geometry,
            "perspective_scale": self.perspective_scale,
            "lighting_color": self.lighting_color,
            "optical_consistency": self.optical_consistency,
            "physical_integration": self.physical_integration,
            "scene_preservation": self.scene_preservation,
            "overall_photographic_naturalness": self.overall_photographic_naturalness,
        }


class CandidateEvaluation(BaseModel):
    """Versioned, checkpoint-safe Critic result for one candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rubric_version: str = Field(min_length=1, max_length=80)
    candidate_id: str = Field(min_length=1, max_length=120)
    round_index: int = Field(ge=0)
    source_manifest_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    scores: DimensionScores
    issues: tuple[CriticIssue, ...] = Field(
        default_factory=tuple, max_length=MAX_CRITIC_ISSUES
    )
    no_meaningful_defect: bool
    identity_match: bool
    prompt_adherent: bool
    recommended_action: EvaluationAction
    summary: str = Field(min_length=1, max_length=500)
    hard_constraint_failures: tuple[str, ...] = Field(default_factory=tuple)
    semantic_conflict: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_critic_output(cls, value: Any) -> Any:
        if isinstance(value, cls) or not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        normalized_issues = tuple(
            issue.model_copy(update={"severity": Severity.WARNING})
            if issue.severity is Severity.BLOCKING
            and issue.confidence < MIN_BLOCKING_CONFIDENCE
            else issue
            for raw_issue in normalized.get("issues", ())
            for issue in (
                raw_issue
                if isinstance(raw_issue, CriticIssue)
                else CriticIssue.model_validate(raw_issue),
            )
        )
        normalized["issues"] = normalized_issues
        normalized["semantic_conflict"] = bool(normalized.get("semantic_conflict")) or (
            bool(normalized.get("no_meaningful_defect"))
            and any(issue.severity is Severity.BLOCKING for issue in normalized_issues)
        )
        return normalized

    @property
    def blocking_issues(self) -> tuple[CriticIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is Severity.BLOCKING)

    @property
    def has_blocking_issue(self) -> bool:
        return bool(self.blocking_issues)

    @property
    def is_checkpoint_safe(self) -> bool:
        """Document that this model only contains references and structured values."""

        return True


class StopAction(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"
    HUMAN_REVIEW = "human_review"


class StopDecision(BaseModel):
    """Deterministic search-loop routing decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: StopAction
    reason: str = Field(min_length=1, max_length=120)
    round_index: int = Field(ge=0)
    global_winner_id: str | None = None
    global_winner_score: float | None = Field(default=None, ge=0, le=100)
    blocking_issue_ids: tuple[str, ...] = Field(default_factory=tuple)
    eligible: bool = True
    planner_required: bool = False
    detail: str = Field(default="", max_length=500)


class DimensionWeights(BaseModel):
    """Versioned ranker weights kept in one explicit domain type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cat_identity: float = 0.24
    pose_geometry: float = 0.10
    perspective_scale: float = 0.14
    lighting_color: float = 0.12
    optical_consistency: float = 0.14
    physical_integration: float = 0.12
    scene_preservation: float = 0.08
    overall_photographic_naturalness: float = 0.06

    @model_validator(mode="after")
    def weights_sum_to_one(self) -> DimensionWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError("ranker dimension weights must sum to 1")
        return self

    def as_mapping(self) -> dict[str, float]:
        return self.model_dump()


class RankingPolicy(BaseModel):
    """Deterministic acceptance and historical-winner policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = "ranker/v1"
    weights: DimensionWeights = Field(default_factory=DimensionWeights)
    blocking_penalty: float = Field(default=18.0, ge=0, le=100)
    accept_threshold: float = Field(default=91.0, ge=0, le=100)
    identity_threshold: float = Field(default=75.0, ge=0, le=100)
    minimum_improvement: float = Field(default=2.0, ge=0, le=100)
    tie_margin: float = Field(default=1.5, ge=0, le=100)


class CandidateScore(BaseModel):
    """A candidate's normalized score and hard-constraint result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    round_index: int = Field(ge=0)
    base_score: float = Field(ge=0, le=100)
    score: float = Field(ge=0, le=100)
    eligible: bool
    hard_fail_reasons: tuple[str, ...] = Field(default_factory=tuple)
    blocking_issue_ids: tuple[str, ...] = Field(default_factory=tuple)


class RoundRanking(BaseModel):
    """Stable ranking result for one round."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_index: int = Field(ge=0)
    scores: tuple[CandidateScore, ...] = Field(default_factory=tuple)
    winner_id: str | None = None
    winner_score: float | None = Field(default=None, ge=0, le=100)


class GlobalWinner(BaseModel):
    """Historical best candidate; it is never implicitly replaced by the last round."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str
    score: float = Field(ge=0, le=100)
    round_index: int = Field(ge=0)
