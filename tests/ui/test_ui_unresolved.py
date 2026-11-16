"""UI tests for saved items and edges that cannot be restored."""

import pytest
from panel.tests.util import serve_component

from panel_flowdash import DashboardEdge, DashboardItem, DashboardModel, register
from panel_flowdash.editor import FlowDash

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


@register(page=False, component=True, provides=[{"key": "value", "type": "str"}])
def source_component(config):
    return "source"


def _editor(**params):
    editor = FlowDash(
        {"Test/source": source_component},
        notifications=False,
        include_builtin_components=False,
        **params,
    )
    editor.load_model(
        DashboardModel(
            dashboard_id="d",
            user_id="u",
            title="Partly missing",
            items=[
                DashboardItem(instance_id="s1", component_id="Test/source", x=0, y=0),
                DashboardItem(instance_id="g1", component_id="Gone/widget", x=320, y=0),
            ],
            edges=[
                DashboardEdge(source="s1", source_port="value", target="g1", target_port="in"),
            ],
        )
    )
    return editor


def test_placeholder_node_and_broken_edge_listing(page):
    serve_component(page, _editor())

    node = page.locator(".react-flow__node").filter(has_text="Gone/widget")
    expect(node).to_be_visible()
    expect(node).to_contain_text("is not available")
    expect(page.get_by_text("1 saved connection could not be restored")).to_be_visible()
    expect(page.get_by_text("source.value → Gone/widget.in")).to_be_visible()


def test_placeholder_tile_in_dashboard_mode(page):
    serve_component(page, _editor(editable=False))

    expect(page.get_by_text("Component 'Gone/widget' is not available")).to_be_visible()
