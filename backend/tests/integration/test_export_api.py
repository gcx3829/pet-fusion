from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient
from PIL import Image


def _create_accepted_search(
    client: TestClient,
    project_payload: list[tuple[str, tuple[str, bytes, str]]],
    search_payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    project_response = client.post("/api/v1/projects", files=project_payload)
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    created_response = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "export-api-search"},
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()
    accepted_response = client.post(
        f"/api/v1/searches/{created['search_id']}/resume",
        json={"action": "accept_global_winner"},
    )
    assert accepted_response.status_code == 200, accepted_response.text
    accepted = accepted_response.json()
    assert accepted["status"] == "accepted"
    assert accepted["global_winner_id"] is not None
    return project, created, accepted


def test_export_api_persists_replays_and_serves_png_and_jpeg(
    client: TestClient,
    project_payload,
    search_payload,
    settings,
) -> None:
    _project, created, accepted = _create_accepted_search(
        client, project_payload, search_payload
    )
    search_id = str(created["search_id"])
    winner_id = str(accepted["global_winner_id"])

    first_response = client.post(f"/api/v1/searches/{search_id}/export", json={})
    assert first_response.status_code == 201, first_response.text
    first = first_response.json()
    assert first["format"] == "png"
    assert first["jpeg_quality"] is None
    assert first["candidate_id"] == winner_id
    assert first["metadata"]["accepted_search_status"] == "accepted"
    assert first["metadata"]["accepted_global_winner_id"] == winner_id
    assert first["metadata"]["source_background_asset_id"]
    assert first["asset"]["mime_type"] == "image/png"
    assert first["asset"]["asset_url"].startswith("/api/v1/assets/")
    assert str(settings.data_dir) not in json.dumps(first)

    explicit_response = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"candidate_id": winner_id},
    )
    assert explicit_response.status_code == 201, explicit_response.text
    assert explicit_response.json() == first

    explicit_png_default = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"jpeg_quality": 95},
    )
    assert explicit_png_default.status_code == 201, explicit_png_default.text
    assert explicit_png_default.json() == first

    retrieved_response = client.get(
        f"/api/v1/searches/{search_id}/exports/{first['export_key']}"
    )
    assert retrieved_response.status_code == 200, retrieved_response.text
    assert retrieved_response.json() == first

    png_asset = client.get(first["asset"]["asset_url"])
    assert png_asset.status_code == 200
    assert png_asset.headers["content-type"] == "image/png"
    with Image.open(__import__("io").BytesIO(png_asset.content)) as image:
        assert image.format == "PNG"

    jpeg_response = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"format": "jpeg", "jpeg_quality": 88},
    )
    assert jpeg_response.status_code == 201, jpeg_response.text
    jpeg = jpeg_response.json()
    assert jpeg["export_key"] != first["export_key"]
    assert jpeg["candidate_id"] == winner_id
    assert jpeg["format"] == "jpeg"
    assert jpeg["jpeg_quality"] == 88
    assert jpeg["asset"]["mime_type"] == "image/jpeg"

    jpeg_asset = client.get(jpeg["asset"]["asset_url"])
    assert jpeg_asset.status_code == 200
    assert jpeg_asset.headers["content-type"] == "image/jpeg"
    with Image.open(__import__("io").BytesIO(jpeg_asset.content)) as image:
        assert image.format == "JPEG"

    with sqlite3.connect(settings.resolved_app_db_path) as connection:
        rows = connection.execute(
            """
            SELECT export_key, search_id, candidate_id, source_manifest_hash, format,
                   jpeg_quality, asset_id, metadata_json
            FROM exports ORDER BY format
            """
        ).fetchall()
    assert len(rows) == 2
    assert {row[4] for row in rows} == {"png", "jpeg"}
    assert {row[2] for row in rows} == {winner_id}
    assert all(json.loads(row[7])["accepted_global_winner_id"] == winner_id for row in rows)


def test_export_api_rejects_unaccepted_loser_and_bad_lineage(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    project_response = client.post("/api/v1/projects", files=project_payload)
    assert project_response.status_code == 201
    project = project_response.json()
    created_response = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "export-rejection-search"},
    )
    assert created_response.status_code == 201
    created = created_response.json()
    search_id = str(created["search_id"])

    unaccepted = client.post(f"/api/v1/searches/{search_id}/export", json={})
    assert unaccepted.status_code == 409
    assert unaccepted.json()["error"]["code"] == "CONFLICT"

    accepted_response = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "accept_global_winner"},
    )
    assert accepted_response.status_code == 200
    accepted = accepted_response.json()
    winner_id = str(accepted["global_winner_id"])
    loser_id = next(
        candidate["candidate_id"]
        for candidate in accepted["candidates"]
        if candidate["candidate_id"] != winner_id
    )

    loser = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"candidate_id": loser_id},
    )
    assert loser.status_code == 409
    assert loser.json()["error"]["code"] == "CONFLICT"

    invalid_quality = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"format": "jpeg", "jpeg_quality": 59},
    )
    assert invalid_quality.status_code == 422
    assert invalid_quality.json()["error"]["code"] == "VALIDATION_FAILED"

    irrelevant_png_quality = client.post(
        f"/api/v1/searches/{search_id}/export",
        json={"format": "png", "jpeg_quality": 88},
    )
    assert irrelevant_png_quality.status_code == 422
    assert irrelevant_png_quality.json()["error"]["code"] == "VALIDATION_FAILED"

    container = client.app.state.container
    persisted = container.app_store.get_search(search_id)
    winner = next(
        candidate for candidate in persisted.candidates if candidate.candidate_id == winner_id
    )
    container.app_store.add_candidate(
        search_id,
        winner.model_copy(
            update={"source_manifest_hash": "f" * 64, "composite": None}
        ),
    )
    mismatch = client.post(f"/api/v1/searches/{search_id}/export", json={})
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "CONFLICT"


def test_cancelled_search_cannot_export_and_missing_export_uses_error_envelope(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    project_response = client.post("/api/v1/projects", files=project_payload)
    assert project_response.status_code == 201
    project_id = project_response.json()["project_id"]
    created_response = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "cancelled-export-search"},
    )
    assert created_response.status_code == 201
    search_id = created_response.json()["search_id"]

    cancelled_response = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "cancel"},
    )
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == "cancelled"

    rejected = client.post(f"/api/v1/searches/{search_id}/export", json={})
    assert rejected.status_code == 409
    assert rejected.json()["error"] == {
        "code": "CONFLICT",
        "message": "Only an accepted search can be exported",
    }

    missing = client.get(f"/api/v1/searches/{search_id}/exports/{'f' * 64}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"

    malformed_key = client.get(
        f"/api/v1/searches/{search_id}/exports/not-a-content-hash"
    )
    assert malformed_key.status_code == 422
    assert malformed_key.json()["error"]["code"] == "VALIDATION_FAILED"

    oversized_search_id = client.post(
        f"/api/v1/searches/{'s' * 121}/export",
        json={},
    )
    assert oversized_search_id.status_code == 422
    assert oversized_search_id.json()["error"]["code"] == "VALIDATION_FAILED"
