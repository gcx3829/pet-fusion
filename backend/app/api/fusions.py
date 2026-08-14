from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Path, UploadFile, status

from app.api.dependencies import ContainerDependency
from app.domain.errors import UploadValidationError
from app.domain.fusions import FusionMaskResponse, FusionResponse, FusionSubmission

router = APIRouter(tags=["fusions"])
SearchIdPath = Annotated[str, Path(pattern=r"^search_[0-9a-f]{32}$")]
FusionKeyPath = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


@router.post(
    "/searches/{search_id}/fusion-masks",
    response_model=FusionMaskResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Search was not found"},
        status.HTTP_409_CONFLICT: {"description": "Search or source lineage conflict"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid alpha-mask upload"},
    },
)
async def upload_fusion_mask(
    search_id: SearchIdPath,
    mask: Annotated[UploadFile, File()],
    container: ContainerDependency,
) -> FusionMaskResponse:
    try:
        content = await mask.read(container.settings.max_upload_bytes + 1)
    finally:
        await mask.close()
    if not content:
        raise UploadValidationError(f"{mask.filename or 'Fusion Mask'} is empty")
    if len(content) > container.settings.max_upload_bytes:
        raise UploadValidationError(
            f"{mask.filename or 'Fusion Mask'} exceeds the "
            f"{container.settings.max_upload_bytes} byte limit"
        )
    source_manifest_hash, asset = await asyncio.to_thread(
        container.fusion_service.register_alpha_mask,
        search_id=search_id,
        png_bytes=content,
    )
    return FusionMaskResponse.from_asset(
        search_id=search_id,
        source_manifest_hash=source_manifest_hash,
        asset=asset,
    )


@router.post(
    "/searches/{search_id}/fusions",
    response_model=FusionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "Search or registered Fusion Mask was not found"
        },
        status.HTTP_409_CONFLICT: {"description": "Fusion state or lineage conflict"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid Fusion request"},
    },
)
def create_fusion(
    search_id: SearchIdPath,
    request: FusionSubmission,
    container: ContainerDependency,
) -> FusionResponse:
    result = container.fusion_service.create(request.to_command(search_id=search_id))
    return FusionResponse.from_result(result)


@router.get(
    "/searches/{search_id}/fusions/{fusion_key}",
    response_model=FusionResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Fusion was not found"},
        status.HTTP_409_CONFLICT: {"description": "Persisted Fusion lineage conflict"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Invalid resource key"},
    },
)
def get_fusion(
    search_id: SearchIdPath,
    fusion_key: FusionKeyPath,
    container: ContainerDependency,
) -> FusionResponse:
    return FusionResponse.from_result(
        container.fusion_service.get(search_id=search_id, fusion_key=fusion_key)
    )
