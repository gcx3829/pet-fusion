from __future__ import annotations

import asyncio
import io
import json
import sqlite3
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.domain.errors import SourceManifestMismatchError
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.persistence.migrations import MIGRATION_VERSION
from app.services.prompt_refiner_service import (
    DeterministicFakePromptRefiner,
    PromptRefinerError,
)


def _create_project(client: TestClient, project_payload) -> dict[str, object]:
    response = client.post("/api/v1/projects", files=project_payload)
    assert response.status_code == 201, response.text
    return response.json()


def _rgba_png(
    *,
    size: tuple[int, int] = (96, 64),
    alpha: int = 180,
    rgb: tuple[int, int, int] = (255, 255, 255),
) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGBA", size, (*rgb, alpha))
    image.save(output, format="PNG")
    return output.getvalue()


def _upload_mask(client: TestClient, project_id: str, data: bytes) -> object:
    response = client.post(
        f"/api/v1/projects/{project_id}/guidance-masks",
        files={"mask": ("guidance.png", data, "image/png")},
    )
    return response


def _oversized_png_header(*, width: int, height: int) -> bytes:
    payload = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    chunk_type = b"IHDR"
    checksum = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", checksum)
    )


class _TerminalPromptRefiner(DeterministicFakePromptRefiner):
    def __init__(self) -> None:
        self.calls = 0

    def refine(self, request, proxies=None, *, request_key=None):
        del request, proxies, request_key
        self.calls += 1
        raise ValueError("invalid prompt schema")


def test_guidance_mask_is_project_bound_and_search_reference_is_immutable(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    uploaded = _upload_mask(
        client,
        project_id,
        _rgba_png(rgb=(12, 34, 56)),
    )
    assert uploaded.status_code == 201, uploaded.text
    mask = uploaded.json()
    assert mask["project_id"] == project_id
    assert mask["source_manifest_hash"] == project["source_manifest"]["manifest_hash"]
    assert mask["asset"]["mime_type"] == "image/png"
    assert mask["asset"]["width"] == project["source_manifest"]["background"]["width"]
    assert mask["asset"]["height"] == project["source_manifest"]["background"]["height"]
    stored = client.get(mask["asset"]["asset_url"])
    assert stored.status_code == 200
    with Image.open(io.BytesIO(stored.content)) as stored_image:
        assert stored_image.format == "PNG"
        assert stored_image.mode == "RGBA"
        assert stored_image.getchannel("A").getextrema() == (180, 180)
        assert stored_image.getchannel("R").getextrema() == (255, 255)
        assert stored_image.getchannel("G").getextrema() == (255, 255)
        assert stored_image.getchannel("B").getextrema() == (255, 255)

    listed = client.get(f"/api/v1/projects/{project_id}/guidance-masks")
    assert listed.status_code == 200, listed.text
    assert [item["asset"]["asset_id"] for item in listed.json()] == [
        mask["asset"]["asset_id"]
    ]

    request = {**search_payload, "guidance_mask_asset_id": mask["asset"]["asset_id"]}
    first = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json=request,
        headers={"Idempotency-Key": "guidance-mask-search"},
    )
    assert first.status_code == 201, first.text
    replay = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json=request,
        headers={"Idempotency-Key": "guidance-mask-search"},
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["search_id"] == first.json()["search_id"]
    search = client.get(f"/api/v1/searches/{first.json()['search_id']}")
    assert search.status_code == 200, search.text
    assert search.json()["guidance_mask_asset"]["asset_id"] == mask["asset"]["asset_id"]

    fingerprint_collision = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json=search_payload,
        headers={"Idempotency-Key": "guidance-mask-search"},
    )
    assert fingerprint_collision.status_code == 409, fingerprint_collision.text

    app_store = client.app.state.container.app_store
    with sqlite3.connect(app_store.path) as connection:
        row = connection.execute(
            "SELECT guidance_mask_asset_id, guidance_mask_source_manifest_hash "
            "FROM search_runs WHERE search_id = ?",
            (first.json()["search_id"],),
        ).fetchone()
    assert row == (mask["asset"]["asset_id"], project["source_manifest"]["manifest_hash"])


