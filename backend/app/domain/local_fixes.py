"""Checkpoint-safe contracts for bounded candidate-to-candidate local fixes."""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.assets import AssetRef, PublicAssetRef, SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.compositing import CompositeResult, Mask, PixelBox
from app.domain.searches import PlacementIntent

_HORIZONTAL_WHITESPACE = re.compile(r"[ \t]+")
_INSTRUCTION_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "system prompt",
    "developer message",
    "assistant message",
    "jailbreak",
    "reveal instructions",
    "follow these instructions instead",
    "<script",
    "```",
    "role:",
)


def normalize_local_fix_instruction(value: str) -> str:
    """Normalize one user correction while rejecting prompt-boundary escapes.

    The Local Fix provider will eventually place this value inside a bounded edit
    prompt. Reject every Unicode line/control separator and common role-override
    marker here so callers cannot turn the single instruction field into a second
    provider message. Normal horizontal spacing is canonicalized for stable hashes.
    """

    if len(value.splitlines()) != 1:
        raise ValueError("Local Fix instruction must be a single line")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError("Local Fix instruction cannot contain control characters")
    normalized = _HORIZONTAL_WHITESPACE.sub(" ", value.strip())
    if not normalized:
        raise ValueError("Local Fix instruction must not be blank")
    lowered = normalized.casefold()
    if any(marker in lowered for marker in _INSTRUCTION_INJECTION_MARKERS):
        raise ValueError("Local Fix instruction contains a forbidden prompt-control marker")
    return normalized


class LocalFixOutcome(StrEnum):
    """The user-visible outcome of a single non-looping Local Fix invocation."""

    APPLIED = "applied"
    FALLBACK = "fallback"


