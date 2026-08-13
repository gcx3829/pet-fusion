from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.dependencies import ContainerDependency
from app.domain.exports import ExportResponse, ExportSubmission

router = APIRouter(tags=["exports"])
SearchIdPath = Annotated[str, Path(min_length=1, max_length=120)]
ExportKeyPath = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


@router.post(
    "/searches/{search_id}/export",
    response_model=ExportResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_export(
    search_id: SearchIdPath,
    request: ExportSubmission,
    container: ContainerDependency,
) -> ExportResponse:
    result = container.export_service.export_global_winner(
        request.to_command(search_id=search_id)
    )
    return ExportResponse.from_result(result)


@router.get(
    "/searches/{search_id}/exports/{export_key}",
    response_model=ExportResponse,
)
def get_export(
    search_id: SearchIdPath,
    export_key: ExportKeyPath,
    container: ContainerDependency,
) -> ExportResponse:
    result = container.app_store.get_export(search_id=search_id, export_key=export_key)
    container.asset_store.assert_export_asset(result.asset)
    return ExportResponse.from_result(result)