def test_guidance_mask_binding_rebases_unchanged_across_rounds_and_stays_reference_only(
    client: TestClient,
    project_payload,
    search_payload,
    fake_generator,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    mask = _upload_mask(client, project_id, _rgba_png(alpha=143)).json()
    request = {
        **search_payload,
        "candidate_count": 2,
        "max_rounds": 2,
        "guidance_mask_asset_id": mask["asset"]["asset_id"],
    }
    created = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json=request,
        headers={"Idempotency-Key": "guidance-mask-two-round-rebase"},
    )
    assert created.status_code == 201, created.text
    search_id = created.json()["search_id"]
    first = client.get(f"/api/v1/searches/{search_id}").json()
    assert first["status"] == "waiting_for_human"
    assert len(fake_generator.requests) == 1

    resumed = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={"action": "continue_one_round", "reviewed_round_index": 0},
    )
    assert resumed.status_code == 200, resumed.text
    second = client.get(f"/api/v1/searches/{search_id}").json()

    assert second["round_index"] == 1
    assert len(fake_generator.requests) == 2
    automatic_request = fake_generator.requests[1]
    assert automatic_request.generation_mode.value == "source_rebase"
    assert automatic_request.visual_anchor is None
    assert automatic_request.prompt_version is not None
    with sqlite3.connect(client.app.state.container.app_store.path) as connection:
        prompt_calls = connection.execute(
            "SELECT COUNT(*) FROM provider_calls WHERE operation = 'prompt_refine'"
        ).fetchone()[0]
    assert prompt_calls == 1
    assert all(
        request.guidance_mask is not None
        and request.guidance_mask.asset_id == mask["asset"]["asset_id"]
        and request.guidance_mask.sha256 == mask["asset"]["sha256"]
        for request in fake_generator.requests
    )
    assert {
        request.source_manifest.manifest_hash for request in fake_generator.requests
    } == {project["source_manifest"]["manifest_hash"]}
    assert all(
        candidate["asset_id"] == candidate["raw_asset_id"]
        == candidate["protected_asset_id"]
        and candidate["composite_floor_applied"] is False
        and candidate["review_asset_kind"] == "raw"
        for candidate in second["candidates"]
    )

    stale_anchor = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json={
            "action": "continue_one_round",
            "reviewed_round_index": 1,
            "selected_candidate_id": first["candidates"][0]["candidate_id"],
        },
    )
    assert stale_anchor.status_code == 409, stale_anchor.text
    assert "currently reviewed round" in stale_anchor.json()["error"]["message"]

    checkpoint_seed = client.app.state.container.search_runner.initial_state(search_id)
    assert_checkpoint_safe(checkpoint_seed)
    serialized = json.dumps(checkpoint_seed, sort_keys=True)
    assert mask["asset"]["asset_id"] in serialized
    assert "data:image" not in serialized.lower()
    assert "iVBOR" not in serialized


