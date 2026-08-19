from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from app.domain.assets import AssetRef
from app.domain.directives import (
    DirectiveCategory,
    PlannerDirective,
    stable_directives_hash,
)
from app.domain.prompts import (
    ProfessionalPromptPlan,
    PromptGenerationMode,
    PromptHistoryEntry,
    PromptPlanProposal,
    PromptRefinementMode,
    VisualAnchorRef,
    stable_prompt_version_hash,
    stable_prompt_version_id,
)
from app.graphs.state import assert_checkpoint_safe


def _asset(*, path: str | None = None) -> AssetRef:
    digest = "b" * 64
    return AssetRef(
        asset_id="ast_" + digest[:32],
        path=path or f"/tmp/assets/{digest[:2]}/{digest}.png",
        sha256=digest,
        mime_type="image/png",
        width=640,
        height=480,
    )


def _prompt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "round_index": 0,
        "canonical_prompt": "Keep the original travel photograph and add the same cat.",
        "canonical_prompt_hash": hashlib.sha256(
            b"Keep the original travel photograph and add the same cat."
        ).hexdigest(),
        "generation_prompt": "Keep the original travel photograph and add the same cat.",
        "generation_prompt_hash": hashlib.sha256(
            b"Keep the original travel photograph and add the same cat."
        ).hexdigest(),
        "canonical_template_version": "canonical-prompt/v3",
        "active_directives": [],
        "active_directives_hash": stable_directives_hash(()),
    }
    payload.update(overrides)
    return payload


def _anchor() -> VisualAnchorRef:
    return VisualAnchorRef.from_raw_asset(
        search_id="search-1",
        candidate_id="cand_r0_v1",
        round_index=0,
        source_manifest_hash="d" * 64,
        raw_asset=_asset(),
    )


def test_initial_and_source_revision_modes_are_explicit() -> None:
    initial = PromptHistoryEntry.model_validate(_prompt_payload())
    assert initial.refinement_mode is PromptRefinementMode.INITIAL
    assert initial.generation_mode is PromptGenerationMode.SOURCE_REBASE
    revision = PromptHistoryEntry.model_validate(
        _prompt_payload(
            round_index=1,
            refinement_mode="revision",
            generation_mode="source_rebase",
            based_on_prompt_version_id=initial.prompt_version_id,
        )
    )
    assert revision.refinement_mode is PromptRefinementMode.REVISION
    assert revision.generation_mode is PromptGenerationMode.SOURCE_REBASE


def test_visual_anchor_requires_revision_and_candidate_anchored_mode() -> None:
    anchor = _anchor()
    with pytest.raises(ValidationError, match="initial prompt versions cannot"):
        PromptHistoryEntry.model_validate(
            _prompt_payload(
                visual_anchor=anchor.model_dump(mode="json"),
                generation_mode="candidate_anchored_rebase",
            )
        )

    with pytest.raises(ValidationError, match="requires a raw visual anchor"):
        PromptHistoryEntry.model_validate(
            _prompt_payload(
                round_index=1,
                refinement_mode="revision",
                generation_mode="candidate_anchored_rebase",
                based_on_prompt_version_id="pv_" + "e" * 32,
            )
        )

    with pytest.raises(ValidationError, match="source rebase cannot"):
        PromptHistoryEntry.model_validate(
            _prompt_payload(
                round_index=1,
                refinement_mode="revision",
                generation_mode="source_rebase",
                visual_anchor=anchor.model_dump(mode="json"),
            )
        )

    anchored = PromptHistoryEntry.model_validate(
        _prompt_payload(
            search_id="search-1",
            source_manifest_hash="d" * 64,
            round_index=1,
            refinement_mode="revision",
            generation_mode="candidate_anchored_rebase",
            based_on_prompt_version_id="pv_" + "e" * 32,
            visual_anchor=anchor.model_dump(mode="json"),
            human_selected_candidate_id="cand_r0_v1",
        )
    )
    assert anchored.visual_anchor_candidate_id == "cand_r0_v1"
    assert anchored.visual_anchor_raw_asset_sha256 == "b" * 64
    assert anchored.visual_anchor_asset == anchor.raw_asset


