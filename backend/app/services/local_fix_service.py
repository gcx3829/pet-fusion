"""Bounded candidate-to-candidate image edits kept outside automatic Search."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import AssetRef
from app.domain.candidates import CandidateRecord
from app.domain.compositing import Mask
from app.domain.errors import ConflictError
from app.domain.local_fixes import (
    LocalFixOutcome,
    LocalFixRequest,
    LocalFixResolution,
    LocalFixResult,
)
from app.domain.searches import SearchStatus
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.image_pipeline import CompositeFloorService

FAKE_LOCAL_FIX_MODEL = "fake-gpt-image-2-local-fix"
LOCAL_FIX_SCHEMA_VERSION = "local-fix/v1"
LOCAL_FIX_PROVIDER_LEASE_SECONDS = 5
LOCAL_FIX_PROVIDER_WAIT_SECONDS = 30.0
LOCAL_FIX_PROVIDER_POLL_SECONDS = 0.02


class LocalFixProviderRequest(BaseModel):
    """Provider request exposing only the visible protected base and tight mask."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = LOCAL_FIX_SCHEMA_VERSION
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_candidate_id: str = Field(min_length=1, max_length=120)
    base_protected_asset: AssetRef
    tight_mask: Mask
    instruction: str = Field(min_length=1, max_length=240)
    instruction_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_generation_depth: int = Field(ge=1, le=2)
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LocalFixGeneratedImage:
    png_bytes: bytes
    provider_request_id: str | None = None


class LocalFixProvider(Protocol):
    """An edit provider intentionally separate from immutable-source generation."""

    async def apply_local_fix(self, request: LocalFixProviderRequest) -> LocalFixGeneratedImage: ...


class DeterministicFakeLocalFixProvider:
    """Offline fixture provider that visibly changes pixels only inside the tight mask."""

    model = FAKE_LOCAL_FIX_MODEL

    def __init__(self) -> None:
        self.call_count = 0
        self.requests: list[LocalFixProviderRequest] = []

    @staticmethod
    def _render(request: LocalFixProviderRequest) -> LocalFixGeneratedImage:
        with Image.open(request.base_protected_asset.filesystem_path) as opened:
            base = opened.convert("RGBA")
        with Image.open(request.tight_mask.asset.filesystem_path) as opened:
            mask = opened.convert("L")
        if mask.size != base.size:
            raise ValueError("Local Fix mask dimensions do not match the base candidate")
        seed = hashlib.sha256(request.instruction_hash.encode("utf-8")).digest()
        overlay = Image.new("RGBA", base.size, (seed[0], seed[1], seed[2], 235))
        # The fake provider deliberately obeys the tight mask; the service still
        # applies two local composite floors so an untrusted live provider cannot
        # change either the base candidate outside the edit or the source background.
        rendered = Image.composite(overlay, base, mask).convert("RGBA")
        output = io.BytesIO()
        rendered.save(output, format="PNG", compress_level=9, optimize=False)
        return LocalFixGeneratedImage(png_bytes=output.getvalue())

    async def apply_local_fix(self, request: LocalFixProviderRequest) -> LocalFixGeneratedImage:
        self.call_count += 1
        self.requests.append(request)
        return await asyncio.to_thread(self._render, request)


