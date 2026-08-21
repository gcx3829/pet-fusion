"""Versioned, checkpoint-safe prompt and visual-anchor contracts.

The prompt model is deliberately split into two layers:

* ``PromptPlanProposal`` describes an untrusted provider proposal.  It has no
  local lineage ID and is never itself a generator input.
* ``PromptVersion`` is the locally normalised, deterministic record persisted
  in search history and checkpoints.

The visual anchor is intentionally a dedicated raw-candidate claim with
content-addressed shape checks.  Database ownership and raw-candidate authority
still have to be verified by the revision service before generation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.assets import AssetRef, PublicAssetRef
from app.domain.directives import PlannerDirective, stable_directives_hash

Hash64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PromptClause = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=600),
]
ModelName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
PromptTask = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
]
PromptOutput = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1_000),
]

PROMPT_VERSION_SCHEMA_VERSION: Final[Literal["prompt-version/v1"]] = "prompt-version/v1"
PROMPT_PLAN_SCHEMA_VERSION: Final[Literal["professional-prompt-plan/v1"]] = (
    "professional-prompt-plan/v1"
)
DEFAULT_PROMPT_TEMPLATE_VERSION: Final[str] = "canonical-prompt/v3"


class PromptRefinementMode(StrEnum):
    """Whether a prompt is the first formulation or a later revision."""

    INITIAL = "initial"
    REVISION = "revision"


# ``PromptMode`` is kept as a short public alias for callers that do not need
# to distinguish the refinement aspect from the generation rebase mode.
PromptMode = PromptRefinementMode


class PromptGenerationMode(StrEnum):
    """The immutable-source policy used by the image-generation round."""

    SOURCE_REBASE = "source_rebase"
    CANDIDATE_ANCHORED_REBASE = "candidate_anchored_rebase"


GenerationMode = PromptGenerationMode


def _assert_no_embedded_image_data(value: object, *, path: str) -> None:
    """Reject image bytes and data URLs before Pydantic serialises a contract."""

    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"image bytes are forbidden in prompt contracts at {path}")
    if isinstance(value, str) and value.strip().lower().startswith("data:image/"):
        raise ValueError(f"image data URLs are forbidden in prompt contracts at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_embedded_image_data(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_embedded_image_data(child, path=f"{path}[{index}]")


def _jsonable(value: object) -> object:
    """Convert contract values to a stable JSON-compatible representation."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    return value


