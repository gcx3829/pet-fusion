from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef, PublicAssetRef


class GuidanceMaskBinding(BaseModel):
    """Immutable project/source binding for one user-authored Guidance Mask."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["guidance-mask/v1"] = "guidance-mask/v1"
    project_id: str = Field(min_length=1, max_length=120)
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: AssetRef
    created_at: datetime


class GuidanceMaskResponse(BaseModel):
    """API-safe representation of a project-scoped Guidance Mask."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["guidance-mask-response/v1"] = "guidance-mask-response/v1"
    project_id: str
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    asset: PublicAssetRef
    created_at: datetime

    @classmethod
    def from_binding(cls, binding: GuidanceMaskBinding) -> GuidanceMaskResponse:
        return cls(
            project_id=binding.project_id,
            source_manifest_hash=binding.source_manifest_hash,
            asset=PublicAssetRef.from_internal(binding.asset),
            created_at=binding.created_at,
        )
