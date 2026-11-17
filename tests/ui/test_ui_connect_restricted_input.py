"""UI test: the canvas refuses a connection into an input a restricted component feeds."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash, MemoryDashboardStore
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem

from ..test_connect_restricted_input import COMPONENTS
from .util import center, serve_component, stable_box

pytest.importorskip("playwright")

pytestmark = pytest.mark.ui


def test_dragging_onto_an_input_a_restricted_component_feeds_is_refused(page):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    model.items = [
        DashboardItem("secret", "T/secret", x=0, y=200),
        DashboardItem("src", "T/source", x=0, y=0),
        DashboardItem("sink", "T/sink", x=320, y=0),
    ]
    model.edges = [DashboardEdge("secret", "value", "sink", "value")]
    store.save_dashboard(model)
    editor = FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        authorize_component=lambda entry: entry.app_id != "T/secret",
        dashboard=model.dashboard_id,
    )
    serve_component(page, editor)

    output = page.locator(".react-flow__node[data-id='src'] .react-flow__handle-right")
    input_handle = page.locator(
        ".react-flow__node[data-id='sink'] .react-flow__handle-left[data-handleid='value']"
    )
    x, y = center(stable_box(output, page))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 25, y)
    wait_until(
        lambda: "rf-handle-invalid" in (input_handle.get_attribute("class") or ""), timeout=8000
    )
    assert "do not have access to" in input_handle.get_attribute("data-tooltip")
    page.mouse.move(*center(input_handle.bounding_box()))
    page.mouse.up()

    assert editor.graph.edges == []
    assert [(e.source, e.target) for e in editor.to_model().edges] == [("secret", "sink")]
