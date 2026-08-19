from __future__ import annotations

import asyncio
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import cast

from langchain_core.runnables import RunnableConfig

from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateResponse
from app.domain.directives import DirectivePolicy, stable_directives_hash
from app.domain.errors import SourceManifestMismatchError
from app.domain.prompts import PromptVersion
from app.domain.searches import SearchStatus
from app.graphs.checkpointer import sqlite_checkpointer
from app.graphs.reducers import empty_evaluation_bucket
from app.graphs.search_graph import (
    SEARCH_STATE_SCHEMA_VERSION,
    SearchGraphServices,
    build_search_graph,
)
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import CriticProvider
from app.services.generator_service import GeneratorService
from app.services.planner_service import FeedbackPlannerService, normalize_active_directives
from app.services.prompt_refiner_service import (
    PromptRefinerError,
    PromptRefinerResult,
    PromptRefinerService,
)
from app.services.proxy_builder import CriticProxyBuilder
from app.services.stop_policy import DeterministicStopPolicy


class SearchRunner:
    def __init__(
        self,
        *,
        app_store: AppStore,
        generator_service: GeneratorService,
        checkpoint_path: Path,
        critic_service: CriticProvider | None = None,
        critic_proxy_builder: CriticProxyBuilder | None = None,
        candidate_ranker: DeterministicCandidateRanker | None = None,
        stop_policy: DeterministicStopPolicy | None = None,
        planner_service: FeedbackPlannerService | None = None,
        prompt_refiner_service: PromptRefinerService | None = None,
    ) -> None:
        self.app_store = app_store
        self.generator_service = generator_service
        self.checkpoint_path = checkpoint_path.resolve()
        self.critic_service = critic_service
        self.critic_proxy_builder = critic_proxy_builder
        self.candidate_ranker = candidate_ranker
        self.stop_policy = stop_policy
        self.planner_service = planner_service
        self.prompt_refiner_service = prompt_refiner_service

    def _restore_current_prompt_version(
        self, search_id: str, search: object
    ) -> PromptVersion | None:
        """Rebuild current prompt lineage from app data, never from a guess.

        Provider-authored versions are stored in ``prompt_refiner_results``;
        local bounded-directive versions are stored in ``prompt_history``.  If
        both stores claim the same ID with different content, recovery is
        blocked rather than silently changing a paid generation input.
        """

        from app.domain.searches import SearchRunRecord

        if not isinstance(search, SearchRunRecord):
            raise TypeError("Prompt lineage restore requires a SearchRunRecord")
        history_versions: dict[str, PromptVersion] = {}
        history_rounds: dict[int, str] = {}
        for entry in search.prompt_history:
            version = PromptVersion.model_validate(entry)
            if version.search_id not in {None, search_id} or version.source_manifest_hash not in {
                None,
                search.source_manifest_hash,
            }:
                raise SourceManifestMismatchError(
                    "Search prompt history does not belong to this immutable search"
                )
            if version.round_index > search.round_index:
                raise SourceManifestMismatchError(
                    "Search prompt history contains a future-round PromptVersion"
                )
            existing_round_id = history_rounds.get(version.round_index)
            if (
                existing_round_id is not None
                and existing_round_id != version.prompt_version_id
            ):
                raise SourceManifestMismatchError(
                    "Search prompt history contains multiple versions for one round"
                )
            history_rounds[version.round_index] = version.prompt_version_id
            existing = history_versions.get(version.prompt_version_id)
            if existing is not None and existing.model_dump(mode="json") != version.model_dump(
                mode="json"
            ):
                raise SourceManifestMismatchError(
                    "Search prompt history contains conflicting PromptVersion lineage"
                )
            history_versions[version.prompt_version_id] = version

        if history_rounds:
            ordered_rounds = sorted(history_rounds)
            expected_rounds = list(range(ordered_rounds[-1] + 1))
            if ordered_rounds != expected_rounds:
                raise SourceManifestMismatchError(
                    "Search prompt history contains a missing round"
                )
            ordered_versions = [
                history_versions[history_rounds[round_index]]
                for round_index in ordered_rounds
            ]
            for previous, current in pairwise(ordered_versions):
                # Legacy PromptHistory rows predate explicit ancestry and are
                # hydrated with a nullable search/source lineage.  New v6
                # versions carry both values and must form one exact chain.
                if (
                    current.search_id is not None
                    and current.source_manifest_hash is not None
                    and current.based_on_prompt_version_id
                    != previous.prompt_version_id
                ):
                    raise SourceManifestMismatchError(
                        "Search prompt history parent lineage is not contiguous"
                    )

        refiner_versions: dict[str, PromptVersion] = {}
        refiner_rounds: dict[int, str] = {}
        for raw_result in self.app_store.list_prompt_refiner_results(search_id):
            try:
                version = PromptRefinerResult.model_validate(raw_result).prompt_version
            except (TypeError, ValueError) as exc:
                raise SourceManifestMismatchError(
                    "Persisted Prompt Refiner result cannot be restored"
                ) from exc
            if (
                version.search_id != search_id
                or version.source_manifest_hash != search.source_manifest_hash
                or version.round_index > search.round_index
            ):
                raise SourceManifestMismatchError(
                    "Persisted Prompt Refiner result does not belong to this search round"
                )
            existing_round_id = refiner_rounds.get(version.round_index)
            if (
                existing_round_id is not None
                and existing_round_id != version.prompt_version_id
            ):
                raise SourceManifestMismatchError(
                    "Persisted Prompt Refiner results contain multiple versions for one round"
                )
            refiner_rounds[version.round_index] = version.prompt_version_id
            existing_refiner = refiner_versions.get(version.prompt_version_id)
            if existing_refiner is not None and existing_refiner.model_dump(
                mode="json"
            ) != version.model_dump(mode="json"):
                raise SourceManifestMismatchError(
                    "Persisted Prompt Refiner results contain conflicting lineage"
                )
            refiner_versions[version.prompt_version_id] = version
            history_round_id = history_rounds.get(version.round_index)
            if (
                history_round_id is not None
                and history_round_id != version.prompt_version_id
            ):
                raise SourceManifestMismatchError(
                    "Prompt history and Prompt Refiner result disagree for one round"
                )
            existing = history_versions.get(version.prompt_version_id)
            if existing is not None and existing.model_dump(mode="json") != version.model_dump(
                mode="json"
            ):
                raise SourceManifestMismatchError(
                    "Prompt history and Prompt Refiner result disagree"
                )

        # A provider result may be durably completed immediately before the
        # nested graph's apply node checkpoints and copies it into
        # ``search_runs.prompt_history``.  Treat that result as an in-flight
        # replay source, not as the currently applied prompt.  Older completed
        # results missing from history indicate durable-state corruption.
        for version_id, version in refiner_versions.items():
            if version_id not in history_versions and version.round_index < search.round_index:
                raise SourceManifestMismatchError(
                    "A prior Prompt Refiner result is missing from prompt history"
                )

        if not history_versions:
            return None
        current = max(history_versions.values(), key=lambda version: version.round_index)
        if current.round_index not in {
            search.round_index,
            search.round_index - 1,
        }:
            raise SourceManifestMismatchError(
                "Search prompt history is not contiguous with the target round"
            )
        return current

    @staticmethod
    def _checkpoint_can_trail_durable_prompt_by_one(
        *,
        checkpoint_state: SearchState,
        durable_prompt: PromptVersion,
        search_round_index: int,
    ) -> bool:
        """Allow the app row to be one apply-node write ahead of checkpoint.

        ``apply_prompt_version`` persists the user-visible PromptVersion before
        LangGraph checkpoints the node result.  A crash in that narrow window
        must replay the round instead of marking the search corrupted.  Only
        an exact same-round child of the checkpoint PromptVersion is accepted;
        every other divergence remains fatal.
        """

        if durable_prompt.round_index != search_round_index:
            return False
        checkpoint_payload = checkpoint_state.get("current_prompt_version")
        checkpoint_id = checkpoint_state.get("prompt_version_id")
        if checkpoint_payload is None and checkpoint_id is None:
            return (
                durable_prompt.round_index == 0
                and durable_prompt.based_on_prompt_version_id is None
            )
        if not isinstance(checkpoint_payload, dict) or not isinstance(
            checkpoint_id, str
        ):
            return False
        try:
            checkpoint_prompt = PromptVersion.model_validate(checkpoint_payload)
        except (TypeError, ValueError):
            return False
        return (
            checkpoint_prompt.prompt_version_id == checkpoint_id
            and checkpoint_prompt.round_index == durable_prompt.round_index - 1
            and durable_prompt.based_on_prompt_version_id
            == checkpoint_prompt.prompt_version_id
        )

    def initial_state(self, search_id: str) -> SearchState:
        search = self.app_store.get_search(search_id)
        project = self.app_store.get_project(search.project_id)
        if project.source_manifest.manifest_hash != search.source_manifest_hash:
            raise SourceManifestMismatchError(
                "Search and project immutable source manifest hashes differ"
            )
        planner_policy = (
            self.planner_service.policy if self.planner_service is not None else DirectivePolicy()
        )
        active_directives = normalize_active_directives(
            search.active_directives,
            policy=planner_policy,
        )
        directive_versions = [
            value
            for item in search.round_history
            for value in [item.get("directive_version")]
            if isinstance(value, int) and value >= 0
        ]
        attempted_categories: list[str] = []
        planner_fallback_attempts = 0
        human_feedback: str | None = None
        human_selected_candidate_id: str | None = None
        for item in search.round_history:
            item_round = item.get("round_index")
            if item_round == search.round_index - 1:
                raw_feedback = item.get("human_feedback")
                if isinstance(raw_feedback, str) and raw_feedback.strip():
                    human_feedback = raw_feedback.strip()
                raw_selected = item.get("human_selected_candidate_id")
                if isinstance(raw_selected, str) and raw_selected:
                    human_selected_candidate_id = raw_selected
            raw_categories = item.get("planned_categories")
            if isinstance(raw_categories, list):
                attempted_categories.extend(
                    category for category in raw_categories if isinstance(category, str)
                )
            raw_fallback_attempts = item.get("planner_fallback_attempts")
            if isinstance(raw_fallback_attempts, int) and not isinstance(
                raw_fallback_attempts, bool
            ):
                planner_fallback_attempts = max(
                    planner_fallback_attempts,
                    min(raw_fallback_attempts, 1),
                )
            elif item.get("planner_fallback_used") is True:
                planner_fallback_attempts = 1
        current_prompt_version = self._restore_current_prompt_version(search_id, search)
        current_prompt_payload = (
            current_prompt_version.model_dump(mode="json")
            if current_prompt_version is not None
            else None
        )
        state: SearchState = {
            "schema_version": SEARCH_STATE_SCHEMA_VERSION,
            "search_id": search.search_id,
            "thread_id": search.thread_id,
            "project_id": search.project_id,
            "status": search.status.value,
            "source_manifest": project.source_manifest.model_dump(mode="json"),
            "source_manifest_hash": search.source_manifest_hash,
            "guidance_mask_asset": (
                search.guidance_mask_asset.model_dump(mode="json")
                if search.guidance_mask_asset is not None
                else None
            ),
            "placement": search.placement.model_dump(mode="json"),
            "user_intent": search.user_intent,
            "active_directives": [item.model_dump(mode="json") for item in active_directives],
            "active_directives_hash": stable_directives_hash(active_directives),
            "directive_policy_version": planner_policy.policy_version,
            "directive_version": max(directive_versions, default=0),
            "attempted_directive_categories": attempted_categories,
            "planner_result": None,
            "planner_input": None,
            "selected_evaluation": None,
            "selected_blocking_issues": [],
            "planner_proposal": None,
            "validated_planner_result": None,
            "planner_round_index": None,
            "planner_fallback_attempts": planner_fallback_attempts,
            "critic_proxy_inputs": {},
            "evaluations_by_candidate": empty_evaluation_bucket(search.round_index),
            "evaluations": [
                item.model_dump(mode="json") for item in self.app_store.list_evaluations(search_id)
            ],
            "round_history": search.round_history,
            "prompt_history": [
                item.model_dump(mode="json") for item in search.prompt_history
            ],
            "current_prompt_version": current_prompt_payload,
            "prompt_version_id": (
                current_prompt_version.prompt_version_id
                if current_prompt_version is not None
                else None
            ),
            "prompt_version_hash": (
                current_prompt_version.prompt_version_hash
                if current_prompt_version is not None
                else None
            ),
            "based_on_prompt_version_id": (
                current_prompt_version.based_on_prompt_version_id
                if current_prompt_version is not None
                else None
            ),
            "prompt_schema_version": (
                current_prompt_version.prompt_schema_version
                if current_prompt_version is not None
                else None
            ),
            "prompt_template_version": (
                current_prompt_version.prompt_template_version
                if current_prompt_version is not None
                else None
            ),
            "prompt_model": (
                current_prompt_version.prompt_model
                if current_prompt_version is not None
                else None
            ),
            "refinement_mode": (
                current_prompt_version.refinement_mode.value
                if current_prompt_version is not None
                else None
            ),
            "generation_mode": (
                current_prompt_version.generation_mode.value
                if current_prompt_version is not None
                else None
            ),
            "visual_anchor": (
                current_prompt_version.visual_anchor.model_dump(mode="json")
                if current_prompt_version is not None
                and current_prompt_version.visual_anchor is not None
                else None
            ),
            "visual_anchor_candidate_id": (
                current_prompt_version.visual_anchor_candidate_id
                if current_prompt_version is not None
                else None
            ),
            "visual_anchor_raw_asset_sha256": (
                current_prompt_version.visual_anchor_raw_asset_sha256
                if current_prompt_version is not None
                else None
            ),
            "visual_anchor_asset": (
                current_prompt_version.visual_anchor_asset.model_dump(mode="json")
                if current_prompt_version is not None
                and current_prompt_version.visual_anchor_asset is not None
                else None
            ),
            "professional_prompt_plan": (
                current_prompt_version.professional_prompt_plan.model_dump(mode="json")
                if current_prompt_version is not None
                and current_prompt_version.professional_prompt_plan is not None
                else None
            ),
            "prompt_summary": (
                current_prompt_version.prompt_summary
                if current_prompt_version is not None
                else None
            ),
            "provider_proposal_hash": (
                current_prompt_version.provider_proposal_hash
                if current_prompt_version is not None
                else None
            ),
            "prompt_refiner_execution_mode": None,
            "prompt_refiner_request": None,
            "prompt_refiner_result": None,
            "human_feedback": human_feedback,
            "human_selected_candidate_id": human_selected_candidate_id,
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
            round_index = result.get("round_index", 0)
            waiting_payload: dict[str, object] = {
                "round_index": round_index,
                "candidates": [
                    CandidateResponse.from_record(candidate).model_dump(mode="json")
                    for candidate in current.candidates
                ],
                "stop_reason": result.get("stop_reason") or "mock_round_complete",
            }
            events: list[tuple[str, str, dict[str, object]]] = [
                (
                    "search:waiting-for-human"
                    if round_index == 0
                    else f"search:waiting-for-human:{round_index}",
                    "search.waiting_for_human",
                    waiting_payload,
                )
            ]
            interrupt_payload = result.get("interrupt_payload")
            if isinstance(interrupt_payload, dict):
                events.append(
                    (
                        f"search:interrupted:{round_index}",
                        "search.interrupted",
                        interrupt_payload,
                    )
                )
            updated = self.app_store.update_search(
                search_id,
                status=result_status,
                round_index=round_index,
                stop_reason=result.get("stop_reason") or "mock_round_complete",
                prompt_history=(
                    result.get("prompt_history")
                    if isinstance(result.get("prompt_history"), list)
                    else None
                ),
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
                events=events,
            )
            if not updated:
                if self.app_store.get_search(search_id).status is SearchStatus.CANCELLED:
                    return
                return

    async def _watch_lease(self, *, search_id: str, worker_id: str, lease_seconds: int) -> None:
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
        done, _ = await asyncio.wait({run_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)
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

    async def run_search(self, search_id: str, *, worker_id: str | None = None) -> SearchState:
        search = self.app_store.get_search(search_id)
        resolved_worker_id = (
            worker_id if worker_id is not None else self.app_store.get_search_lease_owner(search_id)
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
                        critic_proxy_builder=self.critic_proxy_builder,
                        candidate_ranker=self.candidate_ranker,
                        stop_policy=self.stop_policy,
                        planner_service=self.planner_service,
                        prompt_refiner_service=self.prompt_refiner_service,
                    )
                ).compile(checkpointer=checkpointer)
                snapshot = await graph.aget_state(config)
                restart_from_durable_prompt = False
                if snapshot.values:
                    checkpoint_state = cast(SearchState, snapshot.values)
                    project = self.app_store.get_project(search.project_id)
                    checkpoint_manifest = SourceManifest.model_validate(
                        checkpoint_state.get("source_manifest")
                    )
                    checkpoint_guidance_payload = checkpoint_state.get(
                        "guidance_mask_asset"
                    )
                    checkpoint_guidance = (
                        AssetRef.model_validate(checkpoint_guidance_payload)
                        if checkpoint_guidance_payload is not None
                        else None
                    )
                    checkpoint_schema = checkpoint_state.get("schema_version")
                    if (
                        checkpoint_schema
                        not in {SEARCH_STATE_SCHEMA_VERSION, "search-state/v5"}
                        or checkpoint_state.get("search_id") != search.search_id
                        or checkpoint_state.get("project_id") != search.project_id
                        or checkpoint_state.get("source_manifest_hash")
                        != search.source_manifest_hash
                        or checkpoint_manifest != project.source_manifest
                        or project.source_manifest.manifest_hash != search.source_manifest_hash
                        or checkpoint_guidance != search.guidance_mask_asset
                    ):
                        raise SourceManifestMismatchError(
                            "Checkpoint conflicts with immutable search source state"
                        )
                    if checkpoint_schema == SEARCH_STATE_SCHEMA_VERSION:
                        checkpoint_prompt_id = checkpoint_state.get("prompt_version_id")
                        checkpoint_prompt = checkpoint_state.get("current_prompt_version")
                        durable_prompt = self._restore_current_prompt_version(
                            search_id, search
                        )
                        if checkpoint_prompt_id is not None:
                            if (
                                durable_prompt is None
                                or checkpoint_prompt_id
                                != durable_prompt.prompt_version_id
                                or checkpoint_prompt
                                != durable_prompt.model_dump(mode="json")
                            ):
                                if durable_prompt is not None and (
                                    self._checkpoint_can_trail_durable_prompt_by_one(
                                        checkpoint_state=checkpoint_state,
                                        durable_prompt=durable_prompt,
                                        search_round_index=search.round_index,
                                    )
                                ):
                                    restart_from_durable_prompt = True
                                else:
                                    raise SourceManifestMismatchError(
                                        "Checkpoint conflicts with persisted PromptVersion lineage"
                                    )
                        elif checkpoint_prompt is not None:
                            raise SourceManifestMismatchError(
                                "Checkpoint PromptVersion payload is missing its ID"
                            )
                        elif durable_prompt is not None and (
                            durable_prompt.round_index
                            != checkpoint_state.get("round_index")
                        ):
                            # The app row may be one apply-node write ahead of
                            # its checkpoint for the same round.  A prompt from
                            # an older round cannot legitimately disappear from
                            # a v6 checkpoint and must block before generation.
                            raise SourceManifestMismatchError(
                                "Checkpoint is missing its persisted PromptVersion lineage"
                            )
                if snapshot.values:
                    checkpoint_state = cast(SearchState, snapshot.values)
                    if search.status is SearchStatus.RUNNING and checkpoint_state.get(
                        "round_index"
                    ) not in {
                        search.round_index,
                        search.round_index - 1,
                    }:
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
                    or restart_from_durable_prompt
                    else None
                )
                result = cast(
                    SearchState,
                    await graph.ainvoke(graph_input, config=config, version="v1"),
                )
                assert_checkpoint_safe(result)
                self._reconcile_completed_state(search_id, result, worker_id=resolved_worker_id)
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
            failure_events: list[tuple[str, str, dict[str, object]]] = []
            if isinstance(exc, PromptRefinerError):
                failure_events.append(
                    (
                        f"round:{current.round_index}:prompt-refiner:failed",
                        "prompt.refiner.failed",
                        {
                            "round_index": current.round_index,
                            "error_code": type(exc).__name__,
                        },
                    )
                )
            failure_events.append(("search:failed", "search.failed", {"error": error}))
            updated = self.app_store.update_search(
                search_id,
                status=SearchStatus.FAILED,
                error=error,
                stop_reason="execution_failed",
                clear_lease=True,
                expected_statuses=(SearchStatus.RUNNING, SearchStatus.QUEUED),
                expected_lease_owner=resolved_worker_id,
                events=failure_events,
            )
            if not updated and self.app_store.get_search(search_id).status in {
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
