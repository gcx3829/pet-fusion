from __future__ import annotations

import asyncio
import hashlib
import io
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.errors import SourceManifestMismatchError
from app.domain.searches import PlacementIntent
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore

FAKE_IMAGE_MODEL = "fake-gpt-image-2"
GENERATOR_SCHEMA_VERSION = "generator-request/v1"
PROVIDER_RESULT_POLL_SECONDS = 0.02
PROVIDER_RESULT_WAIT_SECONDS = 30.0
PROVIDER_CALL_LEASE_SECONDS = 5


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


class ImageGenerator(Protocol):
    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]: ...


class DeterministicFakeImageGenerator:
    """Test double that paints a deterministic marker over the immutable source image."""

    def __init__(self) -> None:
        self.call_count = 0
        self.requests: list[GenerationRequest] = []

    async def generate_round(self, request: GenerationRequest) -> list[GeneratedImage]:
        self.call_count += 1
        self.requests.append(request)
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


class GeneratorService:
    def __init__(
        self,
        *,
        provider: ImageGenerator,
        asset_store: AssetStore,
        app_store: AppStore,
    ) -> None:
        self.provider = provider
        self.asset_store = asset_store
        self.app_store = app_store

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
            if not self.app_store.renew_provider_call_lease(
                request_key=request_key,
                owner_id=owner_id,
                lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
            ):
                return

    async def generate_round(
        self,
        request: GenerationRequest,
        *,
        expected_manifest_hash: str,
    ) -> list[CandidateRecord]:
        self._assert_rebase_inputs(request, expected_manifest_hash=expected_manifest_hash)
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
        }
        owner_id = f"provider_{uuid4().hex}"
        claimed, status, completed_response = self.app_store.claim_provider_call(
            request_key=request_key,
            operation="generate_round",
            search_id=request.search_id,
            request_payload=audit_payload,
            owner_id=owner_id,
            lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
        )
        if status == "completed" and completed_response is not None:
            return self._completed_candidates(
                search_id=request.search_id, completed_response=completed_response
            )

        existing = self.app_store.find_candidates_for_request(request.search_id, request_key)
        if len(existing) == request.candidate_count:
            for candidate in existing:
                self._emit_candidate_ready(request.search_id, candidate)
            response: dict[str, object] = {
                "candidates": [item.model_dump(mode="json") for item in existing]
            }
            if claimed:
                self.app_store.complete_provider_call(
                    request_key, response, owner_id=owner_id
                )
            return existing

        if not claimed:
            deadline = asyncio.get_running_loop().time() + PROVIDER_RESULT_WAIT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(PROVIDER_RESULT_POLL_SECONDS)
                claimed, status, completed_response = self.app_store.claim_provider_call(
                    request_key=request_key,
                    operation="generate_round",
                    search_id=request.search_id,
                    request_payload=audit_payload,
                    owner_id=owner_id,
                    lease_seconds=PROVIDER_CALL_LEASE_SECONDS,
                )
                if status == "completed" and completed_response is not None:
                    return self._completed_candidates(
                        search_id=request.search_id,
                        completed_response=completed_response,
                    )
                existing = self.app_store.find_candidates_for_request(
                    request.search_id, request_key
                )
                if len(existing) == request.candidate_count:
                    response = {
                        "candidates": [item.model_dump(mode="json") for item in existing]
                    }
                    if claimed:
                        self.app_store.complete_provider_call(
                            request_key, response, owner_id=owner_id
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
                asset = self.asset_store.put_image_bytes(output.png_bytes)
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
                    raw_asset=asset,
                    protected_asset=asset,
                    prompt_hash=request.prompt_hash,
                    request_key=request_key,
                    generation_depth=0,
                    model=request.model,
                    quality=request.quality,
                    size=request.size,
                )
                self.app_store.add_candidate(request.search_id, candidate)
                self._emit_candidate_ready(request.search_id, candidate)
                candidates.append(candidate)
            response = {"candidates": [item.model_dump(mode="json") for item in candidates]}
            self.app_store.complete_provider_call(
                request_key, response, owner_id=owner_id
            )
            return candidates
        except Exception as exc:
            self.app_store.fail_provider_call(
                request_key, type(exc).__name__, owner_id=owner_id
            )
            raise
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
