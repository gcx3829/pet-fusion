from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Header, status

from app.api.dependencies import ContainerDependency
from app.domain.errors import (
    ConflictError,
    ErrorEnvelope,
    UnsupportedMilestoneActionError,
)
from app.domain.searches import (
    CreateSearchRequest,
    CreateSearchResponse,
    ResumeSearchRequest,
    SearchResponse,
    SearchRunRecord,
    SearchStatus,
)

router = APIRouter(tags=["searches"])


def _normalized_feedback(request: ResumeSearchRequest) -> str | None:
    for value in (request.human_feedback, request.updated_user_intent):
        if value is not None and value.strip():
            return value.strip()
    return None


def _is_applied_manual_resume(
    search: SearchRunRecord,
    *,
    reviewed_round_index: int,
    selected_candidate_id: str | None,
    human_feedback: str | None,
) -> bool:
    if search.round_index <= reviewed_round_index:
        return False
    return any(
        item.get("round_index") == reviewed_round_index
        and item.get("human_resume_applied") is True
        and item.get("human_selected_candidate_id") == selected_candidate_id
        and item.get("human_feedback") == human_feedback
        for item in search.round_history
    )


def _search_response(container: object, search_id: str) -> SearchResponse:
    from app.container import AppContainer

    if not isinstance(container, AppContainer):
        raise TypeError("Search response requires an application container")
    search = container.app_store.get_search(search_id)
    return SearchResponse.from_record(
        search,
        evaluations=container.app_store.list_evaluations_with_scores(search_id),
    )


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
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Project was not found",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorEnvelope,
            "description": "Idempotency or Guidance Mask lineage conflict",
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": ErrorEnvelope,
            "description": "Invalid search request",
        },
    },
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
    return _search_response(container, search_id)


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
        return SearchResponse.from_record(container.app_store.get_search(search_id))

    if request.action in {"accept_global_winner", "accept_candidate"}:
        if search.status is SearchStatus.ACCEPTED:
            return _search_response(container, search_id)
        if search.status is not SearchStatus.WAITING_FOR_HUMAN:
            raise ConflictError(
                f"Cannot accept a search with status {search.status.value}"
            )
        selected_candidate_id = (
            request.selected_candidate_id if request.action == "accept_candidate" else None
        )
        if request.action == "accept_candidate" and selected_candidate_id is None:
            raise ConflictError("Cannot accept a candidate without selected_candidate_id")
        if request.action == "accept_global_winner" and search.global_winner_id is None:
            raise ConflictError("Cannot accept without a global winner")
        if not any(
            candidate.candidate_id == (selected_candidate_id or search.global_winner_id)
            for candidate in search.candidates
        ):
            raise ConflictError("Selected candidate is not a candidate in this search")
        if not container.app_store.accept_search(search_id, candidate_id=selected_candidate_id):
            refreshed = container.app_store.get_search(search_id)
            if refreshed.status is SearchStatus.ACCEPTED:
                return _search_response(container, search_id)
            raise ConflictError(
                f"Cannot accept a search with status {refreshed.status.value}"
            )
        return _search_response(container, search_id)

    if request.action == "continue_one_round":
        reviewed_round_index = request.reviewed_round_index
        if reviewed_round_index is None:  # guarded by request validation
            raise ConflictError("Continue requires reviewed_round_index")
        human_feedback = _normalized_feedback(request)
        if _is_applied_manual_resume(
            search,
            reviewed_round_index=reviewed_round_index,
            selected_candidate_id=request.selected_candidate_id,
            human_feedback=human_feedback,
        ):
            return _search_response(container, search_id)
        if search.status is not SearchStatus.WAITING_FOR_HUMAN:
            raise ConflictError(
                f"Continue is only valid from waiting_for_human, got {search.status.value}"
            )
        if search.round_index != reviewed_round_index:
            raise ConflictError(
                f"Reviewed round {reviewed_round_index} is stale; current round is "
                f"{search.round_index}"
            )
        if request.selected_candidate_id is not None:
            selected_matches = [
                candidate
                for candidate in search.candidates
                if candidate.candidate_id == request.selected_candidate_id
            ]
            if len(selected_matches) != 1:
                raise ConflictError("Selected candidate is not a candidate in this search")
            if selected_matches[0].round_index != reviewed_round_index:
                raise ConflictError(
                    "Selected candidate must belong to the currently reviewed round"
                )
        if not container.app_store.queue_next_round(
            search_id,
            reviewed_round_index=reviewed_round_index,
            selected_candidate_id=request.selected_candidate_id,
            human_feedback=human_feedback,
        ):
            refreshed = container.app_store.get_search(search_id)
            if _is_applied_manual_resume(
                refreshed,
                reviewed_round_index=reviewed_round_index,
                selected_candidate_id=request.selected_candidate_id,
                human_feedback=human_feedback,
            ):
                return _search_response(container, search_id)
            raise ConflictError("Search has reached its maximum configured rounds")
        if container.settings.run_inline:
            background_tasks.add_task(_run_inline_search, container, search_id)
        return _search_response(container, search_id)

    raise UnsupportedMilestoneActionError(
        "Supported actions: accept_global_winner, accept_candidate, continue_one_round, and cancel"
    )
