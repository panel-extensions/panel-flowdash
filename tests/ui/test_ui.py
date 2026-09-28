"""UI Test Module."""
# import time

import param
import pytest
from panel.tests.util import serve_component, wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

pytest.importorskip("playwright")

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


@register(page=False, component=True, provides=[{"key": "picked"}])
class Picker(Viewer):
    value = param.Parameter(default="A")

    @param.output(param.Parameter)
    @param.depends("value")
    def picked(self):
        return self.value

    def __panel__(self):
        return "picker"


class YearRange(Viewer):
    start_year = param.Integer(default=2000)

    def __panel__(self):
        return "years"


def test_editor_drag_validation_rejects_untyped_value_of_wrong_type(page):
    editor = FlowDash({"Test/picker": Picker, "Test/years": YearRange}, notifications=False)
    editor.add_component("Test/picker", position=(0, 0))
    editor.add_component("Test/years", position=(420, 0))
    serve_component(page, editor)

    output = page.locator(".react-flow__handle-right[data-handleid='picked']")
    input_handle = page.locator(".react-flow__handle-left[data-handleid='start_year']")
    origin = output.bounding_box()
    page.mouse.move(origin["x"] + origin["width"] / 2, origin["y"] + origin["height"] / 2)
    page.mouse.down()
    page.mouse.move(origin["x"] + origin["width"] / 2 + 25, origin["y"] + origin["height"] / 2)
    wait_until(
        lambda: "rf-handle-invalid" in (input_handle.get_attribute("class") or ""), timeout=8000
    )
    assert "must be an integer" in input_handle.get_attribute("data-tooltip")
    destination = input_handle.bounding_box()
    page.mouse.move(
        destination["x"] + destination["width"] / 2, destination["y"] + destination["height"] / 2
    )
    page.mouse.up()
    page.wait_for_timeout(500)

    assert editor.graph.edges == []
    assert editor._flow.edges == []
