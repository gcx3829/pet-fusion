from __future__ import annotations

import sqlite3

import pytest

from app.config import Settings
from app.container import AppContainer
from app.domain.assets import SourceManifest
from app.domain.errors import SourceManifestMismatchError
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, SearchStatus
from app.persistence.app_store import utcnow
from app.services.generator_service import DeterministicFakeImageGenerator
from tests.conftest import make_image_bytes


async def test_worker_claim_and_checkpoint_resume_do_not_regenerate(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "worker-data",
        run_inline=False,
        worker_lease_seconds=5,
    )
    provider = DeterministicFakeImageGenerator()
    container = AppContainer.build(settings, image_generator=provider)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((170, 90, 40)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(project_id="proj_worker", source_manifest=manifest, created_at=utcnow())
    container.app_store.create_project(project)
    request = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "same cat",
            "candidate_count": 2,
        }
    )
    container.app_store.create_search(
        search_id="search_worker",
        thread_id="search_worker",
        project=project,
        request=request,
    )

    claimed = container.app_store.claim_next_search(worker_id="test-worker", lease_seconds=5)
    assert claimed == "search_worker"
    await container.search_runner.run_search(claimed)
    completed = container.app_store.get_search(claimed)
    assert completed.status is SearchStatus.WAITING_FOR_HUMAN
    assert len(completed.candidates) == 2
    assert provider.call_count == 1

    # Simulate an app-store status write lag after the graph checkpoint completed.
    container.app_store.update_search(claimed, status=SearchStatus.RUNNING)
    resumed = await container.search_runner.run_search(claimed)
    assert resumed["status"] == "waiting_for_human"
    assert provider.call_count == 1
    reconciled = container.app_store.get_search(claimed)
    assert reconciled.status is SearchStatus.WAITING_FOR_HUMAN
    assert len(reconciled.candidates) == 2


async def test_worker_refuses_search_when_immutable_manifest_hash_changed(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "corrupt-data", run_inline=False)
    provider = DeterministicFakeImageGenerator()
    container = AppContainer.build(settings, image_generator=provider)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((160, 80, 30)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_corrupt", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    request = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "same cat",
            "candidate_count": 1,
        }
    )
    container.app_store.create_search(
        search_id="search_corrupt",
        thread_id="search_corrupt",
        project=project,
        request=request,
    )
    with sqlite3.connect(settings.resolved_app_db_path) as connection:
        connection.execute(
            "UPDATE search_runs SET source_manifest_hash = ? WHERE search_id = ?",
            ("f" * 64, "search_corrupt"),
        )
        connection.commit()

    with pytest.raises(SourceManifestMismatchError):
        await container.search_runner.run_search("search_corrupt")

    failed = container.app_store.get_search("search_corrupt")
    assert failed.status is SearchStatus.FAILED
    assert failed.error == {
        "code": "SourceManifestMismatchError",
        "message": "Search execution failed",
    }
    assert provider.call_count == 0


def test_worker_can_renew_its_lease(settings) -> None:
    container = AppContainer.build(settings)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((160, 80, 30)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_lease", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    request = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "same cat",
            "candidate_count": 1,
        }
    )
    container.app_store.create_search(
        search_id="search_lease",
        thread_id="search_lease",
        project=project,
        request=request,
    )

    assert container.app_store.claim_next_search(
        worker_id="worker-a", lease_seconds=5
    ) == "search_lease"
    assert container.app_store.renew_search_lease(
        search_id="search_lease", worker_id="worker-a", lease_seconds=5
    )
    assert not container.app_store.renew_search_lease(
        search_id="search_lease", worker_id="worker-b", lease_seconds=5
    )
    assert container.app_store.claim_next_search(
        worker_id="worker-b", lease_seconds=5
    ) is None
