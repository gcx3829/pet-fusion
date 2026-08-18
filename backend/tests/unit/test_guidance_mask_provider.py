from __future__ import annotations

import base64
import hashlib
import io

import httpx
from openai import AsyncOpenAI
from PIL import Image

from app.domain.assets import AssetRef, SourceManifest
from app.domain.searches import PlacementIntent
from app.services.generator_service import (
    GENERATOR_GUIDANCE_MASK_RESIZE_VERSION,
    GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION,
    GenerationRequest,
    GeneratorService,
    OpenAIImageGenerator,
)
from app.services.openai_image_client import (
    OfficialOpenAIImageEditsTransport,
    OpenAIImageInput,
)


def _png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _asset(path, data: bytes, *, width: int, height: int) -> AssetRef:
    return AssetRef(
        asset_id="ast_" + hashlib.sha256(data).hexdigest()[:32],
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        width=width,
        height=height,
        mime_type="image/png",
    )


def _request(background: AssetRef, reference: AssetRef, guidance: AssetRef) -> GenerationRequest:
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    return GenerationRequest(
        search_id="search_guidance_provider",
        source_manifest=manifest,
        guidance_mask=guidance,
        placement=PlacementIntent(
            x=0.2,
            y=0.2,
            width=0.3,
            height=0.3,
            pose="sitting",
            facing="left",
        ),
        prompt="Use the authored edit region.",
        prompt_hash=hashlib.sha256(b"guidance-provider").hexdigest(),
        round_index=0,
        candidate_count=1,
        model="gpt-image-2",
        quality="high",
        size="auto",
    )


def test_guidance_mask_inverts_user_alpha_without_thresholding(tmp_path) -> None:
    background_data = _png(Image.new("RGB", (3, 1), (10, 20, 30)))
    reference_data = _png(Image.new("RGB", (2, 2), (40, 50, 60)))
    mask_image = Image.new("RGBA", (3, 1), (255, 255, 255, 0))
    mask_image.putdata(
        [
            (255, 255, 255, 0),
            (255, 255, 255, 128),
            (255, 255, 255, 255),
        ]
    )
    mask_data = _png(mask_image)
    background_path = tmp_path / "background.png"
    reference_path = tmp_path / "reference.png"
    mask_path = tmp_path / "guidance.png"
    background_path.write_bytes(background_data)
    reference_path.write_bytes(reference_data)
    mask_path.write_bytes(mask_data)
    background = _asset(background_path, background_data, width=3, height=1)
    reference = _asset(reference_path, reference_data, width=2, height=2)
    guidance = _asset(mask_path, mask_data, width=3, height=1)
    request = _request(background, reference, guidance)
    background_input = OpenAIImageInput(
        filename="00-background.png", png_bytes=background_data, mime_type="image/png"
    )

    provider_mask = OpenAIImageGenerator._provider_mask(request, background_input)

    with Image.open(io.BytesIO(provider_mask.png_bytes)) as opened:
        assert opened.size == (3, 1)
        provider_alpha = opened.getchannel("A")
        assert [provider_alpha.getpixel((x, 0)) for x in range(3)] == [255, 127, 0]


def test_guidance_mask_resizes_to_background_proxy_and_preserves_soft_edges(tmp_path) -> None:
    background_data = _png(Image.new("RGB", (4, 2), (10, 20, 30)))
    reference_data = _png(Image.new("RGB", (2, 2), (40, 50, 60)))
    mask = Image.new("RGBA", (4, 2), (255, 255, 255, 0))
    alpha = Image.new("L", (4, 2))
    alpha.putdata([0, 0, 255, 255, 0, 64, 192, 255])
    mask.putalpha(alpha)
    mask_data = _png(mask)
    background_path = tmp_path / "background.png"
    reference_path = tmp_path / "reference.png"
    mask_path = tmp_path / "guidance.png"
    background_path.write_bytes(background_data)
    reference_path.write_bytes(reference_data)
    mask_path.write_bytes(mask_data)
    request = _request(
        _asset(background_path, background_data, width=4, height=2),
        _asset(reference_path, reference_data, width=2, height=2),
        _asset(mask_path, mask_data, width=4, height=2),
    )
    background_proxy = _png(Image.new("RGB", (2, 1), (10, 20, 30)))

    provider_mask = OpenAIImageGenerator._provider_mask(
        request,
        OpenAIImageInput(
            filename="00-background.png", png_bytes=background_proxy, mime_type="image/png"
        ),
    )

    with Image.open(io.BytesIO(provider_mask.png_bytes)) as opened:
        assert opened.size == (2, 1)
        provider_alpha = opened.getchannel("A")
        alpha_values = [provider_alpha.getpixel((x, 0)) for x in range(2)]
        assert len(alpha_values) == 2
        assert min(alpha_values) < max(alpha_values)
        assert any(value not in {0, 255} for value in alpha_values)