class LocalFixService:
    """Validate, invoke, and locally protect a single candidate-based image edit.

    This service never calls the Search graph and never reads or writes canonical
    prompts. A Local Fix result is a structured, checkpoint-safe continuation
    value; API/database persistence is deliberately deferred to the later product
    slice.
    """

    def __init__(
        self,
        *,
        provider: LocalFixProvider,
        app_store: AppStore,
        asset_store: AssetStore,
        composite_floor: CompositeFloorService | None = None,
        model: str | None = None,
    ) -> None:
        self.provider = provider
        self.app_store = app_store
        self.asset_store = asset_store
        self.composite_floor = composite_floor or CompositeFloorService(asset_store)
        self.model = model or getattr(provider, "model", FAKE_LOCAL_FIX_MODEL)

    @staticmethod
    def instruction_hash(instruction: str) -> str:
        return hashlib.sha256(instruction.strip().encode("utf-8")).hexdigest()

    def build_request_key(self, resolution: LocalFixResolution) -> str:
        request = resolution.request
        payload = {
            "schema_version": LOCAL_FIX_SCHEMA_VERSION,
            "operation": "local_fix",
            "search_id": request.search_id,
            "source_manifest_hash": request.source_manifest_hash,
            "root_candidate_id": resolution.root_candidate_id,
            "base_candidate_id": request.base_candidate_id,
            "base_protected_sha256": resolution.base_candidate.protected_asset.sha256,
            "tight_mask_sha256": request.tight_mask.asset.sha256,
            "instruction_hash": self.instruction_hash(request.instruction),
            "base_generation_depth": request.generation_depth,
            "target_generation_depth": request.generation_depth + 1,
            "model": self.model,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _validate_tight_mask(self, *, resolution: LocalFixResolution) -> None:
        request = resolution.request
        mask = request.tight_mask
        source_background = resolution.source_manifest.background
        base = resolution.base_candidate
        if (base.protected_asset.width, base.protected_asset.height) != (
            source_background.width,
            source_background.height,
        ):
            raise ConflictError(
                "Local Fix base candidate must be a full-resolution protected image"
            )
        image_size = (source_background.width, source_background.height)
        tight_pixels = self._validated_mask_image(
            mask=mask,
            expected_size=image_size,
            label="tight mask",
        )
        outer_mask = resolution.outer_composite_mask
        outer_pixels = self._validated_mask_image(
            mask=outer_mask,
            expected_size=image_size,
            label="original composite floor",
        )
        allowed = outer_mask.allowed_box
        tight = mask.allowed_box
        if (
            tight.x < allowed.x
            or tight.y < allowed.y
            or tight.right > allowed.right
            or tight.bottom > allowed.bottom
        ):
            raise ConflictError(
                "Local Fix tight mask must remain inside the original composite floor"
            )
        tight_binary = tight_pixels.point(lambda alpha: 255 if alpha > 0 else 0)
        outside_outer = outer_pixels.point(lambda alpha: 255 if alpha == 0 else 0)
        if ImageChops.multiply(tight_binary, outside_outer).getbbox() is not None:
            raise ConflictError(
                "Local Fix tight mask contains editable pixels outside the original floor"
            )

    def _validated_mask_image(
        self,
        *,
        mask: Mask,
        expected_size: tuple[int, int],
        label: str,
    ) -> Image.Image:
        """Load one intact mask and prove its pixel support matches its metadata."""

        self.asset_store.assert_intact(mask.asset)
        if (mask.asset.width, mask.asset.height) != expected_size:
            raise ConflictError(f"Local Fix {label} dimensions do not match the source")
        if mask.allowed_box.right > expected_size[0] or mask.allowed_box.bottom > expected_size[1]:
            raise ConflictError(f"Local Fix {label} bounds exceed the source image")
        with Image.open(mask.asset.filesystem_path) as opened:
            pixels = opened.convert("L")
            pixels.load()
        if pixels.size != expected_size:
            raise ConflictError(f"Local Fix {label} decoded dimensions do not match the source")
        support = pixels.getbbox()
        if support is None:
            raise ConflictError(f"Local Fix {label} must contain an editable pixel")
        if (
            support[0] < mask.allowed_box.x
            or support[1] < mask.allowed_box.y
            or support[2] > mask.allowed_box.right
            or support[3] > mask.allowed_box.bottom
        ):
            raise ConflictError(f"Local Fix {label} pixels exceed the declared bounds")
        return pixels

    def _validate_base_composite_lineage(self, resolution: LocalFixResolution) -> None:
        """Ensure a candidate's claimed floor really belongs to this immutable source."""

        composite = resolution.base_candidate.composite
        if composite is None:
            return
        if composite.source_background != resolution.source_manifest.background:
            raise ConflictError(
                "Local Fix candidate composite does not match the source background"
            )
        if composite.raw_candidate != resolution.base_candidate.raw_asset:
            raise ConflictError(
                "Local Fix candidate composite does not match raw candidate lineage"
            )
        if composite.protected_asset != resolution.base_candidate.protected_asset:
            raise ConflictError("Local Fix candidate composite does not match protected lineage")
        if not composite.outside_mask_exact:
            raise ConflictError("Local Fix base candidate lacks verified background protection")
        with (
            Image.open(resolution.source_manifest.background.filesystem_path) as source,
            Image.open(resolution.base_candidate.protected_asset.filesystem_path) as protected,
            Image.open(composite.mask.asset.filesystem_path) as mask,
        ):
            if not self.composite_floor.outside_mask_is_exact(
                background=source,
                protected=protected,
                mask=mask,
            ):
                raise ConflictError(
                    "Local Fix base candidate changed pixels outside its composite floor"
                )

    def resolve(
        self,
        request: LocalFixRequest,
        *,
        previous_result: LocalFixResult | None = None,
    ) -> LocalFixResolution:
        """Resolve trusted lineage before any provider or image side effect."""

        search = self.app_store.get_search(request.search_id)
        if search.status is not SearchStatus.ACCEPTED:
            raise ConflictError("Local Fix requires an accepted search")
        project = self.app_store.get_project(search.project_id)
        manifest = project.source_manifest
        manifest.assert_integrity()
        if manifest.manifest_hash != search.source_manifest_hash:
            raise ConflictError("Search source manifest no longer matches its project")
        if request.source_manifest_hash != search.source_manifest_hash:
            raise ConflictError("Local Fix request source manifest does not match the search")

        historical_candidates = {
            candidate.candidate_id: candidate for candidate in search.candidates
        }
        root_candidate: CandidateRecord
        if previous_result is None:
            base_candidate = historical_candidates.get(request.base_candidate_id)
            if base_candidate is None:
                raise ConflictError(
                    "Local Fix base candidate is not historical for this accepted search"
                )
            if base_candidate.generation_depth != 0:
                raise ConflictError("A Local Fix root must be a source-based historical candidate")
            root_candidate_id = base_candidate.candidate_id
            root_candidate = base_candidate
        else:
            if (
                previous_result.search_id != request.search_id
                or previous_result.source_manifest_hash != request.source_manifest_hash
                or previous_result.outcome is not LocalFixOutcome.APPLIED
                or previous_result.candidate is None
                or previous_result.candidate.candidate_id != request.base_candidate_id
            ):
                raise ConflictError("Local Fix continuation does not match its prior result")
            if previous_result.root_candidate_id not in historical_candidates:
                raise ConflictError("Local Fix root candidate is not historical for this search")
            if historical_candidates[previous_result.root_candidate_id].generation_depth != 0:
                raise ConflictError("A Local Fix root must be a source-based historical candidate")
            persisted_call = self.app_store.get_provider_call(previous_result.request_key)
            if persisted_call is None or persisted_call[0] != "completed":
                raise ConflictError(
                    "Local Fix continuation is missing its completed provider audit"
                )
            persisted_result = persisted_call[1].get("result") if persisted_call[1] else None
            if not isinstance(persisted_result, dict) or (
                LocalFixResult.model_validate(persisted_result) != previous_result
            ):
                raise ConflictError("Local Fix continuation does not match its provider audit")
            base_candidate = previous_result.candidate
            root_candidate_id = previous_result.root_candidate_id
            root_candidate = historical_candidates[root_candidate_id]

        if base_candidate.source_manifest_hash != manifest.manifest_hash:
            raise ConflictError("Local Fix candidate source lineage does not match the search")
        if base_candidate.generation_depth != request.generation_depth:
            raise ConflictError("Local Fix generation depth does not match the base candidate")
        if base_candidate.generation_depth + 1 > 2:
            raise ConflictError("Local Fix generation depth cannot exceed 2")
        self.asset_store.assert_intact(manifest.background)
        self.asset_store.assert_intact(base_candidate.raw_asset)
        self.asset_store.assert_intact(base_candidate.protected_asset)
        outer_composite_mask = (
            root_candidate.composite.mask
            if root_candidate.composite is not None
            else self.composite_floor.create_mask(
                source_background=manifest.background,
                placement=search.placement,
            )
        )
        self.app_store.register_asset(outer_composite_mask.asset)
        resolution = LocalFixResolution(
            request=request,
            source_manifest=manifest,
            placement=search.placement,
            base_candidate=base_candidate,
            root_candidate_id=root_candidate_id,
            outer_composite_mask=outer_composite_mask,
        )
        self._validate_base_composite_lineage(resolution)
        self._validate_tight_mask(resolution=resolution)
        return resolution

    def _fallback(
        self,
        *,
        resolution: LocalFixResolution,
        request_key: str,
        failure_code: str,
    ) -> LocalFixResult:
        request = resolution.request
        return LocalFixResult(
            outcome=LocalFixOutcome.FALLBACK,
            request_key=request_key,
            search_id=request.search_id,
            source_manifest_hash=request.source_manifest_hash,
            root_candidate_id=resolution.root_candidate_id,
            base_candidate_id=request.base_candidate_id,
            instruction_hash=self.instruction_hash(request.instruction),
            generation_depth=resolution.base_candidate.generation_depth,
            fallback_candidate=resolution.base_candidate,
            failure_code=failure_code,
        )

    def _restore_completed_result(
        self,
        *,
        resolution: LocalFixResolution,
        request_key: str,
        response: dict[str, object],
    ) -> LocalFixResult:
        raw_result = response.get("result")
        if not isinstance(raw_result, dict):
            raise RuntimeError("Completed Local Fix provider audit has an invalid response shape")
        result = LocalFixResult.model_validate(raw_result)
        request = resolution.request
        if (
            result.request_key != request_key
            or result.search_id != request.search_id
            or result.base_candidate_id != request.base_candidate_id
            or result.source_manifest_hash != request.source_manifest_hash
            or result.root_candidate_id != resolution.root_candidate_id
            or result.instruction_hash != self.instruction_hash(request.instruction)
            or result.fallback_candidate != resolution.base_candidate
        ):
            raise RuntimeError("Completed Local Fix provider audit does not match the request")
        expected_depth = resolution.base_candidate.generation_depth
        if result.outcome is LocalFixOutcome.APPLIED:
            expected_depth += 1
            if result.candidate is None:
                raise RuntimeError("Completed Local Fix audit lost its output candidate")
            if result.candidate.model != self.model:
                raise RuntimeError("Completed Local Fix audit model does not match the request")
            if result.tight_composite is None or (
                result.tight_composite.mask != request.tight_mask
            ):
                raise RuntimeError("Completed Local Fix audit does not match the tight mask")
            if result.composite is None or (
                result.composite.mask != resolution.outer_composite_mask
                or result.composite.source_background != resolution.source_manifest.background
            ):
                raise RuntimeError("Completed Local Fix audit does not match the outer floor")
        if result.generation_depth != expected_depth:
            raise RuntimeError("Completed Local Fix audit has an invalid generation depth")
        assets = [result.fallback_candidate.raw_asset, result.fallback_candidate.protected_asset]
        if result.provider_raw_asset is not None:
            assets.append(result.provider_raw_asset)
        if result.tight_composite is not None:
            assets.extend(
                [
                    result.tight_composite.protected_asset,
                    result.tight_composite.mask.asset,
                ]
            )
        if result.composite is not None:
            assets.extend([result.composite.protected_asset, result.composite.mask.asset])
        for asset in assets:
            self.asset_store.assert_intact(asset)
            self.app_store.register_asset(asset)
        return result

    def _restore_or_fallback(
        self,
        *,
        resolution: LocalFixResolution,
        request_key: str,
        response: dict[str, object],
    ) -> LocalFixResult:
        """Never re-call a provider when a completed audit is malformed or corrupt."""

        try:
            return self._restore_completed_result(
                resolution=resolution,
                request_key=request_key,
                response=response,
            )
        except Exception:
            return self._fallback(
                resolution=resolution,
                request_key=request_key,
                failure_code="provider_audit_invalid",
            )

    async def _claim_or_reuse(
        self,
        *,
        resolution: LocalFixResolution,
        request_key: str,
        owner_id: str,
    ) -> LocalFixResult | None:
        request = resolution.request
        audit_payload = {
            "schema_version": LOCAL_FIX_SCHEMA_VERSION,
            "source_manifest_hash": request.source_manifest_hash,
            "root_candidate_id": resolution.root_candidate_id,
            "base_candidate_id": request.base_candidate_id,
            "base_protected_asset_id": resolution.base_candidate.protected_asset.asset_id,
            "tight_mask_asset_id": request.tight_mask.asset.asset_id,
            "instruction_hash": self.instruction_hash(request.instruction),
            "base_generation_depth": request.generation_depth,
            "target_generation_depth": request.generation_depth + 1,
            "model": self.model,
        }
        deadline = asyncio.get_running_loop().time() + LOCAL_FIX_PROVIDER_WAIT_SECONDS
        while True:
            claimed, status, completed = await asyncio.to_thread(
                self.app_store.claim_provider_call,
                request_key=request_key,
                operation="local_fix",
                search_id=request.search_id,
                request_payload=audit_payload,
                owner_id=owner_id,
                lease_seconds=LOCAL_FIX_PROVIDER_LEASE_SECONDS,
                max_attempts=1,
            )
            if status == "completed" and completed is not None:
                return await asyncio.to_thread(
                    self._restore_or_fallback,
                    resolution=resolution,
                    request_key=request_key,
                    response=completed,
                )
            if claimed:
                return None
            if status in {"failed_retryable", "failed_terminal"}:
                return self._fallback(
                    resolution=resolution,
                    request_key=request_key,
                    failure_code="provider_unavailable",
                )
            if status == "running":
                abandoned = self._fallback(
                    resolution=resolution,
                    request_key=request_key,
                    failure_code="provider_lease_expired",
                )
                closed = await asyncio.to_thread(
                    self.app_store.complete_expired_provider_call,
                    request_key,
                    {"result": abandoned.model_dump(mode="json")},
                )
                if closed:
                    return abandoned
            if asyncio.get_running_loop().time() >= deadline:
                return self._fallback(
                    resolution=resolution,
                    request_key=request_key,
                    failure_code="provider_call_timeout",
                )
            await asyncio.sleep(LOCAL_FIX_PROVIDER_POLL_SECONDS)

    async def _renew_provider_lease(self, *, request_key: str, owner_id: str) -> None:
        interval = LOCAL_FIX_PROVIDER_LEASE_SECONDS / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.app_store.renew_provider_call_lease,
                request_key=request_key,
                owner_id=owner_id,
                lease_seconds=LOCAL_FIX_PROVIDER_LEASE_SECONDS,
            )
            if not renewed:
                return

    async def apply(self, resolution: LocalFixResolution) -> LocalFixResult:
        """Run exactly one local edit, returning the base candidate on failure."""

        request = resolution.request
        request_key = self.build_request_key(resolution)
        owner_id = f"local_fix_{uuid4().hex}"
        reused = await self._claim_or_reuse(
            resolution=resolution,
            request_key=request_key,
            owner_id=owner_id,
        )
        if reused is not None:
            return reused

        heartbeat = asyncio.create_task(
            self._renew_provider_lease(request_key=request_key, owner_id=owner_id)
        )
        try:
            provider_request_id: str | None = None
            try:
                # All deterministic masks are resolved and verified before the paid
                # side effect. A corrupt floor must never consume a provider call.
                base_composite_mask = resolution.outer_composite_mask
                provider_request = LocalFixProviderRequest(
                    source_manifest_hash=request.source_manifest_hash,
                    base_candidate_id=resolution.base_candidate.candidate_id,
                    base_protected_asset=resolution.base_candidate.protected_asset,
                    tight_mask=request.tight_mask,
                    instruction=request.instruction.strip(),
                    instruction_hash=self.instruction_hash(request.instruction),
                    target_generation_depth=resolution.base_candidate.generation_depth + 1,
                    request_key=request_key,
                )
                generated = await self.provider.apply_local_fix(provider_request)
                provider_request_id = generated.provider_request_id
                if not generated.png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise RuntimeError("Local Fix provider did not return PNG output")
                provider_raw_asset = await asyncio.to_thread(
                    self.asset_store.put_image_bytes, generated.png_bytes
                )
                # First floor: constrain the untrusted edit to the requested tight mask
                # while preserving every other pixel of the selected base candidate.
                tight_composite = await asyncio.to_thread(
                    self.composite_floor.protect_candidate,
                    source_manifest_hash=request.source_manifest_hash,
                    source_background=resolution.base_candidate.protected_asset,
                    raw_candidate=provider_raw_asset,
                    placement=resolution.placement,
                    mask=request.tight_mask,
                    crop_mapping=None,
                )
                # Second floor: restore immutable source pixels outside the original
                # candidate floor, even if the base candidate or provider output drifted.
                final_composite = await asyncio.to_thread(
                    self.composite_floor.protect_candidate,
                    source_manifest_hash=request.source_manifest_hash,
                    source_background=resolution.source_manifest.background,
                    raw_candidate=tight_composite.protected_asset,
                    placement=resolution.placement,
                    mask=base_composite_mask,
                    crop_mapping=None,
                )
                candidate_seed = f"{request.search_id}:{request_key}:local-fix"
                output_candidate = CandidateRecord(
                    candidate_id=(
                        "cand_local_"
                        + hashlib.sha256(candidate_seed.encode("utf-8")).hexdigest()[:28]
                    ),
                    round_index=resolution.base_candidate.round_index,
                    variant_index=resolution.base_candidate.variant_index,
                    raw_asset=tight_composite.protected_asset,
                    protected_asset=final_composite.protected_asset,
                    source_manifest_hash=request.source_manifest_hash,
                    crop_mapping=None,
                    composite=final_composite,
                    prompt_hash=self.instruction_hash(request.instruction),
                    request_key=request_key,
                    generation_depth=resolution.base_candidate.generation_depth + 1,
                    model=self.model,
                    quality=resolution.base_candidate.quality,
                    size=(
                        f"{resolution.source_manifest.background.width}x"
                        f"{resolution.source_manifest.background.height}"
                    ),
                )
                for asset in (
                    provider_raw_asset,
                    tight_composite.protected_asset,
                    tight_composite.mask.asset,
                    final_composite.protected_asset,
                    final_composite.mask.asset,
                ):
                    await asyncio.to_thread(self.app_store.register_asset, asset)
                result = LocalFixResult(
                    outcome=LocalFixOutcome.APPLIED,
                    request_key=request_key,
                    search_id=request.search_id,
                    source_manifest_hash=request.source_manifest_hash,
                    root_candidate_id=resolution.root_candidate_id,
                    base_candidate_id=request.base_candidate_id,
                    instruction_hash=self.instruction_hash(request.instruction),
                    generation_depth=output_candidate.generation_depth,
                    candidate=output_candidate,
                    fallback_candidate=resolution.base_candidate,
                    provider_raw_asset=provider_raw_asset,
                    tight_composite=tight_composite,
                    composite=final_composite,
                )
            except Exception:
                # A failed provider or local image operation never triggers an implicit
                # Search retry. Persisting this fallback also prevents a resume from
                # accidentally charging the same Local Fix request again.
                result = self._fallback(
                    resolution=resolution,
                    request_key=request_key,
                    failure_code="local_fix_failed",
                )

            completed = await asyncio.to_thread(
                self.app_store.complete_provider_call,
                request_key,
                {
                    "result": result.model_dump(mode="json"),
                    "provider": {
                        "model": self.model,
                        "request_id": provider_request_id,
                    },
                },
                owner_id=owner_id,
            )
            if not completed:
                stored = await asyncio.to_thread(self.app_store.get_provider_call, request_key)
                if stored is not None and stored[0] == "completed" and stored[1] is not None:
                    return await asyncio.to_thread(
                        self._restore_or_fallback,
                        resolution=resolution,
                        request_key=request_key,
                        response=stored[1],
                    )
                return self._fallback(
                    resolution=resolution,
                    request_key=request_key,
                    failure_code="provider_audit_unavailable",
                )
            return result
        except asyncio.CancelledError:
            # Cancellation must roll back visibly and close the lease. Without this,
            # a replay would see an exhausted `running` call and wait until timeout;
            # retrying it could also duplicate a paid edit whose outcome is unknown.
            cancelled = self._fallback(
                resolution=resolution,
                request_key=request_key,
                failure_code="local_fix_cancelled",
            )
            with suppress(Exception):
                await asyncio.shield(
                    asyncio.to_thread(
                        self.app_store.complete_provider_call,
                        request_key,
                        {"result": cancelled.model_dump(mode="json")},
                        owner_id=owner_id,
                    )
                )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
