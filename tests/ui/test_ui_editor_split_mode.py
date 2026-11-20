"""UI tests for the Split tab: the canvas and the tile grid side by side."""

import json
import re

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.split_view import STORAGE_KEY
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView
from tests.ui.test_ui_command_palette import _serve_app, _store_with

from .util import center, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# Entering the split moves the canvas into a new pane, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _editor():
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    ids = [editor.add_component(c) for c in (MAP, CHART)]
    return editor, ids


def _open_split(page, editor):
    serve_component(page, editor)
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    expect(page.locator(".gutter")).to_be_visible()
    expect(page.locator(".react-flow__node")).to_have_count(len(editor._tile_items))
    expect(page.locator(".muuri-grid-item")).to_have_count(len(editor._grid_ids))


def _tile(page, editor, instance_id):
    key = next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)
    return page.locator(f".{key}")


def _item(page, editor, instance_id):
    """The grid item holding a tile, which TileGrid marks as selected."""
    return page.locator(".muuri-grid-item").filter(has=_tile(page, editor, instance_id))


def test_split_shows_canvas_beside_grid(page):
    editor, _ids = _editor()
    _open_split(page, editor)

    canvas = page.locator(".react-flow__pane").bounding_box()
    grid = page.locator(".muuri-grid").bounding_box()
    gutter = page.locator(".gutter").bounding_box()
    assert canvas["x"] + canvas["width"] <= gutter["x"] + 1
    assert grid["x"] >= gutter["x"] + gutter["width"] - 1
    # The palette stays beside the split; at 1280 px the inspector waits folded.
    expect(page.get_by_placeholder("Search components")).to_be_visible()
    expect(page.get_by_role("button", name="Show inspector")).to_be_visible()


def test_dragging_the_gutter_resizes_and_remembers_the_sizes(page):
    editor, _ids = _editor()
    _open_split(page, editor)

    before = page.locator(".react-flow__pane").bounding_box()["width"]
    x, y = center(stable_box(page.locator(".gutter")))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 120, y, steps=10)
    page.mouse.up()

    wait_until(lambda: editor._split_view.preferences.sizes[0] > 60, page, timeout=8000)
    wait_until(
        lambda: page.locator(".react-flow__pane").bounding_box()["width"] > before + 100,
        page,
        timeout=8000,
    )
    stored = json.loads(page.evaluate(f"localStorage.getItem({STORAGE_KEY!r})"))
    assert stored["split"] is True
    assert stored["sizes"] == pytest.approx(list(editor._split_view.preferences.sizes))
    assert editor.mode == "split"


def test_stored_split_reopens_on_the_next_visit(page):
    editors = []

    def factory():
        editor, _ids = _editor()
        editors.append(editor)
        return editor

    serve_component(page, factory)
    assert editors[0].mode == "wiring"
    page.evaluate(
        f"localStorage.setItem({STORAGE_KEY!r}, JSON.stringify({{split: true, sizes: [70, 30]}}))"
    )
    page.reload()
    wait_until(lambda: len(editors) == 2 and editors[1].mode == "split", page, timeout=8000)
    expect(page.locator(".gutter")).to_be_visible()
    assert editors[1]._split_view.split.sizes == (70.0, 30.0)


def test_collapsing_the_grid_falls_back_to_dataflow(page):
    editor, _ids = _editor()
    _open_split(page, editor)

    page.locator(".toggle-button-right").click()
    wait_until(lambda: editor.mode == "wiring", page, timeout=8000)
    expect(page.locator(".gutter")).to_have_count(0)
    expect(page.locator(".react-flow__pane")).to_be_visible()
    expect(page.get_by_role("button", name="Dataflow")).to_have_attribute("aria-pressed", "true")


def test_selecting_a_node_outlines_its_tile(page):
    editor, (map_id, chart_id) = _editor()
    _open_split(page, editor)

    page.locator(f".react-flow__node[data-id='{chart_id}']").click()
    wait_until(lambda: editor._flow.selection.get("nodes") == [chart_id], page, timeout=8000)
    expect(_item(page, editor, chart_id)).to_have_class(re.compile("muuri-selected"))
    expect(_item(page, editor, map_id)).not_to_have_class(re.compile("muuri-selected"))


