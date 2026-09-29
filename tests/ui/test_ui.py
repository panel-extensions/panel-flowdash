"""UI Test Module."""
# import time

import json

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


def test_palette_click_places_component_inside_panned_viewport(page):
    editor = FlowDash(
        {"Test/source": source_component}, notifications=False, include_builtin_components=False
    )
    editor.add_component("Test/source", position=(0, 0))
    serve_component(page, editor)

    pane = page.locator(".react-flow__pane")
    box = pane.bounding_box()
    wait_until(lambda: editor._flow.viewport is not None, timeout=8000)
    initial_x = editor._flow.viewport["x"]
    # Grab empty canvas clear of the fitted node, and pan far enough that the
    # next slot of a fixed grid would land off screen.
    start = (box["x"] + box["width"] * 0.15, box["y"] + 60)
    page.mouse.move(*start)
    page.mouse.down()
    page.mouse.move(start[0] + 700, start[1] + 300, steps=10)
    page.mouse.up()
    wait_until(lambda: editor._flow.viewport["x"] > initial_x + 500, timeout=8000)

    page.locator(".MuiListItemButton-root").filter(has_text="Source").click()
    wait_until(lambda: len(editor._flow.nodes) == 2, timeout=8000)

    new_node = page.locator(".react-flow__node").nth(1)
    node_box = new_node.bounding_box()
    assert box["x"] <= node_box["x"] < box["x"] + box["width"]
    assert box["y"] <= node_box["y"] < box["y"] + box["height"]


def test_clear_requires_confirmation(page):
    editor = FlowDash({"Test/source": source_component}, notifications=False)
    editor.add_component("Test/source")
    serve_component(page, editor)

    clear = page.get_by_role("button", name="Clear", exact=True)
    clear.click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Cancel").click()
    wait_until(lambda: not editor._clear_dialog.open, timeout=8000)
    assert len(editor._tile_items) == 1

    clear.click()
    page.get_by_role("dialog").get_by_role("button", name="Clear canvas").click()
    wait_until(lambda: editor._tile_items == [], timeout=8000)


def test_builtin_widget_selection_reaches_graph(page):
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Widgets/Select", config={"default_options": ["A", "B"]})
    editor.mode = "dashboard"
    serve_component(page, editor)

    selection = page.get_by_role("combobox", name="Select")
    selection.click()
    wait_until(lambda: editor._tile_objects[0].dropdown_open, timeout=8000)
    assert editor.graph.get_state(node).selected == "A"


def test_palette_drag_adds_and_wires_components(page):
    editor = FlowDash(
        {"Test/source": source_component, "Test/sink": sink_component},
        notifications=False,
        include_builtin_components=False,
    )
    sink = editor.add_component("Test/sink", position=(300, 100))
    serve_component(page, editor)

    palette = page.locator(".MuiListItemButton-root")
    pane = page.locator(".react-flow__pane")
    expect_count = page.locator(".react-flow__node")
    wait_until(lambda: expect_count.count() == 1, timeout=8000)

    palette.filter(has_text="Source").drag_to(
        page.locator(f".react-flow__node[data-id='{sink}'] .react-flow__handle-left")
    )
    wait_until(lambda: len(editor.graph.edges) == 1, timeout=8000)
    source = editor.graph.edges[0]["source"]
    assert editor.graph.edges[0]["target"] == sink
    assert next(n for n in editor._flow.nodes if n["id"] == source)["position"]["x"] < 300

    palette.filter(has_text="Sink").drag_to(pane, target_position={"x": 40, "y": 40})
    wait_until(lambda: len(editor._tile_items) == 3, timeout=8000)
    assert len(editor.graph.edges) == 1


def _drop_file(locator, name, content):
    """Drop *content* on *locator* as an OS file, returning whether the canvas accepted it."""
    return locator.evaluate(
        """(el, [name, content]) => {
          const rect = el.getBoundingClientRect()
          const dt = new DataTransfer()
          dt.items.add(new File([content], name, { type: "application/json" }))
          const init = {
            dataTransfer: dt, bubbles: true, cancelable: true, composed: true,
            clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2,
          }
          const over = new DragEvent("dragover", init)
          el.dispatchEvent(over)
          el.dispatchEvent(new DragEvent("drop", init))
          return over.defaultPrevented
        }""",
        [name, content],
    )


def test_downloaded_dashboard_recreated_by_dropping_it(page, tmp_path):
    components = {"Test/source": source_component, "Test/sink": sink_component}
    editor = FlowDash(components, notifications=False, include_builtin_components=False)
    editor.new_dashboard("Wired pair")
    src = editor.add_component("Test/source", position=(0, 0))
    dst = editor.add_component("Test/sink", position=(300, 0))
    editor.connect(src, "value", dst, "value")
    serve_component(page, editor)

    with page.expect_download() as download:
        page.get_by_role("button", name="Download").click()
    assert download.value.suggested_filename == "Wired_pair.json"
    path = tmp_path / "export.json"
    download.value.save_as(path)
    content = path.read_text()
    assert json.loads(content)["edges"][0]["source"] == src

    page.get_by_role("button", name="Clear", exact=True).click()
    page.get_by_role("dialog").get_by_role("button", name="Clear canvas").click()
    wait_until(lambda: editor._tile_items == [], timeout=8000)

    pane = page.locator(".react-flow__pane")
    assert _drop_file(pane, "export.json", content)
    wait_until(lambda: len(editor.graph.edges) == 1, timeout=8000)
    assert [i["instance_id"] for i in editor._tile_items] == [src, dst]
    wait_until(lambda: page.locator(".react-flow__node").count() == 2, timeout=8000)
    wait_until(lambda: page.locator(".react-flow__edge").count() == 1, timeout=8000)


def test_dropping_file_on_populated_canvas_asks_first(page):
    editor = FlowDash({"Test/source": source_component}, notifications=False)
    existing = editor.add_component("Test/source")
    export = FlowDash({"Test/source": source_component}, notifications=False)
    replacement = export.add_component("Test/source")
    serve_component(page, editor)

    pane = page.locator(".react-flow__pane")
    assert _drop_file(pane, "other.json", json.dumps(export.export_dashboard()))
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Cancel").click()
    wait_until(lambda: not editor._import_dialog.open, timeout=8000)
    assert editor._tile_items[0]["instance_id"] == existing

    assert _drop_file(pane, "other.json", json.dumps(export.export_dashboard()))
    page.get_by_role("dialog").get_by_role("button", name="Replace canvas").click()
    wait_until(lambda: editor._tile_items[0]["instance_id"] == replacement, timeout=8000)
