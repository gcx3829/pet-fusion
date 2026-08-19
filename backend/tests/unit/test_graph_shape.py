import pytest

from app.config import Settings
from app.graphs.search_graph import SearchGraphServices, build_search_graph
from app.graphs.state import assert_checkpoint_safe


def test_explicit_state_graph_has_evaluation_round_nodes(settings: Settings) -> None:
    from app.container import AppContainer

    container = AppContainer.build(settings)
    container.initialize()
    graph = build_search_graph(
        SearchGraphServices(
            app_store=container.app_store,
            generator_service=container.generator_service,
        )
    )
    assert set(graph.nodes) == {
        "initialize_search",
        "compile_canonical_prompt",
        "multimodal_prompt_subgraph",
        "prepare_round",
        "generate_candidates",
        "critic_subgraph",
        "rank_round",
        "prepare_feedback_planner",
        "feedback_planner",
        "apply_feedback_plan",
        "finalize_mock_round",
        "prepare_next_round",
    }
    xray_nodes = set(graph.compile().get_graph(xray=True).nodes)
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
        "multimodal_prompt_subgraph:prepare_prompt_refiner_request",
        "multimodal_prompt_subgraph:validate_prompt_refiner_request",
        "multimodal_prompt_subgraph:invoke_prompt_refiner",
        "multimodal_prompt_subgraph:apply_prompt_refiner_result",
        "multimodal_prompt_subgraph:apply_local_prompt_version",
    }.issubset(xray_nodes)


def test_checkpoint_guard_rejects_binary_and_image_data_urls() -> None:
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": b"png bytes"})
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": "data:image/png;base64,AAAA"})
