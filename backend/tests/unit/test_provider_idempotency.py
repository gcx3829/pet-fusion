from __future__ import annotations

import asyncio
import hashlib

from app.container import AppContainer
from app.domain.assets import SourceManifest
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, SearchStatus
from app.persistence.app_store import utcnow
from app.services.generator_service import (
    FAKE_IMAGE_MODEL,
    DeterministicFakeImageGenerator,
    GeneratedImage,
    GenerationRequest,
)
from app.services.prompt_compiler import compile_canonical_prompt
from tests.conftest import make_image_bytes


class SlowFakeImageGenerator(DeterministicFakeImageGenerator):
    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        request_key: str | None = None,
    ) -> list[GeneratedImage]:
        await asyncio.sleep(0.05)
        return await super().generate_round(request, request_key=request_key)


async def test_same_request_key_reuses_outputs_without_second_provider_call(
    settings, fake_generator
) -> None:
    container = AppContainer.build(settings, image_generator=fake_generator)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((180, 100, 60)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_idempotency",
        source_manifest=manifest,
        created_at=utcnow(),
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "same exact cat",
            "candidate_count": 2,
            "max_rounds": 1,
        }
    )
    search = container.app_store.create_search(
        search_id="search_idempotency",
        thread_id="search_idempotency",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=1,
    )
    request = GenerationRequest(
        search_id=search.search_id,
        source_manifest=manifest,
        placement=command.placement,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_index=0,
        candidate_count=2,
        model=FAKE_IMAGE_MODEL,
        quality="medium",
        size="96x64",
    )

    first = await container.generator_service.generate_round(
        request, expected_manifest_hash=manifest.manifest_hash
    )
    second = await container.generator_service.generate_round(
        request, expected_manifest_hash=manifest.manifest_hash
    )

    assert first == second
    assert fake_generator.call_count == 1
    assert all(candidate.schema_version == "candidate/v2" for candidate in first)
    assert all(candidate.raw_asset == candidate.protected_asset for candidate in first)
    assert all(candidate.composite is None for candidate in first)
    request_key = container.generator_service.build_request_key(request)
    assert container.app_store.provider_attempt_count(request_key) == 1


async def test_concurrent_same_request_has_single_provider_owner(settings) -> None:
    provider = SlowFakeImageGenerator()
    container = AppContainer.build(settings, image_generator=provider)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes())
    reference = container.asset_store.put_image_bytes(make_image_bytes((180, 100, 60)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_concurrent", source_manifest=manifest, created_at=utcnow()
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "same exact cat",
            "candidate_count": 2,
            "max_rounds": 1,
        }
    )
    search = container.app_store.create_search(
        search_id="search_concurrent",
        thread_id="search_concurrent",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=1,
    )
    request = GenerationRequest(
        search_id=search.search_id,
        source_manifest=manifest,
        placement=command.placement,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_index=0,
        candidate_count=2,
        model=FAKE_IMAGE_MODEL,
        quality="medium",
        size="96x64",
    )

    first, second = await asyncio.gather(
        container.generator_service.generate_round(
            request, expected_manifest_hash=manifest.manifest_hash
        ),
        container.generator_service.generate_round(
            request, expected_manifest_hash=manifest.manifest_hash
        ),
    )

    assert first == second
    assert provider.call_count == 1
    request_key = container.generator_service.build_request_key(request)
    assert container.app_store.provider_attempt_count(request_key) == 1
    assert len(container.app_store.get_search(search.search_id).candidates) == 2

    stale_request = request.model_copy(
        update={
            "round_index": 1,
            "prompt_hash": hashlib.sha256(b"stale-provider-recovery").hexdigest(),
        }
    )
    stale_key = container.generator_service.build_request_key(stale_request)
    claimed, status, _ = container.app_store.claim_provider_call(
        request_key=stale_key,
        operation="generate_round",
        search_id=search.search_id,
        request_payload={"source_manifest_hash": manifest.manifest_hash},
        owner_id="crashed-worker",
        lease_seconds=-1,
    )
    assert claimed and status == "running"
    # A stale provider lease is recoverable only while the owning Search is
    # still authorized for that exact target round.  SearchRunner performs
    # this transition before generation in production.
    assert container.app_store.update_search(
        search.search_id,
        status=SearchStatus.RUNNING,
        round_index=1,
        expected_statuses=[SearchStatus.QUEUED],
    )

    recovered = await container.generator_service.generate_round(
        stale_request, expected_manifest_hash=manifest.manifest_hash
    )

    assert len(recovered) == 2
    assert provider.call_count == 2
    assert container.app_store.provider_attempt_count(stale_key) == 2
    assert [event.type for event in container.app_store.list_events(search.search_id)] == [
        "round.candidate.ready",
        "round.candidate.ready",
        "round.candidate.ready",
        "round.candidate.ready",
    ]
