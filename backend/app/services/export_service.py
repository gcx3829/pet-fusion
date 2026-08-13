from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from typing import Literal

from PIL import Image

from app.domain.assets import AssetRef
from app.domain.candidates import CandidateRecord
from app.domain.compositing import Mask
from app.domain.errors import ConflictError
from app.domain.exports import ExportMetadata, ExportRequest, ExportResult
from app.domain.searches import SearchRunRecord, SearchStatus
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import CompositeFloorService, ImageMetadata

EXPORT_SCHEMA_VERSION = "export/v2"


@dataclass(frozen=True, slots=True)
class ResolvedExport:
    """Trusted, byte-free input to a final local render."""

    request: ExportRequest
    search: SearchRunRecord
    source_background: AssetRef
    candidate: CandidateRecord
    mask: Mask
    source_metadata: ImageMetadata
    export_key: str


class ExportService:
    """Create and persist a guarded final PNG or JPEG from a global winner."""

    def __init__(
        self,
        *,
        app_store: AppStore,
        asset_store: AssetStore,
        composite_floor: CompositeFloorService | None = None,
    ) -> None:
        self.app_store = app_store
        self.asset_store = asset_store
        self.composite_floor = composite_floor or CompositeFloorService(asset_store)

    @staticmethod
    def _export_key(
        *,
        request: ExportRequest,
        candidate_id: str,
        raw_sha256: str,
        source_manifest_hash: str,
        mask_sha256: str,
        crop_mapping: object,
    ) -> str:
        payload = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "search_id": request.search_id,
            "format": request.format,
            "jpeg_quality": request.canonical_jpeg_quality,
            "copy_exif": request.copy_exif,
            "copy_icc": request.copy_icc,
            "candidate_id": candidate_id,
            "raw_sha256": raw_sha256,
            "source_manifest_hash": source_manifest_hash,
            "mask_sha256": mask_sha256,
            "crop_mapping": crop_mapping,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _resolve(self, request: ExportRequest) -> ResolvedExport:
        """Validate the immutable historical lineage before writing any final pixels."""

        search = self.app_store.get_search(request.search_id)
        if search.status is not SearchStatus.ACCEPTED:
            raise ConflictError("Only an accepted search can be exported")
        if search.global_winner_id is None:
            raise ConflictError("Accepted search has no global winner")
        candidate_id = request.candidate_id or search.global_winner_id
        if candidate_id != search.global_winner_id:
            raise ConflictError("Only the historical global winner can be exported")
        candidate = next(
            (item for item in search.candidates if item.candidate_id == candidate_id), None
        )
        if candidate is None:
            raise ConflictError("Global winner is not owned by this search")

        project = self.app_store.get_project(search.project_id)
        project.source_manifest.assert_integrity()
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise ConflictError("Search source manifest no longer matches its project")
        if candidate.source_manifest_hash != search.source_manifest_hash:
            raise ConflictError("Candidate source manifest lineage does not match the search")

        for source_asset in (
            project.source_manifest.background,
            *project.source_manifest.cat_references,
        ):
            self.asset_store.assert_png_lineage_asset(source_asset)
        self.asset_store.assert_png_lineage_asset(candidate.raw_asset)
        self.asset_store.assert_png_lineage_asset(candidate.protected_asset)
        historical_composite = candidate.composite
        if historical_composite is not None:
            if historical_composite.source_background != project.source_manifest.background:
                raise ConflictError(
                    "Candidate composite background does not match the project source"
                )
            if historical_composite.raw_candidate != candidate.raw_asset:
                raise ConflictError(
                    "Candidate composite raw asset does not match candidate lineage"
                )
            if not historical_composite.outside_mask_exact:
                raise ConflictError("Candidate composite did not verify background protection")
            mask = historical_composite.mask
            self.asset_store.assert_png_lineage_asset(mask.asset)
        else:
            mask = self.composite_floor.create_mask(
                source_background=project.source_manifest.background,
                placement=search.placement,
            )
        try:
            self.composite_floor.assert_mask_matches_placement(
                source_background=project.source_manifest.background,
                placement=search.placement,
                mask=mask,
            )
        except ValueError as exc:
            raise ConflictError("Candidate composite mask does not match its lineage") from exc
        # A final export keeps an auditable mask reference even when it has to
        # regenerate the deterministic floor for an older source candidate.
        self.app_store.register_asset(mask.asset)
        source_metadata = self.composite_floor.source_metadata(
            project.source_manifest.background
        )
        crop_mapping = (
            candidate.crop_mapping.model_dump(mode="json")
            if candidate.crop_mapping is not None
            else None
        )
        return ResolvedExport(
            request=request,
            search=search,
            source_background=project.source_manifest.background,
            candidate=candidate,
            mask=mask,
            source_metadata=source_metadata,
            export_key=self._export_key(
                request=request,
                candidate_id=candidate.candidate_id,
                raw_sha256=candidate.raw_asset.sha256,
                source_manifest_hash=search.source_manifest_hash,
                mask_sha256=mask.asset.sha256,
                crop_mapping=crop_mapping,
            ),
        )

    def _validate_cached_export(
        self, *, resolved: ResolvedExport, result: ExportResult
    ) -> ExportResult:
        request = resolved.request
        if (
            result.export_key != resolved.export_key
            or result.search_id != request.search_id
            or result.candidate_id != resolved.candidate.candidate_id
            or result.source_manifest_hash != resolved.search.source_manifest_hash
            or result.format != request.format
            or result.jpeg_quality != request.canonical_jpeg_quality
            or result.copied_exif
            != (request.copy_exif and resolved.source_metadata.exif is not None)
            or result.copied_icc
            != (request.copy_icc and resolved.source_metadata.icc_profile is not None)
        ):
            raise ConflictError("Persisted export does not match the requested lineage")
        self.asset_store.assert_export_asset(result.asset)
        return result

    @staticmethod
    def _normalized_exif(
        exif_bytes: bytes,
        *,
        width: int,
        height: int,
    ) -> bytes:
        """Keep source EXIF while fencing orientation and stale pixel dimensions."""

        exif = Image.Exif()
        exif.load(exif_bytes)
        if 274 in exif:
            del exif[274]
        for tag, value in (
            (256, width),
            (257, height),
            (40962, width),
            (40963, height),
        ):
            if tag in exif:
                exif[tag] = value
        return exif.tobytes()

    def _encode_delivery_asset(
        self,
        *,
        protected_asset: AssetRef,
        request: ExportRequest,
        metadata: ImageMetadata,
    ) -> AssetRef:
        """Encode only the final delivery copy; PNG lineage is never re-used as input."""

        self.asset_store.assert_intact(protected_asset)
        with Image.open(protected_asset.filesystem_path) as opened:
            image = opened.copy()

        output = io.BytesIO()
        save_options: dict[str, object]
        mime_type: Literal["image/png", "image/jpeg"]
        if request.format == "png":
            save_options = {
                "format": "PNG",
                "compress_level": 9,
                "optimize": False,
            }
            mime_type = "image/png"
        else:
            # JPEG has no alpha channel. Composite transparent source photographs
            # over a deterministic white delivery matte instead of exposing the
            # undefined RGB channels hidden behind transparent pixels.
            if "A" in image.getbands():
                rgba = image.convert("RGBA")
                matte = Image.new("RGB", rgba.size, (255, 255, 255))
                matte.paste(rgba, mask=rgba.getchannel("A"))
                image = matte
            else:
                image = image.convert("RGB")
            save_options = {
                "format": "JPEG",
                "quality": request.jpeg_quality,
                "subsampling": 0,
                "optimize": False,
                "progressive": False,
            }
            mime_type = "image/jpeg"
        if request.copy_icc and metadata.icc_profile is not None:
            save_options["icc_profile"] = metadata.icc_profile
        if request.copy_exif and metadata.exif is not None:
            save_options["exif"] = self._normalized_exif(
                metadata.exif,
                width=image.width,
                height=image.height,
            )
        image.save(output, **save_options)  # type: ignore[arg-type]
        asset = self.asset_store.put_export_bytes(output.getvalue(), mime_type=mime_type)
        self.asset_store.assert_export_asset(asset)
        return asset

    def export_global_winner(self, request: ExportRequest) -> ExportResult:
        """Render or replay one accepted historical global-winner export.

        ``export_key`` is derived after canonicalizing an omitted candidate ID to
        the persisted global winner. Therefore an omitted ID and that explicit ID
        replay the same durable export without touching image bytes a second time.
        """

        resolved = self._resolve(request)
        cached = self.app_store.find_export(resolved.export_key)
        if cached is not None:
            return self._validate_cached_export(resolved=resolved, result=cached)

        composite = self.composite_floor.protect_candidate(
            source_manifest_hash=resolved.search.source_manifest_hash,
            source_background=resolved.source_background,
            raw_candidate=resolved.candidate.raw_asset,
            placement=resolved.search.placement,
            crop_mapping=resolved.candidate.crop_mapping,
            mask=resolved.mask,
            copy_icc=resolved.request.copy_icc,
            copy_exif=resolved.request.copy_exif,
        )
        # Composite floor intermediates remain PNG assets and are kept as auditable
        # references. They are never saved into LangGraph state by this service.
        self.app_store.register_asset(composite.protected_asset)
        self.app_store.register_asset(composite.mask.asset)
        asset = self._encode_delivery_asset(
            protected_asset=composite.protected_asset,
            request=resolved.request,
            metadata=resolved.source_metadata,
        )
        metadata = ExportMetadata(
            accepted_global_winner_id=resolved.candidate.candidate_id,
            global_winner_score=resolved.search.global_winner_score,
            source_background_asset_id=resolved.source_background.asset_id,
            raw_candidate_asset_id=resolved.candidate.raw_asset.asset_id,
            protected_candidate_asset_id=composite.protected_asset.asset_id,
            composite_mask_asset_id=composite.mask.asset.asset_id,
            composite_mask_sha256=composite.mask.asset.sha256,
        )
        result = ExportResult(
            export_key=resolved.export_key,
            search_id=resolved.search.search_id,
            candidate_id=resolved.candidate.candidate_id,
            source_manifest_hash=resolved.search.source_manifest_hash,
            format=resolved.request.format,
            jpeg_quality=resolved.request.canonical_jpeg_quality,
            asset=asset,
            composite=composite,
            copied_icc=(
                resolved.request.copy_icc and resolved.source_metadata.icc_profile is not None
            ),
            copied_exif=(
                resolved.request.copy_exif and resolved.source_metadata.exif is not None
            ),
            metadata=metadata,
        )
        persisted = self.app_store.record_export(result)
        return self._validate_cached_export(resolved=resolved, result=persisted)
