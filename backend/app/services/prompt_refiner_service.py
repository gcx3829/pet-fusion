"""Checkpoint-safe multimodal Prompt Refiner service.

The refiner is deliberately independent from ``SearchGraph``.  It turns an
immutable source bundle (and, for a human revision, one selected raw
candidate) into a validated :class:`PromptVersion`.  The provider boundary
returns a structured proposal; all lineage, hashing, prompt compilation and
provider-call idempotency remain local and deterministic.

Image bytes are never part of ``PromptRefinerRequest`` or any result that can
be placed in a LangGraph checkpoint.  The official adapter may materialise
bounded data URLs only while it is executing one provider call.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.assets import AssetRef, SourceManifest
from app.domain.directives import stable_directives_hash
from app.domain.errors import ConfigurationError
from app.domain.evaluations import CandidateEvaluation
from app.domain.prompts import (
    PROMPT_PLAN_SCHEMA_VERSION,
    PromptGenerationMode,
    PromptPlanProposal,
    PromptRefinementMode,
    PromptVersion,
    VisualAnchorRef,
)
from app.domain.searches import SearchStatus
from app.graphs.state import assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.asset_store import AssetStore
from app.services.prompt_compiler import compile_generation_prompt

PROMPT_REFINER_SCHEMA_VERSION: Final[Literal["prompt-refiner-request/v1"]] = (
    "prompt-refiner-request/v1"
)
PROMPT_REFINER_PROXY_SCHEMA_VERSION: Final[Literal["prompt-refiner-proxy/v1"]] = (
    "prompt-refiner-proxy/v1"
)
PROMPT_REFINER_TEMPLATE_VERSION: Final[str] = "multimodal-prompt-refiner/v1"
PROMPT_REFINER_OPERATION: Final[str] = "prompt_refine"
PROMPT_REFINER_PROXY_MAX_SIDE: Final[int] = 1536
PROMPT_REFINER_PROXY_REFERENCE_LIMIT: Final[int] = 5
PROMPT_REFINER_PROVIDER_CALL_LEASE_SECONDS: Final[int] = 5
PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS: Final[int] = 2
PROMPT_REFINER_PROVIDER_RESULT_POLL_SECONDS: Final[float] = 0.02
PROMPT_REFINER_PROVIDER_RESULT_WAIT_SECONDS: Final[float] = 30.0
PROMPT_REFINER_AUDIT_WRITE_MAX_ATTEMPTS: Final[int] = 3
PROMPT_REFINER_USAGE_FIELDS: Final[frozenset[str]] = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "cached_tokens"}
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_hash(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256_text(encoded)


def _asset_lineage(asset: AssetRef) -> dict[str, object]:
    """Return auditable asset identity without a deployment path."""

    return {
        "asset_id": asset.asset_id,
        "sha256": asset.sha256,
        "mime_type": asset.mime_type,
        "width": asset.width,
        "height": asset.height,
    }


def _safe_provider_request_id(value: str | None) -> str | None:
    if value is None or not 1 <= len(value) <= 200 or not value.isprintable():
        return None
    return value


def _safe_usage(value: Mapping[str, object] | None) -> dict[str, int | float]:
    """Keep only finite numeric usage fields; provider text is never audited."""

    if value is None:
        return {}
    result: dict[str, int | float] = {}
    for field in PROMPT_REFINER_USAGE_FIELDS:
        item = value.get(field)
        if isinstance(item, bool):
            continue
        if isinstance(item, int) or (isinstance(item, float) and math.isfinite(item)):
            result[field] = item
    return result


class PromptRefinerError(RuntimeError):
    """Base error for a bounded Prompt Refiner operation."""


class PromptRefinerAuditError(PromptRefinerError):
    """A completed provider audit cannot be safely replayed."""


class PromptRefinerRequest(BaseModel):
    """Structured request accepted by the Prompt Refiner boundary.

    ``source_manifest`` and the optional visual anchor contain only asset
    references.  The revision contract requires the selected candidate, its
    structured evaluation, and the parent prompt version together. Human
    feedback is optional so selecting a candidate alone is a valid re-sampling
    instruction, while any supplied feedback remains bound to that candidate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["prompt-refiner-request/v1"] = PROMPT_REFINER_SCHEMA_VERSION
    search_id: str = Field(min_length=1, max_length=120)
    mode: PromptRefinementMode
    round_index: int = Field(ge=0)
    source_manifest: SourceManifest
    guidance_mask: AssetRef
    user_intent: str = Field(min_length=1, max_length=2_000)
    human_feedback: str | None = Field(default=None, max_length=2_000)
    human_selected_candidate_id: str | None = Field(default=None, max_length=120)
    visual_anchor: VisualAnchorRef | None = None
    selected_candidate_evaluation: CandidateEvaluation | None = None
    parent_prompt_version: PromptVersion | None = None
    prompt_template_version: str = Field(
        default=PROMPT_REFINER_TEMPLATE_VERSION, min_length=1, max_length=120
    )
    prompt_schema_version: str = Field(
        default=PROMPT_PLAN_SCHEMA_VERSION, min_length=1, max_length=120
    )
    generation_model: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def validate_lineage(self) -> PromptRefinerRequest:
        self.source_manifest.assert_integrity()
        guidance = self.guidance_mask
        if guidance.mime_type != "image/png":
            raise ValueError("Prompt Refiner guidance_mask must be a PNG asset")
        if (guidance.width, guidance.height) != (
            self.source_manifest.background.width,
            self.source_manifest.background.height,
        ):
            raise ValueError("Prompt Refiner guidance_mask must match the source background")

        if self.mode is PromptRefinementMode.INITIAL:
            if self.round_index != 0:
                raise ValueError("initial Prompt Refiner requests must belong to round 0")
            if any(
                value is not None
                for value in (
                    self.human_feedback,
                    self.human_selected_candidate_id,
                    self.visual_anchor,
                    self.selected_candidate_evaluation,
                    self.parent_prompt_version,
                )
            ):
                raise ValueError("initial Prompt Refiner requests cannot contain revision lineage")
            return self

        if self.round_index < 1:
            raise ValueError("revision Prompt Refiner requests must belong to a later round")
        if self.parent_prompt_version is None:
            raise ValueError("revision Prompt Refiner requests require a parent PromptVersion")
        if self.visual_anchor is None:
            raise ValueError("revision Prompt Refiner requests require a raw visual anchor")
        if self.selected_candidate_evaluation is None:
            raise ValueError("revision Prompt Refiner requests require the selected Critic result")
        # A selected raw candidate is itself a valid human revision signal.
        # Feedback is optional so the UI can request "keep this candidate and
        # re-sample" while the Critic result still gives the multimodal model
        # bounded evidence.  Older callers that supplied feedback remain fully
        # compatible.
        anchor = self.visual_anchor
        if anchor.search_id != self.search_id:
            raise ValueError("Prompt Refiner visual anchor search does not match the request")
        if anchor.round_index != self.round_index - 1:
            raise ValueError("Prompt Refiner visual anchor must belong to the previous round")
        if anchor.source_manifest_hash != self.source_manifest.manifest_hash:
            raise ValueError("Prompt Refiner visual anchor source manifest does not match")
        if self.human_selected_candidate_id != anchor.candidate_id:
            raise ValueError("Prompt Refiner selected candidate must match the visual anchor")
        evaluation = self.selected_candidate_evaluation
        if evaluation.candidate_id != anchor.candidate_id:
            raise ValueError("Prompt Refiner Critic result must match the visual anchor candidate")
        if evaluation.round_index != anchor.round_index:
            raise ValueError("Prompt Refiner Critic result round does not match the anchor")
        if evaluation.source_manifest_hash not in {
            None,
            self.source_manifest.manifest_hash,
        }:
            raise ValueError("Prompt Refiner Critic result source manifest does not match")
        parent = self.parent_prompt_version
        if parent.round_index != self.round_index - 1:
            raise ValueError(
                "Prompt Refiner parent PromptVersion must belong to the previous round"
            )
        if parent.search_id not in {None, self.search_id}:
            raise ValueError("Prompt Refiner parent PromptVersion search does not match")
        if parent.source_manifest_hash not in {
            None,
            self.source_manifest.manifest_hash,
        }:
            raise ValueError("Prompt Refiner parent PromptVersion source does not match")
        return self

    @property
    def critic_summary_hash(self) -> str | None:
        evaluation = self.selected_candidate_evaluation
        return _sha256_text(evaluation.summary) if evaluation is not None else None

    @property
    def critic_evaluation_hash(self) -> str | None:
        evaluation = self.selected_candidate_evaluation
        return _stable_hash(evaluation) if evaluation is not None else None

    @property
    def is_checkpoint_safe(self) -> bool:
        assert_checkpoint_safe(self.model_dump(mode="json"))
        return True