def test_clicking_a_tile_selects_its_node_and_inspects_it(page):
    editor, (map_id, chart_id) = _editor()
    _open_split(page, editor)

    expect(page.get_by_role("button", name="Show inspector")).to_be_visible()
    _tile(page, editor, map_id).click()
    wait_until(lambda: (editor._flow.selection or {}).get("nodes") == [map_id], page, timeout=8000)
    expect(page.locator(f".react-flow__node[data-id='{map_id}']")).to_have_class(
        re.compile(r"\bselected\b")
    )
    expect(_item(page, editor, map_id)).to_have_class(re.compile("muuri-selected"))
    expect(page.locator(".fd-inspector-header")).to_be_visible()
    assert editor._inspector._shown == ("node", map_id)

    _tile(page, editor, chart_id).click()
    wait_until(
        lambda: (editor._flow.selection or {}).get("nodes") == [chart_id], page, timeout=8000
    )
    expect(_item(page, editor, map_id)).not_to_have_class(re.compile("muuri-selected"))


def test_adding_and_removing_shows_on_both_sides(page):
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    map_id = editor.add_component(MAP)
    _open_split(page, editor)

    page.get_by_placeholder("Search components").fill("chart")
    item = page.locator(".MuiListItemButton-root", has_text="Chart").first
    item.drag_to(page.locator(".react-flow__pane"), target_position={"x": 60, "y": 60})
    wait_until(lambda: len(editor._tile_items) == 2, page, timeout=8000)
    expect(page.locator(".react-flow__node")).to_have_count(2)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)

    _tile(page, editor, map_id).click()
    page.locator(".fd-inspector-menu button, button.fd-inspector-menu").first.click()
    page.get_by_role("menuitem", name="Remove from dashboard").click()
    wait_until(lambda: len(editor._tile_items) == 1, page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(1)
    expect(page.locator(".react-flow__node")).to_have_count(1)
    expect(page.locator(f".react-flow__node[data-id='{map_id}']")).to_have_count(0)


def test_s_toggles_the_split_in_edit_mode(page):
    store, (plant,) = _store_with("Plant")
    apps = []
    _serve_app(page, store, f"/dash/{plant}?edit=true", apps)
    page.get_by_role("button", name="Done").wait_for()

    page.keyboard.press("s")
    wait_until(lambda: apps[0]._editor.mode == "split", page, timeout=10000)
    expect(page.locator(".gutter")).to_be_visible()
    page.keyboard.press("s")
    wait_until(lambda: apps[0]._editor.mode == "wiring", page, timeout=10000)
    expect(page.locator(".gutter")).to_have_count(0)


def test_clicking_a_tile_pans_to_its_off_screen_node(page):
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    editor.add_component(MAP, position=(0, 0))
    far_id = editor.add_component(CHART, position=(3000, 1500))
    fitted = []
    events = editor._split_view.events
    on_resize = events.on_resize
    events.on_resize = lambda: (on_resize(), fitted.append(True))
    _open_split(page, editor)
    # Opening the split fits the view; zooming in before that fit lands would be undone.
    wait_until(lambda: bool(fitted), page, timeout=8000)
    page.wait_for_timeout(500)
    fitted_zoom = (editor._flow.viewport or {}).get("zoom") or 1

    zoom_in = page.locator(".react-flow__controls-zoomin")
    for _ in range(5):
        zoom_in.click()
    pane = page.locator(".react-flow__pane")
    node = page.locator(f".react-flow__node[data-id='{far_id}']")

    def inside():
        p, n = pane.bounding_box(), node.bounding_box()
        cx, cy = n["x"] + n["width"] / 2, n["y"] + n["height"] / 2
        return p["x"] <= cx <= p["x"] + p["width"] and p["y"] <= cy <= p["y"] + p["height"]

    wait_until(lambda: not inside(), page, timeout=8000)
    # Each step zooms by 1.2 from the fitted zoom.
    zoom = fitted_zoom * 1.2**5
    wait_until(
        lambda: (editor._flow.viewport or {}).get("zoom") == pytest.approx(zoom),
        page,
        timeout=8000,
    )
    _tile(page, editor, far_id).click()
    wait_until(inside, page, timeout=8000)
    assert editor._flow.viewport["zoom"] == pytest.approx(zoom)
