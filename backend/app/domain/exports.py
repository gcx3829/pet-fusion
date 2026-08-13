from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef
from app.domain.compositing import CompositeResult


class ExportRequest(BaseModel):
    """A safe export command containing IDs and options, never image payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["export-request/v1"] = "export-request/v1"
    search_id: str = Field(min_length=1)
    candidate_id: str | None = None
    format: Literal["png"] = "png"
    copy_exif: bool = True
    copy_icc: bool = True


class ExportResult(BaseModel):
    """A content-addressed export record suitable for API delivery or checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["export-result/v1"] = "export-result/v1"
    export_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    search_id: str
    candidate_id: str
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: AssetRef
    composite: CompositeResult
    copied_icc: bool
    copied_exif: bool
