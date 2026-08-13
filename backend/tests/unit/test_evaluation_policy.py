import pytest

from app.container import AppContainer
from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.errors import ConflictError
from app.domain.evaluations import (
    CandidateEvaluation,
    CriticIssue,
    DimensionScores,
    GlobalWinner,
    RankingPolicy,
    Severity,
    StopAction,
)
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import utcnow
from app.services.asset_store import AssetStore
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import CriticInput, DeterministicCriticService
from app.services.stop_policy import DeterministicStopPolicy
from tests.conftest import make_image_bytes


def make_evaluation(
    candidate_id: str,
    *,
    round_index: int = 0,
    score_bias: float = 0,
    issue: CriticIssue | None = None,
    no_meaningful_defect: bool = True,
) -> CandidateEvaluation:
    scores = DimensionScores(
        cat_identity=92 + score_bias,
        pose_geometry=90 + score_bias,
        perspective_scale=91 + score_bias,
        lighting_color=89 + score_bias,
        optical_consistency=90 + score_bias,
        physical_integration=88 + score_bias,
        scene_preservation=96,
        overall_photographic_naturalness=90 + score_bias,
    )
    return CandidateEvaluation(
        rubric_version="critic-rubric/test",
        candidate_id=candidate_id,
        round_index=round_index,
        scores=scores,
        issues=(issue,) if issue else (),
        no_meaningful_defect=no_meaningful_defect,
        identity_match=True,
        prompt_adherent=True,
        recommended_action="review",
        summary="fixture",
    )


def test_ranker_is_stable_and_keeps_historical_winner() -> None:
    ranker = DeterministicCandidateRanker()
    first = make_evaluation("candidate-a", score_bias=3)
    second = make_evaluation("candidate-b", score_bias=-2)
    ranking = ranker.rank_round([second, first], round_index=0)

    assert ranking.winner_id == "candidate-a"
    assert ranker.rank_round([first, second], round_index=0).scores == ranking.scores

    existing = GlobalWinner(candidate_id="historical", score=99, round_index=0)
    later = ranker.rank_round([make_evaluation("candidate-c", round_index=1)], round_index=1)
    assert ranker.update_global_winner(existing, later) == existing


def test_blocking_issue_is_the_only_automatic_regeneration_trigger() -> None:
    issue = CriticIssue(
        issue_id="seam",
        category="physical_integration",
        severity=Severity.BLOCKING,
        evidence="Visible seam",
        suggested_fix="Match the local edge sharpness.",
        confidence=0.95,
    )
    ranker = DeterministicCandidateRanker(RankingPolicy(accept_threshold=99.9))
    blocking_eval = make_evaluation("candidate-blocking", issue=issue, no_meaningful_defect=False)
    blocking_ranking = ranker.rank_round([blocking_eval], round_index=0)
    decision = DeterministicStopPolicy(ranker.policy).decide(
        ranking=blocking_ranking,
        evaluations=[blocking_eval],
        global_winner=None,
        max_rounds=3,
    )
    assert decision.action is StopAction.CONTINUE
    assert decision.planner_required is True

    warning = issue.model_copy(update={"severity": Severity.WARNING})
    warning_eval = make_evaluation(
        "candidate-warning", issue=warning, no_meaningful_defect=False
    )
    warning_ranking = ranker.rank_round([warning_eval], round_index=0)
    warning_decision = DeterministicStopPolicy(ranker.policy).decide(
        ranking=warning_ranking,
        evaluations=[warning_eval],
        global_winner=None,
        max_rounds=3,
    )
    assert warning_decision.action is StopAction.HUMAN_REVIEW


def test_ranker_applies_only_documented_blocking_hard_constraints() -> None:
    ranker = DeterministicCandidateRanker()
    seam = CriticIssue(
        issue_id="seam",
        category="physical_integration",
        severity=Severity.BLOCKING,
        evidence="Visible seam",
        confidence=0.9,
    )
    assert ranker.score(
        make_evaluation("repairable", issue=seam, no_meaningful_defect=False)
    ).eligible

    scene = seam.model_copy(
        update={"issue_id": "scene", "category": "scene_preservation"}
    )
    assert not ranker.score(
        make_evaluation("scene-fail", issue=scene, no_meaningful_defect=False)
    ).eligible


