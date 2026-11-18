"""UI tests for each tile's frame option in the Layout tab and the frame it draws in view mode."""

import panel as pn
import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.tile_layout import tile_entry

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Chart")
def chart(config):
    return pn.pane.Markdown("chart tile", height=120)


def _border(tile) -> str:
    return tile.evaluate("el => getComputedStyle(el).borderTopWidth")


def _pick(page, label, option):
    page.get_by_role("combobox", name=label).click()
    page.get_by_role("listbox").get_by_text(option, exact=True).click()


def _done(page, editor):
    page.get_by_role("button", name="Done").click()
    if editor.dirty:
        page.get_by_role("dialog").get_by_role("button", name="Save", exact=True).click()
    wait_until(lambda: not editor.editable, timeout=8000)


def test_frame_option_in_the_layout_tab_frames_the_tile_in_view_mode(page):
    store = MemoryDashboardStore()
    editor = FlowDash({"Test/chart": chart}, notifications=False, store=store, user="alice")
    model = editor.new_dashboard("Frames")
    chart_id = editor.add_component("Test/chart")
    divider_id = editor.add_component("Text/Divider")
    editor.save()
    serve_component(page, editor)

    page.get_by_role("button", name="Layout", exact=True).click()
    wait_until(lambda: editor.mode == "dashboard", timeout=8000)
    _pick(page, "Tile", "Divider")
    wait_until(lambda: editor._layout_panel.selected == divider_id, timeout=8000)
    expect(page.get_by_role("combobox", name="Frame")).to_have_text("Default (unframed)")

    _done(page, editor)
    tiles = page.locator(".flowdash-tile")
    expect(tiles).to_have_count(2)
    wait_until(lambda: _border(tiles.nth(0)) == "1px", page=page)
    assert _border(tiles.nth(1)) == "0px"

    page.get_by_role("button", name="Edit").click()
    wait_until(lambda: editor.editable, timeout=8000)
    page.get_by_role("button", name="Layout", exact=True).click()
    _pick(page, "Tile", "Divider")
    _pick(page, "Frame", "Framed")
    wait_until(lambda: tile_entry(editor, divider_id).get("framed") is True, timeout=8000)
    _pick(page, "Tile", "Chart")
    wait_until(lambda: editor._layout_panel.selected == chart_id, timeout=8000)
    _pick(page, "Frame", "Unframed")
    wait_until(lambda: tile_entry(editor, chart_id).get("framed") is False, timeout=8000)

    _done(page, editor)
    wait_until(lambda: _border(tiles.nth(1)) == "1px", page=page)
    assert _border(tiles.nth(0)) == "0px"
    radius = tiles.nth(1).evaluate("el => getComputedStyle(el).borderTopLeftRadius")
    assert radius == "12px"
    saved = store.load_dashboard("alice", model.dashboard_id)
    saved = {e["instance_id"]: e.get("framed") for e in saved.tile_layout}
    assert saved == {chart_id: False, divider_id: True}
