"""Deterministic candidate scoring and historical Global Winner policy."""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.evaluations import (
    CandidateEvaluation,
    CandidateScore,
    GlobalWinner,
    RankingPolicy,
    RoundRanking,
)

ANATOMY_CATEGORIES = frozenset({"anatomy", "pose_geometry"})
SCENE_PRESERVATION_HARD_FAIL_CONFIDENCE = 0.75


class DeterministicCandidateRanker:
    """Rank only normalized structured evaluations; never calls a model."""

    def __init__(self, policy: RankingPolicy | None = None) -> None:
        self.policy = policy or RankingPolicy()

    def score(self, evaluation: CandidateEvaluation) -> CandidateScore:
        weights = self.policy.weights.as_mapping()
        base_score = sum(
            evaluation.scores.as_mapping()[dimension] * weight
            for dimension, weight in weights.items()
        )
        blocking = evaluation.blocking_issues
        score = max(0.0, min(100.0, base_score - self.policy.blocking_penalty * len(blocking)))
        failures = list(evaluation.hard_constraint_failures)
        if not evaluation.identity_match:
            failures.append("identity_mismatch")
        if evaluation.scores.cat_identity < self.policy.identity_threshold:
            failures.append("identity_below_threshold")
        if not evaluation.prompt_adherent:
            failures.append("prompt_not_adherent")
        for issue in blocking:
            if issue.category in ANATOMY_CATEGORIES or (
                issue.category == "scene_preservation"
                and issue.confidence >= SCENE_PRESERVATION_HARD_FAIL_CONFIDENCE
            ):
                failures.append(f"blocking:{issue.category}")
        if evaluation.semantic_conflict:
            failures.append("critic_semantic_conflict")
        unique_failures = tuple(dict.fromkeys(failures))
        return CandidateScore(
            candidate_id=evaluation.candidate_id,
            round_index=evaluation.round_index,
            base_score=round(base_score, 4),
            score=round(score, 4),
            eligible=not unique_failures,
            hard_fail_reasons=unique_failures,
            blocking_issue_ids=tuple(issue.issue_id for issue in blocking),
        )

    def rank_round(
        self, evaluations: Iterable[CandidateEvaluation], *, round_index: int
    ) -> RoundRanking:
        evaluation_list = tuple(evaluations)
        if any(evaluation.round_index != round_index for evaluation in evaluation_list):
            raise ValueError("all evaluations must belong to the ranked round")
        candidate_ids = [evaluation.candidate_id for evaluation in evaluation_list]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate evaluations must be unique within a round")
        scored = [self.score(evaluation) for evaluation in evaluation_list]
        scored.sort(key=lambda item: (-item.eligible, -item.score, item.candidate_id))
        winner = next((item for item in scored if item.eligible), None)
        return RoundRanking(
            round_index=round_index,
            scores=tuple(scored),
            winner_id=winner.candidate_id if winner else None,
            winner_score=winner.score if winner else None,
        )

    def update_global_winner(
        self,
        current: GlobalWinner | None,
        ranking: RoundRanking,
    ) -> GlobalWinner | None:
        if ranking.winner_id is None or ranking.winner_score is None:
            return current
        proposal = GlobalWinner(
            candidate_id=ranking.winner_id,
            score=ranking.winner_score,
            round_index=ranking.round_index,
        )
        if current is None:
            return proposal
        if proposal.score >= current.score + self.policy.minimum_improvement:
            return proposal
        return current

    def is_tie(self, ranking: RoundRanking) -> bool:
        eligible = [item for item in ranking.scores if item.eligible]
        return (
            len(eligible) >= 2
            and abs(eligible[0].score - eligible[1].score) < self.policy.tie_margin
        )
