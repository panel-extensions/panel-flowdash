"""UI test: every node card on the canvas renders its content after a burst of edits."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash.agent import DashboardOps
from panel_flowdash.editor import FlowDash

from .test_ui_view_header import COMPONENTS
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _node_heights(page):
    return {
        node.get_attribute("data-id"): node.bounding_box()["height"]
        for node in page.locator(".react-flow__node").all()
    }


@pytest.mark.xfail(
    reason="panel-reactflow drops a node's view that arrives after the node "
    "(panel-extensions/panel-reactflow#93)",
    strict=False,
)
@pytest.mark.parametrize("run", range(20))
def test_every_node_card_has_its_content_after_quick_edits(page, run):
    editor = FlowDash(COMPONENTS, notifications=False)
    editor.new_dashboard("Burst")
    ops = DashboardOps(editor)
    serve_component(page, editor)

    ops.add_node("Test/picker", name="picker")
    for i in range(3):
        ops.add_node("Test/chart", name=f"chart{i}")
        ops.connect("picker.picked", f"chart{i}.unit")

    wait_until(lambda: len(_node_heights(page)) == 4, page, timeout=10000)
    page.wait_for_timeout(1500)
    heights = _node_heights(page)
    assert all(height > 20 for height in heights.values()), heights
