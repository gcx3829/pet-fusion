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

from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.compositing import CropMapping, CropPadding, PixelBox
from app.domain.errors import SourceManifestMismatchError
from app.domain.searches import PlacementIntent
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import normalized_placement_to_pixel_box
from app.services.openai_image_client import (
    OpenAIImageEditResult,
    OpenAIImageEditsTransport,
    OpenAIImageInput,
)

FAKE_IMAGE_MODEL = "fake-gpt-image-2"
GENERATOR_SCHEMA_VERSION = "generator-request/v1"
GENERATOR_OUTPUT_SEMANTICS_VERSION = "raw-authority/v1"
GENERATOR_INPUT_PROXY_VERSION = "generator-input-proxy/v2"
GENERATOR_MASK_VERSION = "provider-mask/v1"
GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION = "guidance-mask-user-alpha-editable/v1"
GENERATOR_GUIDANCE_MASK_RESIZE_VERSION = "guidance-mask-resize-lanczos/v1"
GENERATOR_BACKGROUND_MAX_SIDE = 1024
GENERATOR_REFERENCE_MAX_SIDE = 768
GENERATOR_MODEL_MASK_X_PADDING = 0.045
GENERATOR_MODEL_MASK_Y_PADDING = 0.065
GENERATOR_BACKGROUND_PROXY_FORMAT = "png"
GENERATOR_OPAQUE_PROXY_FORMAT = "jpeg"
GENERATOR_OPAQUE_PROXY_QUALITY = 82
GENERATOR_MULTI_CANDIDATE_STRATEGY = "relay-n-fallback-serial/v1"
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
    # This is a reference to the immutable, project-bound user mask. The
    # raster is deliberately not part of the request/checkpoint state.
    guidance_mask: AssetRef | None = None
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


