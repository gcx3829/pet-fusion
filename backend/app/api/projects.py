from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, UploadFile, status

from app.api.dependencies import ContainerDependency
from app.domain.assets import AssetRef, SourceManifest
from app.domain.errors import UploadValidationError
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
