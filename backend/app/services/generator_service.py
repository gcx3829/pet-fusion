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
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.compositing import CropMapping, CropPadding, PixelBox
from app.domain.errors import SourceManifestMismatchError
from app.domain.prompts import (
    PromptGenerationMode,
    PromptRefinementMode,
    PromptVersion,
    VisualAnchorRef,
)
from app.domain.searches import PlacementIntent, SearchStatus
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
GENERATOR_ANCHOR_MAX_SIDE = 1024
GENERATOR_MODEL_MASK_X_PADDING = 0.045
GENERATOR_MODEL_MASK_Y_PADDING = 0.065
GENERATOR_BACKGROUND_PROXY_FORMAT = "png"
GENERATOR_OPAQUE_PROXY_FORMAT = "jpeg"
GENERATOR_OPAQUE_PROXY_QUALITY = 82
GENERATOR_ANCHOR_PROXY_VERSION = "selected-raw-anchor-proxy/v1"
GENERATOR_MULTI_CANDIDATE_STRATEGY = "relay-n-fallback-serial/v1"
PROVIDER_RESULT_POLL_SECONDS = 0.02
PROVIDER_RESULT_WAIT_SECONDS = 30.0
PROVIDER_CALL_LEASE_SECONDS = 5
GENERATOR_PROVIDER_MAX_ATTEMPTS = 2
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
    # Automatic Search remains source-only.  A selected raw candidate can be
    # supplied only through the explicit human-revision mode below.
    generation_mode: PromptGenerationMode = PromptGenerationMode.SOURCE_REBASE
    prompt_version_id: str | None = Field(default=None, pattern=r"^pv_[0-9a-f]{32}$")
    prompt_version_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parent_prompt_version_id: str | None = Field(
        default=None, pattern=r"^pv_[0-9a-f]{32}$"
    )
    # Full PromptVersion is optional for backwards-compatible callers.  The
    # paid service resolves and authorizes it from the persisted Prompt Refiner
    # result; this field is never written to provider audit rows.
    prompt_version: PromptVersion | None = None
    visual_anchor: VisualAnchorRef | None = None

    @model_validator(mode="before")
    @classmethod
    def hydrate_prompt_lineage_fields(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        prompt_version = payload.get("prompt_version")
        if isinstance(prompt_version, Mapping):
            prompt_version = PromptVersion.model_validate(prompt_version)
            payload["prompt_version"] = prompt_version
        if isinstance(prompt_version, PromptVersion):
            payload.setdefault("prompt_version_id", prompt_version.prompt_version_id)
            payload.setdefault("prompt_version_hash", prompt_version.prompt_version_hash)
            payload.setdefault(
                "parent_prompt_version_id", prompt_version.based_on_prompt_version_id
            )
        return payload

    @model_validator(mode="after")
    def validate_generation_lineage_contract(self) -> GenerationRequest:
        if self.generation_mode is PromptGenerationMode.SOURCE_REBASE:
            if self.visual_anchor is not None:
                raise ValueError("source_rebase generation cannot contain a visual anchor")
            if self.prompt_version is not None and (
                self.prompt_version.generation_mode
                is PromptGenerationMode.CANDIDATE_ANCHORED_REBASE
            ):
                raise ValueError("source_rebase generation cannot use an anchored PromptVersion")
            return self

        if self.visual_anchor is None:
            raise ValueError(
                "candidate_anchored_rebase generation requires a VisualAnchorRef"
            )
        if self.round_index < 1:
            raise ValueError(
                "candidate_anchored_rebase generation requires a later target round"
            )
        if self.prompt_version_id is None:
            raise ValueError(
                "candidate_anchored_rebase generation requires a PromptVersion lineage"
            )
        if self.prompt_version_hash is None:
            raise ValueError(
                "candidate_anchored_rebase generation requires a PromptVersion hash"
            )
        if self.parent_prompt_version_id is None:
            raise ValueError(
                "candidate_anchored_rebase generation requires parent PromptVersion lineage"
            )
        if self.prompt_version is not None:
            version = self.prompt_version
            if version.prompt_version_id != self.prompt_version_id:
                raise ValueError("GenerationRequest PromptVersion ID does not match its lineage")
            if (
                self.prompt_version_hash is not None
                and version.prompt_version_hash != self.prompt_version_hash
            ):
                raise ValueError(
                    "GenerationRequest PromptVersion hash does not match its lineage"
                )
            if version.refinement_mode is not PromptRefinementMode.REVISION:
                raise ValueError(
                    "candidate_anchored_rebase is only available for human revisions"
                )
            if version.generation_mode is not PromptGenerationMode.CANDIDATE_ANCHORED_REBASE:
                raise ValueError(
                    "candidate_anchored_rebase requires an anchored PromptVersion"
                )
            if version.visual_anchor != self.visual_anchor:
                raise ValueError("GenerationRequest visual anchor does not match PromptVersion")
            if version.human_selected_candidate_id != self.visual_anchor.candidate_id:
                raise ValueError(
                    "candidate_anchored_rebase must use the human-selected candidate"
                )
        return self


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    png_bytes: bytes
    variant_index: int
    provider_request_id: str | None = None
    provider_usage: Mapping[str, object] | None = None


class ImageGenerator(Protocol):
    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        request_key: str | None = None,
    ) -> list[GeneratedImage]: ...


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

    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        request_key: str | None = None,
    ) -> list[GeneratedImage]:
        del request_key
        self.call_count += 1
        self.requests.append(request)
        return await asyncio.to_thread(self._render_round, request)


