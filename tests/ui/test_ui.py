"""UI Test Module."""
# import time

import json
import re

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.tests.util import serve_component, wait_until
from panel.viewable import Viewer

from panel_flowdash import (
    DashboardItem,
    DashboardModel,
    MemoryDashboardStore,
    current_dashboard,
    register,
)
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, resolve_identity
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.util import confirm

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

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


def _palette_item(page, title):
    # Matched on the title alone: the "Data sources" header also contains "source".
    primary = page.locator(".MuiListItemText-primary", has_text=re.compile(f"^{title}$"))
    return page.locator(".MuiListItemButton-root", has=primary)


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

    _palette_item(page, "source").click()
    wait_until(lambda: len(editor._flow.nodes) == 2, timeout=8000)

    new_node = page.locator(".react-flow__node").nth(1)
    node_box = new_node.bounding_box()
    assert box["x"] <= node_box["x"] < box["x"] + box["width"]
    assert box["y"] <= node_box["y"] < box["y"] + box["height"]


def test_clear_requires_confirmation(page):
    editor = FlowDash({"Test/source": source_component}, notifications=False)
    editor.add_component("Test/source")
    serve_component(page, editor)

    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Clear canvas").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Cancel").click()
    wait_until(lambda: not editor._clear_dialog.open, timeout=8000)
    assert len(editor._tile_items) == 1

    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Clear canvas").click()
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

    pane = page.locator(".react-flow__pane")
    expect_count = page.locator(".react-flow__node")
    wait_until(lambda: expect_count.count() == 1, timeout=8000)

    _palette_item(page, "source").drag_to(
        page.locator(f".react-flow__node[data-id='{sink}'] .react-flow__handle-left")
    )
    wait_until(lambda: len(editor.graph.edges) == 1, timeout=8000)
    source = editor.graph.edges[0]["source"]
    assert editor.graph.edges[0]["target"] == sink
    assert next(n for n in editor._flow.nodes if n["id"] == source)["position"]["x"] < 300

    _palette_item(page, "sink").drag_to(pane, target_position={"x": 40, "y": 40})
    wait_until(lambda: len(editor._tile_items) == 3, timeout=8000)
    assert len(editor.graph.edges) == 1


def test_loading_into_rendered_editor_shows_every_node_view(page):
    components = {"Test/source": source_component, "Test/sink": sink_component}
    editor = FlowDash(components, notifications=False, include_builtin_components=False)
    editor.add_component("Test/source", position=(0, 0))
    saved = FlowDash(components, notifications=False, include_builtin_components=False)
    src = saved.add_component("Test/source", position=(0, 0))
    dst = saved.add_component("Test/sink", position=(300, 0))
    saved.connect(src, "value", dst, "value")
    model = saved.to_model()
    load = pmui.Button(label="Load saved")
    load.on_click(lambda _: editor.load_model(model))
    serve_component(page, pn.Column(load, editor))

    nodes = page.locator(".react-flow__node")
    wait_until(lambda: nodes.count() == 1, timeout=8000)
    page.get_by_role("button", name="Load saved", exact=True).click()
    wait_until(lambda: nodes.count() == 2, timeout=8000)
    wait_until(lambda: page.locator(".react-flow__edge").count() == 1, timeout=8000)
    wait_until(lambda: nodes.filter(has_text="sink").count() == 1, timeout=8000)
    wait_until(lambda: nodes.filter(has_text="source").count() == 1, timeout=8000)


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
        page.get_by_role("button", name="More actions").click()
        page.get_by_role("menuitem", name="Download").click()
    assert download.value.suggested_filename == "Wired_pair.json"
    path = tmp_path / "export.json"
    download.value.save_as(path)
    content = path.read_text()
    assert json.loads(content)["edges"][0]["source"] == src

    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Clear canvas").click()
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
    assert [i["instance_id"] for i in editor._tile_items] == [existing]

    assert _drop_file(pane, "other.json", json.dumps(export.export_dashboard()))
    page.get_by_role("dialog").get_by_role("button", name="Replace canvas").click()
    # Loading clears the canvas before re-adding tiles, so the list is briefly empty.
    wait_until(
        lambda: [i["instance_id"] for i in editor._tile_items] == [replacement], timeout=8000
    )


