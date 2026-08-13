from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.config import Settings
from app.domain.evaluations import (
    CandidateEvaluation,
    CriticIssue,
    Severity,
    StopAction,
    StopDecision,
)
from app.domain.searches import SearchStatus
from app.main import create_app
from app.services.critic_service import CriticInput, DeterministicCriticService
from app.services.stop_policy import DeterministicStopPolicy


class ContinueOncePolicy(DeterministicStopPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.seen_evaluations: list[tuple[str, ...]] = []

    def decide(self, *, ranking, evaluations, global_winner, max_rounds):  # type: ignore[no-untyped-def]
        evaluation_ids = tuple(item.candidate_id for item in evaluations)
        self.seen_evaluations.append(evaluation_ids)
        if ranking.round_index == 0:
            return StopDecision(
                action=StopAction.CONTINUE,
                reason="blocking_issues",
                round_index=ranking.round_index,
                global_winner_id=global_winner.candidate_id if global_winner else None,
                global_winner_score=global_winner.score if global_winner else None,
                planner_required=True,
            )
        return StopDecision(
            action=StopAction.STOP,
            reason="max_rounds",
            round_index=ranking.round_index,
            global_winner_id=global_winner.candidate_id if global_winner else None,
            global_winner_score=global_winner.score if global_winner else None,
        )


class LosingBlockingCritic(DeterministicCriticService):
    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        result = super().evaluate(request)
        if request.candidate.variant_index != 1:
            return result
        issue = CriticIssue(
            issue_id="losing-seam",
            category="physical_integration",
            severity=Severity.BLOCKING,
            evidence="Fixture seam on a losing candidate.",
            suggested_fix="Match local edge sharpness.",
            confidence=0.95,
        )
        return result.model_copy(
            update={
                "issues": (issue,),
                "no_meaningful_defect": False,
                "recommended_action": "regenerate",
            }
        )


def _project(client: TestClient, project_payload) -> dict[str, object]:
    response = client.post(
        "/api/v1/projects",
        files=project_payload,
        data={"cat_name": "Mochi", "cat_traits": "striped tail"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_continue_rebases_to_original_manifest_and_keeps_global_winner(
    client: TestClient, project_payload, search_payload, fake_generator
) -> None:
    project = _project(client, project_payload)
    payload = {**search_payload, "max_rounds": 2, "candidate_count": 2}
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=payload,
        headers={"Idempotency-Key": "two-round-search"},
    )
    assert response.status_code == 201
    search_id = response.json()["search_id"]

    first = client.get(f"/api/v1/searches/{search_id}").json()
    assert first["status"] == "waiting_for_human"
    assert first["round_index"] == 0
    assert first["global_winner_id"] is not None

    continued = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "continue_one_round"},
    )
    assert continued.status_code == 200, continued.text
    second = client.get(f"/api/v1/searches/{search_id}").json()
    assert second["status"] == "waiting_for_human"
    assert second["round_index"] == 1
    assert len(second["round_history"]) == 2
    assert second["global_winner_id"] == first["global_winner_id"]
    assert fake_generator.call_count == 2
    assert len(fake_generator.requests) == 2
    assert (
        fake_generator.requests[0].source_manifest.manifest_hash
        == fake_generator.requests[1].source_manifest.manifest_hash
        == project["source_manifest"]["manifest_hash"]
    )
    events = client.get(f"/api/v1/searches/{search_id}/events").text
    assert events.count("event: round.evaluation.ready") == 4
    assert "event: search.global_winner.updated" in events
    assert "data:image" not in events.lower()


def test_accept_and_cancel_resume_actions_are_idempotent(
    client: TestClient, project_payload, search_payload
) -> None:
    project = _project(client, project_payload)
    created = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "accept-search"},
    ).json()
    search_id = created["search_id"]

    accepted = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "accept_global_winner"},
    )
    replay = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "accept_global_winner"},
    )
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json()["status"] == replay.json()["status"] == "accepted"
    events = client.get(f"/api/v1/searches/{search_id}/events").text
    assert events.count("event: search.accepted") == 1

    cancelled = client.post(
        f"/api/v1/searches/{search_id}/resume", json={"action": "cancel"}
    )
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "CONFLICT"


