"""UI tests that dragging tiles, nodes, palette entries and report blocks selects no text."""

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash, register
from panel_flowdash.ui.drag_guard import DragGuard

from .util import DASHBOARD_LOAD_TIMEOUT, center, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

TEXT = "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor. " * 6


@register(page=False, component=True, title="Notes", default_size={"width": 400, "height": 260})
def notes(config):
    return pn.pane.Markdown(TEXT, sizing_mode="stretch_both")


@register(page=False, component=True, title="Region")
def region(config):
    return pmui.Select(label="Region", options=["North", "South"])


COMPONENTS = {"T/notes": notes, "T/region": region}

# The text selected anywhere on the page, across shadow roots.
_SELECTED = """() => {
  const texts = [document.getSelection()?.toString() ?? ""]
  const walk = (root) => root.querySelectorAll("*").forEach((el) => {
    if (el.shadowRoot) {
      texts.push(el.shadowRoot.getSelection?.()?.toString() ?? "")
      walk(el.shadowRoot)
    }
  })
  walk(document)
  return texts.join("").trim()
}"""

_BODY_USER_SELECT = (
    "() => [document.body.style.userSelect, getComputedStyle(document.body).userSelect]"
)


def _editor(mode="dashboard"):
    editor = FlowDash(COMPONENTS, notifications=False, user="alice", history_coalesce=0)
    editor.new_dashboard("Drag")
    for instance_id in ("first", "second", "third"):
        editor.add_component("T/notes", instance_id=instance_id)
    editor.mode = mode
    return editor


def _drag(page, start, end, *, steps=20, during=None):
    """Drag with the mouse from *start* to *end*, calling *during* before the release."""
    page.mouse.move(*start)
    page.mouse.down()
    for k in range(1, steps + 1):
        page.mouse.move(
            start[0] + (end[0] - start[0]) * k / steps, start[1] + (end[1] - start[1]) * k / steps
        )
        page.wait_for_timeout(20)
    if during is not None:
        during()
    page.mouse.up()


def _held(page):
    assert page.evaluate(_BODY_USER_SELECT)[1] == "none"


def _check_released(page, before):
    wait_until(lambda: page.evaluate(_BODY_USER_SELECT) == before, page, timeout=5000)
    assert page.evaluate(_SELECTED) == ""


def test_dragging_a_tile_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    tiles = page.locator(".muuri-grid-item")
    expect(tiles).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    before = page.evaluate(_BODY_USER_SELECT)

    first = tiles.first
    dragged = page.locator(f".muuri-grid-item[data-id='{first.get_attribute('data-id')}']")
    start = stable_box(dragged, page)
    dragged.hover()
    handle = center(stable_box(dragged.locator(".muuri-handle.drag"), page))
    last = stable_box(tiles.last, page)
    # Across the text of the other tiles, past the bottom of the grid.
    _drag(
        page,
        handle,
        (last["x"] + last["width"] - 20, last["y"] + last["height"] + 40),
        steps=40,
        during=lambda: _held(page),
    )
    wait_until(lambda: stable_box(dragged, page) != start, page, timeout=5000)
    _check_released(page, before)


def test_resizing_a_tile_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    tiles = page.locator(".muuri-grid-item")
    expect(tiles).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    before = page.evaluate(_BODY_USER_SELECT)

    first = tiles.first
    first.hover()
    handle = center(stable_box(first.locator(".muuri-handle.resize"), page))
    last = stable_box(tiles.last, page)
    _drag(
        page,
        handle,
        (last["x"] + last["width"] / 2, last["y"] + last["height"] - 20),
        during=lambda: _held(page),
    )
    _check_released(page, before)


def test_dragging_a_node_on_the_canvas_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor(mode="wiring")
    serve_component(page, editor)
    nodes = page.locator(".react-flow__node")
    expect(nodes).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    before = page.evaluate(_BODY_USER_SELECT)

    header = stable_box(nodes.first, page)
    start = (header["x"] + header["width"] / 2, header["y"] + 14)
    palette = stable_box(page.get_by_placeholder("Search components"), page)
    # Out of the canvas, over the palette's text.
    _drag(page, start, (palette["x"] + 20, palette["y"] + 200), during=lambda: _held(page))
    _check_released(page, before)