class FrameSource(Viewer):
    frame = param.DataFrame(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("frame")
    def rows(self):
        return self.frame

    def __panel__(self):
        return "frame source"


class TotalSink(Viewer):
    total = param.Number(default=0)

    def __panel__(self):
        return "total sink"


# panel-material-ui buttons report clicks as DOM events carrying their model;
# a click on a widget the editor has just re-rendered can reach Bokeh as a
# definition of a model it already knows. That only warns, but this suite
# turns warnings into errors, which would drop the click.
@pytest.mark.filterwarnings("ignore:reference already known")
def test_edge_transform_editor_applies_changes(page):
    import pandas as pd

    editor = FlowDash({"Test/frame": FrameSource, "Test/total": TotalSink}, notifications=False)
    src = editor.add_component("Test/frame", position=(0, 0))
    dst = editor.add_component("Test/total", position=(400, 120))
    editor.graph.get_state(src).rows = pd.DataFrame({"mw": [10, 30]})
    transform = [{"op": "column", "column": "mw"}, {"op": "aggregate", "how": "sum"}]
    assert editor.connect(src, "rows", dst, "total", transform) is True
    serve_component(page, editor)

    page.locator(".react-flow__edge-text").filter(has_text="sum").wait_for()
    page.locator(".react-flow__edge").first.click(force=True)
    page.get_by_text("int · 40").wait_for()

    page.get_by_role("button", name="Add step").click()
    page.get_by_role("menuitem", name="Format").click()

    wait_until(lambda: editor.graph.get_state(dst).total == 40, page)
    page.get_by_text("Not applied: total does not accept", exact=False).wait_for()
    assert editor.graph.get_edge_transform(src, "rows", dst, "total") == transform

    page.get_by_role("button", name="Remove step").nth(2).click()
    page.get_by_text("✓ Applied to total").wait_for()

    page.get_by_role("combobox", name="How").click()
    page.get_by_text("max", exact=True).click()
    wait_until(lambda: editor.graph.get_state(dst).total == 30, page)
    page.locator(".react-flow__edge-text").filter(has_text="max").wait_for()


@register(page=False, component=True, title="Tile")
def tile_component(config):
    return "tile"


def _tile_widths(page):
    """Return [authored width, visible] for each tile, in DOM order."""
    return page.locator(".muuri-grid-item").evaluate_all(
        """els => els.map(el => [
            parseFloat(el.getAttribute('data-width')),
            getComputedStyle(el).display !== 'none',
        ])"""
    )


def _spec(index, width, visible=True, **extra):
    return {"index": index, "width": width, "height": 200, "visible": visible, **extra}


def test_reopened_dashboard_keeps_layout_after_tile_removed(page):
    components = {"Test/tile": tile_component}
    editor = FlowDash(components, notifications=False, include_builtin_components=False)
    ids = [editor.add_component("Test/tile") for _ in range(3)]
    model = editor.to_model()
    model.tile_layout = [
        _spec(0, 100, instance_id=ids[0]),
        _spec(1, 40, instance_id=ids[1]),
        _spec(2, 60, visible=False, instance_id=ids[2]),
    ]
    editor.load_model(model)
    editor.remove_component(ids[0])
    saved = editor.to_model()

    reopened = FlowDash(components, notifications=False, include_builtin_components=False)
    reopened.load_model(saved)
    reopened.mode = "dashboard"
    serve_component(page, reopened)

    wait_until(lambda: _tile_widths(page) == [[40, True], [60, False]], page=page, timeout=8000)


def test_legacy_layout_keeps_slot_of_component_that_no_longer_loads(page):
    components = {"Test/tile": tile_component}
    editor = FlowDash(components, notifications=False, include_builtin_components=False)
    model = DashboardModel(
        dashboard_id="d1",
        user_id="u",
        title="Legacy",
        items=[
            DashboardItem(instance_id="gone", component_id="Gone/missing"),
            DashboardItem(instance_id="k1", component_id="Test/tile"),
            DashboardItem(instance_id="k2", component_id="Test/tile"),
        ],
        tile_layout=[_spec(0, 100), _spec(1, 40), _spec(2, 60, visible=False)],
    )
    editor.load_model(model)
    editor.mode = "dashboard"
    serve_component(page, editor)

    wait_until(
        lambda: _tile_widths(page) == [[100, True], [40, True], [60, False]],
        page=page,
        timeout=8000,
    )


@register(page=False, component=True, provides=["company"])
class CompanyPicker(Viewer):
    company = param.String(default=None, allow_None=True)

    def __panel__(self):
        choose = pmui.Button(label="Choose ACME")
        reset = pmui.Button(label="Reset company")
        choose.on_click(lambda _: setattr(self, "company", "ACME"))
        reset.on_click(lambda _: setattr(self, "company", None))
        return pn.Row(choose, reset)


class CompanyReport(Viewer):
    company = param.String(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    def __panel__(self):
        return pn.pane.Markdown(pn.bind("Report for {}".format, self.param.company))


@register(page=False, component=True, placeholder="Pick a company to start")
class CustomMessageReport(CompanyReport):
    pass


@pytest.mark.filterwarnings("ignore:reference already known")
@pytest.mark.parametrize(
    ("report", "message"),
    [(CompanyReport, "This tile needs Company"), (CustomMessageReport, "Pick a company to start")],
    ids=["default", "custom"],
)
@pytest.mark.parametrize("mode", ["dashboard", "wiring"])
def test_component_waits_for_required_input(page, report, message, mode):
    """A component shows its placeholder until the wired input holds a value, and again after a reset."""
    editor = FlowDash(
        {"Test/picker": CompanyPicker, "Test/report": report},
        notifications=False,
        include_builtin_components=False,
    )
    picker = editor.add_component("Test/picker", position=(0, 0))
    target = editor.add_component("Test/report", position=(600, 0))
    assert editor.connect(picker, "company", target, "company") is True
    editor.mode = mode
    serve_component(page, editor)

    waiting = page.get_by_text(message)
    result = page.get_by_text("Report for ACME")
    waiting.wait_for()
    assert not result.is_visible()

    page.get_by_role("button", name="Choose ACME").click()
    result.wait_for()
    assert not waiting.is_visible()

    page.get_by_role("button", name="Reset company").click()
    waiting.wait_for()
    assert not result.is_visible()


@register(page=False, component=True, title="Gauge")
class Gauge(Viewer):
    reading = param.Integer(default=7)

    @param.output(param.Integer)
    @param.depends("reading")
    def doubled(self):
        return self.reading * 2

    def __flowdash_inspect__(self, port):
        return f"{port} is reading * 2"

    def __panel__(self):
        return "gauge tile"


@pytest.mark.filterwarnings("ignore:reference already known")
def test_inspect_button_opens_sheet_with_current_value(page):
    editor = FlowDash({"Test/gauge": Gauge}, notifications=False, inspect_outputs=True)
    instance_id = editor.add_component("Test/gauge")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_role("button", name="Inspect data for Gauge").click()

    sheet = page.locator(".MuiDrawer-paper")
    sheet.get_by_text("Gauge", exact=True).wait_for()
    sheet.get_by_text("doubled is reading * 2").wait_for()
    assert sheet.get_by_text("14").is_visible()

    editor.graph.get_state(instance_id).reading = 8
    page.get_by_role("button", name="Close inspector").click()
    sheet.wait_for(state="hidden")
    page.get_by_role("button", name="Inspect data for Gauge").click()
    sheet.get_by_text("16").wait_for()


def test_inspect_button_is_absent_by_default(page):
    editor = FlowDash({"Test/gauge": Gauge}, notifications=False)
    editor.add_component("Test/gauge")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_text("gauge tile").wait_for()
    assert not page.get_by_role("button", name="Inspect data for Gauge").is_visible()


# The app re-renders the editor as it loads the dashboard, so the click reaches
# Bokeh as a model it already knows; see `test_edge_transform_editor_applies_changes`.
@pytest.mark.filterwarnings("ignore:reference already known")
def test_inspect_button_in_app_dashboard_page(page):
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.component_library import normalize_components
    from panel_flowdash.dashboard_store import MemoryDashboardStore

    store = MemoryDashboardStore()

    # Built per session, since the app loads its page when the session does.
    def app():
        app = FlowDashApp(
            registry=normalize_components({"Test/gauge": Gauge}),
            store=store,
            home_dashboard="Plant",
            editor_options={"inspect_outputs": True},
            notifications=False,
        )
        model = store.create_dashboard(app._user_id, "Plant")
        model.items = [DashboardItem(instance_id="g1", component_id="Test/gauge", x=0, y=0)]
        store.save_dashboard(model)
        return app

    serve_component(page, app)

    page.get_by_role("button", name="Inspect data for Gauge").click()

    page.locator(".MuiDrawer-paper").get_by_text("14").wait_for()


@register(page=False, component=True, title="Divider")
class Divider(Viewer):
    """A component whose output fails for a zero divisor."""

    divisor = param.Number(default=1)

    @param.output(param.Number)
    @param.depends("divisor")
    def ratio(self):
        return 1 / self.divisor

    @param.depends("divisor")
    def __panel__(self):
        return pn.pane.Markdown(f"divisor {self.divisor}", width=360, height=60)


@pytest.mark.failures
@pytest.mark.parametrize("editable", [True, False], ids=["edit", "view"])
def test_tile_shows_failed_input_and_clears_on_recovery(page, editable):
    editor = FlowDash(
        {"Test/divider": Divider},
        notifications=False,
        include_builtin_components=False,
        propagate_failures=True,
    )
    source = editor.add_component("Test/divider", position=(0, 0))
    target = editor.add_component("Test/divider", position=(400, 0))
    assert editor.connect(source, "ratio", target, "divisor") is True
    editor.graph.get_state(source).divisor = 4
    editor.mode = "dashboard"
    editor.editable = editable
    serve_component(page, editor)

    overlays = page.locator(".flowdash-failure")
    overlay = overlays.nth(1)
    expect(page.get_by_text("divisor 0.25")).to_be_visible()
    expect(overlays).to_have_count(2)
    expect(overlay).to_be_hidden()

    editor.graph.get_state(source).divisor = 0

    expect(overlay).to_be_visible()
    expect(overlay).to_contain_text("Input 'divisor' is stale")
    expect(overlay).to_contain_text(
        "'ratio' of 'Divider' failed (ZeroDivisionError: division by zero)"
    )
    expect(overlays.first).to_be_hidden()
    box = overlay.bounding_box()
    assert box["width"] > 100
    assert box["height"] > 30
    assert editor.graph.get_state(target).divisor == 0.25

    editor.graph.get_state(source).divisor = 2

    expect(overlay).to_be_hidden()
    expect(page.get_by_text("divisor 0.5")).to_be_visible()


_CONFIRMING_COMPONENT = """
import pathlib

import panel_material_ui as pmui

from panel_flowdash import register
from panel_flowdash.util import confirm


def record(text):
    with pathlib.Path({marker!r}).open("a") as f:
        f.write(text + "\\n")


@register(page=False, component=True, title="Saver")
def app(config):
    def ask(_event):
        confirm(
            "Save changes?",
            "This overwrites the stored copy.",
            on_confirm=lambda _e: record("confirmed"),
            on_cancel=lambda _e: record("cancelled"),
        )

    return pmui.Button(label="Save", on_click=ask)
"""


def _serve_dashboard_with_confirm(page, tmp_path, monkeypatch):
    section = tmp_path / "Confirming"
    section.mkdir()
    (section / "__init__.py").write_text("")
    marker = tmp_path / "marker.txt"
    (section / "saver.py").write_text(_CONFIRMING_COMPONENT.format(marker=str(marker)))
    monkeypatch.syspath_prepend(str(tmp_path))
    store = DashboardStore(tmp_path / "test.db")
    dashboard = store.create_dashboard(resolve_identity(AuthConfig()).user, "Confirming")
    dashboard.items = [DashboardItem(instance_id="n0", component_id="Confirming/saver")]
    store.save_dashboard(dashboard)
    routes = FlowDashApp.build_routes(tmp_path, store=store)
    serve_component(page, routes, suffix=f"/dash/{dashboard.dashboard_id}")
    return marker


@pytest.mark.filterwarnings("ignore:reference already known:bokeh.util.warnings.BokehUserWarning")
def test_component_confirm_dialog_in_app(page, tmp_path, monkeypatch):
    marker = _serve_dashboard_with_confirm(page, tmp_path, monkeypatch)

    for label, outcome in [("Cancel", "cancelled"), ("Confirm", "confirmed")]:
        page.get_by_role("button", name="Save", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_text("Save changes?")).to_be_visible()
        expect(dialog.get_by_text("This overwrites the stored copy.")).to_be_visible()
        assert not marker.exists() or outcome not in marker.read_text()

        dialog.get_by_role("button", name=label).click()

        expect(page.get_by_role("dialog")).to_have_count(0)
        wait_until(
            lambda outcome=outcome: marker.exists() and outcome in marker.read_text(), timeout=8000
        )

    assert marker.read_text() == "cancelled\nconfirmed\n"


_asked = []


@register(page=False, component=True, title="Asker")
def asker(config):
    def ask(_event):
        confirm("Proceed?", on_confirm=_asked.append)

    return pmui.Button(label="Ask", on_click=ask)


@pytest.mark.filterwarnings("ignore:reference already known:bokeh.util.warnings.BokehUserWarning")
def test_standalone_editor_hosts_the_page_dialog(page):
    """An embedded `FlowDash` lays out the dialog itself, once."""
    _asked.clear()

    def app():
        editor = FlowDash({"Test/asker": asker}, notifications=False)
        editor.add_component("Test/asker")
        editor.mode = "dashboard"
        return editor

    serve_component(page, app)

    page.get_by_role("button", name="Ask", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Proceed?")).to_be_visible()
    assert not _asked

    dialog.get_by_role("button", name="Confirm").click()

    expect(page.get_by_role("dialog")).to_have_count(0)
    wait_until(lambda: len(_asked) == 1, timeout=8000)


@register(page=False, component=True, title="Live count")
class LiveCount(Viewer):
    """Counts how many times the dashboard asked it to refresh."""

    def __init__(self, **params):
        super().__init__(**params)
        self._count = 0
        self._text = pn.pane.Markdown(pn.bind(self._fetch, current_dashboard().param.refresh))

    def _fetch(self, _refresh):
        self._count += 1
        return f"fetches: {self._count}"

    def __panel__(self):
        return self._text


def _serve_live_dashboard(page, *, refresh_period=0, view=False):
    """Serve one stored dashboard holding a LiveCount, as the editor or as a viewer would see it."""
    store = MemoryDashboardStore(
        {
            "live": DashboardModel(
                dashboard_id="live",
                user_id="alice",
                title="Live",
                items=[DashboardItem(instance_id="count", component_id="Test/count")],
                settings={"refresh_period": refresh_period},
            )
        }
    )
    editors = []

    def app():
        editor = FlowDash(
            {"Test/count": LiveCount},
            notifications=False,
            store=store,
            user="alice",
            dashboard="live",
            editable=not view,
            mode="dashboard",
        )
        editors.append(editor)
        return editor

    serve_component(page, app)
    return store, editors


def _enter_period(field, text):
    """Replace the number input's value and blur it to commit.

    Filling over a value appends to it and typed keys get reformatted mid-entry,
    so the field is emptied first and the new value filled in one go.
    """
    field.fill("")
    field.fill(text)
    field.press("Tab")


def test_refresh_period_set_in_the_settings_form_persists(page):
    store, editors = _serve_live_dashboard(page)

    page.get_by_role("button", name="Settings").click()
    period = page.get_by_role("dialog").get_by_label("Refresh period (s)")
    _enter_period(period, "15")
    wait_until(lambda: editors[-1].settings.refresh_period == 15, page)
    assert editors[-1].dirty

    page.get_by_role("button", name="Close").click()
    page.get_by_role("button", name="Save", exact=True).click()
    wait_until(
        lambda: store.load_dashboard("alice", "live").settings["refresh_period"] == 15, page
    )

    page.reload()
    wait_until(lambda: len(editors) == 2, page)
    assert editors[-1].settings.refresh_period == 15
    page.get_by_role("button", name="Settings").click()
    expect(page.get_by_role("dialog").get_by_label("Refresh period (s)")).to_have_value("15.0")


def test_refresh_button_updates_listening_components_in_view_mode(page):
    _serve_live_dashboard(page, view=True)

    page.get_by_text("fetches: 1").wait_for()
    assert page.get_by_role("button", name="Settings").count() == 0
    page.get_by_role("button", name="Refresh").click()
    page.get_by_text("fetches: 2").wait_for()
    page.get_by_role("button", name="Refresh").click()
    page.get_by_text("fetches: 3").wait_for()
    expect(page.get_by_text("Refreshed ")).to_be_visible()


def test_refresh_period_refreshes_without_interaction(page):
    _serve_live_dashboard(page, refresh_period=1, view=True)

    page.get_by_text("fetches: 3", exact=False).wait_for(timeout=10000)


def test_period_change_applies_to_the_running_page(page):
    _store, editors = _serve_live_dashboard(page)

    page.get_by_text("fetches: 1").wait_for()
    page.get_by_role("button", name="Settings").click()
    period = page.get_by_role("dialog").get_by_label("Refresh period (s)")
    _enter_period(period, "1")
    page.get_by_text("fetches: 3", exact=False).wait_for(timeout=10000)

    _enter_period(period, "0")
    wait_until(lambda: editors[-1]._refresh_timer is None, page)
