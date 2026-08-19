from __future__ import annotations

import io
import json
from collections.abc import Sequence

import pytest
from PIL import Image

from app.container import AppContainer
from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.errors import SourceManifestMismatchError
from app.domain.evaluations import (
    CandidateEvaluation,
    CriticIssue,
    DimensionScores,
    Severity,
)
from app.domain.projects import ProjectRecord
from app.domain.prompts import (
    PromptGenerationMode,
    PromptVersion,
    VisualAnchorRef,
)
from app.domain.searches import CreateSearchRequest, PlacementIntent, SearchStatus
from app.persistence.app_store import utcnow
from app.services.generator_service import (
    GENERATOR_ANCHOR_MAX_SIDE,
    GENERATOR_ANCHOR_PROXY_VERSION,
    GenerationRequest,
    GeneratorService,
    OpenAIImageGenerator,
)
from app.services.openai_image_client import (
    OpenAIImageEditResult,
    OpenAIImageInput,
)
from app.services.prompt_refiner_service import PromptRefinerRequest
from tests.conftest import make_image_bytes


class RecordingTransport:
    def __init__(self, outputs: tuple[bytes, ...]) -> None:
        self.outputs = outputs
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
        mask: OpenAIImageInput | None = None,
        request_key: str | None = None,
    ) -> OpenAIImageEditResult:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "images": tuple(images),
                "mask": mask,
                "n": n,
                "quality": quality,
                "size": size,
                "request_key": request_key,
            }
        )
        return OpenAIImageEditResult(
            png_images=self.outputs,
            request_id="req-anchor-test",
            usage={"input_tokens": 4, "output_tokens": 6, "total_tokens": 10},
        )


def _evaluation(*, candidate_id: str, source_hash: str) -> CandidateEvaluation:
    return CandidateEvaluation(
        rubric_version="critic-rubric/test",
        candidate_id=candidate_id,
        round_index=0,
        source_manifest_hash=source_hash,
        scores=DimensionScores(
            cat_identity=90,
            pose_geometry=82,
            perspective_scale=83,
            lighting_color=84,
            optical_consistency=80,
            physical_integration=78,
            scene_preservation=92,
            overall_photographic_naturalness=85,
        ),
        issues=(
            CriticIssue(
                issue_id="contact",
                category="physical_integration",
                severity=Severity.BLOCKING,
                evidence="Contact shadow is not yet credible.",
                suggested_fix="Strengthen the local contact shadow.",
                confidence=0.9,
            ),
        ),
        no_meaningful_defect=False,
        identity_match=True,
        prompt_adherent=True,
        recommended_action="regenerate",
        summary="The selected candidate needs a local contact correction.",
    )


async def _anchor_fixture(settings):
    container = AppContainer.build(settings)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes(size=(96, 64)))
    reference = container.asset_store.put_image_bytes(
        make_image_bytes((180, 100, 60), size=(48, 36))
    )
    guidance_bytes = io.BytesIO()
    Image.new("RGBA", (96, 64), (255, 255, 255, 160)).save(guidance_bytes, format="PNG")
    guidance = container.asset_store.put_guidance_mask_bytes(guidance_bytes.getvalue())
    for asset in (background, reference, guidance):
        container.app_store.register_asset(asset)
    manifest = SourceManifest.create(background=background, cat_references=[reference])
    project = ProjectRecord(
        project_id="project-generator-anchor",
        source_manifest=manifest,
        created_at=utcnow(),
    )
    container.app_store.create_project(project)
    container.app_store.register_guidance_mask(
        project_id=project.project_id,
        source_manifest_hash=manifest.manifest_hash,
        asset=guidance,
    )
    command = CreateSearchRequest(
        placement=PlacementIntent(
            x=0.2,
            y=0.2,
            width=0.3,
            height=0.4,
            pose="sitting",
            facing="left",
        ),
        user_intent="Place the exact cat naturally in the travel photograph.",
        candidate_count=1,
        max_rounds=3,
        guidance_mask_asset_id=guidance.asset_id,
    )
    search = container.app_store.create_search(
        search_id="search-generator-anchor",
        thread_id="search-generator-anchor",
        project=project,
        request=command,
    )
    initial = await container.prompt_refiner_service.refine(
        PromptRefinerRequest(
            search_id=search.search_id,
            mode="initial",
            round_index=0,
            source_manifest=manifest,
            guidance_mask=guidance,
            user_intent=command.user_intent,
            generation_model="gpt-image-2",
        )
    )
    raw_anchor_asset = container.asset_store.put_image_bytes(
        make_image_bytes((10, 20, 30), size=(1200, 600))
    )
    container.app_store.register_asset(raw_anchor_asset)
    anchor = VisualAnchorRef.from_raw_asset(
        search_id=search.search_id,
        candidate_id="cand_r0_selected",
        round_index=0,
        source_manifest_hash=manifest.manifest_hash,
        raw_asset=raw_anchor_asset,
    )
    candidate = CandidateRecord(
        candidate_id=anchor.candidate_id,
        round_index=0,
        variant_index=0,
        raw_asset=raw_anchor_asset,
        protected_asset=raw_anchor_asset,
        source_manifest_hash=manifest.manifest_hash,
        prompt_hash=initial.prompt_version.generation_prompt_hash,
        request_key="a" * 64,
        generation_depth=0,
        model="gpt-image-2",
        quality="medium",
        size="auto",
    )
    container.app_store.add_candidate(search.search_id, candidate)
    container.app_store.save_evaluation(
        search.search_id,
        _evaluation(candidate_id=anchor.candidate_id, source_hash=manifest.manifest_hash),
    )
    container.app_store.update_search(
        search.search_id,
        status=SearchStatus.WAITING_FOR_HUMAN,
        expected_statuses=[SearchStatus.QUEUED],
    )
    assert container.app_store.queue_next_round(
        search.search_id,
        reviewed_round_index=0,
        selected_candidate_id=anchor.candidate_id,
        human_feedback="Keep this candidate's identity but fix the contact shadow.",
    )
    revision = await container.prompt_refiner_service.refine(
        PromptRefinerRequest(
            search_id=search.search_id,
            mode="revision",
            round_index=1,
            source_manifest=manifest,
            guidance_mask=guidance,
            user_intent=command.user_intent,
            human_feedback="Keep this candidate's identity but fix the contact shadow.",
            human_selected_candidate_id=anchor.candidate_id,
            visual_anchor=anchor,
            selected_candidate_evaluation=_evaluation(
                candidate_id=anchor.candidate_id,
                source_hash=manifest.manifest_hash,
            ),
            parent_prompt_version=initial.prompt_version,
            generation_model="gpt-image-2",
        )
    )
    return container, manifest, guidance, anchor, initial.prompt_version, revision.prompt_version


