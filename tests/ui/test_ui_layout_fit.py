"""UI tests for arranging the Desktop layout in a grid narrower than Desktop."""

import panel as pn
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import tile_entry, update_tile

from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Heading")
class HeadingView(Viewer):
    """A view without width bounds."""

    def __panel__(self):
        return pn.pane.Markdown("# Heading")


@register(page=False, component=True, title="KPI", min_size={"width": 300})
class KpiView(Viewer):
    """A view wider than a quarter of a grid narrower than 1200px."""

    def __panel__(self):
        return pn.pane.Markdown("72,357")


HEADING, KPI = "Demo/heading", "Demo/kpi"


def _editor():
    editor = FlowDash(
        {HEADING: HeadingView, KPI: KpiView}, notifications=False, include_builtin_components=False
    )
    ids = [editor.add_component(c) for c in (HEADING, KPI)]
    update_tile(editor, ids[0], width=75.0, height=130)
    update_tile(editor, ids[1], width=25.0, height=130)
    editor.dirty = False
    return editor, ids


def _tile(page, editor, instance_id):
    key = next(c for c in editor._inspect_tiles[instance_id].css_classes if "-key-" in c)
    return page.locator(".muuri-grid-item").filter(has=page.locator(f".{key}"))


def _assert_side_by_side(page, editor, heading_id, kpi_id):
    grid = stable_box(page.locator(".muuri-grid").filter(visible=True).first, page)
    heading = stable_box(_tile(page, editor, heading_id), page)
    kpi = stable_box(_tile(page, editor, kpi_id), page)
    assert abs(kpi["y"] - heading["y"]) < 1
    assert kpi["x"] >= heading["x"] + heading["width"] - 1
    assert kpi["x"] + kpi["width"] <= grid["x"] + grid["width"] + 0.5
    return grid, heading, kpi


@pytest.mark.parametrize("width", [1440, 1500])
def test_layout_tab_keeps_a_kpi_beside_the_heading_in_a_narrow_grid(page, width):
    editor, (heading_id, kpi_id) = _editor()
    page.set_viewport_size({"width": width, "height": 900})
    serve_component(page, editor)
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=8000)
    expect(page.get_by_text("Arrange for")).to_be_visible()
    assert editor._tile_grid.preview_band == "md"

    grid, _heading, kpi = _assert_side_by_side(page, editor, heading_id, kpi_id)
    # The grid is narrower than Desktop, so a quarter of it is less than the KPI's 300px.
    assert grid["width"] < 1200
    assert kpi["width"] >= 300 - 21
    assert tile_entry(editor, heading_id)["width"] == 75.0
    assert tile_entry(editor, kpi_id)["width"] == 25.0
    assert not editor.dirty


def test_split_tab_reflows_for_the_pane_width(page):
    editor, (heading_id, kpi_id) = _editor()
    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, editor)
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    assert editor._tile_grid.preview_band is None

    # The pane is too narrow for the KPI's 300px beside the heading, so the
    # generated layout puts each on a line of its own, as the dashboard would.
    def stacked():
        heading = _tile(page, editor, heading_id).bounding_box()
        kpi = _tile(page, editor, kpi_id).bounding_box()
        return heading and kpi and kpi["y"] >= heading["y"] + heading["height"] - 1

    wait_until(stacked, page, timeout=8000)
    grid = stable_box(page.locator(".muuri-grid").filter(visible=True).first, page)
    assert grid["width"] < 600
    for instance_id in (heading_id, kpi_id):
        tile = _tile(page, editor, instance_id)
        box = stable_box(tile, page)
        assert box["x"] >= grid["x"] - 0.5
        assert box["x"] + box["width"] <= grid["x"] + grid["width"] + 0.5
        assert box["width"] > grid["width"] * 0.9
        # Nothing in the tile is cut off at its edge.
        assert tile.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
    assert tile_entry(editor, heading_id)["width"] == 75.0
    assert tile_entry(editor, kpi_id)["width"] == 25.0
    assert editor._tile_grid.responsive_layouts == {}
