"""Standalone LangGraph control plane for one bounded Local Fix attempt."""

from __future__ import annotations

import asyncio
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.domain.local_fixes import LocalFixRequest, LocalFixResolution, LocalFixResult
from app.graphs.state import assert_checkpoint_safe
from app.services.local_fix_service import LocalFixService

LOCAL_FIX_STATE_SCHEMA_VERSION = "local-fix-state/v1"


class LocalFixState(TypedDict, total=False):
    """Independent, serializable state with no path back into SearchGraph."""

    schema_version: str
    request: dict[str, object]
    previous_result: dict[str, object] | None
    resolution: dict[str, object]
    result: dict[str, object]
    outcome: str


def build_local_fix_subgraph(service: LocalFixService) -> StateGraph[LocalFixState]:
    """Build a one-shot Local Fix subgraph.

    There is deliberately no edge back to SearchGraph and no loop from ``finalize``.
    Callers may explicitly invoke a new graph run with the prior structured result
    to consume the second (and final) permitted local generation depth.
    """

    async def resolve_local_fix_source(state: LocalFixState) -> dict[str, object]:
        state_version = state.get("schema_version")
        if state_version not in {None, LOCAL_FIX_STATE_SCHEMA_VERSION}:
            raise ValueError("Unsupported Local Fix checkpoint schema version")
        request = LocalFixRequest.model_validate(state["request"])
        previous_payload = state.get("previous_result")
        previous_result = (
            LocalFixResult.model_validate(previous_payload)
            if isinstance(previous_payload, dict)
            else None
        )
        resolution = await asyncio.to_thread(
            service.resolve,
            request,
            previous_result=previous_result,
        )
        payload: dict[str, object] = {
            "schema_version": LOCAL_FIX_STATE_SCHEMA_VERSION,
            "resolution": resolution.model_dump(mode="json"),
        }
        assert_checkpoint_safe(payload, path="local_fix.resolution")
        return payload

    async def apply_tight_local_fix(state: LocalFixState) -> dict[str, object]:
        resolution = LocalFixResolution.model_validate(state["resolution"])
        result = await service.apply(resolution)
        payload: dict[str, object] = {"result": result.model_dump(mode="json")}
        assert_checkpoint_safe(payload, path="local_fix.result")
        return payload

    def finalize_local_fix(state: LocalFixState) -> dict[str, object]:
        result = LocalFixResult.model_validate(state["result"])
        payload: dict[str, object] = {
            "outcome": result.outcome.value,
            "result": result.model_dump(mode="json"),
        }
        assert_checkpoint_safe(payload, path="local_fix.finalized")
        return payload

    graph = StateGraph(LocalFixState)
    graph.add_node("resolve_local_fix_source", resolve_local_fix_source)
    graph.add_node("apply_tight_local_fix", apply_tight_local_fix)
    graph.add_node("finalize_local_fix", finalize_local_fix)
    graph.add_edge(START, "resolve_local_fix_source")
    graph.add_edge("resolve_local_fix_source", "apply_tight_local_fix")
    graph.add_edge("apply_tight_local_fix", "finalize_local_fix")
    graph.add_edge("finalize_local_fix", END)
    return graph