class LocalFixMaskSubmission(BaseModel):
    """Public tight-mask reference or a deterministic structural mask request.

    Clients may name an existing mask asset or submit only the bounded box and
    feather used to construct a server-side PNG mask. Neither form can include a
    server path, digest, or image bytes. The Local Fix service verifies the final
    stored mask's bytes and pixel support before it can reach a provider.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-mask-submission/v1"] = "local-fix-mask-submission/v1"
    asset_id: str | None = Field(
        default=None,
        pattern=r"^ast_[0-9a-f]{32}$",
    )
    coordinate_space: Literal["full_resolution"] = "full_resolution"
    allowed_box: PixelBox
    feather_radius_px: int = Field(default=0, ge=0, le=4096)


class LocalFixSubmission(BaseModel):
    """HTTP input for one Local Fix without untrusted lineage fields.

    The server derives source manifest, base depth, and optional prior-result
    lineage from durable records.  Omitting ``candidate_id`` intentionally means
    the accepted historical global winner.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-submission/v1"] = "local-fix-submission/v1"
    candidate_id: str | None = Field(
        default=None,
        pattern=r"^cand_(?:local_)?[0-9a-f]{28,64}$",
    )
    tight_mask: LocalFixMaskSubmission
    instruction: str = Field(min_length=1, max_length=240)

    @field_validator("instruction", mode="before")
    @classmethod
    def validate_single_instruction(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_local_fix_instruction(value)


class LocalFixRequest(BaseModel):
    """A single, bounded edit request against an already accepted candidate.

    ``generation_depth`` describes the expected depth of ``base_candidate_id``.
    The service independently verifies it against trusted candidate lineage and
    rejects an attempted output at depth three before invoking a provider.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-request/v1"] = "local-fix-request/v1"
    search_id: str = Field(min_length=1, max_length=120)
    base_candidate_id: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tight_mask: Mask
    instruction: str = Field(min_length=1, max_length=240)
    generation_depth: int = Field(ge=0, le=2)

    @field_validator("instruction", mode="before")
    @classmethod
    def validate_single_instruction(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return normalize_local_fix_instruction(value)


class LocalFixResolution(BaseModel):
    """Trusted, asset-reference-only Local Fix input resolved by the service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-resolution/v1"] = "local-fix-resolution/v1"
    request: LocalFixRequest
    source_manifest: SourceManifest
    placement: PlacementIntent
    base_candidate: CandidateRecord
    root_candidate_id: str = Field(min_length=1, max_length=120)
    outer_composite_mask: Mask

    @model_validator(mode="after")
    def validate_trusted_lineage(self) -> LocalFixResolution:
        if self.source_manifest.manifest_hash != self.request.source_manifest_hash:
            raise ValueError("Local Fix request does not match the immutable source manifest")
        if self.base_candidate.candidate_id != self.request.base_candidate_id:
            raise ValueError("Resolved Local Fix candidate does not match the request")
        if self.base_candidate.source_manifest_hash != self.request.source_manifest_hash:
            raise ValueError("Local Fix candidate source lineage does not match the request")
        if self.base_candidate.generation_depth != self.request.generation_depth:
            raise ValueError("Local Fix generation depth does not match the base candidate")
        if self.base_candidate.composite is not None and (
            self.outer_composite_mask != self.base_candidate.composite.mask
        ):
            raise ValueError("Local Fix outer mask does not match the base candidate lineage")
        return self


class LocalFixResult(BaseModel):
    """Structured result of exactly one Local Fix attempt.

    The result deliberately retains the untouched base candidate as an explicit
    rollback target. All images are represented by ``AssetRef`` values; no image
    bytes or provider payloads can enter a LangGraph checkpoint through this
    contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-result/v1"] = "local-fix-result/v1"
    outcome: LocalFixOutcome
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_id: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_candidate_id: str = Field(min_length=1, max_length=120)
    base_candidate_id: str = Field(min_length=1, max_length=120)
    instruction_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_depth: int = Field(ge=0, le=2)
    candidate: CandidateRecord | None = None
    fallback_candidate: CandidateRecord
    provider_raw_asset: AssetRef | None = None
    tight_composite: CompositeResult | None = None
    composite: CompositeResult | None = None
    failure_code: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_outcome_lineage(self) -> LocalFixResult:
        if self.fallback_candidate.candidate_id != self.base_candidate_id:
            raise ValueError("Local Fix fallback must be the requested base candidate")
        if self.fallback_candidate.source_manifest_hash != self.source_manifest_hash:
            raise ValueError("Local Fix fallback source lineage does not match the result")
        if self.outcome is LocalFixOutcome.APPLIED:
            if self.candidate is None or self.provider_raw_asset is None:
                raise ValueError("Applied Local Fix results require generated asset references")
            if self.tight_composite is None or self.composite is None:
                raise ValueError("Applied Local Fix results require both composite records")
            if self.failure_code is not None:
                raise ValueError("Applied Local Fix results cannot contain a failure code")
            if self.candidate.generation_depth != self.generation_depth:
                raise ValueError("Local Fix output depth does not match the result")
            if self.generation_depth != self.fallback_candidate.generation_depth + 1:
                raise ValueError("Local Fix output depth must increment the base by exactly one")
            if self.candidate.source_manifest_hash != self.source_manifest_hash:
                raise ValueError("Local Fix output source lineage does not match the result")
            if self.candidate.request_key != self.request_key:
                raise ValueError("Local Fix output request key does not match the result")
            if self.candidate.prompt_hash != self.instruction_hash:
                raise ValueError("Local Fix output instruction hash does not match the result")
            if self.candidate.candidate_id == self.base_candidate_id:
                raise ValueError("Local Fix output must not reuse its base candidate ID")
            if self.fallback_candidate.generation_depth == 0 and (
                self.root_candidate_id != self.base_candidate_id
            ):
                raise ValueError("A first Local Fix must retain its historical root candidate")
            if self.tight_composite.source_manifest_hash != self.source_manifest_hash:
                raise ValueError("Tight Local Fix composite source lineage does not match")
            if self.tight_composite.source_background != self.fallback_candidate.protected_asset:
                raise ValueError(
                    "Tight Local Fix composite must be based on the selected candidate"
                )
            if self.tight_composite.raw_candidate != self.provider_raw_asset:
                raise ValueError("Tight Local Fix composite must retain provider output lineage")
            if self.tight_composite.protected_asset != self.candidate.raw_asset:
                raise ValueError("Tight Local Fix composite must feed the final composite")
            if self.tight_composite.crop_mapping is not None:
                raise ValueError("Full-resolution Local Fix composites cannot use crop mappings")
            if self.composite.source_manifest_hash != self.source_manifest_hash:
                raise ValueError("Final Local Fix composite source lineage does not match")
            if self.composite.raw_candidate != self.tight_composite.protected_asset:
                raise ValueError("Final Local Fix composite must consume the tight composite")
            if self.composite.protected_asset != self.candidate.protected_asset:
                raise ValueError("Local Fix final composite must match the output candidate")
            return self
        if self.candidate is not None or self.provider_raw_asset is not None:
            raise ValueError("Fallback Local Fix results cannot expose a generated candidate")
        if self.tight_composite is not None or self.composite is not None:
            raise ValueError("Fallback Local Fix results cannot expose composite output")
        if self.failure_code is None:
            raise ValueError("Fallback Local Fix results require a stable failure code")
        if self.fallback_candidate.generation_depth != self.generation_depth:
            raise ValueError("Fallback depth must remain at the base candidate depth")
        return self


class LocalFixResponse(BaseModel):
    """API-safe projection of a checkpoint-safe Local Fix result.

    The internal result intentionally retains filesystem-backed ``AssetRef``
    values for graph recovery.  This response preserves the same lineage and
    outcome information while exposing assets only through authenticated API
    URLs, matching the existing candidate and export response boundaries.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["local-fix-response/v1"] = "local-fix-response/v1"
    fix_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: LocalFixOutcome
    outcome: LocalFixOutcome
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_id: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    root_candidate_id: str = Field(min_length=1, max_length=120)
    base_candidate_id: str = Field(min_length=1, max_length=120)
    instruction_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_depth: int = Field(ge=0, le=2)
    asset: PublicAssetRef
    asset_url: str
    candidate: CandidateResponse | None = None
    fallback_candidate: CandidateResponse
    failure_code: str | None = Field(default=None, max_length=120)

    @classmethod
    def from_result(cls, result: LocalFixResult) -> LocalFixResponse:
        visible_candidate = result.candidate or result.fallback_candidate
        public_asset = PublicAssetRef.from_internal(visible_candidate.protected_asset)
        return cls(
            fix_id=result.request_key,
            status=result.outcome,
            outcome=result.outcome,
            request_key=result.request_key,
            search_id=result.search_id,
            source_manifest_hash=result.source_manifest_hash,
            root_candidate_id=result.root_candidate_id,
            base_candidate_id=result.base_candidate_id,
            instruction_hash=result.instruction_hash,
            generation_depth=result.generation_depth,
            asset=public_asset,
            asset_url=public_asset.asset_url,
            candidate=(
                CandidateResponse.from_record(result.candidate)
                if result.candidate is not None
                else None
            ),
            fallback_candidate=CandidateResponse.from_record(result.fallback_candidate),
            failure_code=result.failure_code,
        )
