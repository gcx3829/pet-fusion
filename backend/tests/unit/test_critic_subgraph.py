from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from dataclasses import replace
from typing import cast

import pytest

from app.config import Settings
from app.container import AppContainer
from app.domain.assets import AssetRef, SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.evaluations import CandidateEvaluation, CriticIssue, Severity
from app.domain.projects import ProjectRecord
from app.domain.searches import CreateSearchRequest, PlacementIntent
from app.graphs.critic_subgraph import CriticSubgraphState, build_critic_subgraph
from app.graphs.reducers import empty_evaluation_bucket, merge_evaluations_by_candidate
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import utcnow
from app.services.candidate_ranker import DeterministicCandidateRanker
from app.services.critic_service import (
    RUBRIC_VERSION,
    CriticEvaluationService,
    CriticInput,
    DeterministicCriticService,
    normalize_critic_evaluation,
)
from app.services.generator_service import FAKE_IMAGE_MODEL, GenerationRequest
from app.services.prompt_compiler import compile_canonical_prompt
from app.services.proxy_builder import CriticProxyBuilder, CriticProxyBundle
from tests.conftest import make_image_bytes


class CountingCritic(DeterministicCriticService):
    def __init__(self) -> None:
        self.call_count = 0

    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        self.call_count += 1
        time.sleep(0.02)
        return super().evaluate(request)


class AlwaysFailCritic(DeterministicCriticService):
    def __init__(self) -> None:
        self.call_count = 0

    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        del request
        self.call_count += 1
        raise RuntimeError("fixture critic unavailable")


class BlockingCritic(DeterministicCriticService):
    def __init__(self) -> None:
        self.call_count = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        self.call_count += 1
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("fixture release timed out")
        return super().evaluate(request)


async def _setup_search_with_candidates(
    settings: Settings,
) -> tuple[
    AppContainer,
    SourceManifest,
    CreateSearchRequest,
    str,
    list[CandidateRecord],
]:
    container = AppContainer.build(settings)
    container.initialize()
    background = container.asset_store.put_image_bytes(make_image_bytes(size=(96, 64)))
    reference_one = container.asset_store.put_image_bytes(
        make_image_bytes((180, 120, 50), size=(40, 30))
    )
    reference_two = container.asset_store.put_image_bytes(
        make_image_bytes((120, 80, 40), size=(48, 36))
    )
    manifest = SourceManifest.create(
        background=background,
        cat_references=[reference_one, reference_two],
    )
    project = ProjectRecord(
        project_id="project-critic-subgraph",
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
        user_intent="Place the exact cat naturally in the photograph.",
        candidate_count=2,
    )
    search = container.app_store.create_search(
        search_id="search-critic-subgraph",
        thread_id="search-critic-subgraph",
        project=project,
        request=command,
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=len(manifest.cat_references),
    )
    candidates = await container.generator_service.generate_round(
        GenerationRequest(
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
        ),
        expected_manifest_hash=manifest.manifest_hash,
    )
    return container, manifest, command, search.search_id, candidates


async def test_critic_subgraph_fans_out_bounded_proxies_and_replays_idempotently(
    settings: Settings,
) -> None:
    container, manifest, command, search_id, candidates = await _setup_search_with_candidates(
        settings
    )
    critic = CountingCritic()
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=len(manifest.cat_references),
    )
    # Real multimodal prompt plans can exceed the legacy 8k Critic task limit.
    prompt = prompt + (" Concrete visual observation." * 360)
    assert 8_000 < len(prompt) <= 12_000
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    graph = build_critic_subgraph(
        app_store=container.app_store,
        proxy_builder=CriticProxyBuilder(
            asset_store=container.asset_store,
            app_store=container.app_store,
            max_side=32,
        ),
        critic_provider=critic,
        candidate_ranker=DeterministicCandidateRanker(),
    ).compile()
    initial_state: CriticSubgraphState = {
        "search_id": search_id,
        "status": "running",
        "source_manifest": manifest.model_dump(mode="json"),
        "placement": command.placement.model_dump(mode="json"),
        "canonical_prompt": prompt,
        "canonical_prompt_hash": prompt_hash,
        "current_candidates": [item.model_dump(mode="json") for item in candidates],
        "evaluations_by_candidate": empty_evaluation_bucket(0),
    }

    result = cast(CriticSubgraphState, await graph.ainvoke(initial_state, version="v1"))
    replay = cast(CriticSubgraphState, await graph.ainvoke(initial_state, version="v1"))

    candidate_ids = {item.candidate_id for item in candidates}
    assert {item["candidate_id"] for item in result["evaluations"]} == candidate_ids
    assert {item["candidate_id"] for item in replay["evaluations"]} == candidate_ids
    assert set(result["evaluations_by_candidate"]["items"]) == candidate_ids
    assert critic.call_count == len(candidates)
    assert len(container.app_store.list_evaluations(search_id)) == len(candidates)
    events = container.app_store.list_events(search_id)
    assert [event.type for event in events].count("round.critic.started") == 1
    assert [event.type for event in events].count("round.evaluation.ready") == len(candidates)

    proxy_inputs = result["critic_proxy_inputs"]
    assert_checkpoint_safe(proxy_inputs)
    for proxy_payload in proxy_inputs.values():
        bundle = CriticProxyBundle.model_validate(proxy_payload)
        for asset in (
            bundle.background_proxy,
            bundle.placement_overlay_proxy,
            bundle.raw_candidate_proxy,
            bundle.scene_comparison_proxy,
            *bundle.reference_proxies,
        ):
            assert asset is not None
            assert max(asset.width, asset.height) <= 32


