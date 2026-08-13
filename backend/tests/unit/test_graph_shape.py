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
        "prepare_round",
        "generate_candidates",
        "evaluate_round",
        "prepare_feedback_planner",
        "feedback_planner",
        "apply_feedback_plan",
        "finalize_mock_round",
        "prepare_next_round",
    }
    xray_nodes = set(graph.compile().get_graph(xray=True).nodes)
    assert {
        "feedback_planner:select_actionable_blocking_issues",
        "feedback_planner:plan_directives",
        "feedback_planner:validate_directive_budget",
        "feedback_planner:replace_or_retain_directives",
        "feedback_planner:emit_next_round_plan",
    }.issubset(xray_nodes)


def test_checkpoint_guard_rejects_binary_and_image_data_urls() -> None:
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": b"png bytes"})
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": "data:image/png;base64,AAAA"})
