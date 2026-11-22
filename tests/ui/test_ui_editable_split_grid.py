"""UI tests for the tile grid in the Split tab, which shows the dashboard at the pane's width.

The pane is narrower than the 1200 px the layout is assumed to be arranged at,
so the grid shows the layout generated for that width. Tiles are arranged in
the Layout tab; in the Split tab only the inspector's tile options edit them.
"""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import tile_entry, update_tile
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView

from .util import serve_component, stable_box

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


def _assert_main_layout(editor):
    """No band got a layout of its own and the Desktop layout kept its reference width."""
    grid = editor._tile_grid
    assert grid.responsive_layouts == {}
    assert grid.reference_width is None


def test_split_grid_shows_the_dashboard_at_the_pane_width(page):
    editor, ids = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))

    grid = page.locator(".muuri-grid")
    # Two half-width tiles are narrower than half their Desktop width here, so they wrap.
    wait_until(
        lambda: all(_fraction(page, editor, i)["width"] > 0.95 for i in ids), page, timeout=8000
    )
    width, scroll_width = grid.evaluate("el => [el.clientWidth, el.scrollWidth]")
    assert width < 1000
    assert scroll_width <= width
    assert editor._tile_grid.preview_band is None
    expect(page.locator(".muuri-breakpoint-toolbar")).to_be_hidden()
    assert [tile_entry(editor, i)["width"] for i in ids] == [50.0, 50.0]


def test_split_grid_has_no_handles_and_leaves_the_layout_alone(page):
    editor, ids = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))
    tile = _tile(page, editor, ids[0])
    wait_until(lambda: _fraction(page, editor, ids[0])["width"] > 0.95, page, timeout=8000)

    tile.hover()
    for handle in ("drag", "resize", "close"):
        expect(tile.locator(f".muuri-handle.{handle}")).to_be_hidden()
    box = stable_box(tile, page)
    page.mouse.move(box["x"] + box["width"] - 4, box["y"] + box["height"] - 4)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] + 80, steps=10)
    page.mouse.up()
    page.wait_for_timeout(300)

    assert not editor.dirty
    assert [tile_entry(editor, i)["width"] for i in ids] == [50.0, 50.0]
    _assert_main_layout(editor)

    # The Layout tab arranges Desktop with its handles.
    _open_layout(page, editor)
    expect(_tile(page, editor, ids[0]).locator(".muuri-handle.drag")).to_be_visible()
    assert editor._tile_grid.preview_band == "md"


def test_tile_options_in_the_split_inspector_edit_the_main_layout(page):
    editor, (_map_id, chart_id) = _editor()
    _open_split(page, editor, sizes=(45.0, 55.0))

    page.locator(f".react-flow__node[data-id='{chart_id}']").click()
    page.get_by_role("tab", name="Tile").click()
    page.get_by_role("combobox", name="Width").click()
    page.get_by_role("listbox").get_by_text("Full (12/12)", exact=True).click()
    wait_until(lambda: tile_entry(editor, chart_id).get("width") == 100, page, timeout=8000)
    wait_until(lambda: _fraction(page, editor, chart_id)["width"] > 0.95, page, timeout=8000)
    page.get_by_role("switch", name="Show on dashboard").click()
    wait_until(lambda: tile_entry(editor, chart_id).get("visible") is False, page, timeout=8000)
    expect(_tile(page, editor, chart_id)).to_be_hidden()
    _assert_main_layout(editor)

    _open_layout(page, editor)
    editor._clear_selection()
    expect(page.get_by_text("hidden", exact=True)).to_be_visible()
    expect(_tile(page, editor, chart_id)).to_be_hidden()