async def test_critic_proxy_uses_raw_when_legacy_protected_asset_differs(
    settings: Settings,
) -> None:
    container, manifest, command, _search_id, candidates = (
        await _setup_search_with_candidates(settings)
    )
    raw_candidate = candidates[0]
    legacy_shaped = raw_candidate.model_copy(
        update={"protected_asset": manifest.background}
    )

    bundle = CriticProxyBuilder(
        asset_store=container.asset_store,
        app_store=container.app_store,
        max_side=64,
    ).build(
        source_manifest=manifest,
        candidate=legacy_shaped,
        placement=command.placement,
    )

    assert bundle.candidate_proxy == bundle.raw_candidate_proxy
    assert bundle.raw_candidate_proxy.sha256 != bundle.background_proxy.sha256
    assert bundle.scene_comparison_proxy is not None
    assert max(
        bundle.scene_comparison_proxy.width,
        bundle.scene_comparison_proxy.height,
    ) <= 64


async def test_critic_subgraph_isolates_exhausted_provider_failures_on_replay(
    settings: Settings,
) -> None:
    container, manifest, command, search_id, candidates = await _setup_search_with_candidates(
        settings
    )
    critic = AlwaysFailCritic()
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=len(manifest.cat_references),
    )
    graph = build_critic_subgraph(
        app_store=container.app_store,
        proxy_builder=CriticProxyBuilder(
            asset_store=container.asset_store,
            app_store=container.app_store,
            max_side=32,
        ),
        critic_provider=critic,
        candidate_ranker=DeterministicCandidateRanker(),
    ).compile()
    initial_state: CriticSubgraphState = {
        "search_id": search_id,
        "status": "running",
        "source_manifest": manifest.model_dump(mode="json"),
        "placement": command.placement.model_dump(mode="json"),
        "canonical_prompt": prompt,
        "canonical_prompt_hash": prompt_hash,
        "current_candidates": [item.model_dump(mode="json") for item in candidates],
        "evaluations_by_candidate": empty_evaluation_bucket(0),
    }

    first = cast(CriticSubgraphState, await graph.ainvoke(initial_state, version="v1"))
    replay = cast(CriticSubgraphState, await graph.ainvoke(initial_state, version="v1"))

    assert critic.call_count == len(candidates) * 2
    for result in (first, replay):
        evaluations = [CandidateEvaluation.model_validate(item) for item in result["evaluations"]]
        assert len(evaluations) == len(candidates)
        assert all(
            "evaluation_unavailable" in item.hard_constraint_failures
            for item in evaluations
        )


def test_critic_reducer_resets_new_round_and_ignores_delayed_old_writes() -> None:
    round_zero = merge_evaluations_by_candidate(
        empty_evaluation_bucket(0),
        {
            "round_index": 0,
            "items": {
                "candidate-zero": {"candidate_id": "candidate-zero", "round_index": 0}
            },
        },
    )
    round_one = merge_evaluations_by_candidate(round_zero, empty_evaluation_bucket(1))
    delayed_round_zero = merge_evaluations_by_candidate(
        round_one,
        {
            "round_index": 0,
            "items": {
                "candidate-delayed": {
                    "candidate_id": "candidate-delayed",
                    "round_index": 0,
                }
            },
        },
    )

    assert round_one == empty_evaluation_bucket(1)
    assert delayed_round_zero == empty_evaluation_bucket(1)


