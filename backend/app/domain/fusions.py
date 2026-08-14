from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef, PublicAssetRef
from app.domain.compositing import CompositeResult, CropMapping, FloatBox


class FusionBox(BaseModel):
    """A user-authored normalized rectangle for the optional final fusion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> FusionBox:
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("fusion box must remain within normalized image bounds")
        return self

    def as_float_box(self) -> FloatBox:
        return FloatBox(x=self.x, y=self.y, width=self.width, height=self.height)


class FusionRequest(BaseModel):
    """Checkpoint-safe command for one explicit user Fusion Mask operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fusion-request/v1"] = "fusion-request/v1"
    search_id: str = Field(pattern=r"^search_[0-9a-f]{32}$")
    candidate_id: str | None = Field(default=None, pattern=r"^cand_[0-9a-f]{32}$")
    mask_asset_id: str | None = Field(default=None, pattern=r"^ast_[0-9a-f]{32}$")
    box: FusionBox | None = None
    feather_radius_px: int = Field(default=8, ge=0, le=256)

    @model_validator(mode="after")
    def validate_mask_source(self) -> FusionRequest:
        if (self.mask_asset_id is None) == (self.box is None):
            raise ValueError("provide exactly one of mask_asset_id or box")
        return self


class FusionResult(BaseModel):
    """Durable result of an explicit Fusion Mask render.

    The raw candidate remains immutable and is never replaced by ``fusion_asset``.
    The embedded CompositeResult is an implementation-safe pixel lineage record;
    it is not a Search/Critic candidate and must not be used for ranking.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fusion-result/v1"] = "fusion-result/v1"
    key_schema_version: Literal["fusion/v1", "fusion/v2"]
    fusion_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_id: str = Field(pattern=r"^search_[0-9a-f]{32}$")
    candidate_id: str = Field(pattern=r"^cand_[0-9a-f]{32}$")
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_asset: AssetRef
    fusion_asset: AssetRef
    mask: AssetRef
    # Present only when the user uploaded a full-resolution alpha mask.  ``mask``
    # is the normalized/feathered mask actually used for the render; retaining
    # this input reference makes that derivation auditable without trusting the
    # client-supplied asset ID on replay.
    input_mask_asset: AssetRef | None = None
    feather_radius_px: int = Field(ge=0, le=256)
    box: FusionBox | None = None
    crop_mapping: CropMapping | None = None
    composite: CompositeResult

    @model_validator(mode="before")
    @classmethod
    def hydrate_v1_mask_lineage(cls, value: object) -> object:
        """Keep initial v1 Fusion rows readable after mask binding was added."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "key_schema_version" not in payload:
            payload["key_schema_version"] = "fusion/v1"
        if (
            payload.get("box") is None
            and "input_mask_asset" not in payload
            and payload.get("mask") is not None
        ):
            # v1 persisted only the derived mask, not the uploaded source mask.
            # Treat that immutable derived asset as the best available legacy
            # audit reference; v2 rows always retain the true input separately.
            payload["input_mask_asset"] = payload["mask"]
        return payload

    @model_validator(mode="after")
    def validate_lineage(self) -> FusionResult:
        if (self.input_mask_asset is None) == (self.box is None):
            raise ValueError("fusion result requires exactly one mask input lineage")
        if self.raw_asset != self.composite.raw_candidate:
            raise ValueError("fusion raw asset must match the composite lineage")
        if self.fusion_asset != self.composite.protected_asset:
            raise ValueError("fusion asset must match the composite output")
        if self.mask != self.composite.mask.asset:
            raise ValueError("fusion mask must match the composite mask")
        if self.crop_mapping != self.composite.crop_mapping:
            raise ValueError("fusion crop mapping must match the composite lineage")
        if self.composite.source_manifest_hash != self.source_manifest_hash:
            raise ValueError("fusion source manifest does not match the composite")
        return self


class FusionSubmission(BaseModel):
    """HTTP body; the search ID is supplied by the URL path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fusion-submission/v1"] = "fusion-submission/v1"
    candidate_id: str | None = Field(default=None, pattern=r"^cand_[0-9a-f]{32}$")
    mask_asset_id: str | None = Field(default=None, pattern=r"^ast_[0-9a-f]{32}$")
    box: FusionBox | None = None
    feather_radius_px: int = Field(default=8, ge=0, le=256)

    @model_validator(mode="after")
    def validate_mask_source(self) -> FusionSubmission:
        if (self.mask_asset_id is None) == (self.box is None):
            raise ValueError("provide exactly one of mask_asset_id or box")
        return self

    def to_command(self, *, search_id: str) -> FusionRequest:
        return FusionRequest(
            search_id=search_id,
            candidate_id=self.candidate_id,
            mask_asset_id=self.mask_asset_id,
            box=self.box,
            feather_radius_px=self.feather_radius_px,
        )


class FusionResponse(BaseModel):
    """API-safe result; no server filesystem paths are exposed."""

    fusion_key: str
    key_schema_version: Literal["fusion/v1", "fusion/v2"]
    search_id: str
    candidate_id: str
    source_manifest_hash: str
    raw_asset: PublicAssetRef
    fusion_asset: PublicAssetRef
    mask_asset: PublicAssetRef
    input_mask_asset: PublicAssetRef | None
    feather_radius_px: int
    box: FusionBox | None
    crop_mapping: CropMapping | None

    @classmethod
    def from_result(cls, result: FusionResult) -> FusionResponse:
        return cls(
            fusion_key=result.fusion_key,
            key_schema_version=result.key_schema_version,
            search_id=result.search_id,
            candidate_id=result.candidate_id,
            source_manifest_hash=result.source_manifest_hash,
            raw_asset=PublicAssetRef.from_internal(result.raw_asset),
            fusion_asset=PublicAssetRef.from_internal(result.fusion_asset),
            mask_asset=PublicAssetRef.from_internal(result.mask),
            input_mask_asset=(
                PublicAssetRef.from_internal(result.input_mask_asset)
                if result.input_mask_asset is not None
                else None
            ),
            feather_radius_px=result.feather_radius_px,
            box=result.box,
            crop_mapping=result.crop_mapping,
        )


class FusionMaskResponse(BaseModel):
    """API-safe registration record for one search-scoped user alpha mask."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["fusion-mask-response/v1"] = "fusion-mask-response/v1"
    search_id: str
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: PublicAssetRef

    @classmethod
    def from_asset(
        cls,
        *,
        search_id: str,
        source_manifest_hash: str,
        asset: AssetRef,
    ) -> FusionMaskResponse:
        return cls(
            search_id=search_id,
            source_manifest_hash=source_manifest_hash,
            asset=PublicAssetRef.from_internal(asset),
        )
