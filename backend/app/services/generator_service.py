from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.compositing import CropMapping, CropPadding, PixelBox
from app.domain.errors import SourceManifestMismatchError
from app.domain.searches import PlacementIntent
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import CompositeFloorService
from app.services.openai_image_client import (
    OpenAIImageEditResult,
    OpenAIImageEditsTransport,
    OpenAIImageInput,
)

FAKE_IMAGE_MODEL = "fake-gpt-image-2"
GENERATOR_SCHEMA_VERSION = "generator-request/v1"
GENERATOR_INPUT_PROXY_VERSION = "generator-input-proxy/v1"
GENERATOR_BACKGROUND_MAX_SIDE = 2048
GENERATOR_REFERENCE_MAX_SIDE = 1536
PROVIDER_RESULT_POLL_SECONDS = 0.02
PROVIDER_RESULT_WAIT_SECONDS = 30.0
PROVIDER_CALL_LEASE_SECONDS = 5
PROVIDER_USAGE_TOTAL_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
PROVIDER_USAGE_DETAIL_FIELDS = frozenset({"image_tokens", "text_tokens"})


class GenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    search_id: str
    source_manifest: SourceManifest
    placement: PlacementIntent
    prompt: str
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    round_index: int = Field(ge=0)
    candidate_count: int = Field(ge=1, le=4)
    model: str
    quality: str
    size: str


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    png_bytes: bytes
    variant_index: int
    provider_request_id: str | None = None
    provider_usage: Mapping[str, object] | None = None


class ImageGenerator(Protocol):
    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]: ...


