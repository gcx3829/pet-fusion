"""Deterministic Critic contract used until a vision provider is enabled."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord
from app.domain.evaluations import (
    CandidateEvaluation,
    CriticIssue,
    DimensionScores,
    Severity,
)
from app.domain.searches import PlacementIntent
from app.persistence.app_store import AppStore
from app.services.proxy_builder import CriticProxyBundle

RUBRIC_VERSION = "critic-rubric/v1-fake"
FAKE_CRITIC_MODEL = "deterministic-critic-fixture/v1"
CRITIC_EVALUATION_SCHEMA_VERSION = "critic-evaluation/v2"
CRITIC_INPUT_SEMANTICS_VERSION = "raw-authority/v1"
CRITIC_PROVIDER_RESULT_POLL_SECONDS = 0.02
CRITIC_PROVIDER_RESULT_WAIT_SECONDS = 30.0
CRITIC_PROVIDER_CALL_LEASE_SECONDS = 30
CRITIC_PROVIDER_MAX_ATTEMPTS = 2
CRITIC_AUDIT_WRITE_MAX_ATTEMPTS = 3
CRITIC_USAGE_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_SUGGESTED_FIX_BOUNDARY = re.compile(r"(?:[;\r\n]+|(?<=[.!?])\s+)")


@dataclass(frozen=True, slots=True)
class CriticInput:
    """References and structured intent only; image bytes never enter graph state."""

    candidate: CandidateRecord
    source_manifest: SourceManifest
    placement: PlacementIntent
    canonical_prompt: str
    canonical_prompt_hash: str
    proxies: CriticProxyBundle | None = None


class CriticStructuredOutput(BaseModel):
    """Pydantic Structured Output returned by a multimodal Critic provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scores: DimensionScores
    issues: tuple[CriticIssue, ...] = Field(default_factory=tuple, max_length=20)
    no_meaningful_defect: bool
    identity_match: bool
    prompt_adherent: bool
    recommended_action: Literal["accept", "regenerate", "review", "none"]
    summary: str = Field(min_length=1, max_length=500)

    def to_evaluation(
        self, *, request: CriticInput, rubric_version: str
    ) -> CandidateEvaluation:
        return CandidateEvaluation(
            rubric_version=rubric_version,
            candidate_id=request.candidate.candidate_id,
            round_index=request.candidate.round_index,
            source_manifest_hash=request.source_manifest.manifest_hash,
            scores=self.scores,
            issues=self.issues,
            no_meaningful_defect=self.no_meaningful_defect,
            identity_match=self.identity_match,
            prompt_adherent=self.prompt_adherent,
            recommended_action=self.recommended_action,
            summary=self.summary,
        )


@dataclass(frozen=True, slots=True)
class CriticProviderResult:
    """Structured evaluation plus safe provider-call audit metadata."""

    evaluation: CandidateEvaluation | Mapping[str, object]
    provider_request_id: str | None = None
    provider_usage: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _OwnedCriticCallOutcome:
    """Result of one lease-owned provider call and its terminal audit write."""

    evaluation: CandidateEvaluation | None


def _safe_critic_usage(usage: Mapping[str, object] | None) -> dict[str, int | float]:
    """Retain finite numeric token totals only; provider text is never audited."""

    if usage is None:
        return {}
    sanitized: dict[str, int | float] = {}
    for field in CRITIC_USAGE_FIELDS:
        value = usage.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
            sanitized[field] = value
    return sanitized


def _safe_provider_request_id(value: str | None) -> str | None:
    if value is None or not 1 <= len(value) <= 200 or not value.isprintable():
        return None
    return value


@runtime_checkable
class CriticProvider(Protocol):
    """Synchronous provider boundary for one independent candidate evaluation."""

    model: str
    rubric_version: str
    provider_fingerprint: str

    def evaluate(
        self, request: CriticInput
    ) -> CandidateEvaluation | Mapping[str, object] | CriticProviderResult:
        """Return structured output only; image bytes remain in referenced assets."""