def _provider_alpha_from_user_alpha(user_alpha: Image.Image) -> Image.Image:
    """Convert the UI mask convention to the OpenAI edit-mask convention.

    The browser-authored mask uses alpha=0 for "keep the original" and
    alpha=255 for "allow editing". OpenAI's image-edit mask reverses that
    meaning: transparent pixels are editable and opaque pixels are preserved.
    Keep this conversion as a tiny pure operation so the 0/128/255 contract is
    easy to test and remains independent of the provider transport.
    """

    if user_alpha.mode != "L":
        user_alpha = user_alpha.convert("L")
    return ImageOps.invert(user_alpha)


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
    """GPT Image 2 edit provider using bounded, in-memory source proxies."""

    def __init__(self, *, transport: OpenAIImageEditsTransport) -> None:
        self.transport = transport

    @staticmethod
    def _source_inputs(request: GenerationRequest) -> tuple[OpenAIImageInput, ...]:
        assets = (request.source_manifest.background, *request.source_manifest.cat_references)
        inputs: list[OpenAIImageInput] = []
        for index, asset in enumerate(assets):
            with Image.open(asset.filesystem_path) as opened:
                oriented = ImageOps.exif_transpose(opened)
                declares_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                normalized = oriented.convert("RGBA" if declares_alpha else "RGB")
                has_transparency = declares_alpha and (
                    normalized.getchannel("A").getextrema() != (255, 255)
                )
                if declares_alpha and not has_transparency:
                    normalized = normalized.convert("RGB")
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
                # The provider mask must have the same dimensions and a
                # compatible raster format as the first image. Keep the
                # background as PNG even when it is opaque; references can
                # still use compact JPEG proxies when they are opaque.
                if has_transparency or index == 0:
                    normalized.save(output, format="PNG", compress_level=9, optimize=False)
                    mime_type = "image/png"
                    suffix = "png"
                else:
                    normalized.save(
                        output,
                        format="JPEG",
                        quality=GENERATOR_OPAQUE_PROXY_QUALITY,
                        optimize=True,
                        progressive=True,
                        subsampling=2,
                    )
                    mime_type = "image/jpeg"
                    suffix = "jpg"
            input_bytes = output.getvalue()
            role = "background" if index == 0 else f"reference-{index}"
            inputs.append(
                OpenAIImageInput(
                    filename=f"{index:02d}-{role}-{asset.asset_id}.{suffix}",
                    png_bytes=input_bytes,
                    mime_type=mime_type,
                )
            )
        return tuple(inputs)

    @staticmethod
    def _model_mask_placement(placement: PlacementIntent) -> PlacementIntent:
        """Expand the provider edit window enough for contact and edge blending."""

        left = max(0.0, placement.x - GENERATOR_MODEL_MASK_X_PADDING)
        top = max(0.0, placement.y - GENERATOR_MODEL_MASK_Y_PADDING)
        right = min(1.0, placement.x + placement.width + GENERATOR_MODEL_MASK_X_PADDING)
        bottom = min(1.0, placement.y + placement.height + GENERATOR_MODEL_MASK_Y_PADDING)
        return placement.model_copy(
            update={
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            }
        )

    @staticmethod
    def _guidance_provider_alpha(
        guidance_mask: AssetRef,
        *,
        source_size: tuple[int, int],
        proxy_size: tuple[int, int],
    ) -> Image.Image:
        """Load and resize a user Guidance Mask into provider alpha semantics.

        ``guidance_mask`` is stored at the immutable source-background
        resolution. The provider receives a bounded background proxy, so the
        alpha plane must be resized with a high-quality filter before its
        semantics are inverted. We intentionally do not threshold the result:
        soft brush edges and partial flow values must survive the resize.
        """

        if guidance_mask.mime_type != "image/png":
            raise SourceManifestMismatchError(
                "Search Guidance Mask must be a canonical PNG asset"
            )
        if (guidance_mask.width, guidance_mask.height) != source_size:
            raise SourceManifestMismatchError(
                "Search Guidance Mask dimensions do not match the source background"
            )
        try:
            with Image.open(guidance_mask.filesystem_path) as opened:
                has_alpha = "A" in opened.getbands() or "transparency" in opened.info
                if not has_alpha:
                    raise SourceManifestMismatchError(
                        "Search Guidance Mask does not contain an alpha channel"
                    )
                user_alpha = opened.convert("RGBA").getchannel("A").copy()
        except SourceManifestMismatchError:
            raise
        except (Image.DecompressionBombError, OSError, ValueError) as exc:
            raise SourceManifestMismatchError(
                "Search Guidance Mask is not a readable PNG asset"
            ) from exc

        if user_alpha.size != source_size:
            # The metadata check above catches a tampered AssetRef, while this
            # second check catches bytes that no longer match those metadata.
            raise SourceManifestMismatchError(
                "Search Guidance Mask bytes do not match its recorded dimensions"
            )
        if user_alpha.size != proxy_size:
            user_alpha = user_alpha.resize(proxy_size, Image.Resampling.LANCZOS)
        return _provider_alpha_from_user_alpha(user_alpha)

    @classmethod
    def _provider_mask(
        cls,
        request: GenerationRequest,
        background_input: OpenAIImageInput,
    ) -> OpenAIImageInput:
        """Build the same-size RGBA mask expected by the image edit endpoint.

        The provider mask uses the inverse alpha convention: transparent pixels
        are the edit region and opaque pixels are preserved. With no authored
        Guidance Mask, the placement window is expanded to give the model room
        to blend contact and edge context. With an authored mask, its soft alpha
        plane is used exactly (after proxy resizing and semantic inversion).
        No local floor is applied during Search; Fusion/Export own any later
        pixel compositing.
        """

        with Image.open(io.BytesIO(background_input.png_bytes)) as opened:
            width, height = opened.size
        if request.guidance_mask is not None:
            provider_alpha = cls._guidance_provider_alpha(
                request.guidance_mask,
                source_size=(
                    request.source_manifest.background.width,
                    request.source_manifest.background.height,
                ),
                proxy_size=(width, height),
            )
        else:
            placement = cls._model_mask_placement(request.placement)
            editable_box = normalized_placement_to_pixel_box(
                placement,
                width=width,
                height=height,
            )
            editable = Image.new("L", (width, height), 0)
            ImageDraw.Draw(editable).rectangle(
                (
                    editable_box.x,
                    editable_box.y,
                    editable_box.right - 1,
                    editable_box.bottom - 1,
                ),
                fill=255,
            )
            provider_alpha = ImageOps.invert(editable)
        mask = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        mask.putalpha(provider_alpha)
        output = io.BytesIO()
        mask.save(output, format="PNG", compress_level=9, optimize=False)
        return OpenAIImageInput(
            filename="00-provider-mask.png",
            png_bytes=output.getvalue(),
            mime_type="image/png",
        )

    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]:
        source_inputs = await asyncio.to_thread(self._source_inputs, request)
        provider_mask = await asyncio.to_thread(
            self._provider_mask,
            request,
            source_inputs[0],
        )
        result: OpenAIImageEditResult = await self.transport.edit(
            model=request.model,
            prompt=request.prompt,
            images=source_inputs,
            mask=provider_mask,
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
    ) -> None:
        self.provider = provider
        self.asset_store = asset_store
        self.app_store = app_store
        self.model = model
        self.quality = quality
        self.size = size

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
        guidance_mask = request.guidance_mask
        payload = {
            "schema_version": GENERATOR_SCHEMA_VERSION,
            "output_semantics_version": GENERATOR_OUTPUT_SEMANTICS_VERSION,
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
            "provider_mask_version": GENERATOR_MASK_VERSION,
            "background_max_side": GENERATOR_BACKGROUND_MAX_SIDE,
            "reference_max_side": GENERATOR_REFERENCE_MAX_SIDE,
            "background_format": GENERATOR_BACKGROUND_PROXY_FORMAT,
            "model_mask_x_padding": GENERATOR_MODEL_MASK_X_PADDING,
            "model_mask_y_padding": GENERATOR_MODEL_MASK_Y_PADDING,
            "opaque_format": GENERATOR_OPAQUE_PROXY_FORMAT,
            "opaque_quality": GENERATOR_OPAQUE_PROXY_QUALITY,
            "transparent_format": "png",
            "multi_candidate_strategy": GENERATOR_MULTI_CANDIDATE_STRATEGY,
        }
        # Keep the legacy placement-only request key byte-for-byte stable. A
        # Guidance Mask introduces an explicit content/transform contract and
        # therefore receives a distinct key namespace only when supplied.
        if guidance_mask is not None:
            payload["guidance_mask"] = {
                "asset_id": guidance_mask.asset_id,
                "sha256": guidance_mask.sha256,
                "semantics_version": GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION,
                "resize_version": GENERATOR_GUIDANCE_MASK_RESIZE_VERSION,
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
        stored_guidance_mask = search.guidance_mask_asset
        if (request.guidance_mask is None) != (stored_guidance_mask is None):
            raise SourceManifestMismatchError(
                "Generator Guidance Mask differs from the immutable Search record"
            )
        if request.guidance_mask is not None:
            if stored_guidance_mask != request.guidance_mask:
                raise SourceManifestMismatchError(
                    "Generator Guidance Mask differs from the immutable Search record"
                )
            guidance = request.guidance_mask
            if guidance.mime_type != "image/png" or (
                guidance.width,
                guidance.height,
            ) != (
                manifest.background.width,
                manifest.background.height,
            ):
                raise SourceManifestMismatchError(
                    "Generator Guidance Mask does not match the source background"
                )
            self.asset_store.assert_intact(guidance)

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
            "output_semantics_version": GENERATOR_OUTPUT_SEMANTICS_VERSION,
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
            "guidance_mask": (
                {
                    "asset_id": request.guidance_mask.asset_id,
                    "sha256": request.guidance_mask.sha256,
                    "semantics_version": GENERATOR_GUIDANCE_MASK_SEMANTICS_VERSION,
                    "resize_version": GENERATOR_GUIDANCE_MASK_RESIZE_VERSION,
                }
                if request.guidance_mask is not None
                else None
            ),
            "input_proxy": {
                "schema_version": GENERATOR_INPUT_PROXY_VERSION,
                "output_semantics_version": GENERATOR_OUTPUT_SEMANTICS_VERSION,
                "provider_mask_version": GENERATOR_MASK_VERSION,
                "background_max_side": GENERATOR_BACKGROUND_MAX_SIDE,
                "reference_max_side": GENERATOR_REFERENCE_MAX_SIDE,
                "background_format": GENERATOR_BACKGROUND_PROXY_FORMAT,
                "model_mask_x_padding": GENERATOR_MODEL_MASK_X_PADDING,
                "model_mask_y_padding": GENERATOR_MODEL_MASK_Y_PADDING,
                "opaque_format": GENERATOR_OPAQUE_PROXY_FORMAT,
                "opaque_quality": GENERATOR_OPAQUE_PROXY_QUALITY,
                "transparent_format": "png",
                "multi_candidate_strategy": GENERATOR_MULTI_CANDIDATE_STRATEGY,
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
            for output in sorted(generated, key=lambda item: item.variant_index):
                raw_asset = await asyncio.to_thread(
                    self.asset_store.put_image_bytes, output.png_bytes
                )
                crop_mapping = self._crop_mapping_for_output(
                    source_manifest=request.source_manifest,
                    output_asset=raw_asset,
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
                    # ``protected_asset`` is retained as a legacy alias only.
                    # Search never floors or rewrites the raw provider output.
                    protected_asset=raw_asset,
                    source_manifest_hash=request.source_manifest.manifest_hash,
                    crop_mapping=crop_mapping,
                    composite=None,
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