@pytest.mark.parametrize(
    "feedback",
    [None, "Keep this candidate's identity and improve the contact shadow."],
)
def test_guidance_selected_candidate_refines_from_that_raw_candidate(
    client: TestClient,
    project_payload,
    search_payload,
    fake_generator,
    feedback: str | None,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    mask = _upload_mask(client, project_id, _rgba_png(alpha=143)).json()
    created = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json={
            **search_payload,
            "candidate_count": 2,
            "max_rounds": 2,
            "guidance_mask_asset_id": mask["asset"]["asset_id"],
        },
        headers={"Idempotency-Key": "guidance-mask-selected-revision"},
    )
    assert created.status_code == 201, created.text
    search_id = created.json()["search_id"]
    first = client.get(f"/api/v1/searches/{search_id}").json()
    selected = first["candidates"][1]
    selected_id = selected["candidate_id"]
    initial_prompt = first["prompt_history"][0]["generation_prompt"]
    assert fake_generator.requests[0].prompt == initial_prompt

    resume_payload: dict[str, object] = {
        "action": "continue_one_round",
        "reviewed_round_index": 0,
        "selected_candidate_id": selected_id,
    }
    if feedback is not None:
        resume_payload["human_feedback"] = feedback
    resumed = client.post(
        f"/api/v1/searches/{search_id}/resume",
        json=resume_payload,
    )
    assert resumed.status_code == 200, resumed.text
    second = client.get(f"/api/v1/searches/{search_id}").json()
    assert second["status"] == "waiting_for_human"
    assert len(fake_generator.requests) == 2

    revision_request = fake_generator.requests[1]
    assert revision_request.generation_mode.value == "candidate_anchored_rebase"
    assert revision_request.visual_anchor is not None
    assert revision_request.visual_anchor.candidate_id == selected_id
    assert revision_request.prompt_version is not None
    assert revision_request.prompt_version.visual_anchor_candidate_id == selected_id
    assert revision_request.prompt_version.based_on_prompt_version_id == (
        first["prompt_history"][0]["prompt_version_id"]
    )
    assert revision_request.prompt != initial_prompt

    history = second["prompt_history"]
    assert [item["round_index"] for item in history] == [0, 1]
    assert history[0]["prompt_model"] == "deterministic-prompt-refiner/v1"
    assert history[0]["generation_model"] == fake_generator.requests[0].model
    assert history[0]["generation_model"] != history[0]["prompt_model"]
    assert history[1]["prompt_model"] == "deterministic-prompt-refiner/v1"
    assert history[1]["generation_model"] == fake_generator.requests[1].model
    assert history[1]["generation_model"] != history[1]["prompt_model"]
    assert history[1]["human_selected_candidate_id"] == selected_id
    assert history[1]["based_on_prompt_version_id"] == history[0]["prompt_version_id"]
    public_anchor = history[1]["visual_anchor"]
    assert public_anchor["candidate_id"] == selected_id
    assert public_anchor["raw_asset"]["asset_id"] == selected["asset_id"]
    assert public_anchor["raw_asset"]["asset_url"] == selected["asset_url"]
    assert "path" not in public_anchor["raw_asset"]
    assert str(client.app.state.container.settings.data_dir) not in json.dumps(
        history,
        ensure_ascii=False,
    )
    round_history = second["round_history"]
    assert round_history[1]["visual_anchor_candidate_id"] == selected_id

    refiner_events = [
        event
        for event in client.app.state.container.app_store.list_events(search_id)
        if event.type in {"prompt.refiner.started", "prompt.refiner.ready"}
    ]
    assert [event.type for event in refiner_events] == [
        "prompt.refiner.started",
        "prompt.refiner.ready",
        "prompt.refiner.started",
        "prompt.refiner.ready",
    ]
    assert all("generation_prompt" not in event.payload for event in refiner_events)
    assert all("human_feedback" not in event.payload for event in refiner_events)


