from __future__ import annotations

import hashlib
import json

from app.domain.errors import ConflictError
from app.domain.exports import ExportRequest, ExportResult
from app.domain.searches import SearchStatus
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import CompositeFloorService

EXPORT_SCHEMA_VERSION = "export/v1"


class ExportService:
    """Create a guarded, reproducible final PNG from an accepted global winner."""

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

    def export_global_winner(self, request: ExportRequest) -> ExportResult:
        """Re-apply the composite floor before a content-addressed final export.

        The current MVP deliberately permits only an accepted search's historical
        global winner. This prevents users or callers from silently exporting a
        lower-ranked candidate or one with a mismatched immutable-source lineage.
        """

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
        self.asset_store.assert_intact(project.source_manifest.background)
        self.asset_store.assert_intact(candidate.raw_asset)

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
            mask = historical_composite.mask
        else:
            mask = None
        metadata = self.composite_floor.source_metadata(project.source_manifest.background)
        composite = self.composite_floor.protect_candidate(
            source_manifest_hash=search.source_manifest_hash,
            source_background=project.source_manifest.background,
            raw_candidate=candidate.raw_asset,
            placement=search.placement,
            crop_mapping=candidate.crop_mapping,
            mask=mask,
            copy_icc=request.copy_icc,
            copy_exif=request.copy_exif,
        )
        return ExportResult(
            export_key=self._export_key(
                request=request,
                candidate_id=candidate.candidate_id,
                raw_sha256=candidate.raw_asset.sha256,
                source_manifest_hash=search.source_manifest_hash,
                mask_sha256=composite.mask.asset.sha256,
                crop_mapping=(
                    candidate.crop_mapping.model_dump(mode="json")
                    if candidate.crop_mapping is not None
                    else None
                ),
            ),
            search_id=search.search_id,
            candidate_id=candidate.candidate_id,
            source_manifest_hash=search.source_manifest_hash,
            asset=composite.protected_asset,
            composite=composite,
            copied_icc=request.copy_icc and metadata.icc_profile is not None,
            copied_exif=request.copy_exif and metadata.exif is not None,
        )
