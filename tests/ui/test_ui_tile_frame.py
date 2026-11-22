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


def _framed(tile) -> bool:
    return tile.evaluate("el => getComputedStyle(el).borderTopColor") != "rgba(0, 0, 0, 0)"


def _pick(page, label, option, current):
    """Pick *option* from the select *label*, once it shows the tile's *current* choice.

    Selecting another tile swaps the Tile tab's controls when the browser gets them;
    a menu opened on the previous tile's select closes as its select goes.
    """
    select = page.get_by_role("combobox", name=label)
    expect(select).to_have_text(current, timeout=8000)
    select.click()
    listbox = page.get_by_role("listbox")
    expect(listbox).to_be_visible()
    listbox.get_by_text(option, exact=True).click()
    expect(select).to_have_text(option)


def _done(page, editor):
    page.get_by_role("button", name="Done").click()
    if editor.dirty:
        page.get_by_role("dialog").get_by_role("button", name="Save", exact=True).click()
    wait_until(lambda: not editor.editable, timeout=8000)


def _select_tile(page, editor, instance_id):
    """Click a tile in the Layout tab, which selects its node and shows its Tile tab.

    A click rather than a selection made from the test's thread: the canvas leaving
    the page as the tab opens reports an empty selection, which could come after it.
    """
    # The grid is filled once the Layout tab is shown. An unframed tile has no frame
    # of its own to find it by, so it is found by its place.
    wait_until(lambda: instance_id in editor._grid_ids, timeout=8000)
    item = page.locator(".muuri-grid-item").nth(editor._grid_ids.index(instance_id))
    expect(item).to_be_visible(timeout=8000)
    item.click(position={"x": 20, "y": 20})
    wait_until(lambda: editor._inspector._shown == ("node", instance_id), timeout=8000)
    expect(page.get_by_role("tab", name="Tile", selected=True)).to_be_visible(timeout=8000)


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
    _select_tile(page, editor, divider_id)
    expect(page.get_by_role("combobox", name="Frame")).to_have_text("Default (unframed)")

    _done(page, editor)
    tiles = page.locator(".flowdash-tile")
    expect(tiles).to_have_count(2)
    wait_until(lambda: _framed(tiles.nth(0)), page=page)
    assert not _framed(tiles.nth(1))

    page.get_by_role("button", name="Edit").click()
    wait_until(lambda: editor.editable, timeout=8000)
    page.get_by_role("button", name="Layout", exact=True).click()
    _select_tile(page, editor, divider_id)
    _pick(page, "Frame", "Framed", "Default (unframed)")
    wait_until(lambda: tile_entry(editor, divider_id).get("framed") is True, timeout=8000)
    _select_tile(page, editor, chart_id)
    _pick(page, "Frame", "Unframed", "Default (framed)")
    wait_until(lambda: tile_entry(editor, chart_id).get("framed") is False, timeout=8000)

    _done(page, editor)
    wait_until(lambda: _framed(tiles.nth(1)), page=page)
    assert not _framed(tiles.nth(0))
    radius = tiles.nth(1).evaluate("el => getComputedStyle(el).borderTopLeftRadius")
    assert radius == "12px"
    saved = store.load_dashboard("alice", model.dashboard_id)
    saved = {e["instance_id"]: e.get("framed") for e in saved.tile_layout}
    assert saved == {chart_id: False, divider_id: True}


_TEXT_LEFT = """el => {
  const range = document.createRange()
  range.selectNodeContents(el)
  return range.getBoundingClientRect().left
}"""


def _text_left(locator):
    return locator.evaluate(_TEXT_LEFT)


def test_unframed_tiles_line_up_with_framed_ones(page):
    editor = FlowDash(notifications=False)
    editor.add_component("Text/Heading")
    editor.add_component("Text/Markdown", config={"text": "Text in a card"})
    editor.add_component("Text/Divider")
    serve_component(page, editor)

    page.get_by_role("button", name="Layout", exact=True).click()
    wait_until(lambda: editor.mode == "dashboard", timeout=8000)
    tiles = page.locator(".flowdash-tile")
    expect(tiles.nth(1).get_by_text("Text in a card", exact=True)).to_be_visible()
    heading = tiles.nth(0).get_by_role("heading", name="Heading")
    arranging = _text_left(heading)

    editor.editable = False
    expect(tiles).to_have_count(3)
    wait_until(lambda: _framed(tiles.nth(1)), page=page)
    assert not _framed(tiles.nth(0)) and not _framed(tiles.nth(2))

    left = _text_left(heading)
    assert abs(left - _text_left(tiles.nth(1).get_by_text("Text in a card", exact=True))) <= 1
    assert abs(left - arranging) <= 1
    card = tiles.nth(1).bounding_box()
    rule = tiles.nth(2).locator(".MuiDivider-root").bounding_box()
    # The rule spans the cards' content box: inside their border and 8 px inset.
    assert abs(rule["x"] - (card["x"] + 9)) <= 1
    assert abs(rule["x"] + rule["width"] - (card["x"] + card["width"] - 9)) <= 1