def test_local_prompt_version_id_and_hash_are_stable() -> None:
    payload = _prompt_payload(
        round_index=1,
        refinement_mode="revision",
        generation_mode="source_rebase",
        based_on_prompt_version_id="pv_" + "e" * 32,
    )
    first = PromptHistoryEntry.model_validate(payload)
    second = PromptHistoryEntry.model_validate(dict(payload))
    assert first.prompt_version_id == second.prompt_version_id
    assert first.prompt_version_hash == second.prompt_version_hash
    assert first.prompt_version_hash == stable_prompt_version_hash(first.model_dump(mode="json"))
    assert first.prompt_version_id == stable_prompt_version_id(first.model_dump(mode="json"))

    provider_audit_variant = PromptHistoryEntry.model_validate(
        {**payload, "provider_proposal_hash": "f" * 64}
    )
    assert provider_audit_variant.prompt_version_id == first.prompt_version_id

    changed_prompt = "Keep the same scene, but place the exact cat more naturally."
    changed = PromptHistoryEntry.model_validate(
        {
            **payload,
            "generation_prompt": changed_prompt,
            "generation_prompt_hash": hashlib.sha256(changed_prompt.encode()).hexdigest(),
        }
    )
    assert changed.prompt_version_hash != first.prompt_version_hash


def test_prompt_version_hash_is_deterministic_for_mapping_key_order() -> None:
    plan_a = ProfessionalPromptPlan(
        task="Add the same cat",
        output="Authentic photo",
        scene_preservation=("Keep the traveller unchanged",),
    )
    plan_b = ProfessionalPromptPlan.model_validate(
        {
            "scene_preservation": ["Keep the traveller unchanged"],
            "output": "Authentic photo",
            "task": "Add the same cat",
        }
    )
    first = PromptHistoryEntry.model_validate(
        _prompt_payload(professional_prompt_plan=plan_a.model_dump(mode="json"))
    )
    second = PromptHistoryEntry.model_validate(
        _prompt_payload(professional_prompt_plan=plan_b.model_dump(mode="json"))
    )
    assert first.prompt_version_hash == second.prompt_version_hash


def test_prompt_version_normalizes_directive_order_and_ignores_anchor_mount_path() -> None:
    directives = (
        PlannerDirective(
            directive_id="directive-b",
            category=DirectiveCategory.LIGHTING_COLOR,
            instruction="Match the local light direction.",
            priority=2,
            expected_effect="Keep the cat consistent with the scene light.",
        ),
        PlannerDirective(
            directive_id="directive-a",
            category=DirectiveCategory.IDENTITY,
            instruction="Preserve the exact face markings.",
            priority=1,
            expected_effect="Keep the selected cat identity recognizable.",
        ),
    )
    first = PromptHistoryEntry.model_validate(
        _prompt_payload(
            active_directives=[item.model_dump(mode="json") for item in directives],
            active_directives_hash=stable_directives_hash(directives),
        )
    )
    second = PromptHistoryEntry.model_validate(
        _prompt_payload(
            active_directives=[
                item.model_dump(mode="json") for item in reversed(directives)
            ],
            active_directives_hash=stable_directives_hash(tuple(reversed(directives))),
        )
    )
    assert first.prompt_version_hash == second.prompt_version_hash

    anchor = _anchor()
    relocated_anchor = VisualAnchorRef.from_raw_asset(
        search_id=anchor.search_id,
        candidate_id=anchor.candidate_id,
        round_index=anchor.round_index,
        source_manifest_hash=anchor.source_manifest_hash,
        raw_asset=_asset(
            path=f"/different/mount/bb/{anchor.raw_asset.sha256}.png"
        ),
    )
    common = {
        "search_id": "search-1",
        "source_manifest_hash": "d" * 64,
        "round_index": 1,
        "refinement_mode": "revision",
        "generation_mode": "candidate_anchored_rebase",
        "based_on_prompt_version_id": "pv_" + "e" * 32,
        "human_selected_candidate_id": "cand_r0_v1",
    }
    anchored_first = PromptHistoryEntry.model_validate(
        _prompt_payload(**common, visual_anchor=anchor.model_dump(mode="json"))
    )
    anchored_relocated = PromptHistoryEntry.model_validate(
        _prompt_payload(
            **common, visual_anchor=relocated_anchor.model_dump(mode="json")
        )
    )
    assert anchored_first.prompt_version_hash == anchored_relocated.prompt_version_hash


def test_old_prompt_history_json_is_readable_and_unknown_fields_are_ignored() -> None:
    legacy = {
        **_prompt_payload(),
        "legacy_provider_field": {"ignored": True},
    }
    entry = PromptHistoryEntry.model_validate(legacy)
    assert entry.prompt_version_id.startswith("pv_")
    assert entry.prompt_schema_version == "professional-prompt-plan/v1"
    assert entry.prompt_template_version == "canonical-prompt/v3"
    assert "legacy_provider_field" not in entry.model_dump(mode="json")
    wire = entry.model_dump(mode="json")
    for legacy_field in (
        "round_index",
        "canonical_prompt",
        "canonical_prompt_hash",
        "generation_prompt",
        "generation_prompt_hash",
        "canonical_template_version",
        "active_directives",
        "active_directives_hash",
        "human_feedback",
        "human_selected_candidate_id",
        "tuned",
    ):
        assert legacy_field in wire
    assert isinstance(wire["active_directives"], list)


