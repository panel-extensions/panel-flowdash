"""UI tests: every node card on the canvas keeps its content through bursts of edits and palette drops."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash.agent import DashboardOps
from panel_flowdash.editor import FlowDash

from .test_ui_palette import _editor, _item, _search
from .test_ui_view_header import COMPONENTS
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _node_heights(page):
    return {
        node.get_attribute("data-id"): node.bounding_box()["height"]
        for node in page.locator(".react-flow__node").all()
    }


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


@pytest.mark.parametrize("run", range(10))
def test_dropping_from_the_palette_keeps_the_other_node_cards(page, run):
    editor = _editor()
    ops = DashboardOps(editor)
    serve_component(page, editor)
    ops.add_node("Test/prices", name="prices")
    ops.add_node("Test/chart", name="chart")
    wait_until(lambda: len(_node_heights(page)) == 2, page, timeout=10000)

    for x in (80, 300):
        _search(page).fill("chart")
        wait_until(lambda: editor._palette_panel.query == "chart", page=page, timeout=8000)
        _item(page, "Price Chart").drag_to(
            page.locator(".react-flow__pane"), target_position={"x": x, "y": 300}
        )

    wait_until(lambda: len(_node_heights(page)) == 4, page, timeout=10000)
    page.wait_for_timeout(1500)
    heights = _node_heights(page)
    assert all(height > 20 for height in heights.values()), heights
