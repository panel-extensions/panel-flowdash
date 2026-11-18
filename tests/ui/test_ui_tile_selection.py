"""UI tests for selecting a tile and its node together, in the Layout and Split tabs."""

import re

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

SELECTED = re.compile(r"\bmuuri-selected\b")


def _editor():
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    ids = [editor.add_component(c) for c in (MAP, CHART)]
    editor.dirty = False
    return editor, ids


def _item(page, editor, instance_id):
    key = next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)
    return page.locator(".muuri-grid-item").filter(has=page.locator(f".{key}"))


def _nodes(editor):
    return (editor._flow.selection or {}).get("nodes") or []


def _open(page, editor, tab):
    page.set_viewport_size({"width": 1700, "height": 1000})
    serve_component(page, editor)
    page.get_by_role("button", name=tab).click()
    wait_until(lambda: editor.mode == {"Layout": "dashboard", "Split": "split"}[tab], page)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)


def test_clicking_a_tile_in_the_layout_tab_selects_its_node(page):
    editor, (map_id, chart_id) = _editor()
    _open(page, editor, "Layout")

    _item(page, editor, chart_id).click()
    wait_until(lambda: _nodes(editor) == [chart_id], page, timeout=8000)
    expect(_item(page, editor, chart_id)).to_have_class(SELECTED)
    expect(page.get_by_role("combobox", name="Tile")).to_have_text(re.compile("Chart"))

    _item(page, editor, map_id).click()
    wait_until(lambda: _nodes(editor) == [map_id], page, timeout=8000)
    expect(_item(page, editor, chart_id)).not_to_have_class(SELECTED)
    assert not editor.dirty

    # The node stays selected on the canvas when switching tabs.
    page.get_by_role("button", name="Dataflow").click()
    expect(page.locator(f".react-flow__node[data-id='{map_id}']")).to_have_class(
        re.compile(r"\bselected\b")
    )


def test_node_selected_on_the_canvas_outlines_its_tile_in_the_layout_tab(page):
    editor, (map_id, chart_id) = _editor()
    page.set_viewport_size({"width": 1700, "height": 1000})
    serve_component(page, editor)
    page.locator(f".react-flow__node[data-id='{chart_id}']").click()
    wait_until(lambda: _nodes(editor) == [chart_id], page, timeout=8000)

    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)
    expect(_item(page, editor, chart_id)).to_have_class(SELECTED)
    expect(_item(page, editor, map_id)).not_to_have_class(SELECTED)

    # Picking a tile in the layout panel selects it on the grid too.
    page.get_by_role("combobox", name="Tile").click()
    page.get_by_role("listbox").get_by_text("Map").click()
    expect(_item(page, editor, map_id)).to_have_class(SELECTED)
    wait_until(lambda: _nodes(editor) == [map_id], page, timeout=8000)


def test_split_tile_click_and_node_click_select_each_other(page):
    editor, (map_id, chart_id) = _editor()
    _open(page, editor, "Split")

    _item(page, editor, chart_id).click()
    wait_until(lambda: _nodes(editor) == [chart_id], page, timeout=8000)
    expect(page.locator(f".react-flow__node[data-id='{chart_id}']")).to_have_class(
        re.compile(r"\bselected\b")
    )
    wait_until(lambda: editor._inspector._shown == ("node", chart_id), page, timeout=8000)
    expect(_item(page, editor, chart_id)).to_have_class(SELECTED)

    page.locator(f".react-flow__node[data-id='{map_id}']").click()
    wait_until(lambda: _nodes(editor) == [map_id], page, timeout=8000)
    expect(_item(page, editor, map_id)).to_have_class(SELECTED)
    expect(_item(page, editor, chart_id)).not_to_have_class(SELECTED)

    # Clicking the empty canvas deselects the tile as well.
    page.locator(".react-flow__pane").click(position={"x": 20, "y": 20})
    wait_until(lambda: _nodes(editor) == [], page, timeout=8000)
    expect(_item(page, editor, map_id)).not_to_have_class(SELECTED)
    assert not editor.dirty


def test_view_mode_shows_no_selection(page):
    editor, (map_id, _chart_id) = _editor()
    editor.editable = False
    page.set_viewport_size({"width": 1700, "height": 1000})
    serve_component(page, editor)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)

    _item(page, editor, map_id).click()
    wait_until(lambda: editor._tile_grid.selected == 0, page, timeout=8000)
    outline = _item(page, editor, map_id).evaluate("el => getComputedStyle(el).outlineStyle")
    assert outline == "none"
    assert _nodes(editor) == []