def test_dragging_from_the_palette_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor(mode="wiring")
    serve_component(page, editor)
    expect(page.locator(".react-flow__node")).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    before = page.evaluate(_BODY_USER_SELECT)

    item = page.locator(".MuiListItemButton-root[draggable='true']").filter(has_text="Region")
    pane = stable_box(page.locator(".react-flow__pane"), page)
    _drag(
        page,
        center(stable_box(item, page)),
        (pane["x"] + 60, pane["y"] + 60),
        during=lambda: _held(page),
    )
    wait_until(lambda: len(editor._flow.nodes) == 4, page, timeout=8000)
    _check_released(page, before)


def test_reordering_report_blocks_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    editor.set_report(
        {
            "title": "Notes",
            "blocks": [{"heading": "Summary"}, {"text": TEXT}, {"text": TEXT}],
        }
    )
    serve_component(page, editor)
    page.get_by_role("button", name="Report", exact=True).click()
    report = page.locator(".flowdash-report")
    expect(report.locator(".fd-report-handle button")).to_have_count(
        3, timeout=DASHBOARD_LOAD_TIMEOUT
    )
    before = page.evaluate(_BODY_USER_SELECT)

    handle = center(stable_box(report.locator(".fd-report-handle button").first, page))
    last = stable_box(report.locator(".fd-report-block").last, page)
    _drag(
        page,
        handle,
        (last["x"] + last["width"] - 40, last["y"] + last["height"] - 10),
        during=lambda: _held(page),
    )
    wait_until(lambda: [next(iter(b)) for b in editor.report["blocks"]][-1] == "heading", page)
    _check_released(page, before)


def test_a_press_between_tiles_selects_no_text_but_tile_text_stays_selectable(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    tiles = page.locator(".muuri-grid-item")
    expect(tiles).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    grid = stable_box(page.locator(".muuri-grid").first, page)
    last = stable_box(tiles.last, page)

    # From the empty grid below the tiles, up across their text.
    _drag(
        page,
        (grid["x"] + grid["width"] - 20, last["y"] + last["height"] + 30),
        (grid["x"] + 40, grid["y"] + 60),
    )
    assert page.evaluate(_SELECTED) == ""

    text = stable_box(tiles.nth(1).get_by_text("Lorem ipsum").first, page)
    _drag(page, (text["x"] + 5, text["y"] + 8), (text["x"] + text["width"] - 10, text["y"] + 30))
    assert "ipsum" in page.evaluate(_SELECTED)


def test_two_guards_on_one_page_restore_the_body(page):
    handle = pn.pane.HTML(
        '<div class="muuri-handle" style="width: 100px; height: 40px">handle</div>'
    )
    serve_component(page, pn.Column(DragGuard(), DragGuard(), handle, "Some text to select"))
    before = page.evaluate(_BODY_USER_SELECT)
    box = stable_box(page.get_by_text("handle", exact=True), page)
    _drag(page, (box["x"] + 10, box["y"] + 10), (300, 300), during=lambda: _held(page))
    _check_released(page, before)


def test_dragging_from_the_canvas_corner_selects_no_text(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor(mode="wiring")
    serve_component(page, editor)
    expect(page.locator(".react-flow__node")).to_have_count(3, timeout=DASHBOARD_LOAD_TIMEOUT)
    before = page.evaluate(_BODY_USER_SELECT)

    pane = stable_box(page.locator(".react-flow__pane"), page)
    palette = stable_box(page.get_by_placeholder("Search components"), page)
    # From beside the attribution in the corner, out over the palette's text.
    _drag(
        page,
        (pane["x"] + pane["width"] - 30, pane["y"] + pane["height"] - 8),
        (palette["x"] + 20, palette["y"] + 250),
        during=lambda: _held(page),
    )
    _check_released(page, before)
