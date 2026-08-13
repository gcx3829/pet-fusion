from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from app.config import Settings
from app.container import AppContainer
from app.domain.assets import AssetRef, SourceManifest
from app.domain.errors import ConfigurationError
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent
from app.persistence.app_store import utcnow
from app.services.generator_service import (
    DeterministicFakeImageGenerator,
    GenerationRequest,
    GeneratorService,
    OpenAIImageGenerator,
)
from app.services.openai_image_client import (
    OfficialOpenAIImageEditsTransport,
    OpenAIImageEditResult,
    OpenAIImageInput,
)
from app.services.prompt_compiler import compile_canonical_prompt
from tests.conftest import make_image_bytes


class RecordingImageEditsTransport:
    def __init__(self, png_images: tuple[bytes, ...]) -> None:
        self.png_images = png_images
        self.calls: list[dict[str, object]] = []

    async def edit(
        self,
        *,
        model: str,
        prompt: str,
        images: Sequence[OpenAIImageInput],
        n: int,
        quality: str,
        size: str,
    ) -> OpenAIImageEditResult:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "images": tuple(images),
                "n": n,
                "quality": quality,
                "size": size,
            }
        )
        return OpenAIImageEditResult(
            png_images=self.png_images,
            request_id="req_offline_transport",
            usage={"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
        )


def test_fake_generator_remains_the_safe_default_even_when_a_key_exists(tmp_path) -> None:
    settings = Settings(
        data_dir=Path(tmp_path) / "fake-default",
        fake_generator=True,
        openai_api_key="test-key-never-sent",
    )

    container = AppContainer.build(settings)

    assert isinstance(container.image_generator, DeterministicFakeImageGenerator)


def test_live_generator_requires_a_nonempty_backend_key(tmp_path) -> None:
    settings = Settings(
        data_dir=Path(tmp_path) / "missing-key",
        fake_generator=False,
        openai_api_key=None,
    )

    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        AppContainer.build(settings)


async def test_official_transport_uses_supported_edit_shape_and_decodes_audit_fields() -> None:
    png_bytes = make_image_bytes((12, 34, 56))
    captured: dict[str, object] = {}

    async def handle_request(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = await request.aread()
        return httpx.Response(
            200,
            headers={"x-request-id": "req_mock_http_transport"},
            json={
                "created": 1,
                "data": [
                    {"b64_json": base64.b64encode(png_bytes).decode("ascii")}
                ],
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 13,
                    "total_tokens": 20,
                },
            },
        )

    transport = OfficialOpenAIImageEditsTransport(
        api_key="test-key-never-sent",
        base_url="https://relay.example.test/v1",
    )
    inputs = (
        OpenAIImageInput(filename="00-background.png", png_bytes=png_bytes),
        OpenAIImageInput(filename="01-reference.png", png_bytes=png_bytes),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http_client:
        client = AsyncOpenAI(
            api_key="test-key-never-sent",
            base_url="https://relay.example.test/v1",
            http_client=http_client,
        )
        transport._client = client
        result = await transport.edit(
            model="gpt-image-2-2026-04-21",
            prompt="offline transport contract",
            images=inputs,
            n=1,
            quality="medium",
            size="1024x1024",
        )

    assert result.png_images == (png_bytes,)
    assert result.request_id == "req_mock_http_transport"
    assert result.usage == {"input_tokens": 7, "output_tokens": 13, "total_tokens": 20}
    assert captured["url"] == "https://relay.example.test/v1/images/edits"
    assert captured["authorization"] == "Bearer test-key-never-sent"
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b'name="image[]"; filename="00-background.png"' in body
    assert b'name="image[]"; filename="01-reference.png"' in body
    assert b'name="model"' in body and b"gpt-image-2-2026-04-21" in body
    assert b'name="output_format"' in body and b"png" in body
    assert b"input_fidelity" not in body


async def test_openai_generator_uses_ordered_png_sources_and_persists_safe_audit(settings) -> None:
    from app.persistence.app_store import AppStore
    from app.services.asset_store import AssetStore

    app_store = AppStore(settings.resolved_app_db_path)
    asset_store = AssetStore(settings.asset_dir, max_image_pixels=settings.max_image_pixels)
    asset_store.initialize()
    app_store.initialize()
    background = asset_store.put_image_bytes(make_image_bytes((15, 20, 25)))
    reference_one = asset_store.put_image_bytes(make_image_bytes((30, 40, 50)))
    reference_two = asset_store.put_image_bytes(make_image_bytes((60, 70, 80)))
    manifest = SourceManifest.create(
        background=background,
        cat_references=[reference_one, reference_two],
    )
    project = ProjectRecord(project_id="proj_openai", source_manifest=manifest, created_at=utcnow())
    app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.3,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "Place the same cat in the scene.",
            "candidate_count": 2,
        }
    )
    search = app_store.create_search(
        search_id="search_openai",
        thread_id="search_openai",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=2,
    )
    transport = RecordingImageEditsTransport(
        (make_image_bytes((120, 30, 40)), make_image_bytes((50, 90, 10)))
    )
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=asset_store,
        app_store=app_store,
        model="gpt-image-2-2026-04-21",
        quality="medium",
        size="1024x1024",
    )
    request = GenerationRequest(
        search_id=search.search_id,
        source_manifest=manifest,
        placement=command.placement,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_index=0,
        candidate_count=2,
        model=service.model,
        quality=service.quality,
        size=service.size or "1024x1024",
    )

    candidates = await service.generate_round(
        request, expected_manifest_hash=manifest.manifest_hash
    )

    assert len(candidates) == 2
    assert [candidate.protected_asset.mime_type for candidate in candidates] == [
        "image/png",
        "image/png",
    ]
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["model"] == "gpt-image-2-2026-04-21"
    assert call["n"] == 2
    assert call["quality"] == "medium"
    assert call["size"] == "1024x1024"
    inputs = call["images"]
    assert isinstance(inputs, tuple)
    assert [item.filename.split("-", 2)[1] for item in inputs] == [
        "background",
        "reference",
        "reference",
    ]
    assert all(item.png_bytes.startswith(b"\x89PNG\r\n\x1a\n") for item in inputs)
    assert all("candidates" not in item.filename for item in inputs)
    request_key = service.build_request_key(request)
    provider_call = app_store.get_provider_call(request_key)
    assert provider_call is not None
    status, response = provider_call
    assert status == "completed"
    assert response is not None
    assert response["provider"] == {
        "model": "gpt-image-2-2026-04-21",
        "request_id": "req_offline_transport",
        "usage": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
    }