async def test_critic_cancellation_finishes_paid_call_audit_before_propagating(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, manifest, command, search_id, candidates = await _setup_search_with_candidates(
        settings
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=len(manifest.cat_references),
    )
    candidate = candidates[0]
    proxies = CriticProxyBuilder(
        asset_store=container.asset_store,
        app_store=container.app_store,
        max_side=32,
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
    critic = BlockingCritic()
    service = CriticEvaluationService(provider=critic, app_store=container.app_store)
    original_complete = container.app_store.complete_provider_call
    audit_write_count = 0

    def flaky_complete(*args: object, **kwargs: object) -> bool:
        nonlocal audit_write_count
        audit_write_count += 1
        if audit_write_count < 3:
            raise RuntimeError("fixture audit write failure")
        return original_complete(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(container.app_store, "complete_provider_call", flaky_complete)

    task = asyncio.create_task(service.evaluate(search_id=search_id, request=request))
    assert await asyncio.to_thread(critic.started.wait, 1)
    task.cancel()
    critic.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    replay = await service.evaluate(search_id=search_id, request=request)
    assert replay.candidate_id == candidate.candidate_id
    assert critic.call_count == 1
    assert audit_write_count == 3
    request_key = service.build_request_key(
        search_id=search_id,
        request=request,
        model=critic.model,
        rubric_version=critic.rubric_version,
        provider_fingerprint=critic.provider_fingerprint,
    )
    provider_call = container.app_store.get_provider_call(request_key)
    assert provider_call is not None
    assert provider_call[0] == "completed"


async def test_critic_cancellation_during_audit_retry_preserves_paid_result(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, manifest, command, search_id, candidates = await _setup_search_with_candidates(
        settings
    )
    prompt, prompt_hash = compile_canonical_prompt(
        placement=command.placement,
        user_intent=command.user_intent,
        reference_count=len(manifest.cat_references),
    )
    candidate = candidates[0]
    proxies = CriticProxyBuilder(
        asset_store=container.asset_store,
        app_store=container.app_store,
        max_side=32,
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
    critic = CountingCritic()
    service = CriticEvaluationService(provider=critic, app_store=container.app_store)
    original_complete = container.app_store.complete_provider_call
    audit_retry_started = threading.Event()
    release_audit_retry = threading.Event()
    audit_write_count = 0

    def retrying_complete(*args: object, **kwargs: object) -> bool:
        nonlocal audit_write_count
        audit_write_count += 1
        if audit_write_count == 1:
            raise RuntimeError("fixture first audit write failure")
        if audit_write_count == 2:
            audit_retry_started.set()
            if not release_audit_retry.wait(timeout=2):
                raise RuntimeError("fixture audit retry release timed out")
        return original_complete(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(container.app_store, "complete_provider_call", retrying_complete)

    task = asyncio.create_task(service.evaluate(search_id=search_id, request=request))
    assert await asyncio.to_thread(audit_retry_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    remained_pending_until_audit_finished = not task.done()
    concurrent_replay = asyncio.create_task(
        service.evaluate(search_id=search_id, request=request)
    )
    await asyncio.sleep(0.04)
    replay_waited_for_owned_audit = not concurrent_replay.done()
    provider_was_not_repeated = critic.call_count == 1
    release_audit_retry.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert remained_pending_until_audit_finished
    assert replay_waited_for_owned_audit
    assert provider_was_not_repeated
    replay = await concurrent_replay
    assert replay.candidate_id == candidate.candidate_id
    assert critic.call_count == 1
    assert audit_write_count == 2
    request_key = service.build_request_key(
        search_id=search_id,
        request=request,
        model=critic.model,
        rubric_version=critic.rubric_version,
        provider_fingerprint=critic.provider_fingerprint,
    )
    provider_call = container.app_store.get_provider_call(request_key)
    assert provider_call is not None
    assert provider_call[0] == "completed"


def test_critic_normalization_fences_provider_lineage_and_deduplicates_issues() -> None:
    source = SourceManifest.create(
        background=AssetRef(
            asset_id="ast_background",
            path="/tmp/background.png",
            sha256="a" * 64,
            width=96,
            height=64,
        ),
        cat_references=[
            AssetRef(
                asset_id="ast_reference",
                path="/tmp/reference.png",
                sha256="b" * 64,
                width=32,
                height=32,
            )
        ],
    )
    candidate = CandidateRecord(
        candidate_id="candidate-correct",
        round_index=0,
        variant_index=0,
        raw_asset=source.background,
        protected_asset=source.background,
        source_manifest_hash=source.manifest_hash,
        prompt_hash="c" * 64,
        request_key="d" * 64,
        model="fake",
        quality="medium",
        size="96x64",
    )
    issue = CriticIssue(
        issue_id="duplicate",
        category="physical_integration",
        severity=Severity.BLOCKING,
        evidence="Visible local seam.",
        confidence=0.9,
    )
    raw = (
        DeterministicCriticService()
        .evaluate(
            CriticInput(
                candidate=candidate,
                source_manifest=source,
                placement=PlacementIntent(
                    x=0.1,
                    y=0.1,
                    width=0.2,
                    height=0.2,
                    pose="sitting",
                    facing="left",
                ),
                canonical_prompt="test prompt",
                canonical_prompt_hash="f" * 64,
            )
        )
        .model_copy(
            update={
                "rubric_version": "critic-rubric/untrusted",
                "candidate_id": "provider-mismatch",
                "round_index": 9,
                "source_manifest_hash": "e" * 64,
                "issues": (issue, issue.model_copy(update={"confidence": 0.95})),
            }
        )
    )

    normalized = normalize_critic_evaluation(
        raw,
        request=CriticInput(
            candidate=candidate,
            source_manifest=source,
            placement=PlacementIntent(
                x=0.1,
                y=0.1,
                width=0.2,
                height=0.2,
                pose="sitting",
                facing="left",
            ),
            canonical_prompt="test prompt",
            canonical_prompt_hash="f" * 64,
        ),
        expected_rubric_version=RUBRIC_VERSION,
    )

    assert normalized.rubric_version == RUBRIC_VERSION
    assert normalized.candidate_id == candidate.candidate_id
    assert normalized.round_index == candidate.round_index
    assert normalized.source_manifest_hash == source.manifest_hash
    assert len(normalized.issues) == 1
    assert normalized.issues[0].confidence == 0.95
    assert set(normalized.hard_constraint_failures) >= {
        "critic_candidate_id_mismatch",
        "critic_round_index_mismatch",
        "critic_source_manifest_mismatch",
        "critic_rubric_version_mismatch",
    }


def test_critic_request_key_fences_model_and_prompt_lineage() -> None:
    source = SourceManifest.create(
        background=AssetRef(
            asset_id="ast_background",
            path="/tmp/background.png",
            sha256="a" * 64,
            width=96,
            height=64,
        ),
        cat_references=[
            AssetRef(
                asset_id="ast_reference",
                path="/tmp/reference.png",
                sha256="b" * 64,
                width=32,
                height=32,
            )
        ],
    )
    candidate = CandidateRecord(
        candidate_id="candidate-key",
        round_index=0,
        variant_index=0,
        raw_asset=source.background,
        protected_asset=source.background,
        source_manifest_hash=source.manifest_hash,
        prompt_hash="c" * 64,
        request_key="d" * 64,
        model="fake",
        quality="medium",
        size="96x64",
    )
    request = CriticInput(
        candidate=candidate,
        source_manifest=source,
        placement=PlacementIntent(
            x=0.1,
            y=0.1,
            width=0.2,
            height=0.2,
            pose="sitting",
            facing="left",
        ),
        canonical_prompt="test prompt",
        canonical_prompt_hash="e" * 64,
    )
    base = CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=request,
        model="gpt-5.6-terra",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:test",
    )

    assert base != CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=request,
        model="gpt-5.6-sol",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:test",
    )
    protected_only_change = candidate.model_copy(
        update={"protected_asset": source.cat_references[0]}
    )
    assert base == CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=replace(request, candidate=protected_only_change),
        model="gpt-5.6-terra",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:test",
    )
    raw_change = candidate.model_copy(update={"raw_asset": source.cat_references[0]})
    assert base != CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=replace(request, candidate=raw_change),
        model="gpt-5.6-terra",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:test",
    )
    assert base != CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=request,
        model="gpt-5.6-terra",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:other-endpoint",
    )
    assert base != CriticEvaluationService.build_request_key(
        search_id="search-key",
        request=replace(request, canonical_prompt_hash="f" * 64),
        model="gpt-5.6-terra",
        rubric_version="critic-rubric/v1",
        provider_fingerprint="openai-responses:test",
    )