class OpenAIImageGenerator:
    """GPT Image 2 edit provider using bounded, in-memory source proxies."""

    def __init__(self, *, transport: OpenAIImageEditsTransport) -> None:
        self.transport = transport

    @staticmethod
    def _asset_input(
        asset: AssetRef,
        *,
        index: int,
        role: str,
        max_side: int,
        force_png: bool = False,
    ) -> OpenAIImageInput:
        """Normalize one immutable/reference asset for the Image API call.

        The selected candidate is deliberately treated as a bounded visual
        reference, not as the editable base.  EXIF orientation is baked into
        the proxy, alpha is preserved only when it is meaningful, and opaque
        images use JPEG to keep relay payloads small.  The field on
        ``OpenAIImageInput`` remains named ``png_bytes`` for compatibility with
        the original transport adapter; ``mime_type`` is authoritative.
        """

        with Image.open(asset.filesystem_path) as opened:
            oriented = ImageOps.exif_transpose(opened)
            declares_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
            normalized = oriented.convert("RGBA" if declares_alpha else "RGB")
            has_transparency = declares_alpha and (
                normalized.getchannel("A").getextrema() != (255, 255)
            )
            if declares_alpha and not has_transparency:
                normalized = normalized.convert("RGB")
            longest_side = max(normalized.size)
            if longest_side > max_side:
                scale = max_side / longest_side
                normalized = normalized.resize(
                    (
                        max(1, round(normalized.width * scale)),
                        max(1, round(normalized.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            output = io.BytesIO()
            # Image 1 must stay PNG because the model mask is dimensionally
            # paired with it. Transparent visual references also stay PNG.
            if force_png or has_transparency:
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
        return OpenAIImageInput(
            filename=f"{index:02d}-{role}-{asset.asset_id}.{suffix}",
            png_bytes=output.getvalue(),
            mime_type=mime_type,
        )

    @classmethod
    def _source_inputs(cls, request: GenerationRequest) -> tuple[OpenAIImageInput, ...]:
        """Build the fixed Image API input order.

        ``image[0]`` is always the immutable original background.  Human
        candidate-anchored revision inserts the selected raw candidate at
        ``image[1]``; immutable cat references follow it.  Automatic/source
        rounds never include a candidate input.
        """

        inputs: list[OpenAIImageInput] = [
            cls._asset_input(
                request.source_manifest.background,
                index=0,
                role="background",
                max_side=GENERATOR_BACKGROUND_MAX_SIDE,
                force_png=True,
            )
        ]
        next_index = 1
        if request.generation_mode is PromptGenerationMode.CANDIDATE_ANCHORED_REBASE:
            # GenerationRequest validation guarantees the anchor is present;
            # keep the explicit guard for callers that bypass Pydantic.
            if request.visual_anchor is None:
                raise SourceManifestMismatchError(
                    "candidate_anchored_rebase generation is missing its visual anchor"
                )
            inputs.append(
                cls._asset_input(
                    request.visual_anchor.raw_asset,
                    index=next_index,
                    role="visual-anchor",
                    max_side=GENERATOR_ANCHOR_MAX_SIDE,
                )
            )
            next_index += 1
        for reference in request.source_manifest.cat_references:
            inputs.append(
                cls._asset_input(
                    reference,
                    index=next_index,
                    role="reference",
                    max_side=GENERATOR_REFERENCE_MAX_SIDE,
                )
            )
            next_index += 1
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

    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        request_key: str | None = None,
    ) -> list[GeneratedImage]:
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
            request_key=request_key,
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
    def _has_explicit_lineage(request: GenerationRequest) -> bool:
        return (
            request.generation_mode is not PromptGenerationMode.SOURCE_REBASE
            or request.prompt_version_id is not None
            or request.prompt_version_hash is not None
            or request.parent_prompt_version_id is not None
            or request.prompt_version is not None
            or request.visual_anchor is not None
        )

    @staticmethod
    def build_request_key(request: GenerationRequest) -> str:
        guidance_mask = request.guidance_mask
        visual_anchor = request.visual_anchor
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
        # Keep the old source-only key byte-for-byte stable for callers that
        # have not opted into the prompt-lineage contract.  Every explicit
        # lineage request (and every candidate-anchored request) includes the
        # mode, PromptVersion and anchor identity so a replay can never cross
        # manual revisions or visual references.
        has_explicit_lineage = GeneratorService._has_explicit_lineage(request)
        if has_explicit_lineage:
            payload["generation_mode"] = request.generation_mode.value
            payload["placement"] = request.placement.model_dump(mode="json")
            payload["prompt_version"] = {
                "prompt_version_id": request.prompt_version_id,
                "prompt_version_hash": request.prompt_version_hash,
                "parent_prompt_version_id": request.parent_prompt_version_id,
            }
            payload["anchor_proxy"] = (
                {
                    "schema_version": GENERATOR_ANCHOR_PROXY_VERSION,
                    "max_side": GENERATOR_ANCHOR_MAX_SIDE,
                    "format": "png_if_transparent_else_jpeg",
                    "opaque_format": GENERATOR_OPAQUE_PROXY_FORMAT,
                    "opaque_quality": GENERATOR_OPAQUE_PROXY_QUALITY,
                }
                if visual_anchor is not None
                else None
            )
        if visual_anchor is not None:
            payload["visual_anchor"] = {
                "schema_version": visual_anchor.schema_version,
                "kind": visual_anchor.kind,
                "search_id": visual_anchor.search_id,
                "candidate_id": visual_anchor.candidate_id,
                "round_index": visual_anchor.round_index,
                "source_manifest_hash": visual_anchor.source_manifest_hash,
                "raw_asset_id": visual_anchor.raw_asset.asset_id,
                "raw_asset_sha256": visual_anchor.raw_asset_sha256,
                "raw_asset_mime_type": visual_anchor.raw_asset.mime_type,
                "raw_asset_width": visual_anchor.raw_asset.width,
                "raw_asset_height": visual_anchor.raw_asset.height,
            }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _resolve_persisted_prompt_version(
        self,
        request: GenerationRequest,
        *,
        search_id: str,
        expected_source_manifest_hash: str,
    ) -> PromptVersion:
        """Resolve the exact Prompt Refiner result authorized for this round."""

        from app.services.prompt_refiner_service import PromptRefinerResult

        prompt_version_id = request.prompt_version_id
        if prompt_version_id is None:
            raise SourceManifestMismatchError(
                "Candidate-anchored generation requires a PromptVersion ID"
            )
        payload = self.app_store.find_prompt_refiner_result_by_prompt_version(
            search_id=search_id,
            prompt_version_id=prompt_version_id,
        )
        if payload is not None:
            try:
                result = PromptRefinerResult.model_validate(payload)
            except (TypeError, ValueError) as exc:
                raise SourceManifestMismatchError(
                    "Persisted Prompt Refiner result is invalid"
                ) from exc
            version = result.prompt_version
        else:
            # Automatic source-only rounds apply bounded directives locally and
            # persist that PromptVersion in Search.prompt_history rather than
            # pretending a non-paid local operation was a provider result.
            search_record = self.app_store.get_search(search_id)
            history_matches = [
                item
                for item in search_record.prompt_history
                if item.prompt_version_id == prompt_version_id
            ]
            if len(history_matches) != 1:
                raise SourceManifestMismatchError(
                    "PromptVersion is not persisted for this search"
                )
            version = history_matches[0]
        if version.prompt_version_id != prompt_version_id:
            raise SourceManifestMismatchError(
                "PromptVersion ID does not match its persisted result"
            )
        if request.prompt_version_hash is not None and (
            version.prompt_version_hash != request.prompt_version_hash
        ):
            raise SourceManifestMismatchError(
                "PromptVersion hash does not match its persisted result"
            )
        if request.prompt_version is not None and (
            request.prompt_version.model_dump(mode="json")
            != version.model_dump(mode="json")
        ):
            raise SourceManifestMismatchError(
                "PromptVersion lineage does not match its persisted result"
            )
        if (
            version.search_id != search_id
            or version.source_manifest_hash != expected_source_manifest_hash
            or version.round_index != request.round_index
        ):
            raise SourceManifestMismatchError(
                "PromptVersion does not belong to this search's target generation round"
            )
        if version.generation_prompt_hash != request.prompt_hash:
            raise SourceManifestMismatchError(
                "Generation prompt hash does not match the authorized PromptVersion"
            )
        if version.generation_prompt != request.prompt:
            raise SourceManifestMismatchError(
                "Generation prompt text does not match the authorized PromptVersion"
            )
        if request.parent_prompt_version_id is not None and (
            version.based_on_prompt_version_id != request.parent_prompt_version_id
        ):
            raise SourceManifestMismatchError(
                "PromptVersion parent lineage does not match the GenerationRequest"
            )
        return version

    def _assert_candidate_anchor_lineage(
        self,
        request: GenerationRequest,
        *,
        search_id: str,
        source_manifest_hash: str,
    ) -> PromptVersion:
        """Authorize a human-selected raw candidate immediately before payment."""

        from app.services.prompt_refiner_service import PromptRefinerResult

        anchor = request.visual_anchor
        if anchor is None:
            raise SourceManifestMismatchError(
                "Candidate-anchored generation requires a VisualAnchorRef"
            )
        if (
            anchor.search_id != search_id
            or anchor.source_manifest_hash != source_manifest_hash
            or anchor.round_index != request.round_index - 1
        ):
            raise SourceManifestMismatchError(
                "Visual anchor must belong to the same search and immediately previous round"
            )
        version = self._resolve_persisted_prompt_version(
            request,
            search_id=search_id,
            expected_source_manifest_hash=source_manifest_hash,
        )
        if (
            version.refinement_mode is not PromptRefinementMode.REVISION
            or version.generation_mode is not PromptGenerationMode.CANDIDATE_ANCHORED_REBASE
            or version.visual_anchor != anchor
            or version.human_selected_candidate_id != anchor.candidate_id
            or version.based_on_prompt_version_id is None
        ):
            raise SourceManifestMismatchError(
                "Candidate anchor is only valid for a persisted human revision PromptVersion"
            )

        parent_payload = self.app_store.find_prompt_refiner_result_by_prompt_version(
            search_id=search_id,
            prompt_version_id=version.based_on_prompt_version_id,
        )
        if parent_payload is not None:
            try:
                parent = PromptRefinerResult.model_validate(parent_payload).prompt_version
            except (TypeError, ValueError) as exc:
                raise SourceManifestMismatchError(
                    "Persisted parent PromptVersion is invalid"
                ) from exc
        else:
            search_record = self.app_store.get_search(search_id)
            parent_matches = [
                item
                for item in search_record.prompt_history
                if item.prompt_version_id == version.based_on_prompt_version_id
            ]
            if len(parent_matches) != 1:
                raise SourceManifestMismatchError(
                    "PromptVersion parent is not persisted for this search"
                )
            parent = parent_matches[0]
        if (
            parent.search_id != search_id
            or parent.source_manifest_hash != source_manifest_hash
            or parent.round_index != request.round_index - 1
            or version.based_on_prompt_version_id != parent.prompt_version_id
        ):
            raise SourceManifestMismatchError(
                "PromptVersion parent does not belong to the previous source round"
            )

        search = self.app_store.get_search(search_id)
        if (
            search.status not in {SearchStatus.QUEUED, SearchStatus.RUNNING}
            or search.round_index != request.round_index
        ):
            raise SourceManifestMismatchError(
                "Candidate-anchored generation does not match an active search target round"
            )
        reviewed_round = request.round_index - 1
        review_entries = [
            item
            for item in search.round_history
            if isinstance(item, Mapping) and item.get("round_index") == reviewed_round
        ]
        if len(review_entries) != 1:
            raise SourceManifestMismatchError(
                "Candidate-anchored generation is missing its persisted human resume"
            )
        review_entry = review_entries[0]
        persisted_feedback = review_entry.get("human_feedback")
        normalized_persisted_feedback = (
            persisted_feedback.strip()
            if isinstance(persisted_feedback, str) and persisted_feedback.strip()
            else ""
        )
        if (
            review_entry.get("human_resume_applied") is not True
            or review_entry.get("human_selected_candidate_id") != anchor.candidate_id
            or normalized_persisted_feedback != (version.human_feedback or "").strip()
        ):
            raise SourceManifestMismatchError(
                "Candidate anchor does not match the persisted human selection and feedback"
            )
        matching = [
            candidate
            for candidate in search.candidates
            if candidate.candidate_id == anchor.candidate_id
        ]
        if len(matching) != 1:
            raise SourceManifestMismatchError(
                "Visual anchor candidate is not persisted in the requested search"
            )
        candidate = matching[0]
        if (
            candidate.round_index != anchor.round_index
            or candidate.source_manifest_hash != source_manifest_hash
            or candidate.generation_depth != 0
            or candidate.raw_authoritative_asset != anchor.raw_asset
            or candidate.raw_asset.sha256 != anchor.raw_asset_sha256
            or candidate.prompt_hash != parent.generation_prompt_hash
        ):
            raise SourceManifestMismatchError(
                "Visual anchor is not the intact raw authority generated from its parent prompt"
            )
        try:
            registered = self.app_store.get_asset(candidate.raw_asset.asset_id)
        except Exception as exc:
            raise SourceManifestMismatchError(
                "Visual anchor raw asset is not registered in app storage"
            ) from exc
        if registered != candidate.raw_asset:
            raise SourceManifestMismatchError(
                "Visual anchor raw asset differs from the canonical app asset"
            )
        self.asset_store.assert_png_lineage_asset(candidate.raw_asset)
        return version

    def _assert_rebase_inputs(
        self, request: GenerationRequest, *, expected_manifest_hash: str
    ) -> PromptVersion | None:
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
            try:
                if self.app_store.get_asset(asset.asset_id) != asset:
                    raise SourceManifestMismatchError(
                        "Generator source asset differs from the canonical app asset"
                    )
            except SourceManifestMismatchError:
                raise
            except Exception as exc:
                raise SourceManifestMismatchError(
                    "Generator source asset is not registered in app storage"
                ) from exc
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
            try:
                if self.app_store.get_asset(guidance.asset_id) != guidance:
                    raise SourceManifestMismatchError(
                        "Generator Guidance Mask differs from the canonical app asset"
                    )
            except SourceManifestMismatchError:
                raise
            except Exception as exc:
                raise SourceManifestMismatchError(
                    "Generator Guidance Mask is not registered in app storage"
                ) from exc

        if request.generation_mode is PromptGenerationMode.SOURCE_REBASE:
            if request.visual_anchor is not None:
                raise SourceManifestMismatchError(
                    "Source rebase generation cannot read a candidate visual anchor"
                )
            if request.prompt_version_id is not None:
                version = self._resolve_persisted_prompt_version(
                    request,
                    search_id=request.search_id,
                    expected_source_manifest_hash=search.source_manifest_hash,
                )
                if version.generation_mode is not PromptGenerationMode.SOURCE_REBASE:
                    raise SourceManifestMismatchError(
                        "Automatic source rebase cannot use an anchored PromptVersion"
                    )
                return version
            return None

        return self._assert_candidate_anchor_lineage(
            request,
            search_id=request.search_id,
            source_manifest_hash=search.source_manifest_hash,
        )

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

    def _assert_completed_audit_lineage(
        self,
        *,
        request: GenerationRequest,
        request_key: str,
        audit_payload: Mapping[str, object],
    ) -> None:
        """Re-authorize a completed replay before returning cached candidates.

        A checkpoint resume must not trust the fact that a request key exists:
        the stored redacted request is compared with the freshly reconstructed
        lineage, and every cached candidate is checked against the same source,
        prompt and raw-only generation contract.  The audit still contains no
        prompt text, image bytes, or Base64.
        """

        record = self.app_store.get_provider_call_record(request_key)
        if record is None or record.get("status") != "completed":
            raise SourceManifestMismatchError(
                "Completed generator replay is missing its provider audit"
            )
        if (
            record.get("operation") != "generate_round"
            or record.get("search_id") != request.search_id
            or record.get("request") != dict(audit_payload)
        ):
            raise SourceManifestMismatchError(
                "Completed generator replay audit does not match request lineage"
            )
        response = record.get("response")
        if not isinstance(response, Mapping):
            raise SourceManifestMismatchError(
                "Completed generator replay has no structured provider response"
            )
        raw_candidates = response.get("candidates")
        if not isinstance(raw_candidates, list):
            raise SourceManifestMismatchError(
                "Completed generator replay provider response is malformed"
            )
        try:
            candidates = [CandidateRecord.model_validate(item) for item in raw_candidates]
        except (TypeError, ValueError) as exc:
            raise SourceManifestMismatchError(
                "Completed generator replay contains an invalid candidate"
            ) from exc
        self._assert_replayed_candidates_lineage(
            request=request,
            request_key=request_key,
            candidates=candidates,
        )

    def _assert_replayed_candidates_lineage(
        self,
        *,
        request: GenerationRequest,
        request_key: str,
        candidates: list[CandidateRecord],
    ) -> None:
        """Validate every cached output before replaying or closing an audit."""

        if len(candidates) != request.candidate_count:
            raise SourceManifestMismatchError(
                "Generator replay candidate count does not match request"
            )
        if sorted(item.variant_index for item in candidates) != list(
            range(request.candidate_count)
        ):
            raise SourceManifestMismatchError(
                "Generator replay candidate variants are incomplete or duplicated"
            )
        for candidate in candidates:
            candidate_seed = (
                f"{request.search_id}:{request.round_index}:{candidate.variant_index}:"
                f"{request_key}"
            )
            expected_candidate_id = (
                "cand_" + hashlib.sha256(candidate_seed.encode("utf-8")).hexdigest()[:32]
            )
            if (
                candidate.candidate_id != expected_candidate_id
                or candidate.request_key != request_key
                or candidate.round_index != request.round_index
                or candidate.source_manifest_hash != request.source_manifest.manifest_hash
                or candidate.prompt_hash != request.prompt_hash
                or candidate.generation_depth != 0
                or candidate.protected_asset != candidate.raw_asset
                or candidate.composite is not None
                or candidate.model != request.model
                or candidate.quality != request.quality
                or candidate.size != request.size
                or candidate.crop_mapping
                != self._crop_mapping_for_output(
                    source_manifest=request.source_manifest,
                    output_asset=candidate.raw_asset,
                )
            ):
                raise SourceManifestMismatchError(
                    "Generator replay candidate lineage does not match request"
                )
            try:
                if self.app_store.get_asset(candidate.raw_asset.asset_id) != candidate.raw_asset:
                    raise SourceManifestMismatchError(
                        "Generator replay raw asset differs from the canonical app asset"
                    )
            except SourceManifestMismatchError:
                raise
            except Exception as exc:
                raise SourceManifestMismatchError(
                    "Generator replay raw asset is not registered in app storage"
                ) from exc
            self.asset_store.assert_png_lineage_asset(candidate.raw_asset)

    @staticmethod
    def _anchor_proxy_audit_metadata(anchor: VisualAnchorRef | None) -> dict[str, object] | None:
        """Describe the exact bounded anchor proxy without persisting image data."""

        if anchor is None:
            return None
        try:
            with Image.open(anchor.raw_asset.filesystem_path) as opened:
                oriented = ImageOps.exif_transpose(opened)
                declares_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                normalized = oriented.convert("RGBA" if declares_alpha else "RGB")
                has_transparency = declares_alpha and (
                    normalized.getchannel("A").getextrema() != (255, 255)
                )
                width, height = normalized.size
                if max(width, height) > GENERATOR_ANCHOR_MAX_SIDE:
                    scale = GENERATOR_ANCHOR_MAX_SIDE / max(width, height)
                    width = max(1, round(width * scale))
                    height = max(1, round(height * scale))
        except (OSError, ValueError) as exc:
            raise SourceManifestMismatchError(
                "Visual anchor raw asset cannot be inspected for its proxy contract"
            ) from exc
        return {
            "schema_version": GENERATOR_ANCHOR_PROXY_VERSION,
            "max_side": GENERATOR_ANCHOR_MAX_SIDE,
            "width": width,
            "height": height,
            "format": "png" if has_transparency else GENERATOR_OPAQUE_PROXY_FORMAT,
            "mime_type": "image/png" if has_transparency else "image/jpeg",
            "opaque_quality": GENERATOR_OPAQUE_PROXY_QUALITY,
            "exif_orientation": "transpose",
        }

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
        authorized_prompt_version = await asyncio.to_thread(
            self._assert_rebase_inputs,
            request,
            expected_manifest_hash=expected_manifest_hash,
        )
        request_key = self.build_request_key(request)
        prompt_version = authorized_prompt_version or request.prompt_version
        anchor = request.visual_anchor
        anchor_proxy_metadata = await asyncio.to_thread(
            self._anchor_proxy_audit_metadata,
            anchor,
        )
        audit_payload: dict[str, object] = {
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
        if self._has_explicit_lineage(request):
            audit_payload.update(
                {
                    "generation_mode": request.generation_mode.value,
                    "placement": request.placement.model_dump(mode="json"),
                    "prompt_version": (
                        {
                            "prompt_version_id": prompt_version.prompt_version_id,
                            "prompt_version_hash": prompt_version.prompt_version_hash,
                            "round_index": prompt_version.round_index,
                            "based_on_prompt_version_id": (
                                prompt_version.based_on_prompt_version_id
                            ),
                        }
                        if prompt_version is not None
                        else {
                            "prompt_version_id": request.prompt_version_id,
                            "prompt_version_hash": request.prompt_version_hash,
                            "round_index": None,
                            "based_on_prompt_version_id": request.parent_prompt_version_id,
                        }
                    ),
                    "visual_anchor": (
                        {
                            "schema_version": anchor.schema_version,
                            "kind": anchor.kind,
                            "search_id": anchor.search_id,
                            "candidate_id": anchor.candidate_id,
                            "round_index": anchor.round_index,
                            "source_manifest_hash": anchor.source_manifest_hash,
                            "raw_asset_id": anchor.raw_asset.asset_id,
                            "raw_asset_sha256": anchor.raw_asset_sha256,
                            "raw_asset_mime_type": anchor.raw_asset.mime_type,
                            "raw_asset_width": anchor.raw_asset.width,
                            "raw_asset_height": anchor.raw_asset.height,
                        }
                        if anchor is not None
                        else None
                    ),
                    "anchor_proxy": anchor_proxy_metadata,
                }
            )
        if anchor is not None:
            input_proxy = audit_payload["input_proxy"]
            assert isinstance(input_proxy, dict)
            input_proxy.update(
                {
                    "anchor_proxy_version": GENERATOR_ANCHOR_PROXY_VERSION,
                    "anchor_max_side": GENERATOR_ANCHOR_MAX_SIDE,
                    "anchor_format": "png_if_transparent_else_jpeg",
                    "anchor_opaque_format": GENERATOR_OPAQUE_PROXY_FORMAT,
                    "anchor_opaque_quality": GENERATOR_OPAQUE_PROXY_QUALITY,
                }
            )
        owner_id = f"provider_{uuid4().hex}"
        claimed, status, completed_response = await asyncio.to_thread(
            self.app_store.claim_provider_call,
            request_key=request_key,
            operation="generate_round",
            search_id=request.search_id,
            request_payload=audit_payload,
            owner_id=owner_id,
            lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
            max_attempts=GENERATOR_PROVIDER_MAX_ATTEMPTS,
        )
        if status == "completed" and completed_response is not None:
            self._assert_completed_audit_lineage(
                request=request,
                request_key=request_key,
                audit_payload=audit_payload,
            )
            return await asyncio.to_thread(
                self._completed_candidates,
                search_id=request.search_id, completed_response=completed_response
            )

        existing = await asyncio.to_thread(
            self.app_store.find_candidates_for_request, request.search_id, request_key
        )
        if len(existing) == request.candidate_count:
            self._assert_replayed_candidates_lineage(
                request=request,
                request_key=request_key,
                candidates=existing,
            )
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
            attempts = await asyncio.to_thread(
                self.app_store.provider_attempt_count, request_key
            )
            if attempts >= GENERATOR_PROVIDER_MAX_ATTEMPTS:
                raise RuntimeError("Image generator provider attempts exhausted")
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
                    max_attempts=GENERATOR_PROVIDER_MAX_ATTEMPTS,
                )
                if status == "completed" and completed_response is not None:
                    self._assert_completed_audit_lineage(
                        request=request,
                        request_key=request_key,
                        audit_payload=audit_payload,
                    )
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
                    self._assert_replayed_candidates_lineage(
                        request=request,
                        request_key=request_key,
                        candidates=existing,
                    )
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
                attempts = await asyncio.to_thread(
                    self.app_store.provider_attempt_count, request_key
                )
                if attempts >= GENERATOR_PROVIDER_MAX_ATTEMPTS:
                    raise RuntimeError("Image generator provider attempts exhausted")
            else:
                raise RuntimeError("Timed out waiting for the in-flight provider call")

        heartbeat = asyncio.create_task(
            self._renew_provider_lease(request_key=request_key, owner_id=owner_id)
        )
        try:
            # The first lineage check happens before claiming the idempotency
            # key.  Re-check the active round after any wait and immediately
            # before the paid side effect so a cancel/accept race cannot start
            # a fresh provider request.
            active_search = await asyncio.to_thread(
                self.app_store.get_search, request.search_id
            )
            if (
                active_search.status
                not in {SearchStatus.QUEUED, SearchStatus.RUNNING}
                or active_search.round_index != request.round_index
            ):
                raise SourceManifestMismatchError(
                    "Image generation no longer matches an active search target round"
                )
            await asyncio.to_thread(
                self._assert_rebase_inputs,
                request,
                expected_manifest_hash=expected_manifest_hash,
            )
            generated = await self.provider.generate_round(
                request,
                request_key=request_key,
            )
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
            completed = await asyncio.to_thread(
                self.app_store.complete_provider_call,
                request_key, response, owner_id=owner_id
            )
            if not completed:
                raise RuntimeError(
                    "Image generator paid result could not close its provider audit"
                )
            return candidates
        except Exception as exc:
            await asyncio.to_thread(
                self.app_store.fail_provider_call,
                request_key,
                type(exc).__name__,
                owner_id=owner_id,
                retryable=not isinstance(
                    exc,
                    (SourceManifestMismatchError, TypeError, ValueError),
                ),
            )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
