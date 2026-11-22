"""UI test: the canvas refuses an output whose type a multiple input's items do not take."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash, MemoryDashboardStore
from panel_flowdash.dashboard_store import DashboardItem

from ..test_multiple_input_types import COMPONENTS
from .util import center, serve_component, stable_box

pytest.importorskip("playwright")

pytestmark = pytest.mark.ui


def test_dragging_onto_a_multiple_input_of_another_type_is_refused(page):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Join")
    model.items = [
        DashboardItem("c", "T/count", x=0, y=0),
        DashboardItem("j", "T/join", x=360, y=0),
    ]
    store.save_dashboard(model)
    editor = FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        dashboard=model.dashboard_id,
    )
    serve_component(page, editor)

    output = page.locator(".react-flow__node[data-id='c'] .react-flow__handle-right")
    texts = page.locator(
        ".react-flow__node[data-id='j'] .react-flow__handle-left[data-handleid='texts']"
    )
    things = page.locator(
        ".react-flow__node[data-id='j'] .react-flow__handle-left[data-handleid='things']"
    )
    x, y = center(stable_box(output, page))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 25, y)
    wait_until(lambda: "rf-handle-invalid" in (texts.get_attribute("class") or ""), timeout=8000)
    assert "Type mismatch" in texts.get_attribute("data-tooltip")
    assert "rf-handle-invalid" not in (things.get_attribute("class") or "")
    page.mouse.move(*center(texts.bounding_box()))
    page.mouse.up()

    assert editor.graph.edges == []
    assert editor.to_model().edges == []