def test_review_false_automatically_runs_next_round_and_supplies_historical_evaluation(
    tmp_path, project_payload, search_payload, fake_generator
) -> None:
    policy = ContinueOncePolicy()
    settings = Settings(data_dir=tmp_path / "automatic", run_inline=True)
    app = create_app(settings, image_generator=fake_generator, stop_policy=policy)
    with TestClient(app) as client:
        project = _project(client, project_payload)
        payload = {**search_payload, "max_rounds": 2, "candidate_count": 2}
        created = client.post(
            f"/api/v1/projects/{project['project_id']}/searches",
            json=payload,
            headers={"Idempotency-Key": "automatic-two-rounds"},
        ).json()
        search = client.get(f"/api/v1/searches/{created['search_id']}").json()

    assert search["status"] == "waiting_for_human"
    assert search["round_index"] == 1
    assert len(search["round_history"]) == 2
    assert fake_generator.call_count == 2
    assert len(policy.seen_evaluations) == 2
    assert len(policy.seen_evaluations[1]) == 4
    assert set(policy.seen_evaluations[0]).issubset(set(policy.seen_evaluations[1]))
    checkpoint_path = settings.resolved_checkpoint_db_path
    with sqlite3.connect(checkpoint_path) as connection:
        thread_ids = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id = ?",
                (created["search_id"],),
            )
        }
    assert thread_ids == {created["search_id"]}

    container = app.state.container
    container.app_store.update_search(created["search_id"], status=SearchStatus.RUNNING)
    asyncio.run(container.search_runner.run_search(created["search_id"]))
    replayed = container.app_store.get_search(created["search_id"])
    assert len(replayed.round_history) == 2


def test_losing_blocking_candidate_does_not_create_selected_directive(
    tmp_path, project_payload, search_payload, fake_generator
) -> None:
    settings = Settings(data_dir=tmp_path / "selected-directives", run_inline=True)
    app = create_app(
        settings,
        image_generator=fake_generator,
        critic_service=LosingBlockingCritic(),
    )
    with TestClient(app) as client:
        project = _project(client, project_payload)
        created = client.post(
            f"/api/v1/projects/{project['project_id']}/searches",
            json={**search_payload, "candidate_count": 2},
            headers={"Idempotency-Key": "losing-directive-search"},
        ).json()
        search = client.get(f"/api/v1/searches/{created['search_id']}").json()
    assert search["global_winner_id"] == search["round_winner_id"]
    assert search["active_directives"] == []


def test_cancel_fences_waiting_state_and_events_in_one_transaction(
    tmp_path, project_payload, search_payload, fake_generator
) -> None:
    settings = Settings(data_dir=tmp_path / "atomic-events", run_inline=False)
    app = create_app(settings, image_generator=fake_generator)
    with TestClient(app) as client:
        project = _project(client, project_payload)
        created = client.post(
            f"/api/v1/projects/{project['project_id']}/searches",
            json=search_payload,
            headers={"Idempotency-Key": "cancel-before-waiting-transition"},
        ).json()

    search_id = created["search_id"]
    store = app.state.container.app_store
    assert store.claim_search(search_id=search_id, worker_id="race-worker", lease_seconds=30)
    assert store.cancel_search(search_id)
    assert not store.update_search(
        search_id,
        status=SearchStatus.WAITING_FOR_HUMAN,
        expected_statuses=(SearchStatus.RUNNING,),
        expected_lease_owner="race-worker",
        events=(
            ("search:waiting-for-human", "search.waiting_for_human", {"round_index": 0}),
            ("search:interrupted:0", "search.interrupted", {"round_index": 0}),
        ),
    )

    event_types = [event.type for event in store.list_events(search_id)]
    assert event_types[-1] == "search.cancelled"
    assert "search.waiting_for_human" not in event_types
    assert "search.interrupted" not in event_types


def test_cancelled_search_ends_before_automatic_next_round(
    tmp_path,
    project_payload,
    search_payload,
    fake_generator,
    monkeypatch: MonkeyPatch,
) -> None:
    policy = ContinueOncePolicy()
    settings = Settings(data_dir=tmp_path / "cancelled-auto-route", run_inline=False)
    app = create_app(settings, image_generator=fake_generator, stop_policy=policy)
    with TestClient(app) as client:
        store = app.state.container.app_store
        original_update = store.update_search

        def update_then_cancel(search_id: str, **kwargs: Any) -> bool:
            updated = original_update(search_id, **kwargs)
            if updated and kwargs.get("stop_reason") == "blocking_issues":
                assert store.cancel_search(search_id)
            return updated

        monkeypatch.setattr(store, "update_search", update_then_cancel)
        project = _project(client, project_payload)
        created = client.post(
            f"/api/v1/projects/{project['project_id']}/searches",
            json={**search_payload, "max_rounds": 2},
            headers={"Idempotency-Key": "cancel-before-auto-route"},
        ).json()
        search_id = created["search_id"]
        worker_id = "cancel-route-worker"
        assert store.claim_search(
            search_id=search_id, worker_id=worker_id, lease_seconds=30
        )
        result = asyncio.run(
            app.state.container.search_runner.run_with_lease(
                search_id=search_id,
                worker_id=worker_id,
                lease_seconds=30,
            )
        )

        assert result["status"] == SearchStatus.CANCELLED.value
        assert store.get_search(search_id).status is SearchStatus.CANCELLED
        assert fake_generator.call_count == 1
        events = store.list_events(search_id)
        cancelled_index = next(
            index for index, event in enumerate(events) if event.type == "search.cancelled"
        )
        assert not {
            "round.started",
            "search.waiting_for_human",
            "search.interrupted",
        }.intersection(event.type for event in events[cancelled_index + 1 :])
