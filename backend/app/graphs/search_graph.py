from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from app.domain.assets import SourceManifest
from app.domain.candidates import CandidateRecord, CandidateResponse
from app.domain.searches import PlacementIntent, SearchStatus
from app.graphs.state import SearchState, assert_checkpoint_safe
from app.persistence.app_store import AppStore
from app.services.generator_service import (
    FAKE_IMAGE_MODEL,
    GenerationRequest,
    GeneratorService,
)
from app.services.prompt_compiler import (
    CANONICAL_TEMPLATE_VERSION,
    compile_canonical_prompt,
)

SEARCH_STATE_SCHEMA_VERSION = "search-state/v1"


@dataclass(frozen=True, slots=True)
class SearchGraphServices:
    app_store: AppStore
    generator_service: GeneratorService


def build_search_graph(services: SearchGraphServices) -> StateGraph[SearchState]:
    """Build the fixed mocked-round graph. No agent or hidden search loop is used."""

    async def initialize_search(state: SearchState) -> dict[str, object]:
        if services.app_store.get_search(state["search_id"]).status is SearchStatus.CANCELLED:
            return {"status": SearchStatus.CANCELLED.value, "round_index": 0}
        services.app_store.update_search(
            state["search_id"], status=SearchStatus.RUNNING, round_index=0
        )
        services.app_store.emit_event(
            search_id=state["search_id"],
            event_key="search:started",
            event_type="search.started",
            payload={"round_index": 0},
        )
        return {"status": SearchStatus.RUNNING.value, "round_index": 0}

    async def compile_prompt(state: SearchState) -> dict[str, object]:
        manifest = SourceManifest.model_validate(state["source_manifest"])
        placement = PlacementIntent.model_validate(state["placement"])
        prompt, prompt_hash = compile_canonical_prompt(
            placement=placement,
            user_intent=state["user_intent"],
            reference_count=len(manifest.cat_references),
        )
        return {
            "canonical_prompt": prompt,
            "canonical_prompt_hash": prompt_hash,
            "canonical_template_version": CANONICAL_TEMPLATE_VERSION,
        }

    async def prepare_round(state: SearchState) -> dict[str, object]:
        if services.app_store.get_search(state["search_id"]).status is SearchStatus.CANCELLED:
            return {"status": SearchStatus.CANCELLED.value, "current_candidates": []}
        services.app_store.emit_event(
            search_id=state["search_id"],
            event_key=f"round:{state['round_index']}:generation:started",
            event_type="round.generation.started",
            payload={
                "round_index": state["round_index"],
                "candidate_count": state["candidate_count"],
                "source_manifest_hash": state["source_manifest_hash"],
                "rebased_to_immutable_source": True,
            },
        )
        return {"current_candidates": []}

    async def generate_candidates(state: SearchState) -> dict[str, object]:
        if services.app_store.get_search(state["search_id"]).status is SearchStatus.CANCELLED:
            return {"status": SearchStatus.CANCELLED.value, "current_candidates": []}
        manifest = SourceManifest.model_validate(state["source_manifest"])
        request = GenerationRequest(
            search_id=state["search_id"],
            source_manifest=manifest,
            placement=PlacementIntent.model_validate(state["placement"]),
            prompt=state["canonical_prompt"],
            prompt_hash=state["canonical_prompt_hash"],
            round_index=state["round_index"],
            candidate_count=state["candidate_count"],
            model=FAKE_IMAGE_MODEL,
            quality="medium",
            size=f"{manifest.background.width}x{manifest.background.height}",
        )
        records = await services.generator_service.generate_round(
            request,
            expected_manifest_hash=state["source_manifest_hash"],
        )
        candidate_payloads = [item.model_dump(mode="json") for item in records]
        assert_checkpoint_safe(candidate_payloads, path="current_candidates")
        return {"current_candidates": candidate_payloads}

    async def finalize_mock_round(state: SearchState) -> dict[str, object]:
        if services.app_store.get_search(state["search_id"]).status is SearchStatus.CANCELLED:
            return {
                "status": SearchStatus.CANCELLED.value,
                "stop_reason": "cancelled_by_user",
            }
        candidate_payloads = state["current_candidates"]
        public_candidates = [
            CandidateResponse.from_record(CandidateRecord.model_validate(item)).model_dump(
                mode="json"
            )
            for item in candidate_payloads
        ]
        summary: dict[str, object] = {
            "schema_version": state["schema_version"],
            "source_manifest_hash": state["source_manifest_hash"],
            "round_index": state["round_index"],
            "candidate_ids": [item["candidate_id"] for item in candidate_payloads],
            "stop_reason": "mock_round_complete",
        }
        assert_checkpoint_safe(summary)
        services.app_store.update_search(
            state["search_id"],
            status=SearchStatus.WAITING_FOR_HUMAN,
            round_index=state["round_index"],
            stop_reason="mock_round_complete",
            state_summary=summary,
            clear_lease=True,
        )
        services.app_store.emit_event(
            search_id=state["search_id"],
            event_key="search:waiting-for-human",
            event_type="search.waiting_for_human",
            payload={
                "round_index": state["round_index"],
                "candidates": public_candidates,
                "stop_reason": "mock_round_complete",
            },
        )
        return {
            "status": SearchStatus.WAITING_FOR_HUMAN.value,
            "stop_reason": "mock_round_complete",
        }

    graph = StateGraph(SearchState)
    graph.add_node("initialize_search", initialize_search)
    graph.add_node("compile_canonical_prompt", compile_prompt)
    graph.add_node("prepare_round", prepare_round)
    graph.add_node("generate_candidates", generate_candidates)
    graph.add_node("finalize_mock_round", finalize_mock_round)
    graph.add_edge(START, "initialize_search")
    graph.add_edge("initialize_search", "compile_canonical_prompt")
    graph.add_edge("compile_canonical_prompt", "prepare_round")
    graph.add_edge("prepare_round", "generate_candidates")
    graph.add_edge("generate_candidates", "finalize_mock_round")
    graph.add_edge("finalize_mock_round", END)
    return graph
