"""HTTP adapters for the isolated, bounded Local Fix graph."""

from __future__ import annotations

import asyncio
from typing import Annotated, cast

from fastapi import APIRouter, Path, status
from langchain_core.runnables import RunnableConfig

from app.api.dependencies import ContainerDependency
from app.domain.compositing import Mask
from app.domain.errors import ConflictError
from app.domain.local_fixes import (
    LocalFixRequest,
    LocalFixResponse,
    LocalFixResult,
    LocalFixSubmission,
)
from app.domain.searches import SearchStatus
from app.graphs.checkpointer import sqlite_checkpointer
from app.graphs.local_fix_graph import (
    LOCAL_FIX_STATE_SCHEMA_VERSION,
    LocalFixState,
    build_local_fix_subgraph,
)

router = APIRouter(tags=["local-fixes"])
SearchIdPath = Annotated[str, Path(pattern=r"^search_[0-9a-f]{32}$")]
FixIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]


def _request_from_submission(
    *,
    search_id: str,
    submission: LocalFixSubmission,
    container: ContainerDependency,
) -> tuple[LocalFixRequest, LocalFixResult | None]:
    """Derive trusted Local Fix input without accepting client lineage claims."""

    search = container.app_store.get_search(search_id)
    if search.status is not SearchStatus.ACCEPTED:
        raise ConflictError("Local Fix requires an accepted search")
    candidate_id = submission.candidate_id or search.global_winner_id
    if candidate_id is None:
        raise ConflictError("Local Fix requires an accepted search with a global winner")

    historical_candidate = next(
        (candidate for candidate in search.candidates if candidate.candidate_id == candidate_id),
        None,
    )
    previous_result = None
    if historical_candidate is None:
        previous_result = container.app_store.find_applied_local_fix_by_candidate(
            search_id=search_id,
            candidate_id=candidate_id,
        )
        if previous_result is not None:
            previous_result = container.local_fix_service.sanitize_persisted_result(
                previous_result
            )
        if previous_result is None or previous_result.candidate is None:
            raise ConflictError("Local Fix base candidate is not available for this search")
        generation_depth = previous_result.candidate.generation_depth
    else:
        generation_depth = historical_candidate.generation_depth

    if submission.tight_mask.asset_id is not None:
        mask_asset = container.app_store.get_asset(submission.tight_mask.asset_id)
        # A Local Fix mask is an internal working PNG, never a delivery export.
        # The service subsequently verifies dimensions, declared support, and
        # containment inside the original candidate composite floor.
        container.asset_store.assert_png_lineage_asset(mask_asset)
        tight_mask = Mask(
            asset=mask_asset,
            coordinate_space=submission.tight_mask.coordinate_space,
            allowed_box=submission.tight_mask.allowed_box,
            feather_radius_px=submission.tight_mask.feather_radius_px,
        )
    else:
        project = container.app_store.get_project(search.project_id)
        project.source_manifest.assert_integrity()
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise ConflictError("Search source manifest no longer matches its project")
        try:
            tight_mask = container.local_fix_service.composite_floor.create_mask_for_box(
                source_background=project.source_manifest.background,
                allowed_box=submission.tight_mask.allowed_box,
                feather_radius_px=submission.tight_mask.feather_radius_px,
            )
        except ValueError as exc:
            raise ConflictError("Local Fix tight mask bounds exceed the source image") from exc
        container.app_store.register_asset(tight_mask.asset)
    request = LocalFixRequest(
        search_id=search_id,
        base_candidate_id=candidate_id,
        source_manifest_hash=search.source_manifest_hash,
        tight_mask=tight_mask,
        instruction=submission.instruction,
        generation_depth=generation_depth,
    )
    return request, previous_result


async def _invoke_local_fix_graph(
    *,
    request: LocalFixRequest,
    previous_result: LocalFixResult | None,
    container: ContainerDependency,
) -> LocalFixResult:
    """Execute the independent graph under a stable, replay-safe checkpoint key."""

    # Resolve before selecting the graph thread ID so each semantic request has a
    # stable checkpoint lineage. ``resolve`` has no provider/image generation side
    # effects; it repeats inside the graph as its explicit first node.
    resolution = await asyncio.to_thread(
        container.local_fix_service.resolve,
        request,
        previous_result=previous_result,
    )
    request_key = container.local_fix_service.build_request_key(resolution)
    initial: LocalFixState = {
        "schema_version": LOCAL_FIX_STATE_SCHEMA_VERSION,
        "request": request.model_dump(mode="json"),
        "previous_result": (
            previous_result.model_dump(mode="json") if previous_result is not None else None
        ),
    }
    config: RunnableConfig = {
        "configurable": {"thread_id": f"local-fix-{request_key}"},
    }
    async with sqlite_checkpointer(container.settings.resolved_checkpoint_db_path) as checkpointer:
        await checkpointer.setup()
        graph = build_local_fix_subgraph(container.local_fix_service).compile(
            checkpointer=checkpointer
        )
        output = cast(
            LocalFixState,
            await graph.ainvoke(initial, config=config, version="v1"),
        )
    raw_result = output.get("result")
    if not isinstance(raw_result, dict):
        raise ConflictError("Local Fix graph returned no structured result")
    try:
        return LocalFixResult.model_validate(raw_result)
    except ValueError as exc:
        raise ConflictError("Local Fix graph returned an invalid structured result") from exc


@router.post(
    "/searches/{search_id}/local-fixes",
    response_model=LocalFixResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Search or mask asset was not found"},
        status.HTTP_409_CONFLICT: {"description": "Local Fix lineage or state conflict"},
    },
)
async def create_local_fix(
    search_id: SearchIdPath,
    submission: LocalFixSubmission,
    container: ContainerDependency,
) -> LocalFixResponse:
    request, previous_result = _request_from_submission(
        search_id=search_id,
        submission=submission,
        container=container,
    )
    result = await _invoke_local_fix_graph(
        request=request,
        previous_result=previous_result,
        container=container,
    )
    candidate = result.candidate or result.fallback_candidate
    container.app_store.emit_event(
        search_id=search_id,
        event_key=f"local-fix:{result.request_key}",
        event_type=f"local_fix.{result.outcome.value}",
        payload={
            "fix_id": result.request_key,
            "base_candidate_id": result.base_candidate_id,
            "candidate_id": candidate.candidate_id,
            "generation_depth": result.generation_depth,
            "asset_id": candidate.protected_asset.asset_id,
        },
    )
    return LocalFixResponse.from_result(result)


@router.get(
    "/searches/{search_id}/local-fixes/{fix_id}",
    response_model=LocalFixResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Local Fix was not found"},
        status.HTTP_409_CONFLICT: {"description": "Local Fix is incomplete or invalid"},
    },
)
def get_local_fix(
    search_id: SearchIdPath,
    fix_id: FixIdPath,
    container: ContainerDependency,
) -> LocalFixResponse:
    result = container.app_store.get_local_fix_result(search_id=search_id, fix_id=fix_id)
    result = container.local_fix_service.sanitize_persisted_result(result)
    return LocalFixResponse.from_result(result)
