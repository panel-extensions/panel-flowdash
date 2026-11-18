"""UI tests for arranging the tiles for one screen size with the Layout tab's band switcher."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import update_tile
from tests.test_ui_layout_tab import CHART, MAP, ChartView, MapView

from .util import center, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _editor():
    editor = FlowDash(
        {MAP: MapView, CHART: ChartView}, notifications=False, include_builtin_components=False
    )
    ids = [editor.add_component(c) for c in (MAP, CHART)]
    for instance_id in ids:
        update_tile(editor, instance_id, width=50.0, height=300)
    editor.dirty = False
    return editor, ids


def _open_layout(page, editor):
    page.set_viewport_size({"width": 1600, "height": 1000})
    serve_component(page, editor)
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)


def _band_button(page, name):
    return page.get_by_role("button", name=name, exact=True)


def _tile(page, editor, instance_id):
    key = next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)
    return page.locator(".muuri-grid-item").filter(has=page.locator(f".{key}"))


def _swap(page, first, second):
    x, y = center(stable_box(first.locator(".muuri-handle.drag"), page))
    target = stable_box(second, page)
    # Level with the handle, so the dragged tile stays inside the grid, which Muuri needs to sort it.
    tx, ty = target["x"] + target["width"] * 0.75, y
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 21):
        page.mouse.move(x + (tx - x) * step / 20, y + (ty - y) * step / 20)
    page.mouse.up()


def _order(layout):
    return [entry.get("index") for entry in layout]


def test_band_switcher_replaces_the_chip_toolbar(page):
    editor, _ids = _editor()
    _open_layout(page, editor)

    expect(page.get_by_text("Arrange for")).to_be_visible()
    for name in ("Desktop", "Tablet", "Phone"):
        expect(_band_button(page, name)).to_be_visible()
    expect(page.locator(".muuri-breakpoint-toolbar")).to_be_hidden()
    assert editor._tile_grid.preview_band == "md"
    expect(
        page.get_by_text("Tablet and Phone follow the Desktop layout automatically.")
    ).to_be_visible()


def test_edits_under_tablet_go_to_its_own_layout(page):
    editor, ids = _editor()
    _open_layout(page, editor)
    base = [dict(entry) for entry in editor._tile_grid.layout]

    _band_button(page, "Tablet").click()
    wait_until(lambda: editor._tile_grid.preview_band == "sm", page, timeout=8000)
    # The grid previews the band at a tablet's width.
    wait_until(
        lambda: page.locator(".muuri-grid").evaluate("el => el.clientWidth") <= 1200,
        page,
        timeout=8000,
    )
    _swap(page, *(_tile(page, editor, i) for i in ids))

    wait_until(lambda: "sm" in editor._tile_grid.responsive_layouts, page, timeout=8000)
    assert editor._tile_grid.layout == base
    assert editor._tile_grid.reference_width is None
    assert editor.dirty
    expect(page.get_by_text("Tablet is arranged separately.")).to_be_visible()
    reset = page.get_by_role("button", name="Reset to automatic")
    expect(reset).to_be_visible()

    _band_button(page, "Desktop").click()
    wait_until(lambda: editor._tile_grid.preview_band == "md", page, timeout=8000)
    expect(reset).to_be_hidden()
    assert editor._tile_grid.layout == base

    _band_button(page, "Tablet").click()
    reset.click()
    wait_until(lambda: editor._tile_grid.responsive_layouts == {}, page, timeout=8000)
    expect(
        page.get_by_text("Tablet and Phone follow the Desktop layout automatically.")
    ).to_be_visible()


def test_edits_under_desktop_go_to_the_main_layout(page):
    editor, ids = _editor()
    _open_layout(page, editor)
    before = _order(editor._tile_grid.layout)

    _swap(page, *(_tile(page, editor, i) for i in ids))

    wait_until(lambda: _order(editor._tile_grid.layout) != before, page, timeout=8000)
    assert editor._tile_grid.responsive_layouts == {}
    assert editor._tile_grid.reference_width >= 1200
