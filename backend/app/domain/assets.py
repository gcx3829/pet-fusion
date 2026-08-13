from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.errors import SourceManifestMismatchError


class AssetKind(StrEnum):
    SOURCE_BACKGROUND = "source_background"
    SOURCE_CAT_REFERENCE = "source_cat_reference"
    CANDIDATE_RAW = "candidate_raw"
    CANDIDATE_PROTECTED = "candidate_protected"


class AssetRef(BaseModel):
    """Internal reference safe for checkpoints; never contains image bytes."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: str = "image/png"
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def filesystem_path(self) -> Path:
        return Path(self.path)


class PublicAssetRef(BaseModel):
    asset_id: str
    sha256: str
    mime_type: str
    width: int
    height: int
    asset_url: str

    @classmethod
    def from_internal(cls, asset: AssetRef) -> PublicAssetRef:
        return cls(
            asset_id=asset.asset_id,
            sha256=asset.sha256,
            mime_type=asset.mime_type,
            width=asset.width,
            height=asset.height,
            asset_url=f"/api/v1/assets/{asset.asset_id}",
        )


class SourceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "source-manifest/v1"
    background: AssetRef
    cat_references: tuple[AssetRef, ...]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_reference_count(self) -> SourceManifest:
        if not 1 <= len(self.cat_references) <= 5:
            raise ValueError("cat_references must contain between 1 and 5 images")
        return self

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "background": {
                "asset_id": self.background.asset_id,
                "sha256": self.background.sha256,
            },
            "cat_references": [
                {"asset_id": ref.asset_id, "sha256": ref.sha256} for ref in self.cat_references
            ],
        }

    def computed_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def assert_integrity(self) -> None:
        if self.computed_hash() != self.manifest_hash:
            raise SourceManifestMismatchError("Immutable source manifest hash mismatch")

    @classmethod
    def create(cls, *, background: AssetRef, cat_references: list[AssetRef]) -> SourceManifest:
        provisional = cls.model_construct(
            schema_version="source-manifest/v1",
            background=background,
            cat_references=tuple(cat_references),
            manifest_hash="0" * 64,
        )
        return cls(
            background=background,
            cat_references=tuple(cat_references),
            manifest_hash=provisional.computed_hash(),
        )


class PublicSourceManifest(BaseModel):
    schema_version: str
    background: PublicAssetRef
    cat_references: list[PublicAssetRef]
    manifest_hash: str

    @classmethod
    def from_internal(cls, manifest: SourceManifest) -> PublicSourceManifest:
        return cls(
            schema_version=manifest.schema_version,
            background=PublicAssetRef.from_internal(manifest.background),
            cat_references=[
                PublicAssetRef.from_internal(reference) for reference in manifest.cat_references
            ],
            manifest_hash=manifest.manifest_hash,
        )