def test_plan_and_anchor_reject_bytes_and_image_data_urls() -> None:
    with pytest.raises((TypeError, ValueError), match="image"):
        ProfessionalPromptPlan.model_validate(
            {"task": b"image bytes", "output": "photo"}
        )
    with pytest.raises(ValueError, match="image data URL"):
        VisualAnchorRef.model_validate(
            {
                "candidate_id": "cand_r0_v1",
                "search_id": "search-1",
                "round_index": 0,
                "source_manifest_hash": "d" * 64,
                "raw_asset": _asset(path=" data:image/png;base64,AAAA").model_dump(
                    mode="json"
                ),
            }
        )


def test_provider_proposal_has_no_local_prompt_lineage_fields() -> None:
    proposal = PromptPlanProposal(
        provider_model="gpt-5.6-terra",
        plan=ProfessionalPromptPlan(task="Add the same cat", output="Authentic photo"),
    )
    assert len(proposal.content_hash) == 64
    assert proposal.content_hash == proposal.model_copy().content_hash
    assert "prompt_version_id" not in proposal.model_dump(mode="json")
    assert "provider_proposal_hash" not in proposal.model_dump(mode="json")
    for forbidden in (
        {"schema_version": "provider-chosen/v9"},
        {"provider_proposal_hash": "f" * 64},
        {"prompt_version_id": "pv_" + "f" * 32},
        {"based_on_prompt_version_id": "pv_" + "f" * 32},
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            PromptPlanProposal.model_validate(
                {
                    "provider_model": "gpt-5.6-terra",
                    "plan": {"task": "Add the same cat", "output": "Authentic photo"},
                    **forbidden,
                }
            )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PromptPlanProposal.model_validate(
            {
                "provider_model": "gpt-5.6-terra",
                "plan": {
                    "schema_version": "provider-chosen/v9",
                    "task": "Add the same cat",
                    "output": "Authentic photo",
                },
            }
        )


def test_anchor_contract_rejects_noncanonical_or_cross_lineage_claims() -> None:
    anchor = _anchor()
    with pytest.raises(ValidationError, match="content-addressed asset ID"):
        VisualAnchorRef.model_validate(
            {
                **anchor.model_dump(mode="json"),
                "raw_asset": {
                    **anchor.raw_asset.model_dump(mode="json"),
                    "asset_id": "ast_" + "f" * 32,
                },
            }
        )
    with pytest.raises(ValidationError, match="search must match"):
        PromptHistoryEntry.model_validate(
            _prompt_payload(
                search_id="another-search",
                source_manifest_hash="d" * 64,
                round_index=1,
                refinement_mode="revision",
                generation_mode="candidate_anchored_rebase",
                based_on_prompt_version_id="pv_" + "e" * 32,
                visual_anchor=anchor.model_dump(mode="json"),
                human_selected_candidate_id="cand_r0_v1",
            )
        )
    with pytest.raises(ValidationError, match="immediately previous round"):
        PromptHistoryEntry.model_validate(
            _prompt_payload(
                search_id="search-1",
                source_manifest_hash="d" * 64,
                round_index=2,
                refinement_mode="revision",
                generation_mode="candidate_anchored_rebase",
                based_on_prompt_version_id="pv_" + "e" * 32,
                visual_anchor=anchor.model_dump(mode="json"),
                human_selected_candidate_id="cand_r0_v1",
            )
        )


def test_frozen_contracts_and_clause_boundaries_are_enforced() -> None:
    entry = PromptHistoryEntry.model_validate(_prompt_payload())
    assert isinstance(entry.active_directives, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        entry.round_index = 1
    with pytest.raises(ValidationError, match="at most 600 characters"):
        ProfessionalPromptPlan(
            task="Add the same cat",
            output="Authentic photo",
            identity_invariants=("x" * 601,),
        )
    with pytest.raises(ValidationError, match="at least 1 character"):
        ProfessionalPromptPlan(task="   ", output="Authentic photo")


def test_checkpoint_guard_rejects_case_and_whitespace_obfuscated_data_urls() -> None:
    with pytest.raises(TypeError, match="Image data URL"):
        assert_checkpoint_safe({"image": "  DATA:IMAGE/png;base64,AAAA"})
