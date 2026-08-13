from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, status

from app.api.dependencies import ContainerDependency
from app.domain.errors import ConflictError, UnsupportedMilestoneActionError
from app.domain.searches import (
    CreateSearchRequest,
    CreateSearchResponse,
    ResumeSearchRequest,
    SearchResponse,
    SearchStatus,
)

router = APIRouter(tags=["searches"])


async def _run_inline_search(container: object, search_id: str) -> None:
    from app.container import AppContainer

    resolved = container
    if not isinstance(resolved, AppContainer):
        raise TypeError("Inline search requires an application container")
    worker_id = f"inline-{uuid4().hex}"
    lease_seconds = resolved.settings.worker_lease_seconds
    if resolved.app_store.claim_search(
        search_id=search_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    ):
        await resolved.search_runner.run_with_lease(
            search_id=search_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )


@router.post(
    "/projects/{project_id}/searches",
    response_model=CreateSearchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_search(
    project_id: str,
    request: CreateSearchRequest,
    background_tasks: BackgroundTasks,
    container: ContainerDependency,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=8,
            max_length=200,
            description="Stable key for safely retrying this search submission",
        ),
    ],
) -> CreateSearchResponse:
    project = container.app_store.get_project(project_id)
    search_id = f"search_{uuid4().hex}"
    search = container.app_store.create_search(
        search_id=search_id,
        thread_id=search_id,
        project=project,
        request=request,
        idempotency_key=idempotency_key,
    )
    container.app_store.emit_event(
        search_id=search.search_id,
        event_key="search:queued",
        event_type="search.queued",
        payload={"project_id": project_id},
    )
    response = CreateSearchResponse.from_record(search)
    if container.settings.run_inline:
        background_tasks.add_task(_run_inline_search, container, search.search_id)
    return response


@router.get("/searches/{search_id}", response_model=SearchResponse)
async def get_search(search_id: str, container: ContainerDependency) -> SearchResponse:
    return SearchResponse.from_record(container.app_store.get_search(search_id))


@router.post("/searches/{search_id}/resume", response_model=SearchResponse)
async def resume_search(
    search_id: str,
    request: ResumeSearchRequest,
    background_tasks: BackgroundTasks,
    container: ContainerDependency,
) -> SearchResponse:
    search = container.app_store.get_search(search_id)
    if request.action == "cancel":
        if search.status in {SearchStatus.ACCEPTED, SearchStatus.FAILED}:
            raise ConflictError(f"Cannot cancel a search with status {search.status.value}")
        container.app_store.cancel_search(search_id)
        refreshed = container.app_store.get_search(search_id)
        if refreshed.status in {SearchStatus.ACCEPTED, SearchStatus.FAILED}:
            raise ConflictError(f"Cannot cancel a search with status {refreshed.status.value}")
        container.app_store.emit_event(
            search_id=search_id,
            event_key="search:cancelled",
            event_type="search.cancelled",
            payload={"reason": "cancelled_by_user"},
        )
        return SearchResponse.from_record(container.app_store.get_search(search_id))

    if request.action == "accept_global_winner":
        if search.status is SearchStatus.ACCEPTED:
            return SearchResponse.from_record(search)
        if search.status is not SearchStatus.WAITING_FOR_HUMAN:
            raise ConflictError(
                f"Cannot accept a search with status {search.status.value}"
            )
        if search.global_winner_id is None:
            raise ConflictError("Cannot accept without a global winner")
        if not any(
            candidate.candidate_id == search.global_winner_id for candidate in search.candidates
        ):
            raise ConflictError("Global winner is not a candidate in this search")
        if not container.app_store.accept_search(search_id):
            refreshed = container.app_store.get_search(search_id)
            if refreshed.status is SearchStatus.ACCEPTED:
                return SearchResponse.from_record(refreshed)
            raise ConflictError(
                f"Cannot accept a search with status {refreshed.status.value}"
            )
        container.app_store.emit_event(
            search_id=search_id,
            event_key="search:accepted",
            event_type="search.accepted",
            payload={
                "global_winner_id": search.global_winner_id,
                "global_winner_score": search.global_winner_score,
            },
        )
        return SearchResponse.from_record(container.app_store.get_search(search_id))

    if request.action == "continue_one_round":
        if search.status is not SearchStatus.WAITING_FOR_HUMAN:
            raise ConflictError(
                f"Continue is only valid from waiting_for_human, got {search.status.value}"
            )
        if not container.app_store.queue_next_round(search_id):
            raise ConflictError("Search has reached its maximum configured rounds")
        queued = container.app_store.get_search(search_id)
        if container.settings.run_inline:
            background_tasks.add_task(_run_inline_search, container, search_id)
        return SearchResponse.from_record(queued)

    raise UnsupportedMilestoneActionError(
        "The mocked vertical slice supports accept_global_winner, continue_one_round, and cancel"
    )
