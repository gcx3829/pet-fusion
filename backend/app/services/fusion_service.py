from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass

from PIL import Image, ImageFilter, UnidentifiedImageError

from app.domain.assets import AssetRef
from app.domain.candidates import CandidateRecord
from app.domain.compositing import Mask, PixelBox
from app.domain.errors import (
    ConflictError,
    NotFoundError,
    SourceManifestMismatchError,
    UploadValidationError,
)
from app.domain.fusions import FusionBox, FusionRequest, FusionResult
from app.domain.projects import ProjectRecord
from app.domain.searches import SearchRunRecord, SearchStatus
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import CompositeFloorService

FUSION_SCHEMA_VERSION = "fusion/v2"


@dataclass(frozen=True, slots=True)
class FusionResolution:
    search: SearchRunRecord
    project: ProjectRecord
    candidate: CandidateRecord
    mask: Mask
    input_mask_asset: AssetRef | None
    fusion_key: str


class FusionService:
    """Render an explicit, user-authored Fusion Mask without changing Search."""

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
    def _pixel_box(box: FusionBox, *, width: int, height: int) -> PixelBox:
        left = int(box.x * width)
        top = int(box.y * height)
        right = min(width, max(left + 1, int((box.x + box.width) * width + 0.999999)))
        bottom = min(height, max(top + 1, int((box.y + box.height) * height + 0.999999)))
        return PixelBox(x=left, y=top, width=right - left, height=bottom - top)

    def register_alpha_mask(self, *, search_id: str, png_bytes: bytes) -> tuple[str, AssetRef]:
        """Canonicalize and bind a user PNG alpha mask to one accepted Search.

        Only the alpha plane is retained.  This prevents source photos, raw
        candidates, and another Search's mask from being reinterpreted as an
        editable region merely because their content-addressed asset ID is known.
        """

        search = self.app_store.get_search(search_id)
        if search.status is not SearchStatus.ACCEPTED:
            raise ConflictError("Fusion Mask upload requires an accepted search")
        project = self.app_store.get_project(search.project_id)
        project.source_manifest.assert_integrity()
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise SourceManifestMismatchError(
                "Fusion Mask source manifest no longer matches the search"
            )
        source_background = project.source_manifest.background
        self.asset_store.assert_png_lineage_asset(source_background)
        try:
            with Image.open(io.BytesIO(png_bytes)) as opened:
                if opened.format != "PNG":
                    raise UploadValidationError("Fusion Mask must be a PNG image")
                if getattr(opened, "is_animated", False):
                    raise UploadValidationError("Animated Fusion Masks are not supported")
                if "A" not in opened.getbands():
                    raise UploadValidationError("Fusion Mask PNG must contain an alpha channel")
                if opened.size != (source_background.width, source_background.height):
                    raise UploadValidationError(
                        "Fusion Mask dimensions must match the source background"
                    )
                alpha = opened.getchannel("A")
                alpha.load()
        except UploadValidationError:
            raise
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise UploadValidationError("Fusion Mask is not a decodable PNG") from exc
        if alpha.getbbox() is None:
            raise UploadValidationError("Fusion Mask must contain a non-zero alpha pixel")

        canonical = Image.new("RGBA", alpha.size, (255, 255, 255, 0))
        canonical.putalpha(alpha)
        output = io.BytesIO()
        canonical.save(output, format="PNG", compress_level=9, optimize=False)
        asset = self.asset_store.put_image_bytes(output.getvalue())
        self.asset_store.assert_png_lineage_asset(asset)
        self.app_store.register_asset(asset)
        self.app_store.register_fusion_mask(
            search_id=search_id,
            source_manifest_hash=search.source_manifest_hash,
            asset=asset,
        )
        return search.source_manifest_hash, asset

    def _mask_from_asset(
        self,
        *,
        search_id: str,
        source_manifest_hash: str,
        source_background: AssetRef,
        mask_asset_id: str,
        feather_radius_px: int,
    ) -> tuple[Mask, AssetRef]:
        mask_asset = self.app_store.get_fusion_mask(
            search_id=search_id,
            source_manifest_hash=source_manifest_hash,
            asset_id=mask_asset_id,
        )
        self.asset_store.assert_png_lineage_asset(mask_asset)
        if (mask_asset.width, mask_asset.height) != (
            source_background.width,
            source_background.height,
        ):
            raise ConflictError("Fusion Mask dimensions must match the source background")
        try:
            with Image.open(mask_asset.filesystem_path) as opened:
                if "A" not in opened.getbands():
                    raise ConflictError("Registered Fusion Mask lost its alpha channel")
                alpha = opened.getchannel("A")
                alpha.load()
                if feather_radius_px:
                    alpha = alpha.filter(ImageFilter.GaussianBlur(feather_radius_px))
                bbox = alpha.getbbox()
        except ConflictError:
            raise
        except (OSError, UnidentifiedImageError) as exc:
            raise ConflictError("Fusion Mask is not a decodable PNG") from exc
        if bbox is None:
            raise ConflictError("Fusion Mask must contain at least one non-zero pixel")
        output = io.BytesIO()
        alpha.save(output, format="PNG", compress_level=9, optimize=False)
        normalized = self.asset_store.put_image_bytes(output.getvalue())
        self.app_store.register_asset(normalized)
        return (
            Mask(
                asset=normalized,
                allowed_box=PixelBox(
                    x=bbox[0],
                    y=bbox[1],
                    width=bbox[2] - bbox[0],
                    height=bbox[3] - bbox[1],
                ),
                feather_radius_px=feather_radius_px,
                mask_scope="placement",
            ),
            mask_asset,
        )

    def _resolve_mask(
        self,
        request: FusionRequest,
        *,
        source_manifest_hash: str,
        source_background: AssetRef,
    ) -> tuple[Mask, AssetRef | None]:
        if request.box is not None:
            allowed_box = self._pixel_box(
                request.box,
                width=source_background.width,
                height=source_background.height,
            )
            mask = self.composite_floor.create_mask_for_box(
                source_background=source_background,
                allowed_box=allowed_box,
                feather_radius_px=request.feather_radius_px,
                mask_scope="placement",
            )
            self.app_store.register_asset(mask.asset)
            return mask, None
        assert request.mask_asset_id is not None
        return self._mask_from_asset(
            search_id=request.search_id,
            source_manifest_hash=source_manifest_hash,
            source_background=source_background,
            mask_asset_id=request.mask_asset_id,
            feather_radius_px=request.feather_radius_px,
        )

    @staticmethod
    def _fusion_key(
        *,
        request: FusionRequest,
        candidate_id: str,
        source_manifest_hash: str,
        source_background_sha256: str,
        raw_sha256: str,
        mask_sha256: str,
        input_mask_sha256: str | None,
        crop_mapping: object,
    ) -> str:
        payload = {
            "schema_version": FUSION_SCHEMA_VERSION,
            "search_id": request.search_id,
            "candidate_id": candidate_id,
            "source_manifest_hash": source_manifest_hash,
            "source_background_sha256": source_background_sha256,
            "raw_sha256": raw_sha256,
            "mask_sha256": mask_sha256,
            "input_mask_sha256": input_mask_sha256,
            "box": request.box.model_dump(mode="json") if request.box else None,
            "mask_asset_id": request.mask_asset_id,
            "feather_radius_px": request.feather_radius_px,
            "crop_mapping": crop_mapping,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _resolve(self, request: FusionRequest) -> FusionResolution:
        search = self.app_store.get_search(request.search_id)
        if search.status is not SearchStatus.ACCEPTED:
            raise ConflictError("Fusion is available only after the search is accepted")
        if search.global_winner_id is None:
            raise ConflictError("Accepted search has no selected candidate")
        candidate_id = request.candidate_id or search.global_winner_id
        candidate = next(
            (item for item in search.candidates if item.candidate_id == candidate_id), None
        )
        if candidate is None:
            raise ConflictError("Fusion candidate does not belong to this search")
        if candidate.generation_depth != 0:
            raise ConflictError("Fusion accepts only raw Search candidates")
        project = self.app_store.get_project(search.project_id)
        project.source_manifest.assert_integrity()
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise SourceManifestMismatchError("Fusion source manifest no longer matches the search")
        if candidate.source_manifest_hash != search.source_manifest_hash:
            raise SourceManifestMismatchError("Fusion candidate lineage does not match the search")
        self.asset_store.assert_png_lineage_asset(project.source_manifest.background)
        self.asset_store.assert_png_lineage_asset(candidate.raw_asset)
        mask, input_mask_asset = self._resolve_mask(
            request,
            source_manifest_hash=search.source_manifest_hash,
            source_background=project.source_manifest.background,
        )
        crop_mapping = (
            candidate.crop_mapping.model_dump(mode="json")
            if candidate.crop_mapping
            else None
        )
        fusion_key = self._fusion_key(
            request=request,
            candidate_id=candidate.candidate_id,
            source_manifest_hash=search.source_manifest_hash,
            source_background_sha256=project.source_manifest.background.sha256,
            raw_sha256=candidate.raw_asset.sha256,
            mask_sha256=mask.asset.sha256,
            input_mask_sha256=(
                input_mask_asset.sha256 if input_mask_asset is not None else None
            ),
            crop_mapping=crop_mapping,
        )
        return FusionResolution(
            search=search,
            project=project,
            candidate=candidate,
            mask=mask,
            input_mask_asset=input_mask_asset,
            fusion_key=fusion_key,
        )

    def create(self, request: FusionRequest) -> FusionResult:
        resolved = self._resolve(request)
        existing = self.app_store.find_fusion(resolved.fusion_key)
        if existing is not None:
            self._assert_result_intact(existing, expected=resolved)
            return existing
        try:
            composite = self.composite_floor.protect_candidate(
                source_manifest_hash=resolved.search.source_manifest_hash,
                source_background=resolved.project.source_manifest.background,
                raw_candidate=resolved.candidate.raw_asset,
                placement=resolved.search.placement,
                crop_mapping=resolved.candidate.crop_mapping,
                mask=resolved.mask,
            )
        except ValueError as exc:
            raise ConflictError("Fusion candidate or crop lineage is invalid") from exc
        self.app_store.register_asset(composite.protected_asset)
        result = FusionResult(
            key_schema_version=FUSION_SCHEMA_VERSION,
            fusion_key=resolved.fusion_key,
            search_id=resolved.search.search_id,
            candidate_id=resolved.candidate.candidate_id,
            source_manifest_hash=resolved.search.source_manifest_hash,
            raw_asset=resolved.candidate.raw_asset,
            fusion_asset=composite.protected_asset,
            mask=resolved.mask.asset,
            input_mask_asset=resolved.input_mask_asset,
            feather_radius_px=request.feather_radius_px,
            box=request.box,
            crop_mapping=resolved.candidate.crop_mapping,
            composite=composite,
        )
        persisted = self.app_store.record_fusion(result)
        self._assert_result_intact(persisted, expected=resolved)
        return persisted

    def get(self, *, search_id: str, fusion_key: str) -> FusionResult:
        result = self.app_store.get_fusion(search_id=search_id, fusion_key=fusion_key)
        self._assert_result_intact(result)
        return result

    def _assert_result_intact(
        self,
        result: FusionResult,
        *,
        expected: FusionResolution | None = None,
    ) -> None:
        search = self.app_store.get_search(result.search_id)
        if search.status is not SearchStatus.ACCEPTED:
            raise ConflictError("Fusion result no longer belongs to an accepted search")
        candidate = next(
            (
                item
                for item in search.candidates
                if item.candidate_id == result.candidate_id
            ),
            None,
        )
        if candidate is None or candidate.generation_depth != 0:
            raise ConflictError("Fusion result no longer belongs to a raw Search candidate")
        project = self.app_store.get_project(search.project_id)
        project.source_manifest.assert_integrity()
        if (
            search.source_manifest_hash != result.source_manifest_hash
            or project.source_manifest.manifest_hash != result.source_manifest_hash
            or project.source_manifest.background != result.composite.source_background
            or candidate.source_manifest_hash != result.source_manifest_hash
            or candidate.raw_asset != result.raw_asset
            or candidate.crop_mapping != result.crop_mapping
        ):
            raise ConflictError("Fusion persisted lineage no longer matches Search authority")
        if (
            result.key_schema_version == FUSION_SCHEMA_VERSION
            and result.input_mask_asset is not None
        ):
            try:
                bound_mask = self.app_store.get_fusion_mask(
                    search_id=result.search_id,
                    source_manifest_hash=result.source_manifest_hash,
                    asset_id=result.input_mask_asset.asset_id,
                )
            except NotFoundError as exc:
                raise ConflictError("Fusion input mask binding is unavailable") from exc
            if bound_mask != result.input_mask_asset:
                raise ConflictError("Fusion input mask binding changed")

        if result.key_schema_version == FUSION_SCHEMA_VERSION:
            request = FusionRequest(
                search_id=result.search_id,
                candidate_id=result.candidate_id,
                mask_asset_id=(
                    result.input_mask_asset.asset_id
                    if result.input_mask_asset is not None
                    else None
                ),
                box=result.box,
                feather_radius_px=result.feather_radius_px,
            )
            recomputed_key = self._fusion_key(
                request=request,
                candidate_id=result.candidate_id,
                source_manifest_hash=result.source_manifest_hash,
                source_background_sha256=result.composite.source_background.sha256,
                raw_sha256=result.raw_asset.sha256,
                mask_sha256=result.mask.sha256,
                input_mask_sha256=(
                    result.input_mask_asset.sha256
                    if result.input_mask_asset is not None
                    else None
                ),
                crop_mapping=(
                    result.crop_mapping.model_dump(mode="json")
                    if result.crop_mapping is not None
                    else None
                ),
            )
            if recomputed_key != result.fusion_key:
                raise ConflictError("Fusion key no longer matches its persisted lineage")
        if expected is not None and (
            result.fusion_key != expected.fusion_key
            or result.search_id != expected.search.search_id
            or result.candidate_id != expected.candidate.candidate_id
            or result.raw_asset != expected.candidate.raw_asset
            or result.mask != expected.mask.asset
            or result.input_mask_asset != expected.input_mask_asset
            or result.crop_mapping != expected.candidate.crop_mapping
        ):
            raise ConflictError("Fusion replay does not match the requested lineage")

        self.asset_store.assert_png_lineage_asset(result.composite.source_background)
        self.asset_store.assert_png_lineage_asset(result.raw_asset)
        self.asset_store.assert_png_lineage_asset(result.mask)
        self.asset_store.assert_png_lineage_asset(result.fusion_asset)
        if result.input_mask_asset is not None:
            self.asset_store.assert_png_lineage_asset(result.input_mask_asset)
        if result.composite.raw_candidate != result.raw_asset:
            raise ConflictError("Fusion raw lineage is inconsistent")
        if result.composite.protected_asset != result.fusion_asset:
            raise ConflictError("Fusion output lineage is inconsistent")
        if not result.composite.outside_mask_exact:
            raise ConflictError("Fusion output lacks exact outside-mask protection")
        with (
            Image.open(result.composite.source_background.filesystem_path) as background,
            Image.open(result.fusion_asset.filesystem_path) as fused,
            Image.open(result.mask.filesystem_path) as mask,
        ):
            mask_pixels = mask.convert("L")
            support = mask_pixels.getbbox()
            declared = result.composite.mask.allowed_box
            expected_support = (
                declared.x,
                declared.y,
                declared.right,
                declared.bottom,
            )
            if support != expected_support:
                raise ConflictError("Fusion mask pixels do not match their declared bounds")
            if not self.composite_floor.outside_mask_is_exact(
                background=background,
                protected=fused,
                mask=mask_pixels,
            ):
                raise ConflictError("Fusion output changed pixels outside its mask")