def test_stop_policy_uses_selected_winner_findings_only() -> None:
    ranker = DeterministicCandidateRanker(RankingPolicy(accept_threshold=99.9))
    losing_issue = CriticIssue(
        issue_id="loser-seam",
        category="physical_integration",
        severity=Severity.BLOCKING,
        evidence="Visible seam on losing candidate",
        confidence=0.9,
    )
    winner = make_evaluation("winner", score_bias=3)
    loser = make_evaluation(
        "loser", score_bias=-3, issue=losing_issue, no_meaningful_defect=False
    )
    ranking = ranker.rank_round([loser, winner], round_index=0)
    global_winner = ranker.update_global_winner(None, ranking)
    decision = DeterministicStopPolicy(ranker.policy).decide(
        ranking=ranking,
        evaluations=[loser, winner],
        global_winner=global_winner,
        max_rounds=3,
    )
    assert decision.action is StopAction.STOP
    assert decision.reason == "no_meaningful_defect"


def test_low_confidence_blocking_is_normalized_to_warning() -> None:
    issue = CriticIssue(
        issue_id="uncertain",
        category="scene_preservation",
        severity=Severity.BLOCKING,
        evidence="Possible background drift",
        confidence=0.4,
    )
    evaluation = make_evaluation(
        "candidate-uncertain", issue=issue, no_meaningful_defect=False
    )
    assert evaluation.issues[0].severity is Severity.WARNING
    assert not evaluation.has_blocking_issue


def test_evaluation_checkpoint_payload_contains_no_image_bytes() -> None:
    evaluation = make_evaluation("candidate-safe")
    payload = evaluation.model_dump(mode="json")
    assert_checkpoint_safe(payload)
    assert "png_bytes" not in str(payload)


def test_fake_critic_is_reproducible_and_uses_asset_references(tmp_path) -> None:
    store = AssetStore(tmp_path / "assets", max_image_pixels=1_000_000)
    store.initialize()
    background = store.put_image_bytes(make_image_bytes(size=(96, 64)))
    reference = store.put_image_bytes(make_image_bytes((10, 20, 30), size=(32, 32)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    candidate = CandidateRecord(
        candidate_id="candidate-fixture",
        round_index=0,
        variant_index=0,
        raw_asset=background,
        protected_asset=background,
        prompt_hash="a" * 64,
        request_key="b" * 64,
        model="fake-gpt-image-2",
        quality="medium",
        size="96x64",
    )
    request = CriticInput(
        candidate=candidate,
        source_manifest=manifest,
        placement=PlacementIntent(
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        ),
    )
    service = DeterministicCriticService()
    first = service.evaluate(request)
    second = service.evaluate(request)
    assert first == second
    assert first.source_manifest_hash == manifest.manifest_hash
    assert first.is_checkpoint_safe


def test_evaluation_persistence_enforces_candidate_lineage(settings, fake_generator) -> None:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((20, 30, 40)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="project-evaluation", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    request = CreateSearchRequest(
        placement=PlacementIntent(
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        ),
        user_intent="same cat",
    )
    for search_id in ("search-evaluation", "search-other"):
        container.app_store.create_search(
            search_id=search_id,
            thread_id=search_id,
            project=project,
            request=request,
        )
    candidate = CandidateRecord(
        candidate_id="candidate-persisted",
        round_index=0,
        variant_index=0,
        raw_asset=background,
        protected_asset=background,
        prompt_hash="a" * 64,
        request_key="b" * 64,
        model="fake-gpt-image-2",
        quality="medium",
        size="96x64",
    )
    container.app_store.add_candidate("search-evaluation", candidate)
    evaluation = make_evaluation(candidate.candidate_id).model_copy(
        update={"source_manifest_hash": manifest.manifest_hash}
    )

    container.app_store.save_evaluation("search-evaluation", evaluation, score=91.0)
    container.app_store.save_evaluation("search-evaluation", evaluation, score=91.0)
    assert container.app_store.list_evaluations("search-evaluation") == [evaluation]

    with pytest.raises(ConflictError):
        container.app_store.save_evaluation("search-other", evaluation, score=91.0)
    with pytest.raises(ConflictError):
        container.app_store.save_evaluation(
            "search-evaluation",
            evaluation.model_copy(update={"round_index": 1}),
            score=91.0,
        )
    with pytest.raises(ValueError):
        container.app_store.save_evaluation("search-evaluation", evaluation, score=101.0)