def _hash_payload(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ProfessionalPromptPlan(BaseModel):
    """Structured visual plan produced from user intent and image context.

    The sections keep the prompt provider's multimodal contribution explicit,
    while allowing the local compiler to decide the final prose and ordering.
    ``preserve_from_anchor`` and ``change_from_anchor`` are empty for the
    initial source-only round and become useful for a selected-candidate
    revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_of_inputs: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=8)
    task: PromptTask
    identity_invariants: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    pet_identity_observations: tuple[PromptClause, ...] = Field(
        default_factory=tuple, max_length=20
    )
    background_observations: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    placement: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    capture_geometry: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    lighting_analysis: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    color_analysis: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    optics_and_depth_analysis: tuple[PromptClause, ...] = Field(
        default_factory=tuple, max_length=12
    )
    texture_and_noise_analysis: tuple[PromptClause, ...] = Field(
        default_factory=tuple, max_length=12
    )
    physical_integration: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    photographic_integration: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    scene_preservation: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    uncertainties: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=12)
    output: PromptOutput
    preserve_from_anchor: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    change_from_anchor: tuple[PromptClause, ...] = Field(default_factory=tuple, max_length=16)
    summary: str = Field(default="", max_length=600)

    @model_validator(mode="before")
    @classmethod
    def reject_embedded_image_data(cls, value: object) -> object:
        _assert_no_embedded_image_data(value, path="professional_prompt_plan")
        return value


class PromptPlanProposal(BaseModel):
    """Untrusted provider semantics before local hashes and lineage are added.

    The proposal deliberately has no ID, hash, schema-version, or ancestry
    fields.  Provider metadata is supplied by the trusted adapter; a model
    response cannot choose any locally persisted lineage value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_model: ModelName
    plan: ProfessionalPromptPlan

    @model_validator(mode="before")
    @classmethod
    def reject_embedded_image_data(cls, value: object) -> object:
        _assert_no_embedded_image_data(value, path="prompt_plan_proposal")
        return value

    @property
    def content_hash(self) -> str:
        """Hash of the structured provider output, independent of its audit ID."""

        return _hash_payload(
            {"provider_model": self.provider_model, "plan": self.plan.model_dump(mode="json")}
        )


# A descriptive alias makes the contract discoverable for callers that use the
# full name from the product language.
ProfessionalPromptPlanProposal = PromptPlanProposal


class VisualAnchorRef(BaseModel):
    """A claimed raw-candidate reference with locally checkable invariants.

    This value cannot prove database ownership by itself.  The revision
    service must resolve ``candidate_id`` inside ``search_id`` and compare the
    persisted candidate's round, source-manifest hash, and authoritative raw
    asset before using it as a generator input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["visual-anchor/v1"] = "visual-anchor/v1"
    kind: Literal["candidate_raw"] = "candidate_raw"
    search_id: str = Field(min_length=1, max_length=120)
    candidate_id: str = Field(min_length=1, max_length=120)
    round_index: int = Field(ge=0)
    source_manifest_hash: Hash64
    raw_asset: AssetRef
    raw_asset_sha256: Hash64

    @model_validator(mode="before")
    @classmethod
    def hydrate_and_reject_unsafe_payload(cls, value: object) -> object:
        _assert_no_embedded_image_data(value, path="visual_anchor")
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        raw_asset = payload.get("raw_asset")
        if isinstance(raw_asset, Mapping):
            unknown_asset_fields = set(raw_asset) - set(AssetRef.model_fields)
            if unknown_asset_fields:
                raise ValueError(
                    "visual anchor raw_asset contains unsupported fields: "
                    + ", ".join(sorted(str(item) for item in unknown_asset_fields))
                )
        if payload.get("raw_asset_sha256") is None and isinstance(raw_asset, Mapping):
            raw_sha = raw_asset.get("sha256")
            if isinstance(raw_sha, str):
                payload["raw_asset_sha256"] = raw_sha
        # ``protected_asset`` is intentionally not hydrated.  A protected or
        # fused reference must fail validation instead of becoming an anchor.
        return payload

    @model_validator(mode="after")
    def validate_raw_lineage(self) -> VisualAnchorRef:
        if self.raw_asset.sha256 != self.raw_asset_sha256:
            raise ValueError("visual anchor raw_asset_sha256 must match raw_asset.sha256")
        if self.raw_asset.asset_id != f"ast_{self.raw_asset.sha256[:32]}":
            raise ValueError("visual anchor raw_asset must use a content-addressed asset ID")
        if self.raw_asset.mime_type != "image/png":
            raise ValueError("visual anchor raw_asset must be an internal PNG lineage asset")
        path = Path(self.raw_asset.path)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or len(self.raw_asset.path) > 4_096
            or path.name != f"{self.raw_asset.sha256}.png"
            or path.parent.name != self.raw_asset.sha256[:2]
            or "exports" in {part.lower() for part in path.parts}
        ):
            raise ValueError(
                "visual anchor raw_asset path must have the internal content-addressed PNG shape"
            )
        return self

    @classmethod
    def from_raw_asset(
        cls,
        *,
        search_id: str,
        candidate_id: str,
        round_index: int,
        source_manifest_hash: str,
        raw_asset: AssetRef,
    ) -> VisualAnchorRef:
        return cls(
            search_id=search_id,
            candidate_id=candidate_id,
            round_index=round_index,
            source_manifest_hash=source_manifest_hash,
            raw_asset=raw_asset,
            raw_asset_sha256=raw_asset.sha256,
        )


def _canonical_directive_payload(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        return value
    try:
        directives = tuple(PlannerDirective.model_validate(item) for item in value)
    except (TypeError, ValueError):
        return value
    return [
        item.model_dump(mode="json")
        for item in sorted(
            directives,
            key=lambda item: (item.priority, item.category.value, item.directive_id),
        )
    ]


def _visual_anchor_identity_payload(value: object) -> object:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        return value
    raw_asset = value.get("raw_asset")
    raw_asset_id = raw_asset.get("asset_id") if isinstance(raw_asset, Mapping) else None
    return {
        "schema_version": value.get("schema_version"),
        "kind": value.get("kind"),
        "search_id": value.get("search_id"),
        "candidate_id": value.get("candidate_id"),
        "round_index": value.get("round_index"),
        "source_manifest_hash": value.get("source_manifest_hash"),
        "raw_asset_id": raw_asset_id,
        "raw_asset_sha256": value.get("raw_asset_sha256"),
    }


def _prompt_identity_payload(value: Mapping[str, object]) -> dict[str, object]:
    """Return only local lineage inputs; provider IDs and derived hashes are excluded."""

    return {
        "schema_version": value.get("schema_version", PROMPT_VERSION_SCHEMA_VERSION),
        "search_id": value.get("search_id"),
        "source_manifest_hash": value.get("source_manifest_hash"),
        "round_index": value.get("round_index", 0),
        "refinement_mode": value.get("refinement_mode", PromptRefinementMode.INITIAL.value),
        "generation_mode": value.get("generation_mode", PromptGenerationMode.SOURCE_REBASE.value),
        "based_on_prompt_version_id": value.get("based_on_prompt_version_id"),
        "prompt_schema_version": value.get("prompt_schema_version", PROMPT_PLAN_SCHEMA_VERSION),
        "prompt_template_version": value.get(
            "prompt_template_version",
            value.get("canonical_template_version", DEFAULT_PROMPT_TEMPLATE_VERSION),
        ),
        "prompt_model": value.get("prompt_model"),
        "generation_model": value.get("generation_model"),
        "canonical_prompt": value.get("canonical_prompt"),
        "canonical_prompt_hash": value.get("canonical_prompt_hash"),
        "generation_prompt": value.get("generation_prompt"),
        "generation_prompt_hash": value.get("generation_prompt_hash"),
        "active_directives_hash": value.get("active_directives_hash"),
        "active_directives": _canonical_directive_payload(value.get("active_directives", [])),
        # Filesystem paths are deployment metadata.  Stable lineage uses the
        # content-addressed identity, not where the same asset is mounted.
        "visual_anchor": _visual_anchor_identity_payload(value.get("visual_anchor")),
        "professional_prompt_plan": value.get("professional_prompt_plan"),
        "prompt_summary": value.get("prompt_summary"),
        # Provider audit hashes are retained on the version, but do not enter
        # the local identity.  A provider adapter must not control local IDs.
        "human_feedback": value.get("human_feedback"),
        "human_selected_candidate_id": value.get("human_selected_candidate_id"),
        "tuned": value.get("tuned", False),
    }


def stable_prompt_version_hash(value: Mapping[str, object]) -> str:
    """Compute the deterministic local fingerprint for a PromptVersion payload."""

    return _hash_payload(_prompt_identity_payload(value))


def stable_prompt_version_id(value: Mapping[str, object]) -> str:
    """Return a compact, deterministic ID derived only from local lineage fields."""

    return f"pv_{stable_prompt_version_hash(value)[:32]}"


class PromptVersion(BaseModel):
    """Locally normalised prompt lineage persisted with a generation round."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: Literal["prompt-version/v1"] = PROMPT_VERSION_SCHEMA_VERSION
    prompt_version_id: str = Field(default="pv_" + "0" * 32, pattern=r"^pv_[0-9a-f]{32}$")
    prompt_version_hash: Hash64 = "0" * 64
    search_id: str | None = Field(default=None, min_length=1, max_length=120)
    source_manifest_hash: Hash64 | None = None
    round_index: int = Field(ge=0)
    refinement_mode: PromptRefinementMode = PromptRefinementMode.INITIAL
    generation_mode: PromptGenerationMode = PromptGenerationMode.SOURCE_REBASE
    based_on_prompt_version_id: str | None = Field(default=None, pattern=r"^pv_[0-9a-f]{32}$")
    prompt_schema_version: str = Field(
        default=PROMPT_PLAN_SCHEMA_VERSION, min_length=1, max_length=120
    )
    prompt_template_version: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE_VERSION, min_length=1, max_length=120
    )
    # Existing API consumers use this name.  It is normalised to the same
    # value as ``prompt_template_version`` and retained during migration.
    canonical_template_version: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE_VERSION, min_length=1, max_length=120
    )
    prompt_model: ModelName | None = None
    generation_model: ModelName | None = None
    canonical_prompt: str = Field(min_length=1, max_length=12_000)
    canonical_prompt_hash: Hash64
    generation_prompt: str = Field(min_length=1, max_length=16_000)
    generation_prompt_hash: Hash64
    active_directives: tuple[PlannerDirective, ...] = Field(default_factory=tuple, max_length=3)
    active_directives_hash: Hash64
    professional_prompt_plan: ProfessionalPromptPlan | None = None
    prompt_summary: str | None = Field(default=None, max_length=600)
    provider_proposal_hash: Hash64 | None = None
    visual_anchor: VisualAnchorRef | None = None
    human_feedback: str | None = Field(default=None, max_length=2_000)
    human_selected_candidate_id: str | None = Field(default=None, max_length=120)
    tuned: bool = False

    @model_validator(mode="before")
    @classmethod
    def hydrate_legacy_fields_and_derived_ids(cls, value: object) -> object:
        _assert_no_embedded_image_data(value, path="prompt_version")
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        round_index = payload.get("round_index", 0)
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            round_index = 0
            payload["round_index"] = round_index

        canonical_template = payload.get("canonical_template_version")
        prompt_template = payload.get("prompt_template_version")
        if not isinstance(prompt_template, str) or not prompt_template:
            prompt_template = (
                canonical_template
                if isinstance(canonical_template, str) and canonical_template
                else DEFAULT_PROMPT_TEMPLATE_VERSION
            )
            payload["prompt_template_version"] = prompt_template
        if not isinstance(canonical_template, str) or not canonical_template:
            payload["canonical_template_version"] = prompt_template
        else:
            # The old field is the public compatibility spelling.  If both are
            # present, the explicit new field is authoritative.
            payload["canonical_template_version"] = prompt_template

        payload.setdefault("schema_version", PROMPT_VERSION_SCHEMA_VERSION)
        payload.setdefault("prompt_schema_version", PROMPT_PLAN_SCHEMA_VERSION)
        payload.setdefault(
            "refinement_mode",
            (
                PromptRefinementMode.REVISION.value
                if round_index > 0
                or bool(payload.get("tuned"))
                or bool(payload.get("human_feedback"))
                else PromptRefinementMode.INITIAL.value
            ),
        )
        payload.setdefault(
            "generation_mode",
            (
                PromptGenerationMode.CANDIDATE_ANCHORED_REBASE.value
                if payload.get("visual_anchor") is not None
                else PromptGenerationMode.SOURCE_REBASE.value
            ),
        )
        raw_directives = payload.get("active_directives", ())
        if not isinstance(raw_directives, (list, tuple)):
            raw_directives = ()
            payload["active_directives"] = raw_directives
        directives = tuple(
            sorted(
                (PlannerDirective.model_validate(item) for item in raw_directives),
                key=lambda item: (item.priority, item.category.value, item.directive_id),
            )
        )
        payload["active_directives"] = directives
        if not isinstance(payload.get("active_directives_hash"), str):
            payload["active_directives_hash"] = stable_directives_hash(directives)
        canonical_prompt = payload.get("canonical_prompt", "legacy prompt")
        generation_prompt = payload.get("generation_prompt", canonical_prompt)
        payload.setdefault(
            "canonical_prompt_hash",
            hashlib.sha256(str(canonical_prompt).encode("utf-8")).hexdigest(),
        )
        payload.setdefault(
            "generation_prompt_hash",
            hashlib.sha256(str(generation_prompt).encode("utf-8")).hexdigest(),
        )
        payload.setdefault("canonical_prompt", str(canonical_prompt))
        payload.setdefault("generation_prompt", str(generation_prompt))
        identity_hash = stable_prompt_version_hash(payload)
        payload.setdefault("prompt_version_hash", identity_hash)
        payload.setdefault("prompt_version_id", f"pv_{identity_hash[:32]}")
        return payload

    @model_validator(mode="after")
    def validate_lineage(self) -> PromptVersion:
        expected_hash = stable_prompt_version_hash(self.model_dump(mode="json"))
        if self.prompt_version_hash != expected_hash:
            raise ValueError("prompt_version_hash must be derived from local prompt lineage")
        if self.prompt_version_id != f"pv_{expected_hash[:32]}":
            raise ValueError("prompt_version_id must be derived from local prompt lineage")
        if self.prompt_template_version != self.canonical_template_version:
            raise ValueError("prompt template compatibility fields must match")
        if not self.canonical_prompt.strip() or not self.generation_prompt.strip():
            raise ValueError("prompt text cannot be blank")
        if (
            self.canonical_prompt_hash
            != hashlib.sha256(self.canonical_prompt.encode("utf-8")).hexdigest()
        ):
            raise ValueError("canonical_prompt_hash must match canonical_prompt")
        if (
            self.generation_prompt_hash
            != hashlib.sha256(self.generation_prompt.encode("utf-8")).hexdigest()
        ):
            raise ValueError("generation_prompt_hash must match generation_prompt")
        if self.active_directives_hash != stable_directives_hash(self.active_directives):
            raise ValueError("active_directives_hash must match active_directives")
        if self.refinement_mode is PromptRefinementMode.INITIAL:
            if self.round_index != 0:
                raise ValueError("initial prompt versions must belong to round 0")
            if self.generation_mode is not PromptGenerationMode.SOURCE_REBASE:
                raise ValueError("initial prompt versions cannot use a candidate visual anchor")
            if self.visual_anchor is not None:
                raise ValueError("initial prompt versions cannot contain a visual anchor")
            if self.based_on_prompt_version_id is not None:
                raise ValueError("initial prompt versions cannot have parent prompt lineage")
        if self.refinement_mode is PromptRefinementMode.REVISION and self.round_index == 0:
            raise ValueError("revision prompt versions must belong to a later round")
        if self.generation_mode is PromptGenerationMode.CANDIDATE_ANCHORED_REBASE:
            if self.refinement_mode is not PromptRefinementMode.REVISION:
                raise ValueError("candidate-anchored rebase requires revision mode")
            if self.visual_anchor is None:
                raise ValueError("candidate-anchored rebase requires a raw visual anchor")
            if self.based_on_prompt_version_id is None:
                raise ValueError("candidate-anchored rebase requires prompt lineage")
            if self.search_id is None or self.visual_anchor.search_id != self.search_id:
                raise ValueError("visual anchor search must match prompt lineage")
            if (
                self.source_manifest_hash is None
                or self.visual_anchor.source_manifest_hash != self.source_manifest_hash
            ):
                raise ValueError("visual anchor source manifest must match prompt lineage")
            if self.visual_anchor.round_index != self.round_index - 1:
                raise ValueError("visual anchor must belong to the immediately previous round")
            if self.human_selected_candidate_id != self.visual_anchor.candidate_id:
                raise ValueError("visual anchor must match the human-selected candidate")
        if self.generation_mode is PromptGenerationMode.SOURCE_REBASE and self.visual_anchor:
            raise ValueError("source rebase cannot contain a visual anchor")
        return self

    @property
    def visual_anchor_candidate_id(self) -> str | None:
        return self.visual_anchor.candidate_id if self.visual_anchor is not None else None

    @property
    def visual_anchor_raw_asset_sha256(self) -> str | None:
        return self.visual_anchor.raw_asset_sha256 if self.visual_anchor is not None else None

    @property
    def visual_anchor_asset(self) -> AssetRef | None:
        return self.visual_anchor.raw_asset if self.visual_anchor is not None else None


