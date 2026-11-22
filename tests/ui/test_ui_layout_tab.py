"""UI tests for the Layout tab: tile options, components off the grid and the status bar."""

import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import tile_entry
from tests.test_ui_layout_tab import CHART, FILTER, MAP, ChartView, MapView, StateFilter

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The panel swaps in new rows when a tile is hidden or shown, so a click can
# reach Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _editor():
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView, FILTER: StateFilter},
        notifications=False,
        include_builtin_components=False,
    )
    ids = [editor.add_component(c) for c in (MAP, CHART, FILTER)]
    return editor, ids


def _open_layout(page, editor):
    serve_component(page, editor)
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)


def _tile_width(page, index):
    return (
        page.locator(".muuri-grid-item")
        .nth(index)
        .evaluate(
            "el => el.getBoundingClientRect().width / el.parentElement.getBoundingClientRect().width"
        )
    )


def test_layout_tab_shows_handles_options_and_the_grid_summary(page):
    editor, (_map_id, _chart_id, _filter_id) = _editor()
    _open_layout(page, editor)

    expect(page.get_by_text("Not on the dashboard")).to_be_visible()
    expect(page.get_by_text("12-column grid")).to_be_visible()
    expect(page.get_by_text("2 tiles, 1 component hidden")).to_be_visible()
    expect(page.get_by_text("in top bar")).to_be_visible()
    # The handles stay visible in the Layout tab rather than only on hover.
    drag = page.locator(".muuri-handle.drag").first
    expect(drag).to_be_visible()
    assert float(drag.evaluate("el => getComputedStyle(el).opacity")) >= 0.7
    assert editor._flow.selection["nodes"] == []


def _click_tile(page, editor, index, instance_id):
    page.locator(".muuri-grid-item").nth(index).click(position={"x": 40, "y": 60})
    wait_until(lambda: editor._inspector._shown == ("node", instance_id), timeout=8000)
    expect(page.get_by_role("tab", name="Tile", selected=True)).to_be_visible()


def test_selected_tile_width_and_height_edit_the_layout(page):
    editor, (_map_id, chart_id, _filter_id) = _editor()
    _open_layout(page, editor)

    _click_tile(page, editor, 1, chart_id)

    page.get_by_role("combobox", name="Width").click()
    page.get_by_role("listbox").get_by_text("Half (6/12)", exact=True).click()
    wait_until(lambda: tile_entry(editor, chart_id).get("width") == 50.0, timeout=8000)
    wait_until(lambda: abs(_tile_width(page, 1) - 0.5) < 0.05, timeout=8000)

    page.get_by_role("combobox", name="Height").click()
    page.get_by_role("listbox").get_by_text("400 px", exact=True).click()
    wait_until(lambda: tile_entry(editor, chart_id).get("height") == 400, timeout=8000)
    wait_until(
        lambda: page.locator(".muuri-grid-item").nth(1).evaluate("el => el.offsetHeight") == 400,
        timeout=8000,
    )
    assert editor.dirty


def test_hidden_tile_is_listed_and_shown_again(page):
    editor, (_map_id, chart_id, _filter_id) = _editor()
    _open_layout(page, editor)

    _click_tile(page, editor, 1, chart_id)
    page.get_by_role("switch", name="Show on dashboard").click()
    wait_until(lambda: tile_entry(editor, chart_id).get("visible") is False, timeout=8000)
    # With nothing selected the sidecar lists it as not on the dashboard.
    editor._clear_selection()
    wait_until(lambda: editor._inspector._shown is None, timeout=8000)
    expect(page.get_by_text("hidden", exact=True)).to_be_visible()
    expect(page.get_by_text("1 tile, 2 components hidden")).to_be_visible()
    expect(page.locator(".muuri-grid-item").nth(1)).to_be_hidden()

    page.get_by_role("button", name="Show Chart on the dashboard").click()
    wait_until(lambda: tile_entry(editor, chart_id).get("visible") is True, timeout=8000)
    expect(page.get_by_text("hidden", exact=True)).to_have_count(0)
    expect(page.get_by_text("2 tiles, 1 component hidden")).to_be_visible()
    expect(page.locator(".muuri-grid-item").nth(1)).to_be_visible()