def test_prompt_refiner_failure_stops_generation_without_image_provider_call(
    client: TestClient,
    project_payload,
    search_payload,
    fake_generator,
) -> None:
    terminal_provider = _TerminalPromptRefiner()
    client.app.state.container.prompt_refiner_service.provider = terminal_provider
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    mask = _upload_mask(client, project_id, _rgba_png(alpha=143)).json()
    with pytest.raises(PromptRefinerError, match="terminally"):
        client.post(
            f"/api/v1/projects/{project_id}/searches",
            json={
                **search_payload,
                "guidance_mask_asset_id": mask["asset"]["asset_id"],
            },
            headers={"Idempotency-Key": "guidance-mask-prompt-failure"},
        )

    # The failed background task still fences the search before re-raising to
    # TestClient, so resolve its ID from the durable event table.
    with sqlite3.connect(client.app.state.container.app_store.path) as connection:
        row = connection.execute(
            "SELECT search_id FROM search_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    failed = client.get(f"/api/v1/searches/{row[0]}").json()
    assert failed["status"] == "failed"
    assert terminal_provider.calls == 1
    assert fake_generator.requests == []
    events = client.app.state.container.app_store.list_events(row[0])
    assert [event.type for event in events[-2:]] == [
        "prompt.refiner.failed",
        "search.failed",
    ]
    assert "message" not in events[-2].payload


def test_guidance_mask_checkpoint_replay_rejects_a_changed_search_binding(
    client: TestClient,
    project_payload,
    search_payload,
    fake_generator,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    first_mask = _upload_mask(client, project_id, _rgba_png(alpha=101)).json()
    second_mask = _upload_mask(client, project_id, _rgba_png(alpha=202)).json()
    created = client.post(
        f"/api/v1/projects/{project_id}/searches",
        json={
            **search_payload,
            "guidance_mask_asset_id": first_mask["asset"]["asset_id"],
        },
        headers={"Idempotency-Key": "guidance-mask-checkpoint-fence"},
    )
    assert created.status_code == 201, created.text
    search_id = created.json()["search_id"]
    assert fake_generator.call_count == 1

    container = client.app.state.container
    with sqlite3.connect(container.app_store.path) as connection:
        connection.execute(
            "UPDATE search_runs SET status = 'running', guidance_mask_asset_id = ? "
            "WHERE search_id = ?",
            (second_mask["asset"]["asset_id"], search_id),
        )
        connection.commit()

    with pytest.raises(
        SourceManifestMismatchError,
        match="Checkpoint conflicts with immutable search source state",
    ):
        asyncio.run(container.search_runner.run_search(search_id))
    assert fake_generator.call_count == 1


def test_guidance_mask_rejects_non_png_missing_or_empty_alpha_and_wrong_size(
    client: TestClient,
    project_payload,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])

    opaque = io.BytesIO()
    Image.new("RGB", (96, 64), (20, 30, 40)).save(opaque, format="PNG")
    response = _upload_mask(client, project_id, opaque.getvalue())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    empty_alpha = _upload_mask(client, project_id, _rgba_png(alpha=0))
    assert empty_alpha.status_code == 422
    assert empty_alpha.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"

    wrong_size = _upload_mask(client, project_id, _rgba_png(size=(95, 64)))
    assert wrong_size.status_code == 422

    jpeg = io.BytesIO()
    Image.new("RGBA", (96, 64), (20, 30, 40, 180)).convert("RGB").save(
        jpeg, format="JPEG"
    )
    non_png = _upload_mask(client, project_id, jpeg.getvalue())
    assert non_png.status_code == 422

    frames = [
        Image.new("RGBA", (96, 64), (255, 255, 255, alpha))
        for alpha in (100, 200)
    ]
    animated = io.BytesIO()
    frames[0].save(
        animated,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=50,
        loop=0,
    )
    animated_response = _upload_mask(client, project_id, animated.getvalue())
    assert animated_response.status_code == 422

    decompression_bomb = _upload_mask(
        client,
        project_id,
        _oversized_png_header(width=100_000, height=100_000),
    )
    assert decompression_bomb.status_code == 422
    assert decompression_bomb.json()["error"]["code"] == "INVALID_IMAGE_UPLOAD"


def test_guidance_mask_normalizes_indexed_transparency(
    client: TestClient,
    project_payload,
) -> None:
    project = _create_project(client, project_payload)
    project_id = str(project["project_id"])
    indexed = Image.new("P", (96, 64), color=1)
    indexed.putpalette([255, 0, 0, 0, 255, 0] + [0] * (256 * 3 - 6))
    indexed.putpixel((0, 0), 0)
    encoded = io.BytesIO()
    indexed.save(encoded, format="PNG", transparency=0)

    uploaded = _upload_mask(client, project_id, encoded.getvalue())
    assert uploaded.status_code == 201, uploaded.text
    stored = client.get(uploaded.json()["asset"]["asset_url"])
    with Image.open(io.BytesIO(stored.content)) as normalized:
        assert normalized.mode == "RGBA"
        assert normalized.getchannel("A").getextrema() == (0, 255)


def test_guidance_mask_cannot_cross_project_or_be_an_unbound_png(
    client: TestClient,
    project_payload,
    search_payload,
) -> None:
    first_project = _create_project(client, project_payload)
    second_project = _create_project(client, project_payload)
    first_id = str(first_project["project_id"])
    second_id = str(second_project["project_id"])
    registered = _upload_mask(client, first_id, _rgba_png()).json()
    mask_id = registered["asset"]["asset_id"]

    cross_project_search = client.post(
        f"/api/v1/projects/{second_id}/searches",
        json={**search_payload, "guidance_mask_asset_id": mask_id},
        headers={"Idempotency-Key": "cross-project-guidance-mask"},
    )
    assert cross_project_search.status_code == 409, cross_project_search.text

    background_id = second_project["source_manifest"]["background"]["asset_id"]
    unbound_global_png = client.post(
        f"/api/v1/projects/{second_id}/searches",
        json={**search_payload, "guidance_mask_asset_id": background_id},
        headers={"Idempotency-Key": "unbound-global-png"},
    )
    assert unbound_global_png.status_code == 409, unbound_global_png.text

    authorized_on_second_project = _upload_mask(client, second_id, _rgba_png())
    assert authorized_on_second_project.status_code == 201
    assert authorized_on_second_project.json()["asset"]["asset_id"] == mask_id

    authorized_search = client.post(
        f"/api/v1/projects/{second_id}/searches",
        json={**search_payload, "guidance_mask_asset_id": mask_id},
        headers={"Idempotency-Key": "authorized-shared-guidance-mask"},
    )
    assert authorized_search.status_code == 201, authorized_search.text


def test_guidance_mask_binding_is_idempotent_under_concurrent_reuse(
    client: TestClient,
    project_payload,
) -> None:
    project_json = _create_project(client, project_payload)
    project_id = str(project_json["project_id"])
    container = client.app.state.container
    project = container.app_store.get_project(project_id)
    normalized = container.asset_store.normalize_guidance_mask(_rgba_png(alpha=137))
    asset = container.asset_store.put_normalized(normalized)
    container.app_store.register_asset(asset)

    def register_once():
        return container.app_store.register_guidance_mask(
            project_id=project_id,
            source_manifest_hash=project.source_manifest.manifest_hash,
            asset=asset,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        bindings = list(executor.map(lambda _index: register_once(), range(8)))

    assert {binding.asset.asset_id for binding in bindings} == {asset.asset_id}
    assert len({binding.created_at for binding in bindings}) == 1
    listed = container.app_store.list_guidance_masks(project_id=project_id)
    assert [binding.asset.asset_id for binding in listed] == [asset.asset_id]


def test_guidance_mask_error_envelopes_are_documented(
    client: TestClient,
) -> None:
    openapi = client.get("/openapi.json").json()
    guidance_responses = openapi["paths"][
        "/api/v1/projects/{project_id}/guidance-masks"
    ]["post"]["responses"]
    search_responses = openapi["paths"][
        "/api/v1/projects/{project_id}/searches"
    ]["post"]["responses"]

    for responses, status_code in (
        (guidance_responses, "404"),
        (guidance_responses, "409"),
        (guidance_responses, "422"),
        (search_responses, "409"),
    ):
        schema = responses[status_code]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/ErrorEnvelope"


def test_guidance_mask_v11_global_unique_binding_migrates_additively(
    tmp_path: Path,
) -> None:
    database = tmp_path / "guidance-v11.sqlite3"
    asset_id = f"ast_{'a' * 32}"
    manifest_hash = "b" * 64
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE assets (
                asset_id TEXT PRIMARY KEY,
                sha256 TEXT NOT NULL UNIQUE,
                path TEXT NOT NULL UNIQUE,
                mime_type TEXT NOT NULL,
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE projects (
                project_id TEXT PRIMARY KEY,
                cat_name TEXT,
                cat_traits TEXT,
                source_manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE guidance_mask_bindings (
                project_id TEXT NOT NULL REFERENCES projects(project_id),
                source_manifest_hash TEXT NOT NULL,
                asset_id TEXT NOT NULL UNIQUE REFERENCES assets(asset_id),
                created_at TEXT NOT NULL,
                PRIMARY KEY (project_id, source_manifest_hash, asset_id)
            );
            CREATE INDEX idx_guidance_mask_bindings_project
            ON guidance_mask_bindings(project_id, source_manifest_hash, created_at);
            """
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (11, 'fixture')"
        )
        connection.execute(
            "INSERT INTO assets VALUES (?, ?, ?, 'image/png', 96, 64, 'fixture')",
            (asset_id, "a" * 64, str(tmp_path / "mask.png")),
        )
        connection.execute(
            "INSERT INTO projects VALUES ('proj_fixture', NULL, NULL, '{}', ?, 'fixture')",
            (manifest_hash,),
        )
        connection.execute(
            "INSERT INTO guidance_mask_bindings VALUES "
            "('proj_fixture', ?, ?, 'fixture')",
            (manifest_hash, asset_id),
        )
        connection.commit()

    AppStore(database).initialize()

    with sqlite3.connect(database) as connection:
        indexes = connection.execute(
            "PRAGMA index_list(guidance_mask_bindings)"
        ).fetchall()
        unique_column_sets = []
        for index in indexes:
            if not index[2]:
                continue
            columns = connection.execute(
                "SELECT name FROM pragma_index_info(?)",
                (index[1],),
            ).fetchall()
            unique_column_sets.append([column[0] for column in columns])
        preserved = connection.execute(
            "SELECT project_id, source_manifest_hash, asset_id, created_at "
            "FROM guidance_mask_bindings"
        ).fetchone()
        versions = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }

    assert ["asset_id"] not in unique_column_sets
    assert preserved == ("proj_fixture", manifest_hash, asset_id, "fixture")
    assert MIGRATION_VERSION in versions