class PromptHistoryEntry(PromptVersion):
    """Backward-compatible flat representation used by the Search API."""

    pass


class PublicVisualAnchorRef(BaseModel):
    """API-safe visual anchor metadata with no deployment filesystem path."""

    schema_version: Literal["visual-anchor/v1"] = "visual-anchor/v1"
    kind: Literal["candidate_raw"] = "candidate_raw"
    search_id: str
    candidate_id: str
    round_index: int
    source_manifest_hash: Hash64
    raw_asset: PublicAssetRef
    raw_asset_sha256: Hash64

    @classmethod
    def from_internal(cls, anchor: VisualAnchorRef) -> PublicVisualAnchorRef:
        return cls(
            schema_version=anchor.schema_version,
            kind=anchor.kind,
            search_id=anchor.search_id,
            candidate_id=anchor.candidate_id,
            round_index=anchor.round_index,
            source_manifest_hash=anchor.source_manifest_hash,
            raw_asset=PublicAssetRef.from_internal(anchor.raw_asset),
            raw_asset_sha256=anchor.raw_asset_sha256,
        )


class PublicPromptHistoryEntry(BaseModel):
    """Public PromptVersion projection retaining prompts and safe lineage only."""

    schema_version: Literal["prompt-version/v1"]
    prompt_version_id: str
    prompt_version_hash: Hash64
    search_id: str | None
    source_manifest_hash: Hash64 | None
    round_index: int
    refinement_mode: PromptRefinementMode
    generation_mode: PromptGenerationMode
    based_on_prompt_version_id: str | None
    prompt_schema_version: str
    prompt_template_version: str
    canonical_template_version: str
    prompt_model: str | None
    generation_model: str | None
    canonical_prompt: str
    canonical_prompt_hash: Hash64
    generation_prompt: str
    generation_prompt_hash: Hash64
    active_directives: tuple[PlannerDirective, ...]
    active_directives_hash: Hash64
    professional_prompt_plan: ProfessionalPromptPlan | None
    prompt_summary: str | None
    provider_proposal_hash: Hash64 | None
    visual_anchor: PublicVisualAnchorRef | None
    human_feedback: str | None
    human_selected_candidate_id: str | None
    tuned: bool

    @classmethod
    def from_internal(cls, version: PromptVersion) -> PublicPromptHistoryEntry:
        payload = version.model_dump(mode="json", exclude={"visual_anchor"})
        return cls.model_validate(
            {
                **payload,
                "visual_anchor": (
                    PublicVisualAnchorRef.from_internal(version.visual_anchor)
                    if version.visual_anchor is not None
                    else None
                ),
            }
        )