def _request(
    *,
    search_id: str,
    manifest: SourceManifest,
    guidance,
    anchor: VisualAnchorRef,
    prompt_version: PromptVersion,
    include_prompt_version: bool = True,
) -> GenerationRequest:
    return GenerationRequest(
        search_id=search_id,
        source_manifest=manifest,
        guidance_mask=guidance,
        placement=PlacementIntent(
            x=0.2,
            y=0.2,
            width=0.3,
            height=0.4,
            pose="sitting",
            facing="left",
        ),
        prompt=prompt_version.generation_prompt,
        prompt_hash=prompt_version.generation_prompt_hash,
        round_index=1,
        candidate_count=1,
        model="gpt-image-2",
        quality="medium",
        size="1024x1024",
        generation_mode=PromptGenerationMode.CANDIDATE_ANCHORED_REBASE,
        prompt_version_id=prompt_version.prompt_version_id,
        prompt_version_hash=prompt_version.prompt_version_hash,
        parent_prompt_version_id=prompt_version.based_on_prompt_version_id,
        prompt_version=(prompt_version if include_prompt_version else None),
        visual_anchor=anchor,
    )


async def test_candidate_anchor_is_image_one_and_is_bounded(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert transport.calls[0]["request_key"] == service.build_request_key(request)
    images = transport.calls[0]["images"]
    assert isinstance(images, tuple)
    assert [item.filename.split("-", 2)[1] for item in images] == [
        "background",
        "visual",
        "reference",
    ]
    assert images[0].filename.startswith("00-background-")
    assert images[1].filename.startswith("01-visual-anchor-")
    assert images[2].filename.startswith("02-reference-")
    assert [item.mime_type for item in images] == [
        "image/png",
        "image/jpeg",
        "image/jpeg",
    ]
    with Image.open(io.BytesIO(images[1].png_bytes)) as proxy:
        assert proxy.size == (GENERATOR_ANCHOR_MAX_SIDE, GENERATOR_ANCHOR_MAX_SIDE // 2)
        assert proxy.format == "JPEG"
    mask = transport.calls[0]["mask"]
    assert isinstance(mask, OpenAIImageInput)
    with Image.open(io.BytesIO(mask.png_bytes)) as mask_image:
        assert mask_image.size == Image.open(io.BytesIO(images[0].png_bytes)).size


async def test_anchor_audit_and_completed_replay_are_lineage_safe(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    first = await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    replay = await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert first == replay
    assert len(transport.calls) == 1
    record = container.app_store.get_provider_call_record(service.build_request_key(request))
    assert record is not None
    audit = record["request"]
    assert isinstance(audit, dict)
    assert audit["generation_mode"] == "candidate_anchored_rebase"
    assert audit["prompt_version"]["prompt_version_id"] == revision.prompt_version_id
    assert audit["visual_anchor"]["candidate_id"] == anchor.candidate_id
    assert audit["visual_anchor"]["raw_asset_sha256"] == anchor.raw_asset_sha256
    assert audit["anchor_proxy"]["schema_version"] == GENERATOR_ANCHOR_PROXY_VERSION
    assert audit["anchor_proxy"]["width"] == GENERATOR_ANCHOR_MAX_SIDE
    encoded = json.dumps(record, ensure_ascii=False)
    assert "data:image/" not in encoded
    assert "base64" not in encoded.lower()


def test_source_rebase_rejects_anchor_at_request_boundary() -> None:
    source_asset = AssetRef(
        asset_id="ast_" + "a" * 32,
        path="/tmp/assets/aa/" + "a" * 64 + ".png",
        sha256="a" * 64,
        width=96,
        height=64,
    )
    reference = AssetRef(
        asset_id="ast_" + "b" * 32,
        path="/tmp/assets/bb/" + "b" * 64 + ".png",
        sha256="b" * 64,
        width=48,
        height=36,
    )
    manifest = SourceManifest.create(background=source_asset, cat_references=[reference])
    anchor = VisualAnchorRef.from_raw_asset(
        search_id="search",
        candidate_id="cand_r0",
        round_index=0,
        source_manifest_hash=manifest.manifest_hash,
        raw_asset=reference,
    )
    with pytest.raises(ValueError, match="source_rebase generation cannot contain"):
        GenerationRequest(
            search_id="search",
            source_manifest=manifest,
            placement=PlacementIntent(
                x=0.1,
                y=0.1,
                width=0.2,
                height=0.2,
                pose="sitting",
                facing="left",
            ),
            prompt="prompt",
            prompt_hash="a" * 64,
            round_index=1,
            candidate_count=1,
            model="gpt-image-2",
            quality="medium",
            size="auto",
            generation_mode=PromptGenerationMode.SOURCE_REBASE,
            visual_anchor=anchor,
        )


async def test_wrong_round_or_depth_rejects_before_provider(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    wrong_round_anchor = anchor.model_copy(update={"round_index": 1})
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=wrong_round_anchor,
        prompt_version=revision,
        include_prompt_version=False,
    )
    with pytest.raises(SourceManifestMismatchError, match="immediately previous round"):
        await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert transport.calls == []


async def test_anchor_requires_matching_persisted_human_resume(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    assert container.app_store.update_search(
        request.search_id,
        round_history=[
            {
                "round_index": 0,
                "human_resume_applied": True,
                "human_selected_candidate_id": anchor.candidate_id,
                "human_feedback": "Different feedback must not authorize this prompt.",
            }
        ],
    )

    with pytest.raises(SourceManifestMismatchError, match="human selection and feedback"):
        await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert transport.calls == []


async def test_cancel_after_provider_claim_is_rechecked_before_paid_anchor_call(
    settings, monkeypatch
) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(
        settings
    )
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    original_claim = container.app_store.claim_provider_call

    def claim_then_cancel(**kwargs):
        result = original_claim(**kwargs)
        if result[0]:
            assert container.app_store.update_search(
                request.search_id,
                status=SearchStatus.CANCELLED,
                expected_statuses=[SearchStatus.QUEUED],
            )
        return result

    monkeypatch.setattr(container.app_store, "claim_provider_call", claim_then_cancel)
    with pytest.raises(SourceManifestMismatchError, match="active search target round"):
        await service.generate_round(
            request,
            expected_manifest_hash=manifest.manifest_hash,
        )
    assert transport.calls == []
    request_key = service.build_request_key(request)
    provider_record = container.app_store.get_provider_call_record(request_key)
    assert provider_record is not None
    assert provider_record["status"] == "failed_terminal"


async def test_anchor_requires_exact_authorized_prompt_text(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
        include_prompt_version=False,
    ).model_copy(update={"prompt": "A different prompt using the authorized hash."})

    with pytest.raises(SourceManifestMismatchError, match="prompt text"):
        await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert transport.calls == []


async def test_anchor_request_key_covers_placement_semantics(settings) -> None:
    _container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    moved = request.model_copy(
        update={
            "placement": request.placement.model_copy(
                update={"x": request.placement.x + 0.05}
            )
        }
    )
    assert GeneratorService.build_request_key(request) != GeneratorService.build_request_key(
        moved
    )


async def test_completed_replay_rejects_non_raw_first_candidate(settings) -> None:
    container, manifest, guidance, anchor, _parent, revision = await _anchor_fixture(settings)
    transport = RecordingTransport((make_image_bytes((40, 50, 60)),))
    service = GeneratorService(
        provider=OpenAIImageGenerator(transport=transport),
        asset_store=container.asset_store,
        app_store=container.app_store,
    )
    request = _request(
        search_id="search-generator-anchor",
        manifest=manifest,
        guidance=guidance,
        anchor=anchor,
        prompt_version=revision,
    )
    await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    request_key = service.build_request_key(request)
    record = container.app_store.get_provider_call_record(request_key)
    assert record is not None and isinstance(record["response"], dict)
    response = dict(record["response"])
    candidates = list(response["candidates"])
    tampered = dict(candidates[0])
    tampered["protected_asset"] = anchor.raw_asset.model_dump(mode="json")
    candidates[0] = tampered
    response["candidates"] = candidates
    with container.app_store._connection() as connection:
        connection.execute(
            "UPDATE provider_calls SET response_json = ? WHERE request_key = ?",
            (json.dumps(response, separators=(",", ":"), sort_keys=True), request_key),
        )
        connection.commit()

    with pytest.raises(SourceManifestMismatchError, match="candidate lineage"):
        await service.generate_round(request, expected_manifest_hash=manifest.manifest_hash)
    assert len(transport.calls) == 1
