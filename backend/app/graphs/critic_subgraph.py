"""Explicit candidate-by-candidate Critic LangGraph subgraph."""

from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.evaluations import CandidateEvaluation
from app.domain.searches import PlacementIntent, SearchStatus
from app.graphs.reducers import CriticEvaluationBucket, merge_evaluations_by_candidate
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import (
    CriticEvaluationService,
    CriticInput,
    CriticProvider,
)
from app.services.proxy_builder import CriticProxyBuilder, CriticProxyBundle


class CriticTask(BaseModel):
    """One isolated, asset-reference-only candidate Critic invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: CandidateRecord
    source_manifest: SourceManifest
    placement: PlacementIntent
    canonical_prompt: str = Field(min_length=1, max_length=8_000)
    canonical_prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proxies: CriticProxyBundle


class CriticSubgraphState(TypedDict, total=False):
    """Subset of parent search state needed by the Critic control plane."""

    search_id: str
    status: str
    source_manifest: dict[str, object]
    placement: dict[str, object]
    canonical_prompt: str
    canonical_prompt_hash: str
    current_candidates: list[dict[str, object]]
    critic_proxy_inputs: dict[str, dict[str, object]]
    critic_task: dict[str, object]
    evaluations_by_candidate: Annotated[
        CriticEvaluationBucket, merge_evaluations_by_candidate
    ]
    evaluations: list[dict[str, object]]


def _public_evaluation(evaluation: CandidateEvaluation, score: float) -> dict[str, object]:
    return {
        "candidate_id": evaluation.candidate_id,
        "round_index": evaluation.round_index,
        "rubric_version": evaluation.rubric_version,
        "score": score,
        "recommended_action": evaluation.recommended_action,
        "blocking_issue_ids": [issue.issue_id for issue in evaluation.blocking_issues],
        "summary": evaluation.summary,
    }


def build_critic_subgraph(
    *,
    app_store: AppStore,
    proxy_builder: CriticProxyBuilder,
    critic_provider: CriticProvider,
    candidate_ranker: DeterministicCandidateRanker,
    critic_evaluation_service: CriticEvaluationService | None = None,
) -> StateGraph[CriticSubgraphState]:
    """Build the fixed blind-evaluation path for every current candidate.

    The nested graph is deliberately compiled without a checkpointer by its parent,
    so it receives the parent search thread's durable checkpoint namespace. Fan-out
    branches return only one keyed structured evaluation each; images stay in the
    asset store and never enter graph state.
    """

    evaluation_service = critic_evaluation_service or CriticEvaluationService(
        provider=critic_provider,
        app_store=app_store,
    )

    def search_is_terminal(search_id: str) -> bool:
        return app_store.get_search(search_id).status in {
            SearchStatus.CANCELLED,
            SearchStatus.ACCEPTED,
        }

    async def build_critic_inputs(state: CriticSubgraphState) -> dict[str, object]:
        search_id = state["search_id"]
        if search_is_terminal(search_id):
            return {"critic_proxy_inputs": {}, "evaluations": []}
        manifest = SourceManifest.model_validate(state["source_manifest"])
        placement = PlacementIntent.model_validate(state["placement"])
        candidates = [CandidateRecord.model_validate(item) for item in state["current_candidates"]]
        bundles: dict[str, dict[str, object]] = {}
        for candidate in candidates:
            bundle = await asyncio.to_thread(
                proxy_builder.build,
                source_manifest=manifest,
                candidate=candidate,
                placement=placement,
            )
            bundles[candidate.candidate_id] = bundle.model_dump(mode="json")
        payload: dict[str, object] = {"critic_proxy_inputs": bundles}
        assert_checkpoint_safe(payload, path="critic_subgraph.proxies")
        event_key = (
            f"round:{candidates[0].round_index}:critic:started" if candidates else "critic:empty"
        )
        app_store.emit_event(
            search_id=search_id,
            event_key=event_key,
            event_type="round.critic.started",
            payload={
                "round_index": candidates[0].round_index if candidates else 0,
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "proxy_schema_version": "critic-proxy/v1",
            },
        )
        return payload

    def fan_out_candidate_evaluations(
        state: CriticSubgraphState,
    ) -> dict[str, object]:
        """Named fan-out boundary; actual sends are emitted by the route below."""

        del state
        return {}

    def dispatch_candidate_evaluations(
        state: CriticSubgraphState,
    ) -> str | list[Send]:
        if search_is_terminal(state["search_id"]):
            return "collect_evaluations"
        manifest = SourceManifest.model_validate(state["source_manifest"])
        placement = PlacementIntent.model_validate(state["placement"])
        canonical_prompt = state["canonical_prompt"]
        canonical_prompt_hash = state["canonical_prompt_hash"]
        bundles = state.get("critic_proxy_inputs", {})
        tasks: list[Send] = []
        for candidate_payload in state.get("current_candidates", []):
            candidate = CandidateRecord.model_validate(candidate_payload)
            proxy_payload = bundles.get(candidate.candidate_id)
            if not isinstance(proxy_payload, dict):
                raise ValueError("Missing Critic proxy bundle for a current candidate")
            task = CriticTask(
                candidate=candidate,
                source_manifest=manifest,
                placement=placement,
                canonical_prompt=canonical_prompt,
                canonical_prompt_hash=canonical_prompt_hash,
                proxies=CriticProxyBundle.model_validate(proxy_payload),
            )
            if (
                task.proxies.candidate_id != candidate.candidate_id
                or task.proxies.source_manifest_hash != manifest.manifest_hash
            ):
                raise ValueError("Critic proxy bundle does not match the dispatched candidate")
            task_payload = task.model_dump(mode="json")
            assert_checkpoint_safe(task_payload, path="critic_subgraph.task")
            tasks.append(
                Send(
                    "evaluate_candidate",
                    {
                        "search_id": state["search_id"],
                        "critic_task": task_payload,
                    },
                )
            )
        return tasks if tasks else "collect_evaluations"

    async def evaluate_candidate(state: CriticSubgraphState) -> dict[str, object]:
        task = CriticTask.model_validate(state["critic_task"])
        evaluation = await evaluation_service.evaluate(
            search_id=state["search_id"],
            request=CriticInput(
                candidate=task.candidate,
                source_manifest=task.source_manifest,
                placement=task.placement,
                canonical_prompt=task.canonical_prompt,
                canonical_prompt_hash=task.canonical_prompt_hash,
                proxies=task.proxies,
            ),
        )
        payload = evaluation.model_dump(mode="json")
        assert_checkpoint_safe(payload, path="critic_subgraph.evaluation")
        return {
            "evaluations_by_candidate": {
                "round_index": evaluation.round_index,
                "items": {evaluation.candidate_id: payload},
            }
        }

    async def collect_evaluations(state: CriticSubgraphState) -> dict[str, object]:
        if search_is_terminal(state["search_id"]):
            return {"evaluations": []}
        candidates = [CandidateRecord.model_validate(item) for item in state["current_candidates"]]
        if not candidates:
            return {"evaluations": []}
        evaluation_bucket = state.get("evaluations_by_candidate")
        if (
            evaluation_bucket is None
            or evaluation_bucket["round_index"] != candidates[0].round_index
        ):
            raise RuntimeError("Critic evaluation reducer is not scoped to the current round")
        evaluations_by_candidate = evaluation_bucket["items"]
        missing = [
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_id not in evaluations_by_candidate
        ]
        if missing:
            raise RuntimeError(
                "Critic fan-out did not return evaluations for: " + ", ".join(missing)
            )
        evaluations = [
            CandidateEvaluation.model_validate(evaluations_by_candidate[candidate.candidate_id])
            for candidate in candidates
        ]
        payload = [evaluation.model_dump(mode="json") for evaluation in evaluations]
        assert_checkpoint_safe(payload, path="critic_subgraph.collected")
        return {"evaluations": payload}

    async def normalize_critic_findings(state: CriticSubgraphState) -> dict[str, object]:
        """Persist normalized evaluations once, with idempotent candidate event keys."""

        if search_is_terminal(state["search_id"]):
            return {"evaluations": []}
        normalized: list[dict[str, object]] = []
        for item in state.get("evaluations", []):
            evaluation = CandidateEvaluation.model_validate(item)
            score = candidate_ranker.score(evaluation).score
            app_store.save_evaluation(state["search_id"], evaluation, score=score)
            app_store.emit_event(
                search_id=state["search_id"],
                event_key=(f"round:{evaluation.round_index}:evaluation:{evaluation.candidate_id}"),
                event_type="round.evaluation.ready",
                payload={"evaluation": _public_evaluation(evaluation, score)},
            )
            normalized.append(evaluation.model_dump(mode="json"))
        assert_checkpoint_safe(normalized, path="critic_subgraph.normalized")
        return {"evaluations": normalized}

    graph = StateGraph(CriticSubgraphState)
    graph.add_node("build_critic_inputs", build_critic_inputs)
    graph.add_node("fan_out_candidate_evaluations", fan_out_candidate_evaluations)
    graph.add_node("evaluate_candidate", evaluate_candidate)
    graph.add_node("collect_evaluations", collect_evaluations)
    graph.add_node("normalize_critic_findings", normalize_critic_findings)
    graph.add_edge(START, "build_critic_inputs")
    graph.add_edge("build_critic_inputs", "fan_out_candidate_evaluations")
    graph.add_conditional_edges(
        "fan_out_candidate_evaluations",
        dispatch_candidate_evaluations,
        {"evaluate_candidate": "evaluate_candidate", "collect_evaluations": "collect_evaluations"},
    )
    graph.add_edge("evaluate_candidate", "collect_evaluations")
    graph.add_edge("collect_evaluations", "normalize_critic_findings")
    graph.add_edge("normalize_critic_findings", END)
    return graph
