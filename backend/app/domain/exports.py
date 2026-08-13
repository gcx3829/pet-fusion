from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef, PublicAssetRef
from app.domain.compositing import CompositeResult

ExportFormat = Literal["png", "jpeg"]
DEFAULT_JPEG_QUALITY = 95
MIN_JPEG_QUALITY = 60
MAX_JPEG_QUALITY = 100


class ExportOptions(BaseModel):
    """Validated output options shared by API submissions and service commands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str | None = Field(default=None, min_length=1, max_length=120)
    format: ExportFormat = "png"
    jpeg_quality: int = Field(
        default=DEFAULT_JPEG_QUALITY,
        ge=MIN_JPEG_QUALITY,
        le=MAX_JPEG_QUALITY,
    )
    copy_exif: bool = True
    copy_icc: bool = True

    @model_validator(mode="after")
    def validate_format_specific_options(self) -> ExportOptions:
        if self.format == "png" and self.jpeg_quality != DEFAULT_JPEG_QUALITY:
            raise ValueError("jpeg_quality can only differ from its default for JPEG exports")
        return self

    @property
    def canonical_jpeg_quality(self) -> int | None:
        """Only JPEG quality participates in identity; PNG has no quality option."""

        return self.jpeg_quality if self.format == "jpeg" else None


class ExportRequest(ExportOptions):
    """A safe export command containing IDs and options, never image payloads."""

    schema_version: Literal["export-request/v1"] = "export-request/v1"
    search_id: str = Field(min_length=1, max_length=120)


class ExportSubmission(ExportOptions):
    """HTTP request body; the search ID is supplied by the URL path."""

    schema_version: Literal["export-submission/v1"] = "export-submission/v1"

    def to_command(self, *, search_id: str) -> ExportRequest:
        return ExportRequest(search_id=search_id, **self.model_dump(exclude={"schema_version"}))


class ExportMetadata(BaseModel):
    """Small, public-safe lineage summary stored next to every final export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["export-metadata/v1"] = "export-metadata/v1"
    accepted_search_status: Literal["accepted"] = "accepted"
    accepted_global_winner_id: str = Field(min_length=1, max_length=120)
    global_winner_score: float | None = Field(default=None, ge=0, le=100)
    source_background_asset_id: str = Field(min_length=1)
    raw_candidate_asset_id: str = Field(min_length=1)
    protected_candidate_asset_id: str = Field(min_length=1)
    composite_mask_asset_id: str = Field(min_length=1)
    composite_mask_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExportResult(BaseModel):
    """A content-addressed export record suitable for API delivery or checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["export-result/v1"] = "export-result/v1"
    export_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_id: str
    candidate_id: str
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    format: ExportFormat
    jpeg_quality: int | None = Field(default=None, ge=MIN_JPEG_QUALITY, le=MAX_JPEG_QUALITY)
    asset: AssetRef
    composite: CompositeResult
    copied_icc: bool
    copied_exif: bool
    metadata: ExportMetadata

    @model_validator(mode="after")
    def validate_delivery_lineage(self) -> ExportResult:
        expected_mime_type = "image/png" if self.format == "png" else "image/jpeg"
        if self.asset.mime_type != expected_mime_type:
            raise ValueError("export asset MIME type does not match the output format")
        if self.format == "png" and self.jpeg_quality is not None:
            raise ValueError("PNG exports cannot retain a JPEG quality")
        if self.format == "jpeg" and self.jpeg_quality is None:
            raise ValueError("JPEG exports require a bounded quality")
        if self.metadata.accepted_global_winner_id != self.candidate_id:
            raise ValueError("export metadata global winner must match the export candidate")
        if self.metadata.source_background_asset_id != self.composite.source_background.asset_id:
            raise ValueError("export metadata source background must match the composite")
        if self.metadata.raw_candidate_asset_id != self.composite.raw_candidate.asset_id:
            raise ValueError("export metadata raw candidate must match the composite")
        if self.metadata.protected_candidate_asset_id != self.composite.protected_asset.asset_id:
            raise ValueError("export metadata protected candidate must match the composite")
        if self.metadata.composite_mask_asset_id != self.composite.mask.asset.asset_id:
            raise ValueError("export metadata mask must match the composite")
        if self.metadata.composite_mask_sha256 != self.composite.mask.asset.sha256:
            raise ValueError("export metadata mask hash must match the composite")
        return self


class ExportResponse(BaseModel):
    """API-safe representation that never discloses server filesystem paths."""

    export_key: str
    search_id: str
    candidate_id: str
    source_manifest_hash: str
    format: ExportFormat
    jpeg_quality: int | None
    asset: PublicAssetRef
    copied_icc: bool
    copied_exif: bool
    metadata: ExportMetadata

    @classmethod
    def from_result(cls, result: ExportResult) -> ExportResponse:
        return cls(
            export_key=result.export_key,
            search_id=result.search_id,
            candidate_id=result.candidate_id,
            source_manifest_hash=result.source_manifest_hash,
            format=result.format,
            jpeg_quality=result.jpeg_quality,
            asset=PublicAssetRef.from_internal(result.asset),
            copied_icc=result.copied_icc,
            copied_exif=result.copied_exif,
            metadata=result.metadata,
        )