class CriticEvaluationService:
    """Idempotent boundary around one candidate-specific Critic provider call."""

    def __init__(self, *, provider: CriticProvider, app_store: AppStore) -> None:
        self.provider = provider
        self.app_store = app_store

    @staticmethod
    def build_request_key(
        *,
        search_id: str,
        request: CriticInput,
        model: str,
        rubric_version: str,
        provider_fingerprint: str,
    ) -> str:
        proxies = request.proxies
        payload = {
            "schema_version": CRITIC_EVALUATION_SCHEMA_VERSION,
            "operation": "critic_evaluate",
            "search_id": search_id,
            "candidate_id": request.candidate.candidate_id,
            "round_index": request.candidate.round_index,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "raw_asset_sha256": request.candidate.raw_authoritative_asset.sha256,
            "input_semantics_version": CRITIC_INPUT_SEMANTICS_VERSION,
            "canonical_prompt_hash": request.canonical_prompt_hash,
            "model": model,
            "rubric_version": rubric_version,
            "provider_fingerprint": provider_fingerprint,
            "proxy_asset_hashes": (
                {
                    "background": proxies.background_proxy.sha256,
                    "placement_overlay": proxies.placement_overlay_proxy.sha256,
                    "references": [item.sha256 for item in proxies.reference_proxies],
                    "candidate": proxies.candidate_proxy.sha256,
                }
                if proxies is not None
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def _renew_lease(self, *, request_key: str, owner_id: str) -> None:
        interval = CRITIC_PROVIDER_CALL_LEASE_SECONDS / 3
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.app_store.renew_provider_call_lease,
                request_key=request_key,
                owner_id=owner_id,
                lease_seconds=CRITIC_PROVIDER_CALL_LEASE_SECONDS,
            )
            if not renewed:
                return

    async def _complete_audit(
        self,
        *,
        request_key: str,
        owner_id: str,
        response: Mapping[str, object],
    ) -> None:
        """Retry the local write without ever repeating the paid provider call."""

        last_error: Exception | None = None
        for attempt in range(CRITIC_AUDIT_WRITE_MAX_ATTEMPTS):
            try:
                completed = await asyncio.to_thread(
                    self.app_store.complete_provider_call,
                    request_key,
                    response,
                    owner_id=owner_id,
                )
                if completed:
                    return
                existing = await asyncio.to_thread(
                    self.app_store.get_provider_call, request_key
                )
                if existing is not None and existing[0] == "completed":
                    return
                last_error = RuntimeError("Critic provider-call lease was lost before audit")
            except Exception as exc:
                last_error = exc
            if attempt + 1 < CRITIC_AUDIT_WRITE_MAX_ATTEMPTS:
                await asyncio.sleep(CRITIC_PROVIDER_RESULT_POLL_SECONDS)
        raise RuntimeError("Unable to persist the completed Critic provider audit") from last_error

    async def _execute_owned_call(
        self,
        *,
        request: CriticInput,
        request_key: str,
        owner_id: str,
        model: str,
        rubric_version: str,
    ) -> _OwnedCriticCallOutcome:
        """Run provider, normalization, and terminal audit under one lease owner.

        The caller shields this whole coroutine. A cancellation must never split a
        paid provider response from the audit record that makes replay idempotent.
        """

        try:
            provider_result = await asyncio.to_thread(self.provider.evaluate, request)
            if isinstance(provider_result, CriticProviderResult):
                raw = provider_result.evaluation
                provider_request_id = _safe_provider_request_id(
                    provider_result.provider_request_id
                )
                provider_usage = _safe_critic_usage(provider_result.provider_usage)
            else:
                raw = provider_result
                provider_request_id = None
                provider_usage = {}
            evaluation = normalize_critic_evaluation(
                raw,
                request=request,
                expected_rubric_version=rubric_version,
            )
        except Exception as exc:
            await asyncio.to_thread(
                self.app_store.fail_provider_call,
                request_key,
                type(exc).__name__,
                owner_id=owner_id,
            )
            return _OwnedCriticCallOutcome(evaluation=None)

        response: dict[str, object] = {
            "evaluation": evaluation.model_dump(mode="json"),
            "provider": {
                "request_id": provider_request_id,
                "usage": provider_usage,
                "model": model,
            },
        }
        await self._complete_audit(
            request_key=request_key,
            owner_id=owner_id,
            response=response,
        )
        return _OwnedCriticCallOutcome(evaluation=evaluation)

    @staticmethod
    async def _await_owned_call(
        task: asyncio.Task[_OwnedCriticCallOutcome],
    ) -> tuple[_OwnedCriticCallOutcome, bool]:
        """Honor cancellation only after the lease-owned task reaches an audit state."""

        cancel_requested = False
        while True:
            try:
                return await asyncio.shield(task), cancel_requested
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancel_requested = True

    @staticmethod
    def _completed_evaluation(
        response: dict[str, object],
        *,
        request: CriticInput,
        expected_rubric_version: str,
    ) -> CandidateEvaluation:
        payload = response.get("evaluation")
        if not isinstance(payload, dict):
            raise RuntimeError("Completed Critic provider audit has an invalid response shape")
        return normalize_critic_evaluation(
            payload,
            request=request,
            expected_rubric_version=expected_rubric_version,
        )

    async def evaluate(self, *, search_id: str, request: CriticInput) -> CandidateEvaluation:
        """Evaluate with bounded retries and reuse one stable paid-call lineage."""

        model = self.provider.model
        rubric_version = self.provider.rubric_version
        provider_fingerprint = self.provider.provider_fingerprint
        request_key = self.build_request_key(
            search_id=search_id,
            request=request,
            model=model,
            rubric_version=rubric_version,
            provider_fingerprint=provider_fingerprint,
        )
        audit_payload: dict[str, object] = {
            "schema_version": CRITIC_EVALUATION_SCHEMA_VERSION,
            "candidate_id": request.candidate.candidate_id,
            "round_index": request.candidate.round_index,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "raw_asset_id": request.candidate.raw_authoritative_asset.asset_id,
            "raw_asset_sha256": request.candidate.raw_authoritative_asset.sha256,
            "input_semantics_version": CRITIC_INPUT_SEMANTICS_VERSION,
            "canonical_prompt_hash": request.canonical_prompt_hash,
            "model": model,
            "rubric_version": rubric_version,
            "provider_fingerprint": provider_fingerprint,
            "proxy_asset_ids": (
                {
                    "background": request.proxies.background_proxy.asset_id,
                    "placement_overlay": request.proxies.placement_overlay_proxy.asset_id,
                    "references": [item.asset_id for item in request.proxies.reference_proxies],
                    "candidate": request.proxies.candidate_proxy.asset_id,
                }
                if request.proxies is not None
                else None
            ),
        }
        while True:
            owner_id = f"critic_{uuid4().hex}"
            claimed, status, completed = await asyncio.to_thread(
                self.app_store.claim_provider_call,
                request_key=request_key,
                operation="critic_evaluate",
                search_id=search_id,
                request_payload=audit_payload,
                owner_id=owner_id,
                lease_seconds=CRITIC_PROVIDER_CALL_LEASE_SECONDS,
                max_attempts=CRITIC_PROVIDER_MAX_ATTEMPTS,
            )
            if status == "completed" and completed is not None:
                try:
                    return self._completed_evaluation(
                        completed,
                        request=request,
                        expected_rubric_version=rubric_version,
                    )
                except (TypeError, ValueError, RuntimeError):
                    return unavailable_critic_evaluation(
                        request=request,
                        rubric_version=rubric_version,
                    )
            if not claimed:
                attempts = await asyncio.to_thread(
                    self.app_store.provider_attempt_count, request_key
                )
                if status == "failed_retryable" and attempts >= CRITIC_PROVIDER_MAX_ATTEMPTS:
                    return unavailable_critic_evaluation(
                        request=request,
                        rubric_version=rubric_version,
                    )
                deadline = (
                    asyncio.get_running_loop().time() + CRITIC_PROVIDER_RESULT_WAIT_SECONDS
                )
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(CRITIC_PROVIDER_RESULT_POLL_SECONDS)
                    claimed, status, completed = await asyncio.to_thread(
                        self.app_store.claim_provider_call,
                        request_key=request_key,
                        operation="critic_evaluate",
                        search_id=search_id,
                        request_payload=audit_payload,
                        owner_id=owner_id,
                        lease_seconds=CRITIC_PROVIDER_CALL_LEASE_SECONDS,
                        max_attempts=CRITIC_PROVIDER_MAX_ATTEMPTS,
                    )
                    if status == "completed" and completed is not None:
                        try:
                            return self._completed_evaluation(
                                completed,
                                request=request,
                                expected_rubric_version=rubric_version,
                            )
                        except (TypeError, ValueError, RuntimeError):
                            return unavailable_critic_evaluation(
                                request=request,
                                rubric_version=rubric_version,
                            )
                    if claimed:
                        break
                    attempts = await asyncio.to_thread(
                        self.app_store.provider_attempt_count, request_key
                    )
                    if (
                        status == "failed_retryable"
                        and attempts >= CRITIC_PROVIDER_MAX_ATTEMPTS
                    ):
                        return unavailable_critic_evaluation(
                            request=request,
                            rubric_version=rubric_version,
                        )
                else:
                    return unavailable_critic_evaluation(
                        request=request,
                        rubric_version=rubric_version,
                    )

            heartbeat = asyncio.create_task(
                self._renew_lease(request_key=request_key, owner_id=owner_id)
            )
            owned_call = asyncio.create_task(
                self._execute_owned_call(
                    request=request,
                    request_key=request_key,
                    owner_id=owner_id,
                    model=model,
                    rubric_version=rubric_version,
                )
            )
            try:
                outcome, cancel_requested = await self._await_owned_call(owned_call)
                if cancel_requested:
                    raise asyncio.CancelledError
                if outcome.evaluation is not None:
                    return outcome.evaluation
                attempts = await asyncio.to_thread(
                    self.app_store.provider_attempt_count, request_key
                )
                if attempts >= CRITIC_PROVIDER_MAX_ATTEMPTS:
                    return unavailable_critic_evaluation(
                        request=request,
                        rubric_version=rubric_version,
                    )
                continue
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise


def unavailable_critic_evaluation(
    *, request: CriticInput, rubric_version: str
) -> CandidateEvaluation:
    """Create a deterministic non-scoring result after the bounded call budget."""

    unavailable_scores = DimensionScores(
        cat_identity=0,
        pose_geometry=0,
        perspective_scale=0,
        lighting_color=0,
        optical_consistency=0,
        physical_integration=0,
        scene_preservation=0,
        overall_photographic_naturalness=0,
    )
    return CandidateEvaluation(
        rubric_version=rubric_version,
        candidate_id=request.candidate.candidate_id,
        round_index=request.candidate.round_index,
        source_manifest_hash=request.source_manifest.manifest_hash,
        scores=unavailable_scores,
        issues=(),
        no_meaningful_defect=False,
        identity_match=False,
        prompt_adherent=False,
        recommended_action="none",
        summary="Critic evaluation unavailable after bounded provider attempts.",
        hard_constraint_failures=("evaluation_unavailable",),
    )


def _single_action_suggested_fix(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    first_action = _SUGGESTED_FIX_BOUNDARY.split(normalized, maxsplit=1)[0].strip()
    return first_action or None


def normalize_critic_evaluation(
    value: CandidateEvaluation | Mapping[str, object],
    *,
    request: CriticInput,
    expected_rubric_version: str | None = None,
) -> CandidateEvaluation:
    """Apply local identity, lineage, and issue normalization to provider output.

    Provider output is untrusted with respect to task routing. The dispatched
    candidate, round, and immutable source manifest are authoritative locally;
    any mismatch becomes a deterministic hard-constraint marker rather than being
    allowed to overwrite another candidate's evaluation.
    """

    evaluation = CandidateEvaluation.model_validate(value)
    expected_candidate_id = request.candidate.candidate_id
    expected_round_index = request.candidate.round_index
    expected_manifest_hash = request.source_manifest.manifest_hash
    resolved_rubric_version = expected_rubric_version or evaluation.rubric_version
    failures = list(evaluation.hard_constraint_failures)
    if evaluation.rubric_version != resolved_rubric_version:
        failures.append("critic_rubric_version_mismatch")
    if evaluation.candidate_id != expected_candidate_id:
        failures.append("critic_candidate_id_mismatch")
    if evaluation.round_index != expected_round_index:
        failures.append("critic_round_index_mismatch")
    if evaluation.source_manifest_hash not in {None, expected_manifest_hash}:
        failures.append("critic_source_manifest_mismatch")

    deduplicated_issues: dict[str, CriticIssue] = {}
    for raw_issue in evaluation.issues:
        issue = raw_issue.model_copy(
            update={
                "suggested_fix": _single_action_suggested_fix(raw_issue.suggested_fix),
            }
        )
        existing = deduplicated_issues.get(issue.issue_id)
        if existing is None or issue.confidence > existing.confidence:
            deduplicated_issues[issue.issue_id] = issue
    normalized_issues = tuple(
        deduplicated_issues[issue_id] for issue_id in sorted(deduplicated_issues)
    )
    semantic_conflict = evaluation.semantic_conflict or (
        evaluation.no_meaningful_defect
        and any(issue.severity is Severity.BLOCKING for issue in normalized_issues)
    )
    return evaluation.model_copy(
        update={
            "rubric_version": resolved_rubric_version,
            "candidate_id": expected_candidate_id,
            "round_index": expected_round_index,
            "source_manifest_hash": expected_manifest_hash,
            "issues": normalized_issues,
            "semantic_conflict": semantic_conflict,
            "hard_constraint_failures": tuple(dict.fromkeys(failures)),
        }
    )


class DeterministicCriticService:
    """A reproducible local fixture for Critic-shaped outputs.

    The service deliberately uses the candidate asset dimensions and a hash-derived
    fixture bias. It is useful for exercising reducers and policies without making
    claims about the quality of a future vision model.
    """

    model = FAKE_CRITIC_MODEL
    rubric_version = RUBRIC_VERSION
    provider_fingerprint = "offline-deterministic-critic/v1"

    @staticmethod
    def _read_dimensions(path: Path) -> tuple[int, int] | None:
        try:
            with Image.open(path) as image:
                return image.size
        except (OSError, UnidentifiedImageError):
            return None

    def evaluate(self, request: CriticInput) -> CandidateEvaluation:
        candidate = request.candidate
        background = request.source_manifest.background
        candidate_size = self._read_dimensions(candidate.raw_authoritative_asset.filesystem_path)
        expected_size = (background.width, background.height)
        digest = hashlib.sha256(
            f"{candidate.candidate_id}:{candidate.request_key}:{RUBRIC_VERSION}".encode()
        ).digest()
        fixture_bias = (digest[0] % 11) - 5

        scene_score = 96.0 if candidate_size == expected_size else 20.0
        identity_score = float(max(0, min(100, 92 + fixture_bias)))
        pose_score = float(max(0, min(100, 90 + (digest[1] % 9) - 4)))
        perspective_score = float(max(0, min(100, 91 + (digest[2] % 7) - 3)))
        lighting_score = float(max(0, min(100, 89 + (digest[3] % 9) - 4)))
        optical_score = float(max(0, min(100, 90 + (digest[4] % 7) - 3)))
        physical_score = float(max(0, min(100, 88 + (digest[5] % 9) - 4)))
        naturalness_score = float(max(0, min(100, 90 + (digest[6] % 7) - 3)))

        issues: list[CriticIssue] = []
        hard_failures: list[str] = []
        if candidate_size is None:
            issues.append(
                CriticIssue(
                    issue_id="asset_unreadable",
                    category="asset_integrity",
                    severity=Severity.BLOCKING,
                    evidence="The raw candidate asset cannot be decoded locally.",
                    suggested_fix="Regenerate the candidate as a valid PNG asset.",
                    confidence=1.0,
                )
            )
            hard_failures.append("asset_unreadable")
        elif candidate_size != expected_size:
            issues.append(
                CriticIssue(
                    issue_id="scene_dimensions_mismatch",
                    category="scene_preservation",
                    severity=Severity.BLOCKING,
                    evidence=(
                        "Raw candidate dimensions "
                        f"{candidate_size} differ from source {expected_size}."
                    ),
                    suggested_fix="Regenerate at the immutable source background dimensions.",
                    confidence=1.0,
                )
            )
            hard_failures.append("scene_dimensions_mismatch")

        identity_match = identity_score >= 75 and not hard_failures
        if not identity_match:
            hard_failures.append("identity_mismatch")

        scores = DimensionScores(
            cat_identity=identity_score,
            pose_geometry=pose_score,
            perspective_scale=perspective_score,
            lighting_color=lighting_score,
            optical_consistency=optical_score,
            physical_integration=physical_score,
            scene_preservation=scene_score,
            overall_photographic_naturalness=naturalness_score,
        )
        blocking = any(issue.severity is Severity.BLOCKING for issue in issues)
        return CandidateEvaluation(
            rubric_version=RUBRIC_VERSION,
            candidate_id=candidate.candidate_id,
            round_index=candidate.round_index,
            source_manifest_hash=request.source_manifest.manifest_hash,
            scores=scores,
            issues=tuple(issues),
            no_meaningful_defect=not blocking,
            identity_match=identity_match,
            prompt_adherent=True,
            recommended_action="regenerate" if blocking else "review",
            summary=(
                "Local fixture found no blocking defect."
                if not blocking
                else "Local fixture found a blocking asset or scene defect."
            ),
            hard_constraint_failures=tuple(dict.fromkeys(hard_failures)),
        )
