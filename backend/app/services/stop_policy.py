"""Deterministic routing policy for future multi-round search loops."""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.evaluations import (
    CandidateEvaluation,
    GlobalWinner,
    RankingPolicy,
    RoundRanking,
    Severity,
    StopAction,
    StopDecision,
)
from app.services.candidate_ranker import DeterministicCandidateRanker


class DeterministicStopPolicy:
    """Only blocking findings can authorize automatic regeneration."""

    def __init__(self, policy: RankingPolicy | None = None) -> None:
        self.policy = policy or RankingPolicy()

    def decide(
        self,
        *,
        ranking: RoundRanking,
        evaluations: Iterable[CandidateEvaluation],
        global_winner: GlobalWinner | None,
        max_rounds: int,
    ) -> StopDecision:
        evaluation_list = tuple(evaluations)
        evaluations_by_candidate = {
            evaluation.candidate_id: evaluation for evaluation in evaluation_list
        }
        target_id = (
            global_winner.candidate_id
            if global_winner is not None
            else ranking.winner_id
        )
        target_evaluation = evaluations_by_candidate.get(target_id) if target_id else None
        blocking = (
            target_evaluation.blocking_issues
            if target_evaluation is not None
            else tuple(
                issue
                for evaluation in evaluation_list
                for issue in evaluation.issues
                if issue.severity is Severity.BLOCKING
            )
        )
        if any(evaluation.semantic_conflict for evaluation in evaluation_list):
            return StopDecision(
                action=StopAction.HUMAN_REVIEW,
                reason="semantic_conflict",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else None,
                global_winner_score=global_winner.score if global_winner else None,
                blocking_issue_ids=tuple(issue.issue_id for issue in blocking),
                eligible=False,
                detail="Critic output contains a no-defect/blocking semantic conflict.",
            )

        if target_id is not None and target_evaluation is None:
            return StopDecision(
                action=StopAction.HUMAN_REVIEW,
                reason="evaluation_unavailable",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else None,
                global_winner_score=global_winner.score if global_winner else None,
                eligible=False,
                detail="The selected winner has no structured evaluation in policy input.",
            )

        target_is_eligible = (
            target_evaluation is not None
            and DeterministicCandidateRanker(self.policy).score(target_evaluation).eligible
        )

        if (
            global_winner is not None
            and global_winner.score >= self.policy.accept_threshold
            and not blocking
            and target_is_eligible
        ):
            return StopDecision(
                action=StopAction.STOP,
                reason="accept_threshold",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id,
                global_winner_score=global_winner.score,
                detail="Historical Global Winner meets the deterministic accept threshold.",
            )

        if (
            target_evaluation is not None
            and target_evaluation.no_meaningful_defect
            and not blocking
            and target_is_eligible
        ):
            return StopDecision(
                action=StopAction.STOP,
                reason="no_meaningful_defect",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else target_id,
                global_winner_score=global_winner.score if global_winner else ranking.winner_score,
                detail="Critic found no meaningful defect and deterministic checks passed.",
            )

        if ranking.round_index + 1 >= max_rounds:
            return StopDecision(
                action=(StopAction.STOP if global_winner is not None else StopAction.HUMAN_REVIEW),
                reason="max_rounds",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else None,
                global_winner_score=global_winner.score if global_winner else None,
                blocking_issue_ids=tuple(issue.issue_id for issue in blocking),
                eligible=global_winner is not None,
                detail=(
                    "Maximum configured rounds reached; preserve historical winner."
                    if global_winner is not None
                    else "Maximum configured rounds reached without an eligible winner."
                ),
            )

        if blocking:
            return StopDecision(
                action=StopAction.CONTINUE,
                reason="blocking_issues",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else None,
                global_winner_score=global_winner.score if global_winner else None,
                blocking_issue_ids=tuple(issue.issue_id for issue in blocking),
                planner_required=True,
                detail="Only blocking issues authorize an automatic regeneration round.",
            )

        return StopDecision(
            action=StopAction.HUMAN_REVIEW,
            reason="no_blocking_issue",
            round_index=ranking.round_index,
            global_winner_id=global_winner.candidate_id if global_winner else None,
            global_winner_score=global_winner.score if global_winner else None,
            detail="Warnings and info findings do not authorize automatic regeneration.",
        )