class PromptRefinerProxyBundle(BaseModel):
    """Bounded local image references prepared for one adapter invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["prompt-refiner-proxy/v1"] = PROMPT_REFINER_PROXY_SCHEMA_VERSION
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    background_proxy: AssetRef
    reference_proxies: tuple[AssetRef, ...] = Field(min_length=1, max_length=5)
    guidance_proxy: AssetRef
    anchor_proxy: AssetRef | None = None
    anchor_candidate_id: str | None = None

    @model_validator(mode="after")
    def validate_anchor_shape(self) -> PromptRefinerProxyBundle:
        if (self.anchor_proxy is None) != (self.anchor_candidate_id is None):
            raise ValueError("Prompt Refiner proxy anchor asset and candidate ID must be paired")
        return self

    @property
    def is_checkpoint_safe(self) -> bool:
        assert_checkpoint_safe(self.model_dump(mode="json"))
        return True


class PromptRefinerProxyBuilder:
    """Create deterministic bounded proxies while retaining only asset refs."""

    def __init__(
        self,
        *,
        asset_store: AssetStore,
        max_side: int = PROMPT_REFINER_PROXY_MAX_SIDE,
        reference_limit: int = PROMPT_REFINER_PROXY_REFERENCE_LIMIT,
    ) -> None:
        if max_side <= 0:
            raise ValueError("Prompt Refiner proxy max_side must be positive")
        if not 1 <= reference_limit <= 5:
            raise ValueError("Prompt Refiner reference_limit must be between 1 and 5")
        self.asset_store = asset_store
        self.max_side = max_side
        self.reference_limit = reference_limit

    @property
    def fingerprint(self) -> str:
        """Hash every proxy semantic that can affect model-visible pixels."""

        return _stable_hash(
            {
                "schema_version": PROMPT_REFINER_PROXY_SCHEMA_VERSION,
                "max_side": self.max_side,
                "reference_limit": self.reference_limit,
                "format": "png",
                "resize": "lanczos",
                "exif_orientation": "transpose",
                "alpha": "preserve",
            }
        )

    @staticmethod
    def _png_bytes(image: Image.Image) -> bytes:
        output = io.BytesIO()
        image.save(output, format="PNG", compress_level=9, optimize=False)
        return output.getvalue()

    def _store_proxy(self, image: Image.Image) -> AssetRef:
        # Proxies are call-local derived cache files, not user-visible assets.
        # They remain content-addressed for reuse but deliberately do not enter
        # app SQLite or a LangGraph checkpoint.
        return self.asset_store.put_image_bytes(self._png_bytes(image))

    def _proxy_asset(self, asset: AssetRef, *, preserve_alpha: bool = False) -> AssetRef:
        self.asset_store.assert_intact(asset)
        try:
            with Image.open(asset.filesystem_path) as opened:
                oriented = ImageOps.exif_transpose(opened)
                has_alpha = preserve_alpha or "A" in oriented.getbands()
                image = oriented.convert("RGBA" if has_alpha else "RGB")
                if max(image.size) > self.max_side:
                    scale = self.max_side / max(image.size)
                    image = image.resize(
                        (
                            max(1, round(image.width * scale)),
                            max(1, round(image.height * scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                return self._store_proxy(image)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise PromptRefinerError(
                "Prompt Refiner proxy source is not a decodable image"
            ) from exc

    def build(self, request: PromptRefinerRequest) -> PromptRefinerProxyBundle:
        request.source_manifest.assert_integrity()
        background_proxy = self._proxy_asset(request.source_manifest.background)
        references = tuple(
            self._proxy_asset(reference)
            for reference in request.source_manifest.cat_references[: self.reference_limit]
        )
        guidance_proxy = self._proxy_asset(request.guidance_mask, preserve_alpha=True)
        anchor_proxy = (
            self._proxy_asset(request.visual_anchor.raw_asset)
            if request.visual_anchor is not None
            else None
        )
        return PromptRefinerProxyBundle(
            source_manifest_hash=request.source_manifest.manifest_hash,
            background_proxy=background_proxy,
            reference_proxies=references,
            guidance_proxy=guidance_proxy,
            anchor_proxy=anchor_proxy,
            anchor_candidate_id=(
                request.visual_anchor.candidate_id if request.visual_anchor is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class PromptRefinerProviderResult:
    """Untrusted structured provider response plus safe transport metadata."""

    proposal: PromptPlanProposal
    provider_request_id: str | None = None
    provider_usage: Mapping[str, object] | None = None


@runtime_checkable
class PromptRefinerProvider(Protocol):
    model: str
    provider_fingerprint: str
    schema_version: str
    proxy_version: str

    def refine(
        self,
        request: PromptRefinerRequest,
        proxies: PromptRefinerProxyBundle | None = None,
        *,
        request_key: str | None = None,
    ) -> PromptPlanProposal | PromptRefinerProviderResult:
        """Return a structured proposal; image bytes stay inside the adapter."""


def _plan_section(title: str, clauses: Sequence[str]) -> list[str]:
    if not clauses:
        return []
    return [title, *(f"- {clause}" for clause in clauses)]


def compile_professional_prompt(
    *, request: PromptRefinerRequest, proposal: PromptPlanProposal
) -> tuple[str, str]:
    """Compile provider plan sections into stable canonical/generation prompts."""

    plan = proposal.plan
    reference_count = len(request.source_manifest.cat_references)
    lines: list[str] = [f"TEMPLATE VERSION: {request.prompt_template_version}"]
    # Input authority and ordering are local invariants, not provider-authored
    # prose.  Keeping this section deterministic prevents a structured model
    # response from relabelling the selected candidate as the immutable base.
    role = ["Image 1 is the immutable original travel photograph and base scene."]
    if request.mode is PromptRefinementMode.REVISION:
        role.append(
            "The next image is the human-selected raw candidate used only as a visual anchor; "
            "the immutable original remains the base scene.",
        )
        role.append(
            f"The following {reference_count} images are identity references for the requested "
            "pet or pets; they may show one animal from multiple views or multiple distinct "
            "animals. Infer grouping from visible evidence and the photographer's direction."
        )
    else:
        role.append(
            f"Images 2..{reference_count + 1} are identity references for the requested pet or "
            "pets; they may show one animal from multiple views or multiple distinct animals. "
            "Infer grouping from visible evidence and the photographer's direction."
        )
    role.append(
        "The Guidance Mask is a soft focus guide for the immutable background, not a pixel lock."
    )
    lines.extend(_plan_section("ROLE OF INPUTS", role))
    lines.extend(_plan_section("TASK", (plan.task,)))
    lines.extend(_plan_section("IDENTITY INVARIANTS", plan.identity_invariants))
    lines.extend(_plan_section("PLACEMENT AND PHOTOGRAPHER INTENT", plan.placement))
    lines.extend(_plan_section("PHOTOGRAPHIC INTEGRATION", plan.photographic_integration))
    lines.extend(_plan_section("SCENE PRESERVATION", plan.scene_preservation))
    if request.mode is PromptRefinementMode.REVISION:
        lines.extend(_plan_section("PRESERVE FROM SELECTED RAW ANCHOR", plan.preserve_from_anchor))
        lines.extend(_plan_section("CHANGE FROM SELECTED RAW ANCHOR", plan.change_from_anchor))
    lines.extend(_plan_section("OUTPUT", (plan.output,)))
    canonical = "\n".join(lines)
    return canonical, _sha256_text(canonical)


class PromptRefinerResult(BaseModel):
    """Validated prompt lineage returned to a graph/API caller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: PromptVersion
    proposal: PromptPlanProposal
    provider_request_id: str | None = None
    provider_usage: dict[str, int | float] = Field(default_factory=dict)
    replayed: bool = False

    @property
    def is_checkpoint_safe(self) -> bool:
        assert_checkpoint_safe(self.model_dump(mode="json"))
        return True


