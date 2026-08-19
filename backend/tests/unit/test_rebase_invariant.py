import hashlib
from pathlib import Path

import pytest
from anyio import Path as AsyncPath

from app.container import AppContainer
from app.domain.assets import SourceManifest
from app.domain.errors import SourceManifestMismatchError
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, SearchStatus
from app.persistence.app_store import utcnow
from app.services.generator_service import FAKE_IMAGE_MODEL, GenerationRequest
from app.services.prompt_compiler import compile_canonical_prompt
from tests.conftest import make_image_bytes


async def test_automatic_rounds_rebase_to_same_source_manifest(settings, fake_generator) -> None:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((200, 120, 40)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(project_id="proj_rebase", source_manifest=manifest, created_at=utcnow())
    container.app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.1,
                "y": 0.1,
                "width": 0.3,
                "height": 0.5,
                "pose": "standing",
                "facing": "camera",
            },
            "user_intent": "same cat",
            "candidate_count": 1,
            "max_rounds": 2,
        }
    )
    search = container.app_store.create_search(
        search_id="search_rebase",
        thread_id="search_rebase",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement, user_intent=command.user_intent, reference_count=1
    )
    for round_index in (0, 1):
        assert container.app_store.update_search(
            search.search_id,
            status=SearchStatus.RUNNING,
            round_index=round_index,
            expected_statuses=[
                SearchStatus.QUEUED if round_index == 0 else SearchStatus.RUNNING
            ],
        )
        await container.generator_service.generate_round(
            GenerationRequest(
                search_id=search.search_id,
                source_manifest=manifest,
                placement=command.placement,
                prompt=prompt + f"\nDirective round {round_index}",
                prompt_hash=hashlib.sha256((prompt_hash + str(round_index)).encode()).hexdigest(),
                round_index=round_index,
                candidate_count=1,
                model=FAKE_IMAGE_MODEL,
                quality="medium",
                size="96x64",
            ),
            expected_manifest_hash=manifest.manifest_hash,
        )

    assert [request.source_manifest.manifest_hash for request in fake_generator.requests] == [
        manifest.manifest_hash,
        manifest.manifest_hash,
    ]
    assert all(request.source_manifest == manifest for request in fake_generator.requests)
    candidate_paths = {
        candidate.protected_asset.path
        for candidate in container.app_store.get_search(search.search_id).candidates
    }
    source_paths = {asset.path for asset in (manifest.background, *manifest.cat_references)}
    assert candidate_paths.isdisjoint(source_paths)


async def test_generator_rejects_candidate_directory_as_source(settings, fake_generator) -> None:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    candidate_dir = Path(settings.asset_dir) / "searches" / "rounds" / "candidates"
    bad_store = container.asset_store
    asset = bad_store.put_image_bytes(make_image_bytes())
    candidate_dir.mkdir(parents=True)
    bad_path = candidate_dir / "candidate.png"
    await AsyncPath(bad_path).write_bytes(await AsyncPath(Path(asset.path)).read_bytes())
    bad_asset = asset.model_copy(update={"path": str(bad_path)})
    manifest = SourceManifest.create(background=bad_asset, cat_references=[asset])
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.1,
                "y": 0.1,
                "width": 0.2,
                "height": 0.2,
                "pose": "sit",
                "facing": "left",
            },
            "user_intent": "same cat",
            "candidate_count": 1,
        }
    )
    project = ProjectRecord(
        project_id="proj_bad_source", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    container.app_store.create_search(
        search_id="search_bad_source",
        thread_id="search_bad_source",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement, user_intent=command.user_intent, reference_count=1
    )
    request = GenerationRequest(
        search_id="search_bad_source",
        source_manifest=manifest,
        placement=command.placement,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_index=1,
        candidate_count=1,
        model=FAKE_IMAGE_MODEL,
        quality="medium",
        size="96x64",
    )
    with pytest.raises(SourceManifestMismatchError):
        await container.generator_service.generate_round(
            request, expected_manifest_hash=manifest.manifest_hash
        )


async def test_generator_rejects_real_candidate_asset_as_rebased_source(
    settings, fake_generator
) -> None:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((200, 120, 40)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_candidate_source", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.1,
                "y": 0.1,
                "width": 0.3,
                "height": 0.5,
                "pose": "standing",
                "facing": "camera",
            },
            "user_intent": "same cat",
            "candidate_count": 1,
        }
    )
    search = container.app_store.create_search(
        search_id="search_candidate_source",
        thread_id="search_candidate_source",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=1,
    )
    candidates = await container.generator_service.generate_round(
        GenerationRequest(
            search_id=search.search_id,
            source_manifest=manifest,
            placement=command.placement,
            prompt=prompt,
            prompt_hash=prompt_hash,
            round_index=0,
            candidate_count=1,
            model=FAKE_IMAGE_MODEL,
            quality="medium",
            size="96x64",
        ),
        expected_manifest_hash=manifest.manifest_hash,
    )
    candidate_manifest = SourceManifest.create(
        background=candidates[0].protected_asset,
        cat_references=[reference],
    )

    with pytest.raises(SourceManifestMismatchError):
        await container.generator_service.generate_round(
            GenerationRequest(
                search_id=search.search_id,
                source_manifest=candidate_manifest,
                placement=command.placement,
                prompt=prompt,
                prompt_hash=prompt_hash,
                round_index=1,
                candidate_count=1,
                model=FAKE_IMAGE_MODEL,
                quality="medium",
                size="96x64",
            ),
            expected_manifest_hash=candidate_manifest.manifest_hash,
        )