async def test_openai_generator_normalizes_jpeg_and_webp_sources_in_memory(tmp_path) -> None:
    background_path = Path(tmp_path) / "background.jpg"
    reference_path = Path(tmp_path) / "reference.webp"
    background_bytes = make_image_bytes((10, 20, 30), image_format="JPEG")
    reference_bytes = make_image_bytes((80, 90, 100), image_format="WEBP")
    background_path.write_bytes(background_bytes)
    reference_path.write_bytes(reference_bytes)
    background = AssetRef(
        asset_id="ast_jpeg_background",
        path=str(background_path),
        sha256=hashlib.sha256(background_bytes).hexdigest(),
        mime_type="image/jpeg",
        width=96,
        height=64,
    )
    reference = AssetRef(
        asset_id="ast_webp_reference",
        path=str(reference_path),
        sha256=hashlib.sha256(reference_bytes).hexdigest(),
        mime_type="image/webp",
        width=96,
        height=64,
    )
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    transport = RecordingImageEditsTransport((make_image_bytes((1, 2, 3)),))
    request = GenerationRequest(
        search_id="search_external_formats",
        source_manifest=manifest,
        placement=PlacementIntent(
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        ),
        prompt="Use the references.",
        prompt_hash=hashlib.sha256(b"external-formats").hexdigest(),
        round_index=0,
        candidate_count=1,
        model="gpt-image-2-2026-04-21",
        quality="medium",
        size="1024x1024",
    )

    result = await OpenAIImageGenerator(transport=transport).generate_round(request)

    assert len(result) == 1
    inputs = transport.calls[0]["images"]
    assert isinstance(inputs, tuple)
    assert [item.filename.split("-", 2)[1] for item in inputs] == ["background", "reference"]
    assert all(item.png_bytes.startswith(b"\x89PNG\r\n\x1a\n") for item in inputs)


