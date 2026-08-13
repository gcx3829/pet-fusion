from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


def _create_project(client: TestClient, project_payload) -> dict[str, object]:
    response = client.post(
        "/api/v1/projects",
        files=project_payload,
        data={"cat_name": "Mochi", "cat_traits": "white muzzle, striped tail"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_mocked_vertical_slice_through_api_and_sse(
    client: TestClient,
    project_payload,
    search_payload,
    settings,
    fake_generator,
) -> None:
    assert client.get("/api/v1/health").json() == {
        "status": "ok",
        "generator": "fake",
    }
    project = _create_project(client, project_payload)
    manifest = project["source_manifest"]
    assert project["cat_name"] == "Mochi"
    assert len(manifest["cat_references"]) == 2
    assert len(manifest["manifest_hash"]) == 64
    assert manifest["background"]["mime_type"] == "image/png"
    assert manifest["background"]["asset_url"].startswith("/api/v1/assets/")

    create_response = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "api-flow-search"},
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()
    assert created["status"] == "queued"
    assert created["thread_id"] == created["search_id"]
    assert created["events_url"].endswith(f"/{created['search_id']}/events")

    search_response = client.get(f"/api/v1/searches/{created['search_id']}")
    assert search_response.status_code == 200
    search = search_response.json()
    assert search["status"] == "waiting_for_human"
    assert search["stop_reason"] in {"accept_threshold", "no_meaningful_defect", "max_rounds"}
    assert search["global_winner_id"] is not None
    assert search["round_index"] == 0
    assert len(search["candidates"]) == 3
    assert fake_generator.call_count == 1

    for index, candidate in enumerate(search["candidates"]):
        assert candidate["variant_index"] == index
        assert candidate["round_index"] == 0
        assert candidate["generation_depth"] == 0
        assert candidate["model"] == "fake-gpt-image-2"
        assert candidate["mime_type"] == "image/png"
        assert candidate["asset_url"].startswith("/api/v1/assets/")
        asset_response = client.get(candidate["asset_url"])
        assert asset_response.status_code == 200
        assert asset_response.headers["content-type"] == "image/png"
        assert asset_response.headers["cache-control"].startswith("private,")
        assert asset_response.content.startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(__import__("io").BytesIO(asset_response.content)) as image:
            assert image.format == "PNG"

    sse_response = client.get(created["events_url"])
    assert sse_response.status_code == 200
    assert sse_response.headers["content-type"].startswith("text/event-stream")
    event_names = [
        line.removeprefix("event: ")
        for line in sse_response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert event_names == [
        "search.queued",
        "search.started",
        "round.generation.started",
            "round.candidate.ready",
            "round.candidate.ready",
            "round.candidate.ready",
            "round.critic.started",
            "round.evaluation.ready",
        "round.evaluation.ready",
        "round.evaluation.ready",
        "round.winner.updated",
        "search.global_winner.updated",
        "search.stop.decided",
        "search.waiting_for_human",
        "search.interrupted",
    ]
    data_lines = [
        json.loads(line.removeprefix("data: "))
        for line in sse_response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert all(item["search_id"] == created["search_id"] for item in data_lines)
    assert "base64" not in sse_response.text.lower()
    assert "data:image" not in sse_response.text.lower()

    assert settings.resolved_checkpoint_db_path.exists()
    with sqlite3.connect(settings.resolved_checkpoint_db_path) as connection:
        checkpoint_count = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        serialized_state = b"".join(
            bytes(value)
            for row in connection.execute(
                "SELECT checkpoint, metadata FROM checkpoints "
                "UNION ALL SELECT value, NULL FROM writes"
            )
            for value in row
            if value is not None
        )
    assert checkpoint_count >= 5
    assert b"\x89PNG\r\n\x1a\n" not in serialized_state
    assert b"data:image" not in serialized_state


def test_resume_cancel_is_idempotent_and_other_actions_are_explicitly_unavailable(
    client: TestClient, project_payload, search_payload
) -> None:
    project = _create_project(client, project_payload)
    created = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "resume-search"},
    ).json()

    unavailable = client.post(
        f"/api/v1/searches/{created['search_id']}/resume",
        json={"action": "continue_one_round"},
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "CONFLICT"

    first = client.post(
        f"/api/v1/searches/{created['search_id']}/resume", json={"action": "cancel"}
    )
    second = client.post(
        f"/api/v1/searches/{created['search_id']}/resume", json={"action": "cancel"}
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    events = client.get(created["events_url"]).text
    assert events.count("event: search.cancelled") == 1


def test_project_upload_validation_and_consistent_error_envelope(
    client: TestClient, project_payload
) -> None:
    missing_references = client.post(
        "/api/v1/projects",
        files=[project_payload[0]],
    )
    assert missing_references.status_code == 422
    assert missing_references.json()["error"]["code"] == "VALIDATION_FAILED"

    six_references = [project_payload[0], *([project_payload[1]] * 6)]
    too_many = client.post("/api/v1/projects", files=six_references)
    assert too_many.status_code == 422
    assert too_many.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    invalid_image = client.post(
        "/api/v1/projects",
        files=[
            ("background", ("bad.png", b"not an image", "image/png")),
            project_payload[1],
        ],
    )
    assert invalid_image.status_code == 422
    assert invalid_image.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"


def test_search_payload_rejects_out_of_bounds_placement(
    client: TestClient, project_payload, search_payload
) -> None:
    project = _create_project(client, project_payload)
    search_payload["placement"] = {
        **search_payload["placement"],
        "x": 0.95,
        "width": 0.2,
    }
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "invalid-placement-search"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_search_submission_requires_and_replays_idempotency_key(
    client: TestClient,
    project_payload,
    search_payload,
    fake_generator,
) -> None:
    project = _create_project(client, project_payload)
    url = f"/api/v1/projects/{project['project_id']}/searches"

    missing = client.post(url, json=search_payload)
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION_FAILED"

    headers = {"Idempotency-Key": "stable-browser-submission"}
    first = client.post(url, json=search_payload, headers=headers)
    replay = client.post(url, json=search_payload, headers=headers)
    assert first.status_code == replay.status_code == 201
    assert first.json()["search_id"] == replay.json()["search_id"]
    assert fake_generator.call_count == 1

    changed_payload = {**search_payload, "candidate_count": 1}
    conflict = client.post(url, json=changed_payload, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"


def test_project_uploads_enforce_combined_byte_and_pixel_budgets(
    tmp_path: Path,
    project_payload,
    fake_generator,
) -> None:
    total_bytes = sum(len(file_tuple[1]) for _, file_tuple in project_payload)
    byte_limited = Settings(
        data_dir=tmp_path / "byte-limited",
        run_inline=True,
        max_upload_bytes=max(len(file_tuple[1]) for _, file_tuple in project_payload) + 1,
        max_total_upload_bytes=total_bytes - 1,
        max_image_pixels=1_000_000,
        max_total_image_pixels=1_000_000,
    )
    with TestClient(create_app(byte_limited, image_generator=fake_generator)) as client:
        response = client.post("/api/v1/projects", files=project_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    pixel_limited = Settings(
        data_dir=tmp_path / "pixel-limited",
        run_inline=True,
        max_upload_bytes=2 * 1024 * 1024,
        max_total_upload_bytes=total_bytes + 1,
        max_image_pixels=1_000_000,
        max_total_image_pixels=(96 * 64 * 2),
    )
    with TestClient(create_app(pixel_limited, image_generator=fake_generator)) as client:
        response = client.post("/api/v1/projects", files=project_payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"
