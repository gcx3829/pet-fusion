from __future__ import annotations

import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.domain.fusions import FusionRequest, FusionResult
from app.persistence.app_store import AppStore
from app.persistence.migrations import MIGRATION_VERSION


def _alpha_mask_bytes(
    *,
    size: tuple[int, int] = (96, 64),
    box: tuple[int, int, int, int] = (45, 25, 75, 55),
) -> bytes:
    output = io.BytesIO()
    mask = Image.new("RGBA", size, (255, 255, 255, 0))
    alpha = Image.new("L", size, 0)
    alpha.paste(255, box)
    mask.putalpha(alpha)
    mask.save(output, format="PNG")
    return output.getvalue()


def _upload_alpha_mask(client: TestClient, search_id: str, data: bytes) -> dict[str, object]:
    response = client.post(
        f"/api/v1/searches/{search_id}/fusion-masks",
        files={"mask": ("fusion-mask.png", data, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _accepted_search(
    client: TestClient,
    project_payload: list[tuple[str, tuple[str, bytes, str]]],
    search_payload: dict[str, object],
) -> tuple[str, dict[str, object]]:
    project = client.post("/api/v1/projects", files=project_payload).json()
    created = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "fusion-api-search"},
    )
    assert created.status_code == 201, created.text
    search_id = str(created.json()["search_id"])
    accepted = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "accept_global_winner"},
    )
    assert accepted.status_code == 200, accepted.text
    return search_id, accepted.json()


def test_fusion_rectangle_is_explicit_idempotent_and_keeps_raw_candidate(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    search_id, accepted = _accepted_search(client, project_payload, search_payload)
    winner_id = str(accepted["global_winner_id"])
    before = next(
        item for item in accepted["candidates"] if item["candidate_id"] == winner_id
    )
    raw_before = client.get(before["raw_asset_url"]).content
    response = client.post(
        f"/api/v1/searches/{search_id}/fusions",
        json={
            "box": {"x": 0.55, "y": 0.45, "width": 0.25, "height": 0.35},
            "feather_radius_px": 4,
        },
    )
    assert response.status_code == 201, response.text
    first = response.json()
    assert first["candidate_id"] == winner_id
    assert first["key_schema_version"] == "fusion/v2"
    assert first["input_mask_asset"] is None
    assert first["raw_asset"]["asset_id"] == before["raw_asset_id"]
    assert first["fusion_asset"]["asset_id"] != first["raw_asset"]["asset_id"]
    assert first["mask_asset"]["mime_type"] == "image/png"
    assert first["fusion_asset"]["asset_url"].startswith("/api/v1/assets/")

    replay = client.post(
        f"/api/v1/searches/{search_id}/fusions",
        json={
            "candidate_id": winner_id,
            "box": {"x": 0.55, "y": 0.45, "width": 0.25, "height": 0.35},
            "feather_radius_px": 4,
        },
    )
    assert replay.status_code == 201
    assert replay.json() == first

    fetched = client.get(
        f"/api/v1/searches/{search_id}/fusions/{first['fusion_key']}"
    )
    assert fetched.status_code == 200
    assert fetched.json() == first

    raw_asset = client.get(before["raw_asset_url"])
    fusion_asset = client.get(first["fusion_asset"]["asset_url"])
    assert raw_asset.status_code == fusion_asset.status_code == 200
    with Image.open(io.BytesIO(raw_asset.content)) as raw_image:
        with Image.open(io.BytesIO(fusion_asset.content)) as fused_image:
            assert raw_image.size == fused_image.size
    assert client.get(before["raw_asset_url"]).content == raw_before

    container = client.app.state.container
    search = container.app_store.get_search(search_id)
    project = container.app_store.get_project(search.project_id)
    mask_asset = container.app_store.get_asset(first["mask_asset"]["asset_id"])
    with (
        Image.open(project.source_manifest.background.filesystem_path) as background,
        Image.open(io.BytesIO(fusion_asset.content)) as fused,
        Image.open(mask_asset.filesystem_path) as mask,
    ):
        assert container.fusion_service.composite_floor.outside_mask_is_exact(
            background=background,
            protected=fused,
            mask=mask,
        )


def test_fusion_accepts_registered_alpha_mask_and_rejects_unaccepted_search(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    project = client.post("/api/v1/projects", files=project_payload).json()
    created = client.post(
        f"/api/v1/projects/{project['project_id']}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "fusion-unaccepted-search"},
    )
    assert created.status_code == 201
    search_id = str(created.json()["search_id"])
    rejected = client.post(
        f"/api/v1/searches/{search_id}/fusions",
        json={"box": {"x": 0, "y": 0, "width": 0.5, "height": 0.5}},
    )
    assert rejected.status_code == 409
    rejected_upload = client.post(
        f"/api/v1/searches/{search_id}/fusion-masks",
        files={"mask": ("fusion-mask.png", _alpha_mask_bytes(), "image/png")},
    )
    assert rejected_upload.status_code == 409
    assert rejected_upload.json()["error"]["code"] == "CONFLICT"

    accepted = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "accept_global_winner"},
    ).json()
    registered = _upload_alpha_mask(client, search_id, _alpha_mask_bytes())
    input_mask = registered["asset"]
    fusion = client.post(
        f"/api/v1/searches/{search_id}/fusions",
        json={
            "candidate_id": accepted["global_winner_id"],
            "mask_asset_id": input_mask["asset_id"],
            "feather_radius_px": 2,
        },
    )
    assert fusion.status_code == 201, fusion.text
    body = fusion.json()
    assert body["input_mask_asset"]["asset_id"] == input_mask["asset_id"]
    assert body["mask_asset"]["asset_id"] != input_mask["asset_id"]

    used_mask = client.get(body["mask_asset"]["asset_url"])
    assert used_mask.status_code == 200
    with Image.open(io.BytesIO(used_mask.content)) as opened:
        pixels = opened.convert("L")
        assert pixels.getpixel((60, 40)) == 255
        assert pixels.getpixel((0, 0)) == 0
        assert 0 < pixels.getpixel((45, 25)) < 255