async def test_openai_source_normalization_does_not_block_lease_heartbeat(
    tmp_path, monkeypatch
) -> None:
    background_path = Path(tmp_path) / "background.png"
    reference_path = Path(tmp_path) / "reference.png"
    background_bytes = make_image_bytes((10, 20, 30))
    reference_bytes = make_image_bytes((80, 90, 100))
    background_path.write_bytes(background_bytes)
    reference_path.write_bytes(reference_bytes)
    manifest = SourceManifest.create(
        background=AssetRef(
            asset_id="ast_background",
            path=str(background_path),
            sha256=hashlib.sha256(background_bytes).hexdigest(),
            width=96,
            height=64,
        ),
        cat_references=[
            AssetRef(
                asset_id="ast_reference",
                path=str(reference_path),
                sha256=hashlib.sha256(reference_bytes).hexdigest(),
                width=96,
                height=64,
            )
        ],
    )
    request = GenerationRequest(
        search_id="search_nonblocking_sources",
        source_manifest=manifest,
        placement=PlacementIntent(
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        ),
        prompt="Use immutable inputs.",
        prompt_hash=hashlib.sha256(b"nonblocking-sources").hexdigest(),
        round_index=0,
        candidate_count=1,
        model="gpt-image-2-2026-04-21",
        quality="medium",
        size="1024x1024",
    )
    original = OpenAIImageGenerator._source_inputs
    started = threading.Event()
    release = threading.Event()

    def slow_source_inputs(value: GenerationRequest) -> tuple[OpenAIImageInput, ...]:
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("event loop did not remain responsive")
        return original(value)

    monkeypatch.setattr(
        OpenAIImageGenerator,
        "_source_inputs",
        staticmethod(slow_source_inputs),
    )
    transport = RecordingImageEditsTransport((make_image_bytes((1, 2, 3)),))
    task = asyncio.create_task(
        OpenAIImageGenerator(transport=transport).generate_round(request)
    )
    assert await asyncio.to_thread(started.wait, 0.5)
    assert not task.done()
    release.set()

    result = await task

    assert len(result) == 1
    assert len(transport.calls) == 1


async def test_provider_audit_omits_non_numeric_usage_payloads(settings) -> None:
    from app.persistence.app_store import AppStore
    from app.services.asset_store import AssetStore

    app_store = AppStore(settings.resolved_app_db_path)
    asset_store = AssetStore(settings.asset_dir, max_image_pixels=settings.max_image_pixels)
    asset_store.initialize()
    app_store.initialize()
    background = asset_store.put_image_bytes(make_image_bytes((15, 20, 25)))
    reference = asset_store.put_image_bytes(make_image_bytes((30, 40, 50)))
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="proj_safe_usage",
        source_manifest=manifest,
        created_at=utcnow(),
    )
    app_store.create_project(project)
    command = CreateSearchRequest.model_validate(
        {
            "placement": {
                "x": 0.2,
                "y": 0.2,
                "width": 0.3,
                "height": 0.3,
                "pose": "sitting",
                "facing": "left",
            },
            "user_intent": "Place the cat.",
            "candidate_count": 1,
        }
    )
    search = app_store.create_search(
        search_id="search_safe_usage",
        thread_id="search_safe_usage",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=1,
    )
    transport = RecordingImageEditsTransport((make_image_bytes((120, 30, 40)),))

    async def unsafe_edit(**kwargs: object) -> OpenAIImageEditResult:
        result = await RecordingImageEditsTransport.edit(transport, **kwargs)  # type: ignore[arg-type]
        return OpenAIImageEditResult(
            png_images=result.png_images,
            request_id=result.request_id,
            usage={
                "input_tokens": 10,
                "debug": "data:image/png;base64,do-not-persist",
                "binary": b"do-not-persist",
                "sk-not-a-usage-field": 123,
                "cached": True,
                "nested": {"output_tokens": 5, "secret": "sk-not-persisted"},
            },
        )

    transport.edit = unsafe_edit  # type: ignore[method-assign]
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=asset_store,
        app_store=app_store,
        model="gpt-image-2-2026-04-21",
        quality="medium",
        size="1024x1024",
    )
    request = GenerationRequest(
        search_id=search.search_id,
        source_manifest=manifest,
        placement=command.placement,
        prompt=prompt,
        prompt_hash=prompt_hash,
        round_index=0,
        candidate_count=1,
        model=service.model,
        quality=service.quality,
        size=service.size or "1024x1024",
    )

    await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)

    provider_call = app_store.get_provider_call(service.build_request_key(request))
    assert provider_call is not None
    _, response = provider_call
    assert response is not None
    serialized = json.dumps(response)
    assert "data:image" not in serialized
    assert "sk-not-persisted" not in serialized
    assert response["provider"] == {
        "model": "gpt-image-2-2026-04-21",
        "request_id": "req_offline_transport",
        "usage": {"input_tokens": 10},
    }
