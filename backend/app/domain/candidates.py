from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef
from app.domain.compositing import CompositeResult, CropMapping
from app.domain.evaluations import CandidateEvaluation


class CandidateRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    # ``candidate/v2`` makes the authority explicit without invalidating old
    # SQLite records, which predate this field and still contain a protected
    # asset.  Raw is the only image used by Search/Critic/user review.
    schema_version: Literal["candidate/v1", "candidate/v2"] = "candidate/v2"

    candidate_id: str
    round_index: int = Field(ge=0)
    variant_index: int = Field(ge=0)
    raw_asset: AssetRef
    # Kept as a legacy compatibility reference.  Raw-first records may omit it
    # on input; the pre-validator aliases raw_asset so older Local Fix/Export
    # code can continue to read the record until it is migrated.
    protected_asset: AssetRef
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    crop_mapping: CropMapping | None = None
    composite: CompositeResult | None = None
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_depth: int = Field(default=0, ge=0, le=2)
    model: str
    quality: str
    size: str

    @model_validator(mode="before")
    @classmethod
    def hydrate_legacy_asset_fields(cls, value: object) -> object:
        """Read both old protected-only rows and new raw-only rows.

        The persisted v1 shape had ``protected_asset`` as the public image.  A
        raw-first candidate does not need a second generated image, so its
        compatibility field is deterministically aliased to ``raw_asset``.
        This keeps old checkpoints readable while making raw the canonical
        source for all new semantics.
        """

        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        if "raw_asset" not in payload and "protected_asset" in payload:
            payload["raw_asset"] = payload["protected_asset"]
        if "protected_asset" not in payload and "raw_asset" in payload:
            payload["protected_asset"] = payload["raw_asset"]
        if payload.get("schema_version") is None:
            # Persisted protected-first rows always carried a composite result.
            # New raw-only construction reaches this pre-validator before the
            # Pydantic default is applied, so treating every missing version as
            # v1 would silently mislabel every new Search candidate.
            payload["schema_version"] = (
                "candidate/v1" if payload.get("composite") is not None else "candidate/v2"
            )
        return payload

    @property
    def raw_authoritative_asset(self) -> AssetRef:
        """The image used for Search review, Critic, and user acceptance.

        ``protected_asset`` and ``composite`` remain on the record so older
        SQLite rows and the isolated Local Fix/Export flows can still be read.
        They are deliberately not the authority for Search semantics.
        """

        return self.raw_asset

    @model_validator(mode="after")
    def validate_composite_lineage(self) -> CandidateRecord:
        if self.composite is None:
            return self
        if self.composite.source_manifest_hash != self.source_manifest_hash:
            raise ValueError("composite source manifest hash must match candidate lineage")
        if self.composite.raw_candidate != self.raw_asset:
            raise ValueError("composite raw candidate must match candidate raw_asset")
        if self.composite.protected_asset != self.protected_asset:
            raise ValueError("composite protected asset must match candidate protected_asset")
        if self.composite.crop_mapping != self.crop_mapping:
            raise ValueError("composite crop mapping must match candidate crop_mapping")
        return self


class CandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    round_index: int
    variant_index: int
    asset_id: str
    asset_url: str
    width: int
    height: int
    mime_type: str
    prompt_hash: str
    request_key: str
    generation_depth: int
    model: str
    quality: str
    size: str
    composite_floor_applied: bool
    composite_mask_asset_id: str | None
    composite_mask_url: str | None
    crop_mapping: CropMapping | None
    # Raw is the canonical Search/Critic/user-review image.  The generic
    # ``asset_*`` fields now point to raw; explicit protected fields below keep
    # old clients and Local Fix/Export diagnostics readable.
    raw_asset_id: str
    raw_asset_url: str
    raw_width: int
    raw_height: int
    raw_mime_type: str
    protected_asset_id: str | None = None
    protected_asset_url: str | None = None
    protected_width: int | None = None
    protected_height: int | None = None
    protected_mime_type: str | None = None
    review_asset_kind: Literal["raw"] = "raw"
    score: float | None = Field(default=None, ge=0, le=100)
    evaluation: CandidateEvaluation | None = None

    @classmethod
    def from_record(
        cls,
        candidate: CandidateRecord,
        *,
        evaluation: CandidateEvaluation | None = None,
        score: float | None = None,
    ) -> CandidateResponse:
        raw = candidate.raw_authoritative_asset
        protected = candidate.protected_asset
        return cls(
            candidate_id=candidate.candidate_id,
            round_index=candidate.round_index,
            variant_index=candidate.variant_index,
            asset_id=raw.asset_id,
            asset_url=f"/api/v1/assets/{raw.asset_id}",
            width=raw.width,
            height=raw.height,
            mime_type=raw.mime_type,
            prompt_hash=candidate.prompt_hash,
            request_key=candidate.request_key,
            generation_depth=candidate.generation_depth,
            model=candidate.model,
            quality=candidate.quality,
            size=candidate.size,
            composite_floor_applied=candidate.composite is not None,
            composite_mask_asset_id=(
                candidate.composite.mask.asset.asset_id if candidate.composite else None
            ),
            composite_mask_url=(
                f"/api/v1/assets/{candidate.composite.mask.asset.asset_id}"
                if candidate.composite
                else None
            ),
            crop_mapping=candidate.crop_mapping,
            raw_asset_id=candidate.raw_asset.asset_id,
            raw_asset_url=f"/api/v1/assets/{candidate.raw_asset.asset_id}",
            raw_width=candidate.raw_asset.width,
            raw_height=candidate.raw_asset.height,
            raw_mime_type=candidate.raw_asset.mime_type,
            protected_asset_id=protected.asset_id,
            protected_asset_url=f"/api/v1/assets/{protected.asset_id}",
            protected_width=protected.width,
            protected_height=protected.height,
            protected_mime_type=protected.mime_type,
            review_asset_kind="raw",
            score=score,
            evaluation=evaluation,
        )
