from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.directives import (
    DirectiveCategory,
    PlannerAction,
    PlannerInput,
    PlannerResult,
    stable_directives_hash,
)
from app.domain.evaluations import (
    CandidateEvaluation,
    GlobalWinner,
    RoundRanking,
    StopAction,
    StopDecision,
)
from app.domain.searches import PlacementIntent, PromptHistoryEntry, SearchStatus
from app.graphs.critic_subgraph import build_critic_subgraph
from app.graphs.feedback_planner_subgraph import build_feedback_planner_subgraph
from app.graphs.reducers import empty_evaluation_bucket
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import CriticProvider, DeterministicCriticService
from app.services.generator_service import (
    GenerationRequest,
    GeneratorService,
)
from app.services.planner_service import (
    FeedbackPlannerService,
    normalize_active_directives,
)
from app.services.prompt_compiler import (
    CANONICAL_TEMPLATE_VERSION,
    compile_canonical_prompt,
    compile_generation_prompt,
)
from app.services.proxy_builder import CriticProxyBuilder
from app.services.stop_policy import DeterministicStopPolicy

SEARCH_STATE_SCHEMA_VERSION = "search-state/v4"


@dataclass(frozen=True, slots=True)
class SearchGraphServices:
    app_store: AppStore
    generator_service: GeneratorService
    lease_owner: str | None = None
    critic_service: CriticProvider | None = None
    critic_proxy_builder: CriticProxyBuilder | None = None
    candidate_ranker: DeterministicCandidateRanker | None = None
    stop_policy: DeterministicStopPolicy | None = None
    planner_service: FeedbackPlannerService | None = None


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
    planner_service = services.planner_service or FeedbackPlannerService()
    # Compiled with ``checkpointer=None`` so this subgraph inherits the parent
    # search checkpointer when it is installed as a graph node below.
    planner_graph = build_feedback_planner_subgraph(planner_service).compile()
    critic_proxy_builder = services.critic_proxy_builder or CriticProxyBuilder(
        asset_store=services.generator_service.asset_store,
        app_store=services.app_store,
    )
    # The nested subgraph inherits the parent durable checkpoint when the root
    # graph is compiled with one; it writes only asset references and structured
    # keyed evaluations into the parent state.
    critic_graph = build_critic_subgraph(
        app_store=services.app_store,
        proxy_builder=critic_proxy_builder,
        critic_provider=critic_service,
        candidate_ranker=candidate_ranker,
    ).compile()

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
        raw_directives = state.get("active_directives", [])
        active_directives = normalize_active_directives(
            raw_directives if isinstance(raw_directives, list) else (),
            policy=planner_service.policy,
        )
        canonical_prompt, canonical_prompt_hash = compile_canonical_prompt(
            placement=placement,
            user_intent=state["user_intent"],
            reference_count=len(manifest.cat_references),
        )
        generation_prompt, generation_prompt_hash = compile_generation_prompt(
            canonical_prompt=canonical_prompt,
            active_directives=active_directives,
            human_feedback=(
                state.get("human_feedback")
                if isinstance(state.get("human_feedback"), str)
                else None
            ),
        )
        prompt_history = [
            dict(item)
            for item in state.get("prompt_history", [])
            if isinstance(item, dict) and item.get("round_index") != state["round_index"]
        ]
        prompt_history.append(
            PromptHistoryEntry(
                round_index=state["round_index"],
                canonical_prompt=canonical_prompt,
                canonical_prompt_hash=canonical_prompt_hash,
                generation_prompt=generation_prompt,
                generation_prompt_hash=generation_prompt_hash,
                canonical_template_version=CANONICAL_TEMPLATE_VERSION,
                active_directives=[
                    item.model_dump(mode="json") for item in active_directives
                ],
                active_directives_hash=stable_directives_hash(active_directives),
                human_feedback=(
                    state.get("human_feedback")
                    if isinstance(state.get("human_feedback"), str)
                    else None
                ),
                human_selected_candidate_id=(
                    state.get("human_selected_candidate_id")
                    if isinstance(state.get("human_selected_candidate_id"), str)
                    else None
                ),
                tuned=bool(active_directives or state.get("human_feedback")),
            ).model_dump(mode="json")
        )
        prompt_history.sort(key=lambda item: cast(int, item.get("round_index", 0)))
        assert_checkpoint_safe(prompt_history, path="prompt_history")
        if not fenced_update(
            state["search_id"],
            prompt_history=prompt_history,
        ):
            current = services.app_store.get_search(state["search_id"])
            return {"status": current.status.value}
        return {
            "canonical_prompt": canonical_prompt,
            "canonical_prompt_hash": canonical_prompt_hash,
            "canonical_template_version": CANONICAL_TEMPLATE_VERSION,
            "generation_prompt": generation_prompt,
            "generation_prompt_hash": generation_prompt_hash,
            "prompt_history": prompt_history,
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
            prompt=state["generation_prompt"],
            prompt_hash=state["generation_prompt_hash"],
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

    async def rank_round(state: SearchState) -> dict[str, object]:
        current = services.app_store.get_search(state["search_id"])
        if current.status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return {"status": current.status.value, "current_candidates": []}
        candidates = [CandidateRecord.model_validate(item) for item in state["current_candidates"]]
        evaluations = [
            CandidateEvaluation.model_validate(item) for item in state.get("evaluations", [])
        ]
        evaluation_by_candidate = {
            evaluation.candidate_id: evaluation for evaluation in evaluations
        }
        missing = [
            candidate.candidate_id
            for candidate in candidates
            if candidate.candidate_id not in evaluation_by_candidate
        ]
        if missing:
            raise RuntimeError(
                "Critic subgraph did not produce evaluations for current candidates: "
                + ", ".join(missing)
            )
        evaluations = [evaluation_by_candidate[candidate.candidate_id] for candidate in candidates]
        scores = {
            evaluation.candidate_id: candidate_ranker.score(evaluation).score
            for evaluation in evaluations
        }

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
        unavailable = [
            evaluation
            for evaluation in evaluations
            if "evaluation_unavailable" in evaluation.hard_constraint_failures
        ]
        available_count = len(evaluations) - len(unavailable)
        if unavailable and available_count < 2:
            decision = StopDecision(
                action=StopAction.HUMAN_REVIEW,
                reason="critic_evaluations_insufficient",
                round_index=state["round_index"],
                global_winner_id=(global_winner.candidate_id if global_winner else None),
                global_winner_score=(global_winner.score if global_winner else None),
                eligible=False,
                detail=(
                    "Fewer than two candidates have usable Critic evaluations after "
                    "bounded provider retries."
                ),
            )
        else:
            decision = stop_policy.decide(
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
        round_history.sort(key=lambda item: cast(int, item.get("round_index", 0)))
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
                    "global_winner_id": (global_winner.candidate_id if global_winner else None),
                },
            ),
        ]
        if current.global_winner_id != (global_winner.candidate_id if global_winner else None):
            transition_events.insert(
                1,
                (
                    f"round:{state['round_index']}:global-winner",
                    "search.global_winner.updated",
                    {
                        "round_index": state["round_index"],
                        "global_winner_id": (global_winner.candidate_id if global_winner else None),
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
            prompt_history=(
                state.get("prompt_history")
                if isinstance(state.get("prompt_history"), list)
                else None
            ),
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
            "stop_action": decision.action.value,
            "stop_reason": decision.reason,
        }

    async def prepare_feedback_planner(state: SearchState) -> dict[str, object]:
        """Project one selected winner into the bounded child-graph contract."""

        stopped = is_stopped(state["search_id"])
        if stopped is not None:
            return {"status": stopped.value}
        selected_candidate_id = state.get("global_winner_id") or state.get("round_winner_id")
        if not isinstance(selected_candidate_id, str):
            return {
                "stop_action": PlannerAction.HUMAN_REVIEW.value,
                "stop_reason": "planner_selection_unavailable",
                "planner_input": None,
            }
        raw_active_directives = state.get("active_directives", [])
        active_directives = normalize_active_directives(
            raw_active_directives if isinstance(raw_active_directives, list) else (),
            policy=planner_service.policy,
        )
        raw_attempted_categories = state.get("attempted_directive_categories", [])
        attempted_categories: list[DirectiveCategory] = []
        if isinstance(raw_attempted_categories, list):
            for category in raw_attempted_categories:
                if not isinstance(category, str):
                    continue
                try:
                    attempted_categories.append(DirectiveCategory(category))
                except ValueError:
                    continue
        selected_evaluation = next(
            (
                evaluation
                for evaluation in services.app_store.list_evaluations(state["search_id"])
                if evaluation.candidate_id == selected_candidate_id
            ),
            None,
        )
        planner_input = PlannerInput(
            search_id=state["search_id"],
            round_index=state["round_index"],
            selected_candidate_id=selected_candidate_id,
            global_winner_id=(
                state["global_winner_id"]
                if isinstance(state.get("global_winner_id"), str)
                else None
            ),
            canonical_prompt_hash=state["canonical_prompt_hash"],
            canonical_prompt_summary=(
                "Immutable-source placement prompt with identity and scene-preservation "
                "constraints; detailed user text remains outside the planner contract."
            ),
            blocking_issues=(),
            active_directives=active_directives,
            attempted_categories=tuple(attempted_categories),
            directive_policy=planner_service.policy,
            directive_version=state.get("directive_version", 0),
            fallback_attempts=state.get("planner_fallback_attempts", 0),
        )
        payload: dict[str, object] = {
            "planner_input": planner_input.model_dump(mode="json"),
            "selected_evaluation": (
                selected_evaluation.model_dump(mode="json")
                if selected_evaluation is not None
                else None
            ),
            "selected_blocking_issues": [],
            "planner_proposal": None,
            "validated_planner_result": None,
            "planner_result": None,
            "planner_round_index": None,
        }
        assert_checkpoint_safe(payload, path="feedback_planner.input")
        return payload

    async def apply_feedback_plan(state: SearchState) -> dict[str, object]:
        """Persist a child-graph result through the search lease fence."""

        stopped = is_stopped(state["search_id"])
        if stopped is not None:
            return {"status": stopped.value}
        planner_payload = state.get("planner_result")
        planner_input_payload = state.get("planner_input")
        if not isinstance(planner_payload, dict) or not isinstance(planner_input_payload, dict):
            return {
                "stop_action": PlannerAction.HUMAN_REVIEW.value,
                "stop_reason": "planner_result_unavailable",
            }
        result = PlannerResult.model_validate(planner_payload)
        planner_input = PlannerInput.model_validate(planner_input_payload)
        validated_payload = state.get("validated_planner_result")
        validated = (
            PlannerResult.model_validate(validated_payload)
            if isinstance(validated_payload, dict)
            else result
        )
        directives_payload = [item.model_dump(mode="json") for item in result.directives]
        current = services.app_store.get_search(state["search_id"])
        round_history = [dict(item) for item in current.round_history]
        history_entry = next(
            (item for item in round_history if item.get("round_index") == state["round_index"]),
            None,
        )
        if history_entry is None:
            history_entry = {"round_index": state["round_index"]}
            round_history.append(history_entry)
        planned_categories = (
            [item.category.value for item in validated.directives]
            if result.action is PlannerAction.CONTINUE
            else []
        )
        fallback_attempts_after = max(
            planner_input.fallback_attempts,
            1 if result.fallback_used else 0,
        )
        history_entry.update(
            {
                "planner_action": result.action.value,
                "planner_stop_reason": result.stop_reason,
                "planned_categories": planned_categories,
                "directive_version": result.directive_version,
                "directive_policy_version": result.directive_policy_version,
                "active_directives_hash": result.active_directives_hash,
                "planner_fallback_used": result.fallback_used,
                "planner_fallback_attempts": fallback_attempts_after,
            }
        )
        round_history.sort(key=lambda item: cast(int, item.get("round_index", 0)))
        raw_attempted_categories = state.get("attempted_directive_categories", [])
        previous_attempts = (
            [value for value in raw_attempted_categories if isinstance(value, str)]
            if isinstance(raw_attempted_categories, list)
            else []
        )
        attempted_after = [*previous_attempts, *planned_categories]
        event_payload: dict[str, object] = {
            "round_index": state["round_index"],
            "selected_candidate_id": planner_input.selected_candidate_id,
            "action": result.action.value,
            "stop_reason": result.stop_reason,
            "directive_policy_version": result.directive_policy_version,
            "directive_version": result.directive_version,
            "active_directives_hash": result.active_directives_hash,
            "directives": [
                {
                    "directive_id": item.directive_id,
                    "category": item.category.value,
                    "priority": item.priority,
                    "replaces_category": (
                        item.replaces_category.value if item.replaces_category else None
                    ),
                }
                for item in result.directives
            ],
        }
        if not fenced_update(
            state["search_id"],
            active_directives=directives_payload,
            round_history=round_history,
            prompt_history=(
                state.get("prompt_history")
                if isinstance(state.get("prompt_history"), list)
                else None
            ),
            stop_reason=result.stop_reason or state.get("stop_reason"),
            events=(
                (
                    f"round:{state['round_index']}:planner",
                    "search.planner.ready",
                    event_payload,
                ),
            ),
        ):
            latest = services.app_store.get_search(state["search_id"])
            return {"status": latest.status.value}
        payload: dict[str, object] = {
            "round_history": round_history,
            "active_directives": directives_payload,
            "active_directives_hash": result.active_directives_hash,
            "directive_policy_version": result.directive_policy_version,
            "directive_version": result.directive_version,
            "attempted_directive_categories": attempted_after,
            "planner_round_index": state["round_index"],
            "planner_fallback_attempts": fallback_attempts_after,
            "stop_action": result.action.value,
            "stop_reason": result.stop_reason or state.get("stop_reason"),
        }
        assert_checkpoint_safe(payload, path="feedback_planner.output")
        return payload

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
            "evaluations_by_candidate": empty_evaluation_bucket(next_round),
            "evaluations": [],
            "round_winner_id": None,
            "stop_action": None,
            "stop_reason": None,
            "planner_input": None,
            "selected_evaluation": None,
            "selected_blocking_issues": [],
            "human_feedback": None,
            "human_selected_candidate_id": None,
            "planner_proposal": None,
            "validated_planner_result": None,
            "planner_result": None,
            "planner_round_index": None,
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
        if state.get("current_candidates"):
            allowed_actions.insert(0, "accept_candidate")
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
                for issue_id in cast(list[object], evaluation.get("hard_constraint_failures", []))
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
            prompt_history=(
                state.get("prompt_history")
                if isinstance(state.get("prompt_history"), list)
                else None
            ),
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

    def route_after_evaluate(state: SearchState) -> str:
        persisted_status = services.app_store.get_search(state["search_id"]).status
        if state.get("status") in {
            SearchStatus.CANCELLED.value,
            SearchStatus.ACCEPTED.value,
        } or persisted_status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return "finalize"
        if (
            state.get("stop_action") == PlannerAction.CONTINUE.value
            and state["round_index"] + 1 < state["max_rounds"]
        ):
            return "prepare_feedback_planner"
        return "finalize"

    def route_after_planner_input(state: SearchState) -> str:
        persisted_status = services.app_store.get_search(state["search_id"]).status
        if state.get("status") in {
            SearchStatus.CANCELLED.value,
            SearchStatus.ACCEPTED.value,
        } or persisted_status in {SearchStatus.CANCELLED, SearchStatus.ACCEPTED}:
            return "finalize"
        return "feedback_planner" if isinstance(state.get("planner_input"), dict) else "finalize"

    graph = StateGraph(SearchState)
    graph.add_node("initialize_search", initialize_search)
    graph.add_node("compile_canonical_prompt", compile_prompt)
    graph.add_node("prepare_round", prepare_round)
    graph.add_node("generate_candidates", generate_candidates)
    graph.add_node("critic_subgraph", critic_graph)
    graph.add_node("rank_round", rank_round)
    graph.add_node("prepare_feedback_planner", prepare_feedback_planner)
    graph.add_node("feedback_planner", planner_graph)
    graph.add_node("apply_feedback_plan", apply_feedback_plan)
    graph.add_node("prepare_next_round", prepare_next_round)
    graph.add_node("finalize_mock_round", finalize_mock_round)
    graph.add_edge(START, "initialize_search")
    graph.add_edge("initialize_search", "compile_canonical_prompt")
    graph.add_edge("compile_canonical_prompt", "prepare_round")
    graph.add_edge("prepare_round", "generate_candidates")
    graph.add_edge("generate_candidates", "critic_subgraph")
    graph.add_edge("critic_subgraph", "rank_round")
    graph.add_conditional_edges(
        "rank_round",
        route_after_evaluate,
        {
            "prepare_feedback_planner": "prepare_feedback_planner",
            "finalize": "finalize_mock_round",
        },
    )
    graph.add_conditional_edges(
        "prepare_feedback_planner",
        route_after_planner_input,
        {"feedback_planner": "feedback_planner", "finalize": "finalize_mock_round"},
    )
    graph.add_edge("feedback_planner", "apply_feedback_plan")
    graph.add_edge("apply_feedback_plan", "finalize_mock_round")
    graph.add_conditional_edges(
        "finalize_mock_round",
        route_after_finalize,
        {"prepare_next_round": "prepare_next_round", "end": END},
    )
    graph.add_edge("prepare_next_round", "compile_canonical_prompt")
    return graph
