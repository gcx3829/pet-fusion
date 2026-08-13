from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import cast

from langchain_core.runnables import RunnableConfig

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateResponse
from app.domain.errors import SourceManifestMismatchError
from app.domain.searches import SearchStatus
from app.graphs.checkpointer import sqlite_checkpointer
from app.graphs.search_graph import (
    SEARCH_STATE_SCHEMA_VERSION,
    SearchGraphServices,
    build_search_graph,
)
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import DeterministicCriticService
from app.services.generator_service import GeneratorService
from app.services.stop_policy import DeterministicStopPolicy


class SearchRunner:
    def __init__(
        self,
        *,
        app_store: AppStore,
        generator_service: GeneratorService,
        checkpoint_path: Path,
        critic_service: DeterministicCriticService | None = None,
        candidate_ranker: DeterministicCandidateRanker | None = None,
        stop_policy: DeterministicStopPolicy | None = None,
    ) -> None:
        self.app_store = app_store
        self.generator_service = generator_service
        self.checkpoint_path = checkpoint_path.resolve()
        self.critic_service = critic_service
        self.candidate_ranker = candidate_ranker
        self.stop_policy = stop_policy

    def initial_state(self, search_id: str) -> SearchState:
        search = self.app_store.get_search(search_id)
        project = self.app_store.get_project(search.project_id)
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise SourceManifestMismatchError(
                "Search and project immutable source manifest hashes differ"
            )
        state: SearchState = {
            "schema_version": SEARCH_STATE_SCHEMA_VERSION,
            "search_id": search.search_id,
            "thread_id": search.thread_id,
            "project_id": search.project_id,
            "status": search.status.value,
            "source_manifest": project.source_manifest.model_dump(mode="json"),
            "source_manifest_hash": search.source_manifest_hash,
            "placement": search.placement.model_dump(mode="json"),
            "user_intent": search.user_intent,
            "active_directives": search.active_directives,
            "evaluations": [
                item.model_dump(mode="json")
                for item in self.app_store.list_evaluations(search_id)
            ],
            "round_history": search.round_history,
            "round_index": search.round_index,
            "max_rounds": search.max_rounds,
            "review_each_round": search.review_each_round,
            "candidate_count": search.candidate_count,
            "current_candidates": [],
            "round_winner_id": search.round_winner_id,
            "global_winner_id": search.global_winner_id,
            "global_winner_score": search.global_winner_score,
            "stop_action": None,
            "stop_reason": None,
            "interrupt_payload": search.interrupt_payload,
            "error": None,
        }
        assert_checkpoint_safe(state)
        return state

    def _reconcile_completed_state(
        self, search_id: str, result: SearchState, *, worker_id: str | None
    ) -> None:
        """Repair an app-store write lost after the durable graph checkpoint committed."""

        current = self.app_store.get_search(search_id)
        if current.status is SearchStatus.CANCELLED:
            return
        result_status = SearchStatus(result["status"])
        if result_status is SearchStatus.WAITING_FOR_HUMAN:
            updated = self.app_store.update_search(
                search_id,
                status=result_status,
                round_index=result.get("round_index", 0),
                stop_reason=result.get("stop_reason") or "mock_round_complete",
                state_summary={
                    "schema_version": result["schema_version"],
                    "source_manifest_hash": result["source_manifest_hash"],
                    "round_index": result.get("round_index", 0),
                    "candidate_ids": [
                        item["candidate_id"] for item in result.get("current_candidates", [])
                    ],
                    "stop_reason": result.get("stop_reason") or "mock_round_complete",
                },
                clear_lease=True,
                expected_statuses=(SearchStatus.RUNNING,),
                expected_lease_owner=worker_id,
            )
            if not updated:
                if self.app_store.get_search(search_id).status is SearchStatus.CANCELLED:
                    return
                return
            current = self.app_store.get_search(search_id)
            self.app_store.emit_event(
                search_id=search_id,
                event_key=(
                    "search:waiting-for-human"
                    if current.round_index == 0
                    else f"search:waiting-for-human:{current.round_index}"
                ),
                event_type="search.waiting_for_human",
                payload={
                    "round_index": current.round_index,
                    "candidates": [
                        CandidateResponse.from_record(candidate).model_dump(mode="json")
                        for candidate in current.candidates
                    ],
                    "stop_reason": current.stop_reason,
                },
            )

    async def _watch_lease(
        self, *, search_id: str, worker_id: str, lease_seconds: int
    ) -> None:
        interval = max(0.5, lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            if not self.app_store.renew_search_lease(
                search_id=search_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            ):
                raise RuntimeError("Search worker lost its lease")

    async def run_with_lease(
        self, *, search_id: str, worker_id: str, lease_seconds: int
    ) -> SearchState:
        run_task = asyncio.create_task(self.run_search(search_id, worker_id=worker_id))
        lease_task = asyncio.create_task(
            self._watch_lease(
                search_id=search_id,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
        )
        done, _ = await asyncio.wait(
            {run_task, lease_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if run_task in done:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            return await run_task
        if lease_task in done:
            run_task.cancel()
            with suppress(asyncio.CancelledError):
                await run_task
            await lease_task
            raise RuntimeError("Search worker lease ended unexpectedly")
        raise RuntimeError("Search runner stopped without a completed task")

    async def run_search(
        self, search_id: str, *, worker_id: str | None = None
    ) -> SearchState:
        search = self.app_store.get_search(search_id)
        resolved_worker_id = (
            worker_id
            if worker_id is not None
            else self.app_store.get_search_lease_owner(search_id)
        )
        if search.status.is_stream_terminal:
            return self.initial_state(search_id) | {
                "status": search.status.value,
                "current_candidates": [item.model_dump(mode="json") for item in search.candidates],
                "stop_reason": search.stop_reason,
            }
        config: RunnableConfig = {"configurable": {"thread_id": search.thread_id}}
        try:
            async with sqlite_checkpointer(self.checkpoint_path) as checkpointer:
                await checkpointer.setup()
                graph = build_search_graph(
                    SearchGraphServices(
                        app_store=self.app_store,
                        generator_service=self.generator_service,
                        lease_owner=resolved_worker_id,
                        critic_service=self.critic_service,
                        candidate_ranker=self.candidate_ranker,
                        stop_policy=self.stop_policy,
                    )
                ).compile(checkpointer=checkpointer)
                snapshot = await graph.aget_state(config)
                if snapshot.values:
                    checkpoint_state = cast(SearchState, snapshot.values)
                    project = self.app_store.get_project(search.project_id)
                    checkpoint_manifest = SourceManifest.model_validate(
                        checkpoint_state.get("source_manifest")
                    )
                    if (
                        checkpoint_state.get("schema_version")
                        != SEARCH_STATE_SCHEMA_VERSION
                        or checkpoint_state.get("search_id") != search.search_id
                        or checkpoint_state.get("project_id") != search.project_id
                        or checkpoint_state.get("source_manifest_hash")
                        != search.source_manifest_hash
                        or checkpoint_manifest != project.source_manifest
                        or project.source_manifest.manifest_hash
                        != search.source_manifest_hash
                    ):
                        raise SourceManifestMismatchError(
                            "Checkpoint conflicts with immutable search source state"
                        )
                if snapshot.values:
                    checkpoint_state = cast(SearchState, snapshot.values)
                    if (
                        search.status is SearchStatus.RUNNING
                        and checkpoint_state.get("round_index") not in {
                            search.round_index,
                            search.round_index - 1,
                        }
                    ):
                        raise SourceManifestMismatchError(
                            "Checkpoint round does not match the running search"
                        )
                # A queued next round deliberately starts a new graph invocation on
                # the same fixed thread; running recovery resumes the checkpoint.
                checkpoint_round = (
                    cast(SearchState, snapshot.values).get("round_index")
                    if snapshot.values
                    else None
                )
                graph_input = (
                    self.initial_state(search_id)
                    if not snapshot.values
                    or search.status is SearchStatus.QUEUED
                    or checkpoint_round != search.round_index
                    else None
                )
                result = cast(
                    SearchState,
                    await graph.ainvoke(graph_input, config=config, version="v1"),
                )
                assert_checkpoint_safe(result)
                self._reconcile_completed_state(
                    search_id, result, worker_id=resolved_worker_id
                )
                return result
        except Exception as exc:
            error: dict[str, object] = {
                "code": type(exc).__name__,
                "message": "Search execution failed",
            }
            current = self.app_store.get_search(search_id)
            if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
                return self.initial_state(search_id) | {
                    "status": current.status.value,
                    "current_candidates": [
                        item.model_dump(mode="json") for item in current.candidates
                    ],
                    "stop_reason": current.stop_reason,
                }
            updated = self.app_store.update_search(
                search_id,
                status=SearchStatus.FAILED,
                error=error,
                stop_reason="execution_failed",
                clear_lease=True,
                expected_statuses=(SearchStatus.RUNNING, SearchStatus.QUEUED),
                expected_lease_owner=resolved_worker_id,
            )
            if updated:
                self.app_store.emit_event(
                    search_id=search_id,
                    event_key="search:failed",
                    event_type="search.failed",
                    payload={"error": error},
                )
            elif self.app_store.get_search(search_id).status in {
                SearchStatus.CANCELLED,
                SearchStatus.ACCEPTED,
            }:
                current = self.app_store.get_search(search_id)
                return self.initial_state(search_id) | {
                    "status": current.status.value,
                    "current_candidates": [
                        item.model_dump(mode="json") for item in current.candidates
                    ],
                    "stop_reason": current.stop_reason,
                }
            raise
