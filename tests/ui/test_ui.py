"""UI Test Module."""
# import time

import panel as pn
import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

pytest.importorskip("playwright")
from playwright.sync_api import expect

# from panel.pane import panel
# from panel.tests.util import serve_component
# from playwright.sync_api import expect

pytestmark = pytest.mark.ui


def test_param_defer_load(page):
    """Example of a UI test using Playwright."""
    # def defer_load():
    #     time.sleep(0.5)
    #     return "I render after load!"

    # component = panel(defer_load, defer_load=True)

    # serve_component(page, component)

    # assert page.locator(".pn-loading")
    # expect(page.locator(".markdown").locator("div")).to_have_text("I render after load!\n")


@register(page=False, component=True, provides=[{"key": "value", "type": "str"}])
def source_component(config):
    return "source"


@register(page=False, component=True, requires=[{"key": "value", "type": "str"}])
def sink_component(config):
    return "sink"


def test_editor_drag_validation_rejects_occupied_input(page):
    editor = FlowDash(
        {"Test/source": source_component, "Test/sink": sink_component}, notifications=False
    )
    source_a = editor.add_component("Test/source", position=(0, 0))
    source_b = editor.add_component("Test/source", position=(0, 180))
    sink = editor.add_component("Test/sink", position=(280, 0))
    editor.connect(source_a, "value", sink, "value")
    serve_component(page, editor)

    output = (
        page.locator(".react-flow__node")
        .filter(has_text="source")
        .nth(1)
        .locator(".react-flow__handle-right")
    )
    input_handle = (
        page.locator(".react-flow__node")
        .filter(has_text="sink")
        .locator(".react-flow__handle-left")
    )
    origin = output.bounding_box()
    page.mouse.move(origin["x"] + origin["width"] / 2, origin["y"] + origin["height"] / 2)
    page.mouse.down()
    page.mouse.move(origin["x"] + origin["width"] / 2 + 25, origin["y"] + origin["height"] / 2)
    wait_until(
        lambda: "rf-handle-invalid" in (input_handle.get_attribute("class") or ""), timeout=8000
    )
    assert "already has a connection" in input_handle.get_attribute("data-tooltip")
    destination = input_handle.bounding_box()
    page.mouse.move(
        destination["x"] + destination["width"] / 2, destination["y"] + destination["height"] / 2
    )
    page.mouse.up()

    assert len(editor.graph.edges) == 1
    assert editor.graph.edges[0]["source"] == source_a
    assert source_b not in [edge["source"] for edge in editor.graph.edges]


@register(page=False, component=True)
class SplitView(pn.viewable.Viewer):
    def __init__(self, **params):
        super().__init__(**params)
        self.first = pn.pane.Markdown("First part")
        self.second = pn.pane.Markdown("Second part")

    def __panel__(self):
        return pn.Column(self.first, self.second)

    def __flowdash__(self):
        return {"first": self.first, "second": self.second}


def test_editor_renders_parts_as_separate_tiles(page):
    editor = FlowDash({"Test/split": SplitView}, notifications=False)
    editor.add_component("Test/split")
    editor.mode = "dashboard"
    serve_component(page, editor)

    tiles = page.locator(".muuri-item")
    expect(tiles).to_have_count(2)
    expect(tiles.nth(0)).to_contain_text("First part")
    expect(tiles.nth(1)).to_contain_text("Second part")
