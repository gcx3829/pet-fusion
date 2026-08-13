"""Explicit, checkpoint-safe LangGraph subgraph for narrow feedback planning."""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.directives import PlannerInput, PlannerProposal, PlannerResult
from app.domain.evaluations import CandidateEvaluation
from app.graphs.state import assert_checkpoint_safe
from app.services.planner_service import (
    FeedbackPlannerService,
    select_actionable_blocking_issues,
)


class FeedbackPlannerState(TypedDict, total=False):
    """Only structured planner and evaluation projections may cross this graph."""

    planner_input: dict[str, object]
    selected_evaluation: dict[str, object] | None
    selected_blocking_issues: list[dict[str, object]]
    planner_proposal: dict[str, object]
    validated_planner_result: dict[str, object]
    planner_result: dict[str, object]
    active_directives: list[dict[str, object]]


def build_feedback_planner_subgraph(
    planner_service: FeedbackPlannerService,
) -> StateGraph[FeedbackPlannerState]:
    """Build the fixed feedback path required by the search control plane."""

    async def select_actionable_blocking_issues_node(
        state: FeedbackPlannerState,
    ) -> dict[str, object]:
        planner_input = PlannerInput.model_validate(state["planner_input"])
        selected_evaluation_payload = state.get("selected_evaluation")
        selected_evaluation = (
            CandidateEvaluation.model_validate(selected_evaluation_payload)
            if isinstance(selected_evaluation_payload, dict)
            else None
        )
        selected = select_actionable_blocking_issues(
            selected_candidate_id=planner_input.selected_candidate_id,
            evaluations=(selected_evaluation,) if selected_evaluation is not None else (),
        )
        selected_issue_payload = [item.model_dump(mode="json") for item in selected]
        updated_input = planner_input.model_copy(update={"blocking_issues": selected})
        payload: dict[str, object] = {
            "planner_input": updated_input.model_dump(mode="json"),
            "selected_blocking_issues": selected_issue_payload,
        }
        assert_checkpoint_safe(payload, path="feedback_planner.selection")
        return payload

    async def plan_directives(state: FeedbackPlannerState) -> dict[str, object]:
        planner_input = PlannerInput.model_validate(state["planner_input"])
        proposal = planner_service.request_proposal(planner_input)
        payload = proposal.model_dump(mode="json")
        assert_checkpoint_safe(payload, path="feedback_planner.proposal")
        return {"planner_proposal": payload}

    async def validate_directive_budget(state: FeedbackPlannerState) -> dict[str, object]:
        planner_input = PlannerInput.model_validate(state["planner_input"])
        proposal = PlannerProposal.model_validate(state["planner_proposal"])
        validated = planner_service.validate_directive_budget(planner_input, proposal)
        payload = validated.model_dump(mode="json")
        assert_checkpoint_safe(payload, path="feedback_planner.validation")
        return {"validated_planner_result": payload}

    async def replace_or_retain_directives(state: FeedbackPlannerState) -> dict[str, object]:
        planner_input = PlannerInput.model_validate(state["planner_input"])
        validated = PlannerResult.model_validate(state["validated_planner_result"])
        result = planner_service.replace_or_retain_directives(planner_input, validated)
        payload = result.model_dump(mode="json")
        assert_checkpoint_safe(payload, path="feedback_planner.replacement")
        return {"planner_result": payload}

    async def emit_next_round_plan(state: FeedbackPlannerState) -> dict[str, object]:
        result = PlannerResult.model_validate(state["planner_result"])
        directives = [item.model_dump(mode="json") for item in result.directives]
        payload: dict[str, object] = {"active_directives": directives}
        assert_checkpoint_safe(payload, path="feedback_planner.output")
        return payload

    graph = StateGraph(FeedbackPlannerState)
    graph.add_node(
        "select_actionable_blocking_issues", select_actionable_blocking_issues_node
    )
    graph.add_node("plan_directives", plan_directives)
    graph.add_node("validate_directive_budget", validate_directive_budget)
    graph.add_node("replace_or_retain_directives", replace_or_retain_directives)
    graph.add_node("emit_next_round_plan", emit_next_round_plan)
    graph.add_edge(START, "select_actionable_blocking_issues")
    graph.add_edge("select_actionable_blocking_issues", "plan_directives")
    graph.add_edge("plan_directives", "validate_directive_budget")
    graph.add_edge("validate_directive_budget", "replace_or_retain_directives")
    graph.add_edge("replace_or_retain_directives", "emit_next_round_plan")
    graph.add_edge("emit_next_round_plan", END)
    return graph
