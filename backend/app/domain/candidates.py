from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef


class CandidateRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str
    round_index: int = Field(ge=0)
    variant_index: int = Field(ge=0)
    raw_asset: AssetRef
    protected_asset: AssetRef
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_depth: int = Field(default=0, ge=0, le=2)
    model: str
    quality: str
    size: str


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
        )
