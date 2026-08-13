from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef
from app.domain.compositing import CompositeResult, CropMapping


class CandidateRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    round_index: int = Field(ge=0)
    variant_index: int = Field(ge=0)
    raw_asset: AssetRef
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

    @classmethod
    def from_record(cls, candidate: CandidateRecord) -> CandidateResponse:
        asset = candidate.protected_asset
        return cls(
            candidate_id=candidate.candidate_id,
            round_index=candidate.round_index,
            variant_index=candidate.variant_index,
            asset_id=asset.asset_id,
            asset_url=f"/api/v1/assets/{asset.asset_id}",
            width=asset.width,
            height=asset.height,
            mime_type=asset.mime_type,
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
        )