class DeterministicFakePromptRefiner:
    """Offline deterministic provider producing a bounded professional plan."""

    model = "deterministic-prompt-refiner/v1"
    schema_version: str = PROMPT_REFINER_SCHEMA_VERSION
    proxy_version: str = PROMPT_REFINER_PROXY_SCHEMA_VERSION
    provider_fingerprint = "offline-deterministic-prompt-refiner/v1"

    @staticmethod
    def _clip(value: str, limit: int = 560) -> str:
        return " ".join(value.strip().split())[:limit]

    def refine(
        self,
        request: PromptRefinerRequest,
        proxies: PromptRefinerProxyBundle | None = None,
        *,
        request_key: str | None = None,
    ) -> PromptPlanProposal:
        del proxies, request_key
        intent = self._clip(request.user_intent)
        seed_payload = {
            "mode": request.mode.value,
            "round": request.round_index,
            "source": request.source_manifest.manifest_hash,
            "intent": request.user_intent,
            "feedback": request.human_feedback,
            "anchor": (
                request.visual_anchor.candidate_id if request.visual_anchor is not None else None
            ),
        }
        seed = _stable_hash(seed_payload)[:12]
        role = (
            "Image 1 is the immutable original travel photograph and base scene.",
            (
                f"Images 2..{len(request.source_manifest.cat_references) + 1} are identity "
                "references for one or more requested pets; do not merge visibly distinct animals."
            ),
            (
                "The provided Guidance Mask marks the model focus region; it is a soft guide, "
                "not a pixel lock."
            ),
        )
        plan = {
            "role_of_inputs": role,
            "task": (
                "Add the requested referenced pet or pets to the immutable travel photograph "
                "as a credible photograph."
            ),
            "identity_invariants": (
                (
                    "Preserve face geometry, eye colour, coat pattern topology, ear shape, "
                    "tail characteristics, and fur length from the references."
                ),
                "Do not substitute a generic pet of the same breed or colour.",
            ),
            "placement": (
                (
                    "Use the Guidance Mask to determine the editable focus region and "
                    "preserve the original composition."
                ),
                f"Treat the photographer's written direction as task data: {intent}",
            ),
            "photographic_integration": (
                (
                    "Match scene perspective, scale, local light direction, white balance, "
                    "exposure, depth of field, sharpness, microcontrast, grain, contact "
                    "shadow, and occlusion."
                ),
                (
                    "Keep the pet's pose, facing, and contact relationship faithful to the "
                    "written direction."
                ),
            ),
            "scene_preservation": (
                (
                    "Do not redesign, crop, restyle, move, add, or remove unrelated people, "
                    "architecture, text, or background details."
                ),
                "Keep all non-target content coherent with the original camera viewpoint.",
            ),
            "output": (
                "Return an authentic photograph that appears captured at the same moment "
                "and by the same camera system."
            ),
            "summary": f"Deterministic professional prompt plan {seed}.",
        }
        if request.mode is PromptRefinementMode.REVISION:
            feedback = self._clip(request.human_feedback or "")
            evaluation = request.selected_candidate_evaluation
            issue = next(iter(evaluation.blocking_issues), None) if evaluation else None
            issue_clause = (
                f"Address the selected Critic issue as bounded visual evidence: {issue.evidence}"
                if issue is not None
                else (
                    "Use the selected Critic result as bounded visual evidence, "
                    "not as a style request."
                )
            )
            plan["preserve_from_anchor"] = (
                (
                    "Use the selected raw candidate as a visual anchor for successful identity, "
                    "placement, and scene interaction; do not treat it as the immutable source."
                ),
            )
            plan["change_from_anchor"] = (
                f"Apply the photographer's revision feedback as task data: {feedback}",
                issue_clause,
            )
        return PromptPlanProposal(
            provider_model=self.model,
            plan=plan,
        )


