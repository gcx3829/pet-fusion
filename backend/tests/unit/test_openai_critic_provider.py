from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from app.config import Settings
from app.container import AppContainer
from app.domain.assets import SourceManifest
from app.domain.evaluations import (
    CriticCategory,
    CriticIssue,
    DimensionScores,
    Severity,
)
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent
from app.persistence.app_store import utcnow
from app.services.critic_service import (
    CriticEvaluationService,
    CriticInput,
    CriticStructuredOutput,
)
from app.services.generator_service import FAKE_IMAGE_MODEL, GenerationRequest
from app.services.openai_critic_client import OfficialOpenAICriticProvider
from app.services.prompt_compiler import compile_canonical_prompt
from app.services.proxy_builder import CriticProxyBuilder
from tests.conftest import make_image_bytes


class StubUsage:
    def model_dump(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {
            "input_tokens": 123,
            "output_tokens": 45,
            "total_tokens": 168,
            "provider_text": "must-not-be-audited",
        }


class StubResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed=CriticStructuredOutput(
                scores=DimensionScores(
                    cat_identity=94,
                    pose_geometry=91,
                    perspective_scale=90,
                    lighting_color=89,
                    optical_consistency=88,
                    physical_integration=75,
                    scene_preservation=96,
                    overall_photographic_naturalness=90,
                ),
                issues=(
                    CriticIssue(
                        issue_id="contact-seam",
                        category="contact-shadow",
                        severity=Severity.WARNING,
                        evidence="A small local contact seam is visible.",
                        suggested_fix="Soften the local seam. Redesign the whole background.",
                        confidence=0.8,
                    ),
                ),
                no_meaningful_defect=False,
                identity_match=True,
                prompt_adherent=True,
                recommended_action="review",
                summary="The candidate is usable with one minor local warning.",
            ),
            _request_id="req_critic_stub",
            usage=StubUsage(),
        )


class StubClient:
    def __init__(self) -> None:
        self.responses = StubResponses()


def test_container_selects_live_critic_without_constructing_a_client(
    settings: Settings,
) -> None:
    live_settings = settings.model_copy(
        update={
            "fake_critic": False,
            "openai_api_key": SecretStr("stub-container-key"),
            "openai_base_url": "https://relay.example.test/v1",
            "openai_critic_model": "gpt-5.6-terra",
        }
    )

    container = AppContainer.build(live_settings)

    provider = container.search_runner.critic_service
    assert isinstance(provider, OfficialOpenAICriticProvider)
    assert provider.model == "gpt-5.6-terra"
    assert provider._client is None
    assert "stub-container-key" not in repr(live_settings)


async def test_official_critic_uses_structured_responses_and_safe_idempotent_audit(
    settings: Settings,
) -> None:
    container = AppContainer.build(settings)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes(size=(96, 64)))
    reference = container.asset_store.put_image_bytes(
        make_image_bytes((120, 80, 40), size=(48, 36))
    )
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="project-live-critic-stub",
        source_manifest=manifest,
        created_at=utcnow(),
    )
    container.app_store.create_project(project)
    command = CreateSearchRequest(
        placement=PlacementIntent(
            x=0.2,
            y=0.2,
            width=0.3,
            height=0.4,
            pose="sitting",
            facing="left",
        ),
        user_intent="Place the exact pet naturally in the photograph.",
        candidate_count=1,
    )
    search = container.app_store.create_search(
        search_id="search-live-critic-stub",
        thread_id="search-live-critic-stub",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=1,
    )
    candidate = (
        await container.generator_service.generate_round(
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
    )[0]
    proxies = CriticProxyBuilder(
        asset_store=container.asset_store,
        app_store=container.app_store,
        max_side=64,
    ).build(
        source_manifest=manifest,
        candidate=candidate,
        placement=command.placement,
    )
    request = CriticInput(
        candidate=candidate,
        source_manifest=manifest,
        placement=command.placement,
        canonical_prompt=prompt,
        canonical_prompt_hash=prompt_hash,
        proxies=proxies,
    )
    stub_client = StubClient()
    factory_arguments: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> StubClient:
        factory_arguments.append(kwargs)
        return stub_client

    provider = OfficialOpenAICriticProvider(
        api_key="stub-secret-key",
        base_url="https://relay.example.test/v1/",
        model="gpt-5.6-terra",
        asset_store=container.asset_store,
        client_factory=client_factory,
    )
    service = CriticEvaluationService(provider=provider, app_store=container.app_store)

    first = await service.evaluate(search_id=search.search_id, request=request)
    replay = await service.evaluate(search_id=search.search_id, request=request)

    assert first == replay
    assert first.issues[0].category is CriticCategory.PHYSICAL_INTEGRATION
    assert first.issues[0].suggested_fix == "Soften the local seam."
    assert factory_arguments == [
        {
            "api_key": "stub-secret-key",
            "base_url": "https://relay.example.test/v1",
        }
    ]
    assert len(stub_client.responses.calls) == 1
    call = stub_client.responses.calls[0]
    assert call["model"] == "gpt-5.6-terra"
    assert call["text_format"] is CriticStructuredOutput
    assert call["store"] is False
    image_parts = [
        part
        for message in call["input"]
        for part in message["content"]
        if part["type"] == "input_image"
    ]
    assert len(image_parts) == 4
    assert all(part["image_url"].startswith("data:image/png;base64,") for part in image_parts)
    assert all(part["detail"] == "high" for part in image_parts)

    request_key = service.build_request_key(
        search_id=search.search_id,
        request=request,
        model=provider.model,
        rubric_version=provider.rubric_version,
        provider_fingerprint=provider.provider_fingerprint,
    )
    provider_call = container.app_store.get_provider_call(request_key)
    assert provider_call is not None
    status, response = provider_call
    assert status == "completed"
    assert response is not None
    assert response["provider"] == {
        "request_id": "req_critic_stub",
        "usage": {"input_tokens": 123, "output_tokens": 45, "total_tokens": 168},
        "model": "gpt-5.6-terra",
    }
    with sqlite3.connect(settings.resolved_app_db_path) as connection:
        request_json, response_json = connection.execute(
            "SELECT request_json, response_json FROM provider_calls WHERE request_key = ?",
            (request_key,),
        ).fetchone()
    audited_request = json.loads(request_json)
    assert audited_request["input_semantics_version"] == "raw-authority/v1"
    assert audited_request["raw_asset_id"] == request.candidate.raw_asset.asset_id
    assert audited_request["raw_asset_sha256"] == request.candidate.raw_asset.sha256
    assert all("protected" not in key for key in audited_request)
    audited = audited_request, json.loads(response_json)
    audited_text = json.dumps(audited, sort_keys=True)
    assert "stub-secret-key" not in audited_text
    assert "relay.example.test" not in audited_text
    assert "data:image" not in audited_text
    assert prompt not in audited_text
    assert "must-not-be-audited" not in audited_text
