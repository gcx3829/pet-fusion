import pytest

from app.graphs.search_graph import SearchGraphServices, build_search_graph
from app.graphs.state import assert_checkpoint_safe


def test_explicit_state_graph_has_fixed_mock_vertical_slice_nodes(settings) -> None:
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
        "finalize_mock_round",
    }


def test_checkpoint_guard_rejects_binary_and_image_data_urls() -> None:
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": b"png bytes"})
    with pytest.raises(TypeError):
        assert_checkpoint_safe({"image": "data:image/png;base64,AAAA"})