def test_guidance_mask_request_key_contains_asset_and_transform_contract(
    tmp_path, monkeypatch
) -> None:
    import app.services.generator_service as generator_module

    background_data = _png(Image.new("RGB", (3, 1), (10, 20, 30)))
    reference_data = _png(Image.new("RGB", (2, 2), (40, 50, 60)))
    mask_data = _png(Image.new("RGBA", (3, 1), (255, 255, 255, 180)))
    background_path = tmp_path / "background.png"
    reference_path = tmp_path / "reference.png"
    mask_path = tmp_path / "guidance.png"
    background_path.write_bytes(background_data)
    reference_path.write_bytes(reference_data)
    mask_path.write_bytes(mask_data)
    request = _request(
        _asset(background_path, background_data, width=3, height=1),
        _asset(reference_path, reference_data, width=2, height=2),
        _asset(mask_path, mask_data, width=3, height=1),
    )

    request_key = GeneratorService.build_request_key(request)
    assert request_key != GeneratorService.build_request_key(
        request.model_copy(update={"guidance_mask": None})
    )
    # The key is intentionally opaque, but changing the immutable mask bytes
    # must produce a different idempotency key.
    changed = request.guidance_mask.model_copy(update={"sha256": "f" * 64})
    assert request_key != GeneratorService.build_request_key(
        request.model_copy(update={"guidance_mask": changed})
    )

    monkeypatch.setattr(
        generator_module,
        "GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION",
        GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION + "-changed",
    )
    assert request_key != GeneratorService.build_request_key(request)
    monkeypatch.setattr(
        generator_module,
        "GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION",
        GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION,
    )
    monkeypatch.setattr(
        generator_module,
        "GENERATOR_GUIDANCE_MASK_RESIZE_VERSION",
        GENERATOR_GUIDANCE_MASK_RESIZE_VERSION + "-changed",
    )
    assert request_key != GeneratorService.build_request_key(request)


async def test_authored_guidance_mask_is_the_mask_in_official_multipart_request(
    tmp_path,
) -> None:
    background_data = _png(Image.new("RGB", (4, 2), (10, 20, 30)))
    reference_data = _png(Image.new("RGB", (2, 2), (40, 50, 60)))
    authored = Image.new("RGBA", (4, 2), (255, 255, 255, 0))
    authored.putalpha(Image.frombytes("L", (4, 2), bytes([0, 64, 128, 255] * 2)))
    mask_data = _png(authored)
    background_path = tmp_path / "background.png"
    reference_path = tmp_path / "reference.png"
    mask_path = tmp_path / "guidance.png"
    background_path.write_bytes(background_data)
    reference_path.write_bytes(reference_data)
    mask_path.write_bytes(mask_data)
    request = _request(
        _asset(background_path, background_data, width=4, height=2),
        _asset(reference_path, reference_data, width=2, height=2),
        _asset(mask_path, mask_data, width=4, height=2),
    )
    captured: dict[str, bytes] = {}
    output_png = _png(Image.new("RGB", (4, 2), (90, 80, 70)))

    async def handle_request(http_request: httpx.Request) -> httpx.Response:
        captured["body"] = await http_request.aread()
        return httpx.Response(
            200,
            headers={"x-request-id": "req-guidance-multipart"},
            json={
                "created": 1,
                "data": [
                    {"b64_json": base64.b64encode(output_png).decode("ascii")}
                ],
            },
        )

    transport = OfficialOpenAIImageEditsTransport(
        api_key="test-key-never-sent",
        base_url="https://relay.example.test/v1",
    )
    source_inputs = OpenAIImageGenerator._source_inputs(request)
    expected_mask = OpenAIImageGenerator._provider_mask(request, source_inputs[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as client:
        transport._client = AsyncOpenAI(
            api_key="test-key-never-sent",
            base_url="https://relay.example.test/v1",
            http_client=client,
        )
        generated = await OpenAIImageGenerator(transport=transport).generate_round(request)

    assert generated[0].png_bytes == output_png
    body = captured["body"]
    assert b'name="mask"; filename="00-provider-mask.png"' in body
    assert expected_mask.png_bytes in body
    assert mask_data not in body
