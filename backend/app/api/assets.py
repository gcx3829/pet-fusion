from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.dependencies import ContainerDependency

router = APIRouter(prefix="/assets", tags=["assets"])


@router.get("/{asset_id}", response_class=FileResponse)
async def get_asset(asset_id: str, container: ContainerDependency) -> FileResponse:
    asset = container.app_store.get_asset(asset_id)
    container.asset_store.assert_intact(asset)
    return FileResponse(
        path=asset.path,
        media_type="image/png",
        headers={
            "ETag": f'"sha256-{asset.sha256}"',
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
