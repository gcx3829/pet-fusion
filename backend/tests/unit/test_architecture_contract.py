"""Offline contracts for the public architecture and provider safety boundaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.container import AppContainer
from app.graphs.local_fix_graph import build_local_fix_subgraph
from app.graphs.search_graph import SearchGraphServices, build_search_graph
from app.graphs.state import assert_checkpoint_safe
from app.main import create_app
from app.services.generator_service import (
    DeterministicFakeImageGenerator,
    OpenAIImageGenerator,
)
from app.services.local_fix_service import (
    DeterministicFakeLocalFixProvider,
    LocalFixService,
)
from app.services.openai_critic_client import OfficialOpenAICriticProvider


def test_offline_architecture_contract_keeps_graphs_explicit_and_checkpoint_safe(
    settings: Settings,
) -> None:
    container = AppContainer.build(settings)
    container.initialize()

    search_graph = build_search_graph(
        SearchGraphServices(
            app_store=container.app_store,
            generator_service=container.generator_service,
        )
    )
    xray_nodes = set(search_graph.compile().get_graph(xray=True).nodes)
    assert {
        "critic_subgraph:build_critic_inputs",
        "critic_subgraph:fan_out_candidate_evaluations",
        "critic_subgraph:evaluate_candidate",
        "critic_subgraph:collect_evaluations",
        "critic_subgraph:normalize_critic_findings",
        "feedback_planner:select_actionable_blocking_issues",
        "feedback_planner:plan_directives",
        "feedback_planner:validate_directive_budget",
        "feedback_planner:replace_or_retain_directives",
        "feedback_planner:emit_next_round_plan",
    }.issubset(xray_nodes)

    local_fix_graph = build_local_fix_subgraph(
        LocalFixService(
            provider=DeterministicFakeLocalFixProvider(),
            app_store=container.app_store,
            asset_store=container.asset_store,
        )
    ).compile()
    assert set(local_fix_graph.get_graph().nodes) == {
        "__start__",
        "resolve_local_fix_source",
        "apply_tight_local_fix",
        "finalize_local_fix",
        "__end__",
    }
    assert {
        ("__start__", "resolve_local_fix_source"),
        ("resolve_local_fix_source", "apply_tight_local_fix"),
        ("apply_tight_local_fix", "finalize_local_fix"),
        ("finalize_local_fix", "__end__"),
    } == {
        (edge.source, edge.target) for edge in local_fix_graph.get_graph().edges
    }

    checkpoint_payload = {
        "search_id": "search-contract",
        "source_manifest": {
            "background": {
                "asset_id": "src_background",
                "path": "/server-only/assets/background.png",
                "sha256": "0" * 64,
                "mime_type": "image/png",
                "width": 1024,
                "height": 768,
            },
            "cat_references": [
                {
                    "asset_id": "src_reference",
                    "path": "/server-only/assets/reference.png",
                    "sha256": "1" * 64,
                    "mime_type": "image/png",
                    "width": 512,
                    "height": 512,
                }
            ],
        },
        "candidate": {
            "candidate_id": "candidate-contract",
            "raw_asset_id": "cand_raw",
            "protected_asset_id": "cand_protected",
        },
    }
    assert_checkpoint_safe(checkpoint_payload)
    serialized = json.dumps(checkpoint_payload, sort_keys=True)
    assert "base64" not in serialized
    assert "data:image" not in serialized
    with pytest.raises(TypeError, match="Binary checkpoint value"):
        assert_checkpoint_safe({"candidate": {"image": b"not-a-reference"}})
    with pytest.raises(TypeError, match="Image data URL"):
        assert_checkpoint_safe(
            {"candidate": {"image": "data:image/png;base64,not-a-reference"}}
        )


def test_default_pytest_harness_overrides_an_unsafe_dotenv(tmp_path: Path) -> None:
    unsafe_dotenv = tmp_path / ".env"
    unsafe_dotenv.write_text(
        "FAKE_GENERATOR=0\n"
        "PET_FUSION_FAKE_CRITIC=0\n"
        "OPENAI_API_KEY=live-key-that-tests-must-ignore\n"
        "PET_FUSION_OPENAI_BASE_URL=https://relay.invalid/v1\n",
        encoding="utf-8",
    )

    resolved = Settings(_env_file=unsafe_dotenv)

    assert resolved.fake_generator is True
    assert resolved.fake_critic is True
    assert not (
        resolved.openai_api_key and resolved.openai_api_key.get_secret_value()
    )
    assert not resolved.openai_base_url


def test_provider_switches_keep_credentials_server_side_and_routes_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_client_accesses: list[str] = []

    def forbid_image_client(_transport: object) -> None:
        provider_client_accesses.append("image")
        raise AssertionError("offline architecture tests must not construct a network client")

    def forbid_critic_client(_provider: object) -> None:
        provider_client_accesses.append("critic")
        raise AssertionError("offline architecture tests must not construct a network client")

    monkeypatch.setattr(
        "app.services.openai_image_client.OfficialOpenAIImageEditsTransport._get_client",
        forbid_image_client,
    )
    monkeypatch.setattr(
        "app.services.openai_critic_client.OfficialOpenAICriticProvider._get_client",
        forbid_critic_client,
    )

    secret = "test-openai-key-must-not-leak"
    offline_settings = Settings(
        data_dir=tmp_path / "offline-data",
        fake_generator=True,
        fake_critic=True,
        openai_api_key=SecretStr(secret),
    )
    offline_container = AppContainer.build(offline_settings)
    assert isinstance(offline_container.image_generator, DeterministicFakeImageGenerator)
    assert offline_container.search_runner.critic_service is None

    live_settings = Settings(
        data_dir=tmp_path / "live-data",
        fake_generator=False,
        fake_critic=False,
        openai_api_key=SecretStr(secret),
        openai_base_url="https://relay.invalid/v1",
    )
    live_container = AppContainer.build(live_settings)
    assert isinstance(live_container.image_generator, OpenAIImageGenerator)
    assert isinstance(live_container.search_runner.critic_service, OfficialOpenAICriticProvider)
    assert secret not in repr(live_settings)

    # Construction and public route discovery are deliberately network-free: the
    # official SDK clients are lazy and provider calls require a submitted search.
    app = create_app(live_settings)
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        openapi = client.get("/openapi.json")
        create_export = client.post("/api/v1/searches/missing/export", json={})
        get_export = client.get(
            "/api/v1/searches/missing/exports/" + "0" * 64,
        )
    assert health.json() == {"status": "ok", "generator": "openai"}
    assert secret not in health.text
    assert secret not in openapi.text
    assert create_export.status_code == get_export.status_code == 404
    assert create_export.json()["error"]["code"] == "NOT_FOUND"
    assert get_export.json()["error"]["code"] == "NOT_FOUND"
    assert provider_client_accesses == []
