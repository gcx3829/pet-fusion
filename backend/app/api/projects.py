from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile, status

from app.api.dependencies import ContainerDependency
from app.domain.assets import AssetRef, SourceManifest
from app.domain.errors import ErrorEnvelope, UploadValidationError
from app.domain.guidance_masks import GuidanceMaskResponse
from app.domain.projects import ProjectRecord, ProjectResponse
from app.persistence.app_store import utcnow

router = APIRouter(prefix="/projects", tags=["projects"])


async def _read_upload(upload: UploadFile, *, max_bytes: int) -> bytes:
    try:
        content = await upload.read(max_bytes + 1)
    finally:
        await upload.close()
    if not content:
        raise UploadValidationError(f"{upload.filename or 'upload'} is empty")
    if len(content) > max_bytes:
        raise UploadValidationError(
            f"{upload.filename or 'upload'} exceeds the {max_bytes} byte limit"
        )
    return content


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    container: ContainerDependency,
    background: Annotated[UploadFile, File()],
    cat_references: Annotated[list[UploadFile], File()],
    cat_name: Annotated[str | None, Form(max_length=200)] = None,
    cat_traits: Annotated[str | None, Form(max_length=4000)] = None,
) -> ProjectResponse:
    if not 1 <= len(cat_references) <= 5:
        raise UploadValidationError("Upload between 1 and 5 cat reference images")

    assets: list[AssetRef] = []
    total_bytes = 0
    total_pixels = 0
    for upload in (background, *cat_references):
        raw_image = await _read_upload(
            upload, max_bytes=container.settings.max_upload_bytes
        )
        total_bytes += len(raw_image)
        if total_bytes > container.settings.max_total_upload_bytes:
            raise UploadValidationError(
                "Combined uploads exceed the total byte safety limit"
            )
        normalized = container.asset_store.normalize_image(raw_image)
        total_pixels += normalized.width * normalized.height
        if total_pixels > container.settings.max_total_image_pixels:
            raise UploadValidationError(
                "Combined uploads exceed the total pixel safety limit"
            )
        assets.append(container.asset_store.put_normalized(normalized))
    manifest = SourceManifest.create(background=assets[0], cat_references=assets[1:])
    manifest.assert_integrity()
    project = ProjectRecord(
        project_id=f"proj_{uuid4().hex}",
        cat_name=cat_name.strip() if cat_name and cat_name.strip() else None,
        cat_traits=cat_traits.strip() if cat_traits and cat_traits.strip() else None,
        source_manifest=manifest,
        created_at=utcnow(),
    )
    container.app_store.create_project(project)
    return ProjectResponse.from_record(project)


@router.post(
    "/{project_id}/guidance-masks",
    response_model=GuidanceMaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Project was not found",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Project source lineage conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Invalid Guidance Mask upload"
        },
    },
)
async def register_guidance_mask(
    project_id: str,
    mask: Annotated[UploadFile, File()],
    container: ContainerDependency,
) -> GuidanceMaskResponse:
    """Register one same-size alpha PNG against this project's source manifest."""

    project = container.app_store.get_project(project_id)
    content = await _read_upload(mask, max_bytes=container.settings.max_upload_bytes)
    normalized = await asyncio.to_thread(
        container.asset_store.normalize_guidance_mask,
        content,
    )
    background = project.source_manifest.background
    if (normalized.width, normalized.height) != (background.width, background.height):
        raise UploadValidationError(
            "Guidance Mask dimensions must match the source background"
        )
    asset = await asyncio.to_thread(container.asset_store.put_normalized, normalized)
    container.asset_store.assert_png_lineage_asset(asset)
    container.app_store.register_asset(asset)
    binding = container.app_store.register_guidance_mask(
        project_id=project.project_id,
        source_manifest_hash=project.source_manifest.manifest_hash,
        asset=asset,
    )
    return GuidanceMaskResponse.from_binding(binding)


@router.get(
    "/{project_id}/guidance-masks",
    response_model=list[GuidanceMaskResponse],
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Project was not found",
        }
    },
)
def list_guidance_masks(
    project_id: str,
    container: ContainerDependency,
) -> list[GuidanceMaskResponse]:
    return [
        GuidanceMaskResponse.from_binding(binding)
        for binding in container.app_store.list_guidance_masks(project_id=project_id)
    ]
