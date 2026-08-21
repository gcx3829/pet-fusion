"""Explicit LangGraph subgraph for multimodal prompt formulation.

The parent SearchGraph owns round routing.  This child graph owns the prompt
boundary itself: it prepares a checkpoint-safe request, validates all local
lineage claims, invokes the idempotent PromptRefinerService when a multimodal
call is required, and applies one locally normalised PromptVersion.  It is
compiled without a checkpointer so a parent graph can nest it directly and
inherit the parent's durable checkpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from app.domain.assets import AssetRef, SourceManifest
from app.domain.errors import SourceManifestMismatchError
from app.domain.prompts import (
    PromptRefinementMode,
    PromptVersion,
    VisualAnchorRef,
)
from app.domain.searches import SearchStatus
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.prompt_refiner_service import (
    PromptRefinerRequest,
    PromptRefinerResult,
    PromptRefinerService,
)


@dataclass(frozen=True, slots=True)
class MultimodalPromptGraphServices:
    app_store: AppStore
    prompt_refiner_service: PromptRefinerService
    # The PromptVersion records the downstream image generator model, not the
    # model used to formulate the plan.  Keeping the two identities separate
    # makes provider audits and replay lineage unambiguous.
    generation_model: str | None = None
    lease_owner: str | None = None


def _safe_prompt_lineage(version: PromptVersion) -> dict[str, object]:
    """Return the redacted metadata used by round history and SSE events."""

    return {
        "prompt_version_id": version.prompt_version_id,
        "prompt_version_hash": version.prompt_version_hash,
        "based_on_prompt_version_id": version.based_on_prompt_version_id,
        "generation_mode": version.generation_mode.value,
        "refinement_mode": version.refinement_mode.value,
        "visual_anchor_candidate_id": version.visual_anchor_candidate_id,
        "prompt_model": version.prompt_model,
        "generation_model": version.generation_model,
        "prompt_schema_version": version.prompt_schema_version,
        "prompt_template_version": version.prompt_template_version,
        "generation_prompt_hash": version.generation_prompt_hash,
        "canonical_prompt_hash": version.canonical_prompt_hash,
        "provider_proposal_hash": version.provider_proposal_hash,
        "active_directives_hash": version.active_directives_hash,
    }


def _current_prompt_version(state: SearchState) -> PromptVersion | None:
    raw_target_round = state.get("round_index", 0)
    target_round: int = raw_target_round if isinstance(raw_target_round, int) else 0
    payload = state.get("current_prompt_version")
    if isinstance(payload, dict):
        return PromptVersion.model_validate(payload)
    prompt_version_id = state.get("prompt_version_id")
    if isinstance(prompt_version_id, str):
        for raw in state.get("prompt_history", []):
            if isinstance(raw, dict) and raw.get("prompt_version_id") == prompt_version_id:
                return PromptVersion.model_validate(raw)
    prior_rounds = [
        raw
        for raw in state.get("prompt_history", [])
        if isinstance(raw, dict)
        and isinstance(raw.get("round_index"), int)
        and cast(int, raw["round_index"]) < target_round
    ]
    if prior_rounds:
        raw = max(prior_rounds, key=lambda item: cast(int, item["round_index"]))
        return PromptVersion.model_validate(raw)
    return None


def _persisted_anchor(
    *, services: MultimodalPromptGraphServices, state: SearchState
) -> tuple[VisualAnchorRef, Any]:
    selected_id = state.get("human_selected_candidate_id")
    if not isinstance(selected_id, str) or not selected_id:
        raise SourceManifestMismatchError(
            "Prompt Refiner revision requires an explicit human-selected candidate"
        )
    search = services.app_store.get_search(state["search_id"])
    matches = [item for item in search.candidates if item.candidate_id == selected_id]
    if len(matches) != 1:
        raise SourceManifestMismatchError(
            "Prompt Refiner selected candidate is not persisted in this search"
        )
    candidate = matches[0]
    expected_round = state["round_index"] - 1
    if candidate.round_index != expected_round:
        raise SourceManifestMismatchError(
            "Prompt Refiner selected candidate must belong to the reviewed previous round"
        )
    anchor = VisualAnchorRef.from_raw_asset(
        search_id=state["search_id"],
        candidate_id=candidate.candidate_id,
        round_index=candidate.round_index,
        source_manifest_hash=state["source_manifest_hash"],
        raw_asset=candidate.raw_authoritative_asset,
    )
    evaluations = [
        item
        for item in services.app_store.list_evaluations(state["search_id"])
        if item.candidate_id == selected_id and item.round_index == expected_round
    ]
    if len(evaluations) != 1:
        raise SourceManifestMismatchError(
            "Prompt Refiner selected candidate is missing its persisted Critic evaluation"
        )
    return anchor, evaluations[0]


def build_multimodal_prompt_subgraph(
    services: MultimodalPromptGraphServices,
) -> StateGraph[SearchState]:
    """Build the nested Prompt Refiner graph.

    ``local`` mode is intentionally handled inside the same child graph.  It
    performs no external call and only applies bounded active directives to
    the previous professional base prompt, which keeps automatic Planner
    rounds source-only while making the branch visible in graph inspection.
    """

    prompt_service = services.prompt_refiner_service

    def fenced_update(
        search_id: str,
        *,
        expected_statuses: tuple[SearchStatus, ...] = (SearchStatus.RUNNING,),
        **kwargs: Any,
    ) -> bool:
        return services.app_store.update_search(
            search_id,
            expected_statuses=expected_statuses,
            expected_lease_owner=services.lease_owner,
            **kwargs,
        )

    async def prepare_prompt_refiner_request(state: SearchState) -> dict[str, object]:
        execution_mode = state.get("prompt_refiner_execution_mode")
        if execution_mode == "local":
            return {"prompt_refiner_request": None, "prompt_refiner_result": None}
        if execution_mode not in {"initial", "revision"}:
            raise SourceManifestMismatchError(
                "Prompt Refiner execution mode is missing or unsupported"
            )
        source_manifest = SourceManifest.model_validate(state["source_manifest"])
        guidance_payload = state.get("guidance_mask_asset")
        if not isinstance(guidance_payload, dict):
            raise SourceManifestMismatchError(
                "Multimodal Prompt Refiner requires the persisted Guidance Mask"
            )
        guidance_mask = AssetRef.model_validate(guidance_payload)
        if execution_mode == "initial":
            request = PromptRefinerRequest(
                search_id=state["search_id"],
                mode=PromptRefinementMode.INITIAL,
                round_index=state["round_index"],
                source_manifest=source_manifest,
                guidance_mask=guidance_mask,
                user_intent=state["user_intent"],
                generation_model=services.generation_model,
            )
        else:
            parent = _current_prompt_version(state)
            if parent is None:
                raise SourceManifestMismatchError(
                    "Prompt Refiner revision has no persisted parent PromptVersion"
                )
            anchor, evaluation = _persisted_anchor(services=services, state=state)
            raw_feedback = state.get("human_feedback")
            feedback = raw_feedback.strip() if isinstance(raw_feedback, str) else None
            request = PromptRefinerRequest(
                search_id=state["search_id"],
                mode=PromptRefinementMode.REVISION,
                round_index=state["round_index"],
                source_manifest=source_manifest,
                guidance_mask=guidance_mask,
                user_intent=state["user_intent"],
                human_feedback=feedback or None,
                human_selected_candidate_id=anchor.candidate_id,
                visual_anchor=anchor,
                selected_candidate_evaluation=evaluation,
                parent_prompt_version=parent,
                generation_model=services.generation_model,
            )
        request = prompt_service.enrich_request(request)
        request_payload = request.model_dump(mode="json")
        assert_checkpoint_safe(request_payload, path="prompt_refiner_request")
        request_key = PromptRefinerService.build_request_key(
            request,
            model=prompt_service.provider.model,
            provider_fingerprint=prompt_service.provider.provider_fingerprint,
            schema_version=prompt_service.provider.schema_version,
            proxy_version=prompt_service.provider.proxy_version,
            proxy_fingerprint=prompt_service.proxy_builder.fingerprint,
        )
        services.app_store.emit_event(
            search_id=state["search_id"],
            event_key=f"round:{state['round_index']}:prompt-refiner:started",
            event_type="prompt.refiner.started",
            payload={
                "round_index": state["round_index"],
                "mode": request.mode.value,
                "request_key": request_key,
                "source_manifest_hash": request.source_manifest.manifest_hash,
                "selected_candidate_id": request.human_selected_candidate_id,
                "based_on_prompt_version_id": (
                    request.parent_prompt_version.prompt_version_id
                    if request.parent_prompt_version is not None
                    else None
                ),
                "generation_mode": (
                    "candidate_anchored_rebase"
                    if request.visual_anchor is not None
                    else "source_rebase"
                ),
                "prompt_model": prompt_service.provider.model,
                "generation_model": services.generation_model,
                "prompt_schema_version": request.prompt_schema_version,
                "prompt_template_version": request.prompt_template_version,
            },
        )
        return {
            "prompt_refiner_request": request_payload,
            "prompt_refiner_result": None,
        }

    async def validate_prompt_refiner_request(state: SearchState) -> dict[str, object]:
        if state.get("prompt_refiner_execution_mode") == "local":
            return {}
        payload = state.get("prompt_refiner_request")
        if not isinstance(payload, dict):
            raise SourceManifestMismatchError("Prompt Refiner request was not prepared")
        request = PromptRefinerRequest.model_validate(payload)
        if request.search_id != state["search_id"]:
            raise SourceManifestMismatchError("Prompt Refiner request search lineage mismatch")
        if request.round_index != state["round_index"]:
            raise SourceManifestMismatchError("Prompt Refiner request round lineage mismatch")
        if request.source_manifest.manifest_hash != state["source_manifest_hash"]:
            raise SourceManifestMismatchError(
                "Prompt Refiner request source manifest lineage mismatch"
            )
        assert_checkpoint_safe(request.model_dump(mode="json"), path="prompt_refiner_request")
        return {"prompt_refiner_request": request.model_dump(mode="json")}

    async def invoke_prompt_refiner(state: SearchState) -> dict[str, object]:
        payload = state.get("prompt_refiner_request")
        if not isinstance(payload, dict):
            raise SourceManifestMismatchError("Prompt Refiner request is missing before invoke")
        request = PromptRefinerRequest.model_validate(payload)
        result = await prompt_service.refine(request)
        result_payload = result.model_dump(mode="json")
        assert_checkpoint_safe(result_payload, path="prompt_refiner_result")
        return {"prompt_refiner_result": result_payload}

    async def apply_local_prompt_version(state: SearchState) -> dict[str, object]:
        parent = _current_prompt_version(state)
        if parent is None:
            raise SourceManifestMismatchError(
                "Automatic source rebase has no professional base PromptVersion"
            )
        version = prompt_service.apply_local_directives(
            parent=parent,
            search_id=state["search_id"],
            source_manifest_hash=state["source_manifest_hash"],
            round_index=state["round_index"],
            active_directives=state.get("active_directives", []),
            human_feedback=(
                state.get("human_feedback")
                if isinstance(state.get("human_feedback"), str)
                else None
            ),
            generation_model=services.generation_model,
        )
        return await _apply_prompt_version(state, version, replayed=False)

    async def apply_prompt_refiner_result(state: SearchState) -> dict[str, object]:
        payload = state.get("prompt_refiner_result")
        if not isinstance(payload, dict):
            raise SourceManifestMismatchError("Prompt Refiner result is missing before apply")
        result = PromptRefinerResult.model_validate(payload)
        return await _apply_prompt_version(
            state,
            result.prompt_version,
            replayed=result.replayed,
            provider_result=result,
        )

    async def _apply_prompt_version(
        state: SearchState,
        version: PromptVersion,
        *,
        replayed: bool,
        provider_result: PromptRefinerResult | None = None,
    ) -> dict[str, object]:
        history = [
            dict(item)
            for item in state.get("prompt_history", [])
            if isinstance(item, dict)
            and item.get("prompt_version_id") != version.prompt_version_id
            and item.get("round_index") != version.round_index
        ]
        history.append(version.model_dump(mode="json"))

        def history_round(item: dict[str, object]) -> int:
            value = item.get("round_index")
            return value if isinstance(value, int) else 0

        history.sort(key=history_round)
        round_history = [dict(item) for item in state.get("round_history", [])]
        round_entry = next(
            (item for item in round_history if item.get("round_index") == version.round_index),
            None,
        )
        if round_entry is None:
            round_entry = {"round_index": version.round_index}
            round_history.append(round_entry)
        round_entry.update(_safe_prompt_lineage(version))
        round_history.sort(key=history_round)
        event_payload = {
            "round_index": version.round_index,
            **_safe_prompt_lineage(version),
            "replayed": replayed,
        }
        if not fenced_update(
            state["search_id"],
            prompt_history=history,
            round_history=round_history,
            active_directives=[item.model_dump(mode="json") for item in version.active_directives],
            events=(
                (
                    f"round:{version.round_index}:prompt-refiner:ready",
                    "prompt.refiner.ready",
                    event_payload,
                ),
            ),
        ):
            latest = services.app_store.get_search(state["search_id"])
            return {"status": latest.status.value}
        payload: dict[str, object] = {
            "current_prompt_version": version.model_dump(mode="json"),
            "prompt_version_id": version.prompt_version_id,
            "prompt_version_hash": version.prompt_version_hash,
            "based_on_prompt_version_id": version.based_on_prompt_version_id,
            "prompt_schema_version": version.prompt_schema_version,
            "prompt_template_version": version.prompt_template_version,
            "canonical_template_version": version.canonical_template_version,
            "prompt_model": version.prompt_model,
            "refinement_mode": version.refinement_mode.value,
            "generation_mode": version.generation_mode.value,
            "visual_anchor": (
                version.visual_anchor.model_dump(mode="json")
                if version.visual_anchor is not None
                else None
            ),
            "visual_anchor_candidate_id": version.visual_anchor_candidate_id,
            "visual_anchor_raw_asset_sha256": version.visual_anchor_raw_asset_sha256,
            "visual_anchor_asset": (
                version.visual_anchor_asset.model_dump(mode="json")
                if version.visual_anchor_asset is not None
                else None
            ),
            "professional_prompt_plan": (
                version.professional_prompt_plan.model_dump(mode="json")
                if version.professional_prompt_plan is not None
                else None
            ),
            "prompt_summary": version.prompt_summary,
            "provider_proposal_hash": version.provider_proposal_hash,
            "canonical_prompt": version.canonical_prompt,
            "canonical_prompt_hash": version.canonical_prompt_hash,
            "generation_prompt": version.generation_prompt,
            "generation_prompt_hash": version.generation_prompt_hash,
            "prompt_history": history,
            "round_history": round_history,
            "active_directives": [
                item.model_dump(mode="json") for item in version.active_directives
            ],
            "active_directives_hash": version.active_directives_hash,
            "prompt_refiner_result": (
                provider_result.model_dump(mode="json") if provider_result is not None else None
            ),
        }
        assert_checkpoint_safe(payload, path="prompt_refiner.output")
        return payload

    def route_after_validation(state: SearchState) -> str:
        return (
            "apply_local_prompt_version"
            if state.get("prompt_refiner_execution_mode") == "local"
            else "invoke_prompt_refiner"
        )

    graph = StateGraph(SearchState)
    graph.add_node("prepare_prompt_refiner_request", prepare_prompt_refiner_request)
    graph.add_node("validate_prompt_refiner_request", validate_prompt_refiner_request)
    graph.add_node("invoke_prompt_refiner", invoke_prompt_refiner)
    graph.add_node("apply_prompt_refiner_result", apply_prompt_refiner_result)
    graph.add_node("apply_local_prompt_version", apply_local_prompt_version)
    graph.add_edge(START, "prepare_prompt_refiner_request")
    graph.add_edge("prepare_prompt_refiner_request", "validate_prompt_refiner_request")
    graph.add_conditional_edges(
        "validate_prompt_refiner_request",
        route_after_validation,
        {
            "invoke_prompt_refiner": "invoke_prompt_refiner",
            "apply_local_prompt_version": "apply_local_prompt_version",
        },
    )
    graph.add_edge("invoke_prompt_refiner", "apply_prompt_refiner_result")
    graph.add_edge("apply_prompt_refiner_result", END)
    graph.add_edge("apply_local_prompt_version", END)
    return graph
