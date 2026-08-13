from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.evaluations import (
    CandidateEvaluation,
    GlobalWinner,
    RoundRanking,
    StopDecision,
)
from app.domain.searches import PlacementIntent, SearchStatus
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import CriticInput, DeterministicCriticService
from app.services.generator_service import (
    GenerationRequest,
    GeneratorService,
)
from app.services.prompt_compiler import (
    CANONICAL_TEMPLATE_VERSION,
    compile_canonical_prompt,
)
from app.services.stop_policy import DeterministicStopPolicy

SEARCH_STATE_SCHEMA_VERSION = "search-state/v1"


@dataclass(frozen=True, slots=True)
class SearchGraphServices:
    app_store: AppStore
    generator_service: GeneratorService
    lease_owner: str | None = None
    critic_service: DeterministicCriticService | None = None
    candidate_ranker: DeterministicCandidateRanker | None = None
    stop_policy: DeterministicStopPolicy | None = None


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


def build_search_graph(services: SearchGraphServices) -> StateGraph[SearchState]:
    """Run deterministic rounds from the immutable project manifest.

    A blocking ``CONTINUE`` decision automatically loops when the search does not
    request review after every round. Explicit human resume starts the next round by
    invoking this same graph again on the same fixed LangGraph thread.
    """

    critic_service = services.critic_service or DeterministicCriticService()
    candidate_ranker = services.candidate_ranker or DeterministicCandidateRanker()
    stop_policy = services.stop_policy or DeterministicStopPolicy(candidate_ranker.policy)

    def fenced_update(
        search_id: str,
        *,
        expected_statuses: tuple[SearchStatus, ...] = (SearchStatus.RUNNING,),
        **kwargs: Any,
    ) -> bool:
        return services.app_store.update_search(
            search_id,
            expected_statuses=expected_statuses,
            expected_lease_owner=services.lease_owner,
            **kwargs,
        )

    def is_stopped(search_id: str) -> SearchStatus | None:
        status = services.app_store.get_search(search_id).status
        return status if status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED} else None

    async def initialize_search(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value}
        event_key = (
            "search:started"
            if state["round_index"] == 0
            else f"search:round:{state['round_index']}:started"
        )
        event_type = "search.started" if state["round_index"] == 0 else "round.started"
        if not fenced_update(
            state["search_id"],
            expected_statuses=(SearchStatus.QUEUED, SearchStatus.RUNNING),
            status=SearchStatus.RUNNING,
            round_index=state["round_index"],
            events=(
                (
                    event_key,
                    event_type,
                    {"round_index": state["round_index"]},
                ),
            ),
        ):
            current = services.app_store.get_search(state["search_id"])
            return {"status": current.status.value}
        return {"status": SearchStatus.RUNNING.value}

    async def compile_prompt(state: SearchState) -> dict[str, object]:
        stopped = is_stopped(state["search_id"])
        if stopped is not None:
            return {"status": stopped.value}
        manifest = SourceManifest.model_validate(state["source_manifest"])
        placement = PlacementIntent.model_validate(state["placement"])
        directives = state.get("active_directives", [])
        directive_text = "\n".join(
            str(item.get("directive", ""))
            for item in directives
            if isinstance(item, dict) and item.get("directive")
        )
        intent = state["user_intent"]
        if directive_text:
            intent = f"{intent}\nFocused repair directives:\n{directive_text}"
        prompt, prompt_hash = compile_canonical_prompt(
            placement=placement,
            user_intent=intent,
            reference_count=len(manifest.cat_references),
        )
        return {
            "canonical_prompt": prompt,
            "canonical_prompt_hash": prompt_hash,
            "canonical_template_version": CANONICAL_TEMPLATE_VERSION,
        }

    async def prepare_round(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value, "current_candidates": []}
        services.app_store.emit_event(
            search_id=state["search_id"],
            event_key=f"round:{state['round_index']}:generation:started",
            event_type="round.generation.started",
            payload={
                "round_index": state["round_index"],
                "candidate_count": state["candidate_count"],
                "source_manifest_hash": state["source_manifest_hash"],
                "rebased_to_immutable_source": True,
            },
        )
        return {"current_candidates": [], "evaluations": []}

    async def generate_candidates(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value, "current_candidates": []}
        manifest = SourceManifest.model_validate(state["source_manifest"])
        request = GenerationRequest(
            search_id=state["search_id"],
            source_manifest=manifest,
            placement=PlacementIntent.model_validate(state["placement"]),
            prompt=state["canonical_prompt"],
            prompt_hash=state["canonical_prompt_hash"],
            round_index=state["round_index"],
            candidate_count=state["candidate_count"],
            model=services.generator_service.model,
            quality=services.generator_service.quality,
            size=(
                services.generator_service.size
                or f"{manifest.background.width}x{manifest.background.height}"
            ),
        )
        records = await services.generator_service.generate_round(
            request,
            expected_manifest_hash=state["source_manifest_hash"],
        )
        candidate_payloads = [item.model_dump(mode="json") for item in records]
        assert_checkpoint_safe(candidate_payloads, path="current_candidates")
        return {"current_candidates": candidate_payloads}

    async def evaluate_round(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value, "current_candidates": []}
        manifest = SourceManifest.model_validate(state["source_manifest"])
        placement = PlacementIntent.model_validate(state["placement"])
        candidates = [CandidateRecord.model_validate(item) for item in state["current_candidates"]]
        evaluations: list[CandidateEvaluation] = []
        scores: dict[str, float] = {}
        for candidate in candidates:
            evaluation = critic_service.evaluate(
                CriticInput(candidate=candidate, source_manifest=manifest, placement=placement)
            )
            scored = candidate_ranker.score(evaluation)
            services.app_store.save_evaluation(
                state["search_id"], evaluation, score=scored.score
            )
            evaluations.append(evaluation)
            scores[candidate.candidate_id] = scored.score
            services.app_store.emit_event(
                search_id=state["search_id"],
                event_key=f"round:{state['round_index']}:evaluation:{candidate.candidate_id}",
                event_type="round.evaluation.ready",
                payload={"evaluation": _public_evaluation(evaluation, scored.score)},
            )

        ranking: RoundRanking = candidate_ranker.rank_round(
            evaluations, round_index=state["round_index"]
        )
        current_global = (
            GlobalWinner(
                candidate_id=current.global_winner_id,
                score=current.global_winner_score,
                round_index=next(
                    (
                        cast(int, item["round_index"])
                        for item in reversed(current.round_history)
                        if item.get("global_winner_id") == current.global_winner_id
                        and isinstance(item.get("round_index"), int)
                    ),
                    0,
                ),
            )
            if current.global_winner_id and current.global_winner_score is not None
            else None
        )
        global_winner = candidate_ranker.update_global_winner(current_global, ranking)
        persisted_evaluations = services.app_store.list_evaluations(state["search_id"])
        decision: StopDecision = stop_policy.decide(
            ranking=ranking,
            evaluations=persisted_evaluations,
            global_winner=global_winner,
            max_rounds=state["max_rounds"],
        )
        history_entry: dict[str, object] = {
            "round_index": state["round_index"],
            "candidate_ids": [candidate.candidate_id for candidate in candidates],
            "evaluations": [
                _public_evaluation(evaluation, scores[evaluation.candidate_id])
                for evaluation in evaluations
            ],
            "round_winner_id": ranking.winner_id,
            "round_winner_score": ranking.winner_score,
            "global_winner_id": global_winner.candidate_id if global_winner else None,
            "global_winner_score": global_winner.score if global_winner else None,
            "stop_action": decision.action.value,
            "stop_reason": decision.reason,
        }
        round_history = [
            *(
                item
                for item in current.round_history
                if item.get("round_index") != state["round_index"]
            ),
            history_entry,
        ]
        round_history.sort(
            key=lambda item: cast(int, item.get("round_index", 0))
        )
        selected_id = (
            global_winner.candidate_id if global_winner is not None else ranking.winner_id
        )
        selected_evaluation = next(
            (item for item in persisted_evaluations if item.candidate_id == selected_id),
            None,
        )
        active_directives = (
            [
                {"directive": issue.suggested_fix or issue.category, "issue_id": issue.issue_id}
                for issue in (selected_evaluation.blocking_issues if selected_evaluation else ())
                if issue.suggested_fix
            ][:3]
        )
        transition_events: list[tuple[str, str, dict[str, object]]] = [
            (
                f"round:{state['round_index']}:winner",
                "round.winner.updated",
                {
                    "round_index": state["round_index"],
                    "round_winner_id": ranking.winner_id,
                    "round_winner_score": ranking.winner_score,
                },
            ),
            (
                f"round:{state['round_index']}:stop",
                "search.stop.decided",
                {
                    "round_index": state["round_index"],
                    "action": decision.action.value,
                    "reason": decision.reason,
                    "global_winner_id": (
                        global_winner.candidate_id if global_winner else None
                    ),
                },
            ),
        ]
        if current.global_winner_id != (
            global_winner.candidate_id if global_winner else None
        ):
            transition_events.insert(
                1,
                (
                    f"round:{state['round_index']}:global-winner",
                    "search.global_winner.updated",
                    {
                        "round_index": state["round_index"],
                        "global_winner_id": (
                            global_winner.candidate_id if global_winner else None
                        ),
                        "global_winner_score": global_winner.score if global_winner else None,
                    },
                ),
            )
        if not fenced_update(
            state["search_id"],
            round_winner_id=ranking.winner_id,
            global_winner_id=global_winner.candidate_id if global_winner else None,
            global_winner_score=global_winner.score if global_winner else None,
            round_history=round_history,
            active_directives=active_directives,
            stop_reason=decision.reason,
            events=transition_events,
        ):
            latest = services.app_store.get_search(state["search_id"])
            return {"status": latest.status.value}
        evaluation_payloads = [evaluation.model_dump(mode="json") for evaluation in evaluations]
        return {
            "evaluations": evaluation_payloads,
            "round_history": round_history,
            "round_winner_id": ranking.winner_id,
            "global_winner_id": global_winner.candidate_id if global_winner else None,
            "global_winner_score": global_winner.score if global_winner else None,
            "active_directives": active_directives,
            "stop_action": decision.action.value,
            "stop_reason": decision.reason,
        }

    async def prepare_next_round(state: SearchState) -> dict[str, object]:
        next_round = state["round_index"] + 1
        if not fenced_update(
            state["search_id"],
            status=SearchStatus.RUNNING,
            round_index=next_round,
            clear_round_winner=True,
            clear_stop_reason=True,
            clear_state_summary=True,
            clear_interrupt_payload=True,
            events=(
                (
                    f"search:round:{next_round}:started",
                    "round.started",
                    {"round_index": next_round, "automatic": True},
                ),
            ),
        ):
            latest = services.app_store.get_search(state["search_id"])
            return {"status": latest.status.value}
        return {
            "round_index": next_round,
            "current_candidates": [],
            "evaluations": [],
            "round_winner_id": None,
            "stop_action": None,
            "stop_reason": None,
            "status": SearchStatus.RUNNING.value,
        }

    async def finalize_mock_round(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value, "stop_reason": current.stop_reason}
        if (
            state.get("stop_action") == "continue"
            and not state.get("review_each_round", False)
            and state["round_index"] + 1 < state["max_rounds"]
        ):
            return {
                "status": SearchStatus.RUNNING.value,
                "stop_reason": state.get("stop_reason"),
            }
        allowed_actions = ["cancel"]
        if state.get("global_winner_id"):
            allowed_actions.insert(0, "accept_global_winner")
        if state["round_index"] + 1 < state["max_rounds"]:
            allowed_actions.insert(0, "continue_one_round")
        interrupt_payload: dict[str, object] = {
            "type": "search_review",
            "search_id": state["search_id"],
            "round_index": state["round_index"],
            "global_winner_id": state.get("global_winner_id"),
            "global_winner_score": state.get("global_winner_score"),
            "candidate_ids": [item["candidate_id"] for item in state["current_candidates"]],
            "blocking_issues": [
                issue_id
                for evaluation in state.get("evaluations", [])
                for issue_id in cast(
                    list[object], evaluation.get("hard_constraint_failures", [])
                )
            ],
            "allowed_actions": allowed_actions,
        }
        summary: dict[str, object] = {
            "schema_version": state["schema_version"],
            "source_manifest_hash": state["source_manifest_hash"],
            "round_index": state["round_index"],
            "candidate_ids": [item["candidate_id"] for item in state["current_candidates"]],
            "evaluation_count": len(state.get("evaluations", [])),
            "global_winner_id": state.get("global_winner_id"),
            "global_winner_score": state.get("global_winner_score"),
            "stop_reason": state.get("stop_reason"),
        }
        assert_checkpoint_safe(interrupt_payload)
        assert_checkpoint_safe(summary)
        public_candidates = [
            CandidateResponse.from_record(CandidateRecord.model_validate(item)).model_dump(
                mode="json"
            )
            for item in state["current_candidates"]
        ]
        waiting_event_key = (
            "search:waiting-for-human"
            if state["round_index"] == 0
            else f"search:waiting-for-human:{state['round_index']}"
        )
        if not fenced_update(
            state["search_id"],
            status=SearchStatus.WAITING_FOR_HUMAN,
            round_index=state["round_index"],
            stop_reason=state.get("stop_reason") or "round_complete",
            state_summary=summary,
            interrupt_payload=interrupt_payload,
            clear_lease=True,
            events=(
                (
                    waiting_event_key,
                    "search.waiting_for_human",
                    {
                        "round_index": state["round_index"],
                        "candidates": public_candidates,
                        "stop_reason": state.get("stop_reason"),
                        "global_winner_id": state.get("global_winner_id"),
                    },
                ),
                (
                    f"search:interrupted:{state['round_index']}",
                    "search.interrupted",
                    interrupt_payload,
                ),
            ),
        ):
            latest = services.app_store.get_search(state["search_id"])
            return {"status": latest.status.value}
        return {
            "status": SearchStatus.WAITING_FOR_HUMAN.value,
            "stop_reason": state.get("stop_reason"),
            "interrupt_payload": interrupt_payload,
        }

    def route_after_finalize(state: SearchState) -> str:
        state_status = state.get("status")
        persisted_status = services.app_store.get_search(state["search_id"]).status
        if state_status in {
            SearchStatus.CANCELLED.value,
            SearchStatus.ACCEPTED.value,
        } or persisted_status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return "end"
        if (
            state.get("stop_action") == "continue"
            and not state.get("review_each_round", False)
            and state["round_index"] + 1 < state["max_rounds"]
        ):
            return "prepare_next_round"
        return "end"

    graph = StateGraph(SearchState)
    graph.add_node("initialize_search", initialize_search)
    graph.add_node("compile_canonical_prompt", compile_prompt)
    graph.add_node("prepare_round", prepare_round)
    graph.add_node("generate_candidates", generate_candidates)
    graph.add_node("evaluate_round", evaluate_round)
    graph.add_node("prepare_next_round", prepare_next_round)
    graph.add_node("finalize_mock_round", finalize_mock_round)
    graph.add_edge(START, "initialize_search")
    graph.add_edge("initialize_search", "compile_canonical_prompt")
    graph.add_edge("compile_canonical_prompt", "prepare_round")
    graph.add_edge("prepare_round", "generate_candidates")
    graph.add_edge("generate_candidates", "evaluate_round")
    graph.add_edge("evaluate_round", "finalize_mock_round")
    graph.add_conditional_edges(
        "finalize_mock_round",
        route_after_finalize,
        {"prepare_next_round": "prepare_next_round", "end": END},
    )
    graph.add_edge("prepare_next_round", "compile_canonical_prompt")
    return graph