class DeterministicFakeImageGenerator:
    """Test double that paints a deterministic marker over the immutable source image."""

    def __init__(self) -> None:
        self.call_count = 0
        self.requests: list[GenerationRequest] = []

    @staticmethod
    def _render_round(request: GenerationRequest) -> list[GeneratedImage]:
        with Image.open(request.source_manifest.background.path) as opened:
            base = opened.convert("RGBA")

        results: list[GeneratedImage] = []
        for variant_index in range(request.candidate_count):
            canvas = base.copy()
            overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            left = round(request.placement.x * canvas.width)
            top = round(request.placement.y * canvas.height)
            right = round((request.placement.x + request.placement.width) * canvas.width)
            bottom = round((request.placement.y + request.placement.height) * canvas.height)
            seed = hashlib.sha256(f"{request.prompt_hash}:{variant_index}".encode()).digest()
            color = (seed[0], seed[1], seed[2], 210)
            shadow_height = max(2, round((bottom - top) * 0.08))
            draw.ellipse(
                (left, bottom - shadow_height, right, bottom + shadow_height),
                fill=(0, 0, 0, 70),
            )
            draw.rounded_rectangle(
                (left, top, right, bottom),
                radius=max(2, min(right - left, bottom - top) // 4),
                fill=color,
                outline=(255, 255, 255, 230),
                width=max(1, canvas.width // 300),
            )
            canvas = Image.alpha_composite(canvas, overlay).convert("RGB")
            output = io.BytesIO()
            canvas.save(output, format="PNG", compress_level=9, optimize=False)
            results.append(GeneratedImage(png_bytes=output.getvalue(), variant_index=variant_index))
        return results

    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]:
        self.call_count += 1
        self.requests.append(request)
        return await asyncio.to_thread(self._render_round, request)


class OpenAIImageGenerator:
    """GPT Image 2 edit provider using bounded, in-memory PNG source proxies."""

    def __init__(self, *, transport: OpenAIImageEditsTransport) -> None:
        self.transport = transport

    @staticmethod
    def _source_inputs(request: GenerationRequest) -> tuple[OpenAIImageInput, ...]:
        assets = (request.source_manifest.background, *request.source_manifest.cat_references)
        inputs: list[OpenAIImageInput] = []
        for index, asset in enumerate(assets):
            with Image.open(asset.filesystem_path) as opened:
                has_alpha = "A" in opened.getbands() or "transparency" in opened.info
                normalized = opened.convert("RGBA" if has_alpha else "RGB")
                max_side = (
                    GENERATOR_BACKGROUND_MAX_SIDE
                    if index == 0
                    else GENERATOR_REFERENCE_MAX_SIDE
                )
                longest_side = max(normalized.size)
                if longest_side > max_side:
                    scale = max_side / longest_side
                    target_size = (
                        max(1, round(normalized.width * scale)),
                        max(1, round(normalized.height * scale)),
                    )
                    normalized = normalized.resize(target_size, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                normalized.save(output, format="PNG", compress_level=9, optimize=False)
            png_bytes = output.getvalue()
            role = "background" if index == 0 else f"reference-{index}"
            inputs.append(
                OpenAIImageInput(
                    filename=f"{index:02d}-{role}-{asset.asset_id}.png",
                    png_bytes=png_bytes,
                )
            )
        return tuple(inputs)

    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]:
        source_inputs = await asyncio.to_thread(self._source_inputs, request)
        result: OpenAIImageEditResult = await self.transport.edit(
            model=request.model,
            prompt=request.prompt,
            images=source_inputs,
            n=request.candidate_count,
            quality=request.quality,
            size=request.size,
        )
        if len(result.png_images) != request.candidate_count:
            raise RuntimeError("OpenAI Image API returned an unexpected number of candidates")
        generated: list[GeneratedImage] = []
        for variant_index, png_bytes in enumerate(result.png_images):
            if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("OpenAI Image API did not return a PNG candidate")
            generated.append(
                GeneratedImage(
                    png_bytes=png_bytes,
                    variant_index=variant_index,
                    provider_request_id=result.request_id,
                    provider_usage=result.usage,
                )
            )
        return generated


def _safe_provider_usage(usage: Mapping[str, object]) -> dict[str, object]:
    """Keep numeric token accounting only; never retain provider text or binary payloads."""

    def numeric(value: object) -> int | float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None

    sanitized: dict[str, object] = {}
    for field in PROVIDER_USAGE_TOTAL_FIELDS:
        if (value := numeric(usage.get(field))) is not None:
            sanitized[field] = value
    for field in ("input_tokens_details", "output_tokens_details"):
        details = usage.get(field)
        if not isinstance(details, Mapping):
            continue
        sanitized_details = {
            detail_field: value
            for detail_field in PROVIDER_USAGE_DETAIL_FIELDS
            if (value := numeric(details.get(detail_field))) is not None
        }
        if sanitized_details:
            sanitized[field] = sanitized_details
    return sanitized


class GeneratorService:
    def __init__(
        self,
        *,
        provider: ImageGenerator,
        asset_store: AssetStore,
        app_store: AppStore,
        model: str = FAKE_IMAGE_MODEL,
        quality: str = "medium",
        size: str | None = None,
        composite_floor: CompositeFloorService | None = None,
    ) -> None:
        self.provider = provider
        self.asset_store = asset_store
        self.app_store = app_store
        self.model = model
        self.quality = quality
        self.size = size
        self.composite_floor = composite_floor or CompositeFloorService(asset_store)

    @staticmethod
    def _crop_mapping_for_output(
        *,
        source_manifest: SourceManifest,
        output_asset: AssetRef,
    ) -> CropMapping | None:
        """Describe how a provider canvas rebases to the immutable full background.

        GPT Image may produce a canvas independent of the source dimensions. Until
        crop planning is introduced, the whole immutable background is the stable
        crop and the provider canvas maps onto it without padding. Full-size output
        intentionally returns ``None`` to preserve the direct-pixel path.
        """

        background = source_manifest.background
        if (output_asset.width, output_asset.height) == (background.width, background.height):
            return None
        return CropMapping(
            full_width=background.width,
            full_height=background.height,
            crop_box=PixelBox(x=0, y=0, width=background.width, height=background.height),
            canvas_width=output_asset.width,
            canvas_height=output_asset.height,
            padding=CropPadding(),
        )

    @staticmethod
    def build_request_key(request: GenerationRequest) -> str:
        payload = {
            "schema_version": GENERATOR_SCHEMA_VERSION,
            "operation": "generate_round",
            "search_id": request.search_id,
            "round_index": request.round_index,
            "candidate_count": request.candidate_count,
            "model": request.model,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "prompt_hash": request.prompt_hash,
            "quality": request.quality,
            "size": request.size,
            "input_proxy_version": GENERATOR_INPUT_PROXY_VERSION,
            "background_max_side": GENERATOR_BACKGROUND_MAX_SIDE,
            "reference_max_side": GENERATOR_REFERENCE_MAX_SIDE,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _assert_rebase_inputs(
        self, request: GenerationRequest, *, expected_manifest_hash: str
    ) -> None:
        manifest = request.source_manifest
        manifest.assert_integrity()
        search = self.app_store.get_search(request.search_id)
        project = self.app_store.get_project(search.project_id)
        if (
            expected_manifest_hash != search.source_manifest_hash
            or manifest.manifest_hash != search.source_manifest_hash
            or manifest != project.source_manifest
        ):
            raise SourceManifestMismatchError(
                "Generator inputs differ from the search's immutable source manifest"
            )
        for asset in (manifest.background, *manifest.cat_references):
            lowered_parts = {part.lower() for part in asset.filesystem_path.parts}
            if "candidates" in lowered_parts or "rounds" in lowered_parts:
                raise SourceManifestMismatchError(
                    "Automatic search cannot read a previous candidate as generator input"
                )
            self.asset_store.assert_intact(asset)

    def _emit_candidate_ready(self, search_id: str, candidate: CandidateRecord) -> None:
        self.app_store.emit_event(
            search_id=search_id,
            event_key=(f"candidate:{candidate.round_index}:{candidate.variant_index}:ready"),
            event_type="round.candidate.ready",
            payload={"candidate": CandidateResponse.from_record(candidate).model_dump(mode="json")},
        )

    def _completed_candidates(
        self,
        *,
        search_id: str,
        completed_response: dict[str, object],
    ) -> list[CandidateRecord]:
        stored_candidates = completed_response.get("candidates")
        if not isinstance(stored_candidates, list):
            raise RuntimeError("Completed provider audit has an invalid response shape")
        completed = [CandidateRecord.model_validate(item) for item in stored_candidates]
        for candidate in completed:
            self.app_store.add_candidate(search_id, candidate)
            self._emit_candidate_ready(search_id, candidate)
        return completed

    async def _renew_provider_lease(
        self, *, request_key: str, owner_id: str
    ) -> None:
        interval = PROVIDER_CALL_LEASE_SECONDS / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.app_store.renew_provider_call_lease,
                request_key=request_key,
                owner_id=owner_id,
                lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
            )
            if not renewed:
                return

    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        expected_manifest_hash: str,
    ) -> list[CandidateRecord]:
        await asyncio.to_thread(
            self._assert_rebase_inputs,
            request,
            expected_manifest_hash=expected_manifest_hash,
        )
        request_key = self.build_request_key(request)
        audit_payload = {
            "schema_version": GENERATOR_SCHEMA_VERSION,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "source_asset_ids": [
                request.source_manifest.background.asset_id,
                *[asset.asset_id for asset in request.source_manifest.cat_references],
            ],
            "prompt_hash": request.prompt_hash,
            "round_index": request.round_index,
            "candidate_count": request.candidate_count,
            "model": request.model,
            "quality": request.quality,
            "size": request.size,
            "input_proxy": {
                "schema_version": GENERATOR_INPUT_PROXY_VERSION,
                "background_max_side": GENERATOR_BACKGROUND_MAX_SIDE,
                "reference_max_side": GENERATOR_REFERENCE_MAX_SIDE,
            },
        }
        owner_id = f"provider_{uuid4().hex}"
        claimed, status, completed_response = await asyncio.to_thread(
            self.app_store.claim_provider_call,
            request_key=request_key,
            operation="generate_round",
            search_id=request.search_id,
            request_payload=audit_payload,
            owner_id=owner_id,
            lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
        )
        if status == "completed" and completed_response is not None:
            return await asyncio.to_thread(
                self._completed_candidates,
                search_id=request.search_id, completed_response=completed_response
            )

        existing = await asyncio.to_thread(
            self.app_store.find_candidates_for_request, request.search_id, request_key
        )
        if len(existing) == request.candidate_count:
            for candidate in existing:
                await asyncio.to_thread(self._emit_candidate_ready, request.search_id, candidate)
            existing_response: dict[str, object] = {
                "candidates": [item.model_dump(mode="json") for item in existing]
            }
            if claimed:
                await asyncio.to_thread(
                    self.app_store.complete_provider_call,
                    request_key, existing_response, owner_id=owner_id
                )
            return existing

        if not claimed:
            deadline = asyncio.get_running_loop().time() + PROVIDER_RESULT_WAIT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(PROVIDER_RESULT_POLL_SECONDS)
                claimed, status, completed_response = await asyncio.to_thread(
                    self.app_store.claim_provider_call,
                    request_key=request_key,
                    operation="generate_round",
                    search_id=request.search_id,
                    request_payload=audit_payload,
                    owner_id=owner_id,
                    lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
                )
                if status == "completed" and completed_response is not None:
                    return await asyncio.to_thread(
                        self._completed_candidates,
                        search_id=request.search_id,
                        completed_response=completed_response,
                    )
                existing = await asyncio.to_thread(
                    self.app_store.find_candidates_for_request,
                    request.search_id,
                    request_key,
                )
                if len(existing) == request.candidate_count:
                    existing_response = {
                        "candidates": [item.model_dump(mode="json") for item in existing]
                    }
                    if claimed:
                        await asyncio.to_thread(
                            self.app_store.complete_provider_call,
                            request_key, existing_response, owner_id=owner_id
                        )
                    return existing
                if claimed:
                    break
            else:
                raise RuntimeError("Timed out waiting for the in-flight provider call")

        heartbeat = asyncio.create_task(
            self._renew_provider_lease(request_key=request_key, owner_id=owner_id)
        )
        try:
            generated = await self.provider.generate_round(request)
            if len(generated) != request.candidate_count:
                raise RuntimeError(
                    "Image generator returned a different number of candidates than requested"
                )
            candidates: list[CandidateRecord] = []
            composite_mask = await asyncio.to_thread(
                self.composite_floor.create_mask,
                source_background=request.source_manifest.background,
                placement=request.placement,
            )
            for output in sorted(generated, key=lambda item: item.variant_index):
                raw_asset = await asyncio.to_thread(
                    self.asset_store.put_image_bytes, output.png_bytes
                )
                crop_mapping = self._crop_mapping_for_output(
                    source_manifest=request.source_manifest,
                    output_asset=raw_asset,
                )
                composite = await asyncio.to_thread(
                    self.composite_floor.protect_candidate,
                    source_manifest_hash=request.source_manifest.manifest_hash,
                    source_background=request.source_manifest.background,
                    raw_candidate=raw_asset,
                    placement=request.placement,
                    crop_mapping=crop_mapping,
                    mask=composite_mask,
                )
                candidate_seed = (
                    f"{request.search_id}:{request.round_index}:{output.variant_index}:"
                    f"{request_key}"
                )
                candidate_id = (
                    "cand_" + hashlib.sha256(candidate_seed.encode("utf-8")).hexdigest()[:32]
                )
                candidate = CandidateRecord(
                    candidate_id=candidate_id,
                    round_index=request.round_index,
                    variant_index=output.variant_index,
                    raw_asset=raw_asset,
                    protected_asset=composite.protected_asset,
                    source_manifest_hash=request.source_manifest.manifest_hash,
                    crop_mapping=crop_mapping,
                    composite=composite,
                    prompt_hash=request.prompt_hash,
                    request_key=request_key,
                    generation_depth=0,
                    model=request.model,
                    quality=request.quality,
                    size=request.size,
                )
                await asyncio.to_thread(
                    self.app_store.add_candidate, request.search_id, candidate
                )
                await asyncio.to_thread(
                    self._emit_candidate_ready, request.search_id, candidate
                )
                candidates.append(candidate)
            provider_request_ids = {
                item.provider_request_id
                for item in generated
                if item.provider_request_id is not None
            }
            provider_usage = next(
                (
                    _safe_provider_usage(item.provider_usage)
                    for item in generated
                    if item.provider_usage is not None
                ),
                {},
            )
            response: dict[str, object] = {
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "provider": {
                    "request_id": next(iter(provider_request_ids), None),
                    "usage": provider_usage,
                    "model": request.model,
                },
            }
            await asyncio.to_thread(
                self.app_store.complete_provider_call,
                request_key, response, owner_id=owner_id
            )
            return candidates
        except Exception as exc:
            await asyncio.to_thread(
                self.app_store.fail_provider_call,
                request_key, type(exc).__name__, owner_id=owner_id
            )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
