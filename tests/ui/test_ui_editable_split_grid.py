"""UI tests for arranging tiles in the Split tab, which edits the dashboard's own layout.

The grid pane is narrower than the 1200 px the layout is assumed to be arranged
at, so these tests also check that the narrow pane does not turn edits into a
tablet layout.
"""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import tile_entry, update_tile
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView

from .util import center, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

VIEWPORT = {"width": 1600, "height": 1000}


def _editor():
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    ids = [editor.add_component(c) for c in (MAP, CHART)]
    for instance_id in ids:
        update_tile(editor, instance_id, width=50.0, height=300)
    editor.dirty = False
    return editor, ids


def _open_split(page, editor, sizes=None):
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    if sizes is not None:
        editor._split_view.preferences.sizes = sizes
        editor._split_view.restore()
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)
    expect(page.locator(".muuri-handle.drag").first).to_be_visible()


def _open_layout(page, editor):
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=8000)
    # The grid has no box while the tab it is on is still hidden.
    grid = page.locator(".muuri-grid")
    wait_until(lambda: (box := grid.bounding_box()) is not None and box["width"] > 1000, page)


def _tiles(page):
    """The left edge and width of each tile, as fractions of the grid's width."""
    return page.locator(".muuri-grid").evaluate(
        """grid => {
          const g = grid.getBoundingClientRect()
          return [...grid.querySelectorAll(':scope > .muuri-grid-item')].map(el => {
            const r = el.getBoundingClientRect()
            const tile = el.querySelector('[class*="flowdash-tile-key-"]')
            const key = [...(tile?.classList || [])].find(c => c.startsWith('flowdash-tile-key-'))
            return {key, left: (r.left - g.left) / g.width,
                    width: r.width / g.width, visible: r.width > 0 && el.style.display !== 'none'}
          })
        }"""
    )


def _key(editor, instance_id):
    return next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)


def _tile(page, editor, instance_id):
    key = _key(editor, instance_id)
    return page.locator(".muuri-grid-item").filter(has=page.locator(f".{key}"))


def _fraction(page, editor, instance_id):
    key = _key(editor, instance_id)
    return next(t for t in _tiles(page) if t["key"] == key)


def _span(page, editor, instance_id):
    """The share of the grid's width a tile takes up, with its 20 px of margins."""
    width = page.locator(".muuri-grid").evaluate("el => el.getBoundingClientRect().width")
    return _fraction(page, editor, instance_id)["width"] + 20 / width


def _order(page, editor):
    keys = {_key(editor, i): i for i in editor._grid_ids}
    return [keys[t["key"]] for t in sorted(_tiles(page), key=lambda t: t["left"])]


def _layout_order(editor):
    return [e["instance_id"] for e in sorted(editor.layout, key=lambda e: e.get("index", 0))]


def _assert_main_layout(editor, reference_width=1200):
    grid = editor._tile_grid
    assert grid.responsive_layouts == {}
    assert grid.reference_width == reference_width


def test_split_grid_fits_the_pane_and_shows_the_main_layout(page):
    editor, ids = _editor()
    _open_split(page, editor)

    grid = page.locator(".muuri-grid")
    wait_until(lambda: _fraction(page, editor, ids[1])["left"] > 0.4, page, timeout=8000)
    width, scroll_width = grid.evaluate("el => [el.clientWidth, el.scrollWidth]")
    assert width < 1000
    assert scroll_width <= width
    # Both half-width tiles share a row, as in the main layout, not stacked as on a narrow screen.
    assert [round(_fraction(page, editor, i)["width"], 1) for i in ids] == [0.5, 0.5]
    assert editor._tile_grid.preview_band == "md"
    expect(page.locator(".muuri-breakpoint-toolbar")).to_be_hidden()


def test_dragging_a_tile_in_the_split_reorders_the_main_layout(page):
    editor, ids = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))
    wait_until(lambda: _fraction(page, editor, ids[1])["left"] > 0.4, page, timeout=8000)
    assert _order(page, editor) == ids
    first, second = (_tile(page, editor, i) for i in ids)

    x, y = center(stable_box(first.locator(".muuri-handle.drag"), page))
    target = stable_box(second, page)
    # Level with the handle, so the dragged tile stays inside the grid, which Muuri needs to sort it.
    tx, ty = target["x"] + target["width"] * 0.75, y
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 21):
        page.mouse.move(x + (tx - x) * step / 20, y + (ty - y) * step / 20)
    page.mouse.up()

    swapped = list(reversed(ids))
    wait_until(lambda: _layout_order(editor) == swapped, page, timeout=8000)
    wait_until(lambda: _order(page, editor) == swapped, page, timeout=8000)
    assert editor.dirty
    _assert_main_layout(editor)

    _open_layout(page, editor)
    wait_until(lambda: _order(page, editor) == swapped, page, timeout=8000)
    assert _layout_order(editor) == swapped
    _assert_main_layout(editor)


def test_resizing_a_tile_in_the_split_changes_its_main_layout_width(page):
    editor, (map_id, _chart_id) = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))
    tile = _tile(page, editor, map_id)
    start = tile_entry(editor, map_id).get("width")

    x, y = center(stable_box(tile.locator(".muuri-handle.resize"), page))
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(x - 24 * step, y)
    page.mouse.up()

    wait_until(lambda: tile_entry(editor, map_id).get("width") != start, page, timeout=8000)
    width = tile_entry(editor, map_id)["width"]
    assert width < start
    _assert_main_layout(editor)
    wait_until(lambda: abs(_span(page, editor, map_id) - width / 100) < 0.02, page, timeout=8000)

    _open_layout(page, editor)
    wait_until(lambda: abs(_span(page, editor, map_id) - width / 100) < 0.02, page, timeout=8000)
    assert tile_entry(editor, map_id)["width"] == width
    _assert_main_layout(editor)


def test_hiding_a_tile_in_the_split_hides_it_in_the_main_layout(page):
    editor, (_map_id, chart_id) = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))
    tile = _tile(page, editor, chart_id)

    tile.hover()
    tile.locator(".muuri-handle.close").click()
    wait_until(lambda: tile_entry(editor, chart_id).get("visible") is False, page, timeout=8000)
    _assert_main_layout(editor)

    _open_layout(page, editor)
    expect(page.get_by_text("hidden by you")).to_be_visible()
    expect(_tile(page, editor, chart_id)).to_be_hidden()


def test_tile_options_in_the_split_inspector_edit_the_main_layout(page):
    editor, (_map_id, chart_id) = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))

    page.locator(f".react-flow__node[data-id='{chart_id}']").click()
    page.get_by_role("tab", name="Tile").click()
    page.get_by_role("combobox", name="Width").click()
    page.get_by_role("listbox").get_by_text("Full width", exact=True).click()
    wait_until(lambda: tile_entry(editor, chart_id).get("width") == 100, page, timeout=8000)
    wait_until(lambda: _fraction(page, editor, chart_id)["width"] > 0.95, page, timeout=8000)
    page.get_by_role("switch", name="Show on dashboard").click()
    wait_until(lambda: tile_entry(editor, chart_id).get("visible") is False, page, timeout=8000)
    expect(_tile(page, editor, chart_id)).to_be_hidden()
    _assert_main_layout(editor, reference_width=None)

    _open_layout(page, editor)
    expect(page.get_by_text("hidden by you")).to_be_visible()
    expect(_tile(page, editor, chart_id)).to_be_hidden()