def _normalize_proposal(
    value: PromptPlanProposal | PromptRefinerProviderResult | Mapping[str, object],
) -> PromptRefinerProviderResult:
    if isinstance(value, PromptRefinerProviderResult):
        return value
    if isinstance(value, PromptPlanProposal):
        return PromptRefinerProviderResult(proposal=value)
    return PromptRefinerProviderResult(proposal=PromptPlanProposal.model_validate(value))


class PromptRefinerService:
    """Idempotent, lease-owned service around one Prompt Refiner call."""

    def __init__(
        self,
        *,
        provider: PromptRefinerProvider,
        app_store: AppStore,
        asset_store: AssetStore,
        proxy_builder: PromptRefinerProxyBuilder | None = None,
    ) -> None:
        self.provider = provider
        self.app_store = app_store
        self.asset_store = asset_store
        self.proxy_builder = proxy_builder or PromptRefinerProxyBuilder(
            asset_store=asset_store,
        )

    @staticmethod
    def apply_local_directives(
        *,
        parent: PromptVersion,
        search_id: str,
        source_manifest_hash: str,
        round_index: int,
        active_directives: Sequence[object] = (),
        human_feedback: str | None = None,
        generation_model: str | None = None,
    ) -> PromptVersion:
        """Create a source-only PromptVersion without another paid call.

        Automatic Planner rounds deliberately reuse the multimodal base plan
        and apply only the bounded active directives locally.  A human
        continue without an explicit candidate uses the same compatibility
        path, with optional feedback appended once.  This helper creates a
        fresh lineage version so the generator can authorize the exact prompt
        for the target round without pretending that a previous candidate is
        an image input.
        """

        from app.domain.directives import PlannerDirective

        if round_index < 1 or parent.round_index != round_index - 1:
            raise ValueError(
                "Local PromptVersion parent must belong to the immediately previous round"
            )
        if parent.search_id not in {None, search_id}:
            raise ValueError("Local PromptVersion parent search lineage does not match")
        if parent.source_manifest_hash not in {None, source_manifest_hash}:
            raise ValueError("Local PromptVersion parent source lineage does not match")

        directives = tuple(
            sorted(
                (PlannerDirective.model_validate(item) for item in active_directives),
                key=lambda item: (item.priority, item.category.value, item.directive_id),
            )
        )
        prompt, prompt_hash = compile_generation_prompt(
            canonical_prompt=parent.canonical_prompt,
            active_directives=directives,
            human_feedback=human_feedback,
        )
        return PromptVersion(
            search_id=search_id,
            source_manifest_hash=source_manifest_hash,
            round_index=round_index,
            refinement_mode=PromptRefinementMode.REVISION,
            generation_mode=PromptGenerationMode.SOURCE_REBASE,
            based_on_prompt_version_id=parent.prompt_version_id,
            prompt_schema_version=parent.prompt_schema_version,
            prompt_template_version=parent.prompt_template_version,
            canonical_template_version=parent.canonical_template_version,
            prompt_model=parent.prompt_model,
            generation_model=generation_model or parent.generation_model,
            canonical_prompt=parent.canonical_prompt,
            canonical_prompt_hash=parent.canonical_prompt_hash,
            generation_prompt=prompt,
            generation_prompt_hash=prompt_hash,
            active_directives=directives,
            active_directives_hash=stable_directives_hash(directives),
            professional_prompt_plan=parent.professional_prompt_plan,
            prompt_summary=parent.prompt_summary,
            provider_proposal_hash=parent.provider_proposal_hash,
            human_feedback=human_feedback,
            human_selected_candidate_id=None,
            tuned=bool(directives or (human_feedback and human_feedback.strip())),
        )

    @staticmethod
    def build_request_key(
        request: PromptRefinerRequest,
        *,
        model: str,
        provider_fingerprint: str,
        schema_version: str = PROMPT_REFINER_SCHEMA_VERSION,
        proxy_version: str = PROMPT_REFINER_PROXY_SCHEMA_VERSION,
        proxy_fingerprint: str | None = None,
    ) -> str:
        source_assets = [
            _asset_lineage(request.source_manifest.background),
            *(_asset_lineage(item) for item in request.source_manifest.cat_references),
        ]
        anchor = request.visual_anchor
        parent = request.parent_prompt_version
        payload = {
            "schema_version": schema_version,
            "operation": PROMPT_REFINER_OPERATION,
            "model": model,
            "provider_fingerprint": provider_fingerprint,
            "proxy_version": proxy_version,
            "proxy_fingerprint": proxy_fingerprint,
            "prompt_template_version": request.prompt_template_version,
            "prompt_schema_version": request.prompt_schema_version,
            "search_id": request.search_id,
            "round_index": request.round_index,
            "mode": request.mode.value,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "source_assets": source_assets,
            "guidance": _asset_lineage(request.guidance_mask),
            "user_intent_hash": _sha256_text(request.user_intent),
            "feedback_hash": (
                _sha256_text(request.human_feedback)
                if request.human_feedback is not None
                else None
            ),
            "human_selected_candidate_id": request.human_selected_candidate_id,
            "anchor": (
                {
                    "candidate_id": anchor.candidate_id,
                    "round_index": anchor.round_index,
                    "raw_asset_id": anchor.raw_asset.asset_id,
                    "raw_asset_sha256": anchor.raw_asset_sha256,
                }
                if anchor is not None
                else None
            ),
            "parent_prompt_version": (
                {
                    "prompt_version_id": parent.prompt_version_id,
                    "prompt_version_hash": parent.prompt_version_hash,
                }
                if parent is not None
                else None
            ),
            "critic_summary_hash": request.critic_summary_hash,
            "critic_evaluation_hash": request.critic_evaluation_hash,
            "generation_model": request.generation_model,
        }
        return _stable_hash(payload)

    def _audit_payload(
        self,
        request: PromptRefinerRequest,
        *,
        request_key: str,
    ) -> dict[str, object]:
        anchor = request.visual_anchor
        parent = request.parent_prompt_version
        return {
            "schema_version": PROMPT_REFINER_SCHEMA_VERSION,
            "operation": PROMPT_REFINER_OPERATION,
            "request_key": request_key,
            "search_id": request.search_id,
            "round_index": request.round_index,
            "mode": request.mode.value,
            "model": self.provider.model,
            "provider_fingerprint": self.provider.provider_fingerprint,
            "prompt_template_version": request.prompt_template_version,
            "prompt_schema_version": request.prompt_schema_version,
            "proxy_version": self.provider.proxy_version,
            "proxy_fingerprint": self.proxy_builder.fingerprint,
            "source_manifest_hash": request.source_manifest.manifest_hash,
            "source_asset_ids": [
                item.asset_id
                for item in (
                    request.source_manifest.background,
                    *request.source_manifest.cat_references,
                )
            ],
            "source_asset_hashes": [
                item.sha256
                for item in (
                    request.source_manifest.background,
                    *request.source_manifest.cat_references,
                )
            ],
            "guidance_mask_asset_id": request.guidance_mask.asset_id,
            "guidance_mask_sha256": request.guidance_mask.sha256,
            "user_intent_hash": _sha256_text(request.user_intent),
            "feedback_hash": (
                _sha256_text(request.human_feedback)
                if request.human_feedback is not None
                else None
            ),
            "human_selected_candidate_id": request.human_selected_candidate_id,
            "anchor": (
                {
                    "candidate_id": anchor.candidate_id,
                    "round_index": anchor.round_index,
                    "raw_asset_id": anchor.raw_asset.asset_id,
                    "raw_asset_sha256": anchor.raw_asset_sha256,
                }
                if anchor is not None
                else None
            ),
            "parent_prompt_version": (
                {
                    "prompt_version_id": parent.prompt_version_id,
                    "prompt_version_hash": parent.prompt_version_hash,
                }
                if parent is not None
                else None
            ),
            "critic_summary_hash": request.critic_summary_hash,
            "critic_evaluation_hash": request.critic_evaluation_hash,
            "generation_model": request.generation_model,
        }

    @staticmethod
    def _compile_result(
        *,
        request: PromptRefinerRequest,
        proposal: PromptPlanProposal,
        request_key: str,
        provider_request_id: str | None,
        provider_usage: Mapping[str, object] | None,
        replayed: bool,
        provider_model: str,
    ) -> PromptRefinerResult:
        canonical_prompt, canonical_hash = compile_professional_prompt(
            request=request, proposal=proposal
        )
        active_directives_hash = stable_directives_hash(())
        version = PromptVersion(
            search_id=request.search_id,
            source_manifest_hash=request.source_manifest.manifest_hash,
            round_index=request.round_index,
            refinement_mode=request.mode,
            generation_mode=(
                PromptGenerationMode.CANDIDATE_ANCHORED_REBASE
                if request.mode is PromptRefinementMode.REVISION
                else PromptGenerationMode.SOURCE_REBASE
            ),
            based_on_prompt_version_id=(
                request.parent_prompt_version.prompt_version_id
                if request.parent_prompt_version is not None
                else None
            ),
            prompt_schema_version=request.prompt_schema_version,
            prompt_template_version=request.prompt_template_version,
            canonical_template_version=request.prompt_template_version,
            prompt_model=provider_model,
            generation_model=request.generation_model,
            canonical_prompt=canonical_prompt,
            canonical_prompt_hash=canonical_hash,
            generation_prompt=canonical_prompt,
            generation_prompt_hash=canonical_hash,
            active_directives=(),
            active_directives_hash=active_directives_hash,
            professional_prompt_plan=proposal.plan,
            prompt_summary=proposal.plan.summary or proposal.plan.task,
            provider_proposal_hash=proposal.content_hash,
            visual_anchor=request.visual_anchor,
            human_feedback=request.human_feedback,
            human_selected_candidate_id=request.human_selected_candidate_id,
            tuned=request.mode is PromptRefinementMode.REVISION,
        )
        return PromptRefinerResult(
            request_key=request_key,
            prompt_version=version,
            proposal=proposal,
            provider_request_id=provider_request_id,
            provider_usage=_safe_usage(provider_usage),
            replayed=replayed,
        )

    @staticmethod
    def _completed_response_payload(
        response: Mapping[str, object],
        result_payload: Mapping[str, object],
        *,
        request: PromptRefinerRequest,
        request_key: str,
        audit_payload: Mapping[str, object],
        provider_model: str,
    ) -> PromptRefinerResult:
        """Validate redacted audit metadata and replay the durable result.

        The provider audit intentionally does not carry the proposal: a model
        can echo untrusted prompt/feedback text into a structured field.  The
        full result is read from ``prompt_refiner_results`` and is checked
        against a fresh local compilation before it is returned.
        """

        if response.get("request_key") != request_key:
            raise PromptRefinerAuditError("Prompt Refiner audit request key mismatch")
        expected_fingerprint = _stable_hash(audit_payload)
        if response.get("audit_fingerprint") != expected_fingerprint:
            raise PromptRefinerAuditError("Prompt Refiner audit lineage fingerprint mismatch")
        if response.get("result_key") != request_key:
            raise PromptRefinerAuditError("Prompt Refiner audit result key mismatch")
        try:
            stored_result = PromptRefinerResult.model_validate(result_payload)
        except (TypeError, ValueError) as exc:
            raise PromptRefinerAuditError("Prompt Refiner stored result is invalid") from exc
        if stored_result.request_key != request_key:
            raise PromptRefinerAuditError("Prompt Refiner stored result key mismatch")
        provider = response.get("provider")
        if not isinstance(provider, Mapping):
            provider = {}
        if provider.get("model") != provider_model:
            raise PromptRefinerAuditError("Prompt Refiner provider model mismatch")
        response_request_id = (
            _safe_provider_request_id(provider.get("request_id"))
            if isinstance(provider.get("request_id"), str)
            else None
        )
        response_usage = (
            _safe_usage(provider.get("usage"))
            if isinstance(provider.get("usage"), Mapping)
            else {}
        )
        if response_request_id != stored_result.provider_request_id:
            raise PromptRefinerAuditError("Prompt Refiner provider request ID mismatch")
        if response_usage != stored_result.provider_usage:
            raise PromptRefinerAuditError("Prompt Refiner provider usage mismatch")
        try:
            rebuilt = PromptRefinerService._compile_result(
                request=request,
                proposal=stored_result.proposal,
                request_key=request_key,
                provider_request_id=stored_result.provider_request_id,
                provider_usage=stored_result.provider_usage,
                replayed=True,
                provider_model=provider_model,
            )
        except (TypeError, ValueError) as exc:
            raise PromptRefinerAuditError(
                "Prompt Refiner stored result cannot be compiled"
            ) from exc
        if rebuilt.prompt_version != stored_result.prompt_version:
            raise PromptRefinerAuditError("Prompt Refiner prompt lineage has been tampered with")
        if rebuilt.proposal != stored_result.proposal:
            raise PromptRefinerAuditError("Prompt Refiner proposal has been tampered with")
        return rebuilt

    async def _renew_lease(self, *, request_key: str, owner_id: str) -> None:
        interval = max(0.2, PROMPT_REFINER_PROVIDER_CALL_LEASE_SECONDS / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.app_store.renew_provider_call_lease,
                request_key=request_key,
                owner_id=owner_id,
                lease_seconds=PROMPT_REFINER_PROVIDER_CALL_LEASE_SECONDS,
            )
            if not renewed:
                return

    async def _persist_completion(
        self,
        *,
        request_key: str,
        owner_id: str,
        result: Mapping[str, object],
        response: Mapping[str, object],
    ) -> bool:
        last_error: Exception | None = None
        for attempt in range(PROMPT_REFINER_AUDIT_WRITE_MAX_ATTEMPTS):
            try:
                completed = await asyncio.to_thread(
                    self.app_store.complete_prompt_refiner_call,
                    request_key,
                    result,
                    response,
                    owner_id=owner_id,
                )
                if completed:
                    return True
                existing = await asyncio.to_thread(
                    self.app_store.get_provider_call, request_key
                )
                if existing is not None and existing[0] == "completed":
                    return False
                last_error = PromptRefinerAuditError("Prompt Refiner provider lease was lost")
            except Exception as exc:  # pragma: no cover - exercised by SQLite fault injection
                last_error = exc
            if attempt + 1 < PROMPT_REFINER_AUDIT_WRITE_MAX_ATTEMPTS:
                await asyncio.sleep(PROMPT_REFINER_PROVIDER_RESULT_POLL_SECONDS)
        raise PromptRefinerAuditError(
            "Unable to persist completed Prompt Refiner audit"
        ) from last_error

    @staticmethod
    def _is_retryable_provider_error(exc: Exception) -> bool:
        """Classify failures without importing the optional SDK at module load."""

        if isinstance(
            exc,
            (
                PromptRefinerAuditError,
                ConfigurationError,
                TypeError,
                ValueError,
            ),
        ):
            return False
        status_code = getattr(exc, "status_code", None)
        if isinstance(status_code, int):
            return status_code in {408, 409, 429} or status_code >= 500
        name = type(exc).__name__.lower()
        if "timeout" in name or "connection" in name or "ratelimit" in name:
            return True
        # Unknown transport/runtime failures receive one bounded retry.  Schema,
        # lineage, and configuration failures were classified terminal above.
        return True

    def _assert_persisted_lineage(
        self,
        request: PromptRefinerRequest,
        *,
        require_payable: bool,
    ) -> None:
        """Re-authorize all request claims against app SQLite before payment."""

        search = self.app_store.get_search(request.search_id)
        if require_payable:
            if search.status in {
                SearchStatus.ACCEPTED,
                SearchStatus.FAILED,
                SearchStatus.CANCELLED,
            }:
                raise PromptRefinerError(
                    "Prompt Refiner cannot run for terminal search status "
                    f"{search.status.value}"
                )
            if search.round_index != request.round_index:
                raise PromptRefinerError(
                    "Prompt Refiner round does not follow the persisted search round"
                )
        project = self.app_store.get_project(search.project_id)
        if (
            search.source_manifest_hash != project.source_manifest.manifest_hash
            or request.source_manifest != project.source_manifest
        ):
            raise PromptRefinerError(
                "Prompt Refiner source manifest does not match persisted search lineage"
            )
        if search.user_intent != request.user_intent:
            raise PromptRefinerError(
                "Prompt Refiner user intent does not match the persisted search"
            )
        if (
            search.guidance_mask_asset is None
            or search.guidance_mask_asset != request.guidance_mask
        ):
            raise PromptRefinerError(
                "Prompt Refiner Guidance Mask is not authorized by the persisted search"
            )
        for asset in (
            project.source_manifest.background,
            *project.source_manifest.cat_references,
            search.guidance_mask_asset,
        ):
            if self.app_store.get_asset(asset.asset_id) != asset:
                raise PromptRefinerError(
                    "Prompt Refiner source asset does not match its canonical app record"
                )

        if request.mode is PromptRefinementMode.INITIAL:
            return

        assert request.visual_anchor is not None
        assert request.selected_candidate_evaluation is not None
        assert request.parent_prompt_version is not None
        review_entries = [
            item
            for item in search.round_history
            if isinstance(item, dict)
            and item.get("round_index") == request.round_index - 1
        ]
        if len(review_entries) != 1:
            raise PromptRefinerError(
                "Prompt Refiner revision is missing its persisted human resume"
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
            or review_entry.get("human_selected_candidate_id")
            != request.visual_anchor.candidate_id
            or normalized_persisted_feedback != (request.human_feedback or "").strip()
        ):
            raise PromptRefinerError(
                "Prompt Refiner revision does not match the persisted human selection and feedback"
            )
        candidates = [
            candidate
            for candidate in search.candidates
            if candidate.candidate_id == request.visual_anchor.candidate_id
        ]
        if len(candidates) != 1:
            raise PromptRefinerError(
                "Prompt Refiner selected candidate is not persisted in this search"
            )
        candidate = candidates[0]
        anchor = request.visual_anchor
        if (
            candidate.round_index != anchor.round_index
            or candidate.source_manifest_hash != search.source_manifest_hash
            or candidate.generation_depth != 0
            or candidate.raw_authoritative_asset != anchor.raw_asset
            or candidate.raw_asset.sha256 != anchor.raw_asset_sha256
        ):
            raise PromptRefinerError(
                "Prompt Refiner visual anchor is not the persisted raw Search authority"
            )
        if self.app_store.get_asset(candidate.raw_asset.asset_id) != candidate.raw_asset:
            raise PromptRefinerError(
                "Prompt Refiner visual anchor asset is not registered canonically"
            )
        persisted_evaluations = self.app_store.list_evaluations(request.search_id)
        if request.selected_candidate_evaluation not in persisted_evaluations:
            raise PromptRefinerError(
                "Prompt Refiner Critic result does not match persisted candidate evaluation"
            )

        parent = request.parent_prompt_version
        parent_payload = parent.model_dump(mode="json")
        parent_is_search_history = any(
            item.model_dump(mode="json") == parent_payload for item in search.prompt_history
        )
        persisted_parent = self.app_store.find_prompt_refiner_result_by_prompt_version(
            search_id=request.search_id,
            prompt_version_id=parent.prompt_version_id,
        )
        parent_is_refiner_result = False
        if persisted_parent is not None:
            try:
                stored_result = PromptRefinerResult.model_validate(persisted_parent)
            except (TypeError, ValueError) as exc:
                raise PromptRefinerAuditError(
                    "Persisted parent Prompt Refiner result is invalid"
                ) from exc
            parent_is_refiner_result = (
                stored_result.prompt_version.model_dump(mode="json") == parent_payload
            )
        if not parent_is_search_history and not parent_is_refiner_result:
            raise PromptRefinerError(
                "Prompt Refiner parent PromptVersion is not persisted for this search"
            )
        if candidate.prompt_hash != parent.generation_prompt_hash:
            raise PromptRefinerError(
                "Prompt Refiner parent PromptVersion did not generate the visual anchor"
            )

    async def _execute_owned_call(
        self,
        *,
        request: PromptRefinerRequest,
        proxies: PromptRefinerProxyBundle,
        request_key: str,
        owner_id: str,
        audit_payload: Mapping[str, object],
    ) -> PromptRefinerResult | None:
        try:
            raw = await asyncio.to_thread(
                self.provider.refine,
                request,
                proxies,
                request_key=request_key,
            )
            provider_result = _normalize_proposal(raw)
            # Provider model metadata is transport authority.  The structured
            # model output cannot select a different model identity for lineage.
            proposal = provider_result.proposal.model_copy(
                update={"provider_model": self.provider.model}
            )
            provider_request_id = _safe_provider_request_id(provider_result.provider_request_id)
            provider_usage = _safe_usage(provider_result.provider_usage)
            # Compile now so malformed provider plans consume a bounded attempt and
            # can never be recorded as a successful prompt version.
            result = self._compile_result(
                request=request,
                proposal=proposal,
                request_key=request_key,
                provider_request_id=provider_request_id,
                provider_usage=provider_usage,
                replayed=False,
                provider_model=self.provider.model,
            )
            response: dict[str, object] = {
                "request_key": request_key,
                "audit_fingerprint": _stable_hash(audit_payload),
                "result_key": request_key,
                "provider": {
                    "request_id": provider_request_id,
                    "usage": provider_usage,
                    "model": self.provider.model,
                },
            }
            stored = await self._persist_completion(
                request_key=request_key,
                owner_id=owner_id,
                result=result.model_dump(mode="json"),
                response=response,
            )
            return result if stored else None
        except Exception as exc:
            await asyncio.to_thread(
                self.app_store.fail_provider_call,
                request_key,
                type(exc).__name__,
                owner_id=owner_id,
                retryable=self._is_retryable_provider_error(exc),
            )
            return None

    @staticmethod
    async def _await_owned_call(
        task: asyncio.Task[PromptRefinerResult | None],
    ) -> tuple[PromptRefinerResult | None, bool]:
        cancel_requested = False
        while True:
            try:
                return await asyncio.shield(task), cancel_requested
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancel_requested = True

    def _read_completed_audit(
        self,
        *,
        request_key: str,
        request: PromptRefinerRequest,
        audit_payload: Mapping[str, object],
    ) -> PromptRefinerResult | None:
        record = self.app_store.get_provider_call_record(request_key)
        if record is None or record["status"] != "completed":
            return None
        stored_request = record.get("request")
        if stored_request != dict(audit_payload):
            raise PromptRefinerAuditError("Prompt Refiner request audit lineage mismatch")
        response = record.get("response")
        if not isinstance(response, Mapping):
            raise PromptRefinerAuditError("Prompt Refiner completed audit response is missing")
        result_payload = self.app_store.get_prompt_refiner_result(request_key)
        if result_payload is None:
            raise PromptRefinerAuditError("Prompt Refiner completed result is missing")
        return self._completed_response_payload(
            response,
            result_payload,
            request=request,
            request_key=request_key,
            audit_payload=audit_payload,
            provider_model=self.provider.model,
        )

    async def refine(self, request: PromptRefinerRequest) -> PromptRefinerResult:
        """Run at most two provider attempts and replay a validated completion."""

        if not request.is_checkpoint_safe:
            raise PromptRefinerError("Prompt Refiner request is not checkpoint-safe")
        # Completed calls remain safely replayable after a Search reaches a
        # terminal state.  All immutable lineage is still revalidated here;
        # payability/current-round checks happen only when a new call is needed.
        self._assert_persisted_lineage(request, require_payable=False)
        model = self.provider.model
        provider_fingerprint = self.provider.provider_fingerprint
        request_key = self.build_request_key(
            request,
            model=model,
            provider_fingerprint=provider_fingerprint,
            schema_version=self.provider.schema_version,
            proxy_version=self.provider.proxy_version,
            proxy_fingerprint=self.proxy_builder.fingerprint,
        )
        audit_payload = self._audit_payload(request, request_key=request_key)

        existing = await asyncio.to_thread(
            self._read_completed_audit,
            request_key=request_key,
            request=request,
            audit_payload=audit_payload,
        )
        if existing is not None:
            return existing

        proxies: PromptRefinerProxyBundle | None = None
        while True:
            self._assert_persisted_lineage(request, require_payable=True)
            owner_id = f"prompt_refiner_{uuid4().hex}"
            claimed, status, _completed = await asyncio.to_thread(
                self.app_store.claim_provider_call,
                request_key=request_key,
                operation=PROMPT_REFINER_OPERATION,
                search_id=request.search_id,
                request_payload=audit_payload,
                owner_id=owner_id,
                lease_seconds=PROMPT_REFINER_PROVIDER_CALL_LEASE_SECONDS,
                max_attempts=PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS,
            )
            if status == "completed":
                replay = await asyncio.to_thread(
                    self._read_completed_audit,
                    request_key=request_key,
                    request=request,
                    audit_payload=audit_payload,
                )
                if replay is None:
                    raise PromptRefinerAuditError(
                        "Prompt Refiner completed audit cannot be replayed"
                    )
                return replay
            if status == "failed_terminal":
                raise PromptRefinerError("Prompt Refiner provider failed terminally")
            if not claimed:
                attempts = await asyncio.to_thread(
                    self.app_store.provider_attempt_count, request_key
                )
                if attempts >= PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS:
                    raise PromptRefinerError("Prompt Refiner provider attempts exhausted")
                deadline = (
                    asyncio.get_running_loop().time() + PROMPT_REFINER_PROVIDER_RESULT_WAIT_SECONDS
                )
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(PROMPT_REFINER_PROVIDER_RESULT_POLL_SECONDS)
                    claimed, status, _completed = await asyncio.to_thread(
                        self.app_store.claim_provider_call,
                        request_key=request_key,
                        operation=PROMPT_REFINER_OPERATION,
                        search_id=request.search_id,
                        request_payload=audit_payload,
                        owner_id=owner_id,
                        lease_seconds=PROMPT_REFINER_PROVIDER_CALL_LEASE_SECONDS,
                        max_attempts=PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS,
                    )
                    if status == "completed":
                        replay = await asyncio.to_thread(
                            self._read_completed_audit,
                            request_key=request_key,
                            request=request,
                            audit_payload=audit_payload,
                        )
                        if replay is None:
                            raise PromptRefinerAuditError(
                                "Prompt Refiner completed audit cannot be replayed"
                            )
                        return replay
                    if status == "failed_terminal":
                        raise PromptRefinerError(
                            "Prompt Refiner provider failed terminally"
                        )
                    if claimed:
                        break
                    attempts = await asyncio.to_thread(
                        self.app_store.provider_attempt_count, request_key
                    )
                    if attempts >= PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS:
                        raise PromptRefinerError("Prompt Refiner provider attempts exhausted")
                else:
                    raise PromptRefinerError(
                        "Timed out waiting for the in-flight Prompt Refiner call"
                    )

            # Claims are re-authorized immediately before the paid side effect;
            # proxy generation therefore cannot turn a forged request into a call.
            try:
                self._assert_persisted_lineage(request, require_payable=True)
            except Exception as exc:
                await asyncio.to_thread(
                    self.app_store.fail_provider_call,
                    request_key,
                    type(exc).__name__,
                    owner_id=owner_id,
                    retryable=False,
                )
                raise
            if proxies is None:
                proxies = await asyncio.to_thread(self.proxy_builder.build, request)
            heartbeat = asyncio.create_task(
                self._renew_lease(request_key=request_key, owner_id=owner_id)
            )
            owned_call = asyncio.create_task(
                self._execute_owned_call(
                    request=request,
                    proxies=proxies,
                    request_key=request_key,
                    owner_id=owner_id,
                    audit_payload=audit_payload,
                )
            )
            try:
                result, cancel_requested = await self._await_owned_call(owned_call)
                if cancel_requested:
                    raise asyncio.CancelledError
                if result is not None:
                    completed = await asyncio.to_thread(
                        self._read_completed_audit,
                        request_key=request_key,
                        request=request,
                        audit_payload=audit_payload,
                    )
                    if completed is None:
                        raise PromptRefinerAuditError(
                            "Prompt Refiner completion disappeared before local replay"
                        )
                    return completed.model_copy(update={"replayed": False})
                completed = await asyncio.to_thread(
                    self._read_completed_audit,
                    request_key=request_key,
                    request=request,
                    audit_payload=audit_payload,
                )
                if completed is not None:
                    return completed
                attempts = await asyncio.to_thread(
                    self.app_store.provider_attempt_count, request_key
                )
                if attempts >= PROMPT_REFINER_PROVIDER_MAX_ATTEMPTS:
                    raise PromptRefinerError("Prompt Refiner provider attempts exhausted")
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