def test_fusion_mask_upload_is_alpha_only_search_scoped_and_enveloped(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    first_search_id, _first = _accepted_search(client, project_payload, search_payload)
    second_search_id, second = _accepted_search(client, project_payload, search_payload)
    registered = _upload_alpha_mask(client, first_search_id, _alpha_mask_bytes())
    mask_asset_id = registered["asset"]["asset_id"]

    cross_search = client.post(
        f"/api/v1/searches/{second_search_id}/fusions",
        json={"mask_asset_id": mask_asset_id, "feather_radius_px": 0},
    )
    assert cross_search.status_code == 404
    assert cross_search.json()["error"]["code"] == "NOT_FOUND"

    container = client.app.state.container
    second_search = container.app_store.get_search(second_search_id)
    second_project = container.app_store.get_project(second_search.project_id)
    arbitrary_source = client.post(
        f"/api/v1/searches/{second_search_id}/fusions",
        json={
            "candidate_id": second["global_winner_id"],
            "mask_asset_id": second_project.source_manifest.background.asset_id,
            "feather_radius_px": 0,
        },
    )
    assert arbitrary_source.status_code == 404
    assert arbitrary_source.json()["error"]["code"] == "NOT_FOUND"

    rgb_output = io.BytesIO()
    Image.new("RGB", (96, 64), "white").save(rgb_output, format="PNG")
    invalid_alpha = client.post(
        f"/api/v1/searches/{second_search_id}/fusion-masks",
        files={"mask": ("not-alpha.png", rgb_output.getvalue(), "image/png")},
    )
    assert invalid_alpha.status_code == 422
    assert invalid_alpha.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    wrong_size = client.post(
        f"/api/v1/searches/{second_search_id}/fusion-masks",
        files={"mask": ("wrong-size.png", _alpha_mask_bytes(size=(48, 32)), "image/png")},
    )
    assert wrong_size.status_code == 422
    assert wrong_size.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    malformed = client.get(f"/api/v1/searches/not-a-search/fusions/{'a' * 64}")
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "VALIDATION_FAILED"


def test_fusion_concurrent_replay_is_single_row_and_does_not_mutate_search(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    search_id, _accepted = _accepted_search(client, project_payload, search_payload)
    container = client.app.state.container
    before = container.app_store.get_search(search_id)
    evaluations_before = container.app_store.list_evaluations_with_scores(search_id)
    events_before = container.app_store.list_events(search_id)
    request = FusionRequest(
        search_id=search_id,
        box={"x": 0.5, "y": 0.4, "width": 0.3, "height": 0.4},
        feather_radius_px=3,
    )

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(container.fusion_service.create, [request] * 6))

    assert len({result.fusion_key for result in results}) == 1
    assert all(result == results[0] for result in results)
    with sqlite3.connect(container.settings.resolved_app_db_path) as connection:
        row_count = connection.execute(
            "SELECT COUNT(*) FROM fusions WHERE search_id = ?", (search_id,)
        ).fetchone()
    assert row_count is not None and row_count[0] == 1
    assert container.app_store.get_search(search_id) == before
    assert container.app_store.list_evaluations_with_scores(search_id) == evaluations_before
    assert container.app_store.list_events(search_id) == events_before


def test_fusion_get_rejects_stale_candidate_lineage(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    search_id, accepted = _accepted_search(client, project_payload, search_payload)
    created = client.post(
        f"/api/v1/searches/{search_id}/fusions",
        json={"box": {"x": 0.5, "y": 0.4, "width": 0.3, "height": 0.4}},
    )
    assert created.status_code == 201
    body = created.json()
    container = client.app.state.container
    search = container.app_store.get_search(search_id)
    winner = next(
        item for item in search.candidates if item.candidate_id == accepted["global_winner_id"]
    )
    replacement = next(
        item for item in search.candidates if item.candidate_id != winner.candidate_id
    )
    container.app_store.add_candidate(
        search_id,
        winner.model_copy(
            update={
                "raw_asset": replacement.raw_asset,
                "protected_asset": replacement.raw_asset,
            }
        ),
    )

    stale = client.get(
        f"/api/v1/searches/{search_id}/fusions/{body['fusion_key']}"
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONFLICT"


def test_fusion_v9_table_migrates_additively(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (9, 'fixture')"
        )
        connection.execute(
            """
            CREATE TABLE fusions (
                fusion_key TEXT PRIMARY KEY,
                search_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source_manifest_hash TEXT NOT NULL,
                raw_asset_id TEXT NOT NULL,
                mask_asset_id TEXT NOT NULL,
                fusion_asset_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.commit()

    store = AppStore(database)
    store.initialize()

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(fusions)").fetchall()
        }
        search_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(search_runs)").fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
    assert {"source_background_asset_id", "input_mask_asset_id"} <= columns
    assert "fusion_mask_inputs" in tables
    assert "guidance_mask_bindings" in tables
    assert {
        "guidance_mask_asset_id",
        "guidance_mask_source_manifest_hash",
    } <= search_columns
    assert MIGRATION_VERSION in versions


def test_fusion_v1_alpha_result_remains_readable(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    search_id, _accepted = _accepted_search(client, project_payload, search_payload)
    registered = _upload_alpha_mask(client, search_id, _alpha_mask_bytes())
    result = client.app.state.container.fusion_service.create(
        FusionRequest(
            search_id=search_id,
            mask_asset_id=registered["asset"]["asset_id"],
            feather_radius_px=2,
        )
    )
    legacy_payload = result.model_dump(mode="json")
    legacy_payload.pop("key_schema_version")
    legacy_payload.pop("input_mask_asset")

    legacy = FusionResult.model_validate(legacy_payload)

    assert legacy.key_schema_version == "fusion/v1"
    assert legacy.input_mask_asset == legacy.mask
