"""UI tests for the edit button on a node's sidecar header and canvas card, and the one-line settings toggles."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash
from tests.ui.test_ui_spec_settings import Chart, Rows
from tests.ui.util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

TIMEOUT = 15000


def _serve(page, width=1440):
    editor = FlowDash(
        {"T/rows": Rows, "T/chart": Chart}, notifications=False, include_builtin_components=False
    )
    rows = editor.add_component("T/rows", position=(0, 0))
    chart = editor.add_component("T/chart", position=(320, 0))
    editor.connect(rows, "data", chart, "data")
    page.set_viewport_size({"width": width, "height": 900})
    serve_component(page, editor)
    return editor, rows, chart


def _select(page, editor, node):
    card = page.locator(f".react-flow__node[data-id='{node}']")
    # The canvas fits its view as the cards render, so a click made while it settles can miss.
    for _ in range(5):
        stable_box(card, page)
        card.click()
        try:
            wait_until(lambda: editor._flow.selection.get("nodes") == [node], page, timeout=2000)
            return card
        except TimeoutError:
            continue
    raise AssertionError(f"{node} was not selected")


def _header(page):
    return page.locator(".fd-inspector-header").filter(visible=True)


def test_the_sidecar_header_button_opens_the_spec(page):
    editor, _rows, chart = _serve(page)
    _select(page, editor, chart)

    edit = _header(page).get_by_role("button", name="Edit spec")
    expect(edit).to_be_visible(timeout=TIMEOUT)
    # Between the status dot and the menu.
    status = stable_box(_header(page).locator(".fd-inspector-status"), page)
    menu = stable_box(_header(page).get_by_role("button", name="More actions"), page)
    box = stable_box(edit, page)
    assert status["x"] < box["x"] < menu["x"]

    edit.click()
    expect(page.locator(".fd-code-panel .fd-spec-form")).to_be_visible(timeout=TIMEOUT)
    wait_until(lambda: editor.code_panel.instance_id == chart, page, timeout=TIMEOUT)


def test_a_node_without_a_spec_has_no_edit_button(page):
    editor, rows, _chart = _serve(page)
    _select(page, editor, rows)

    expect(_header(page).get_by_role("button", name="More actions")).to_be_visible(timeout=TIMEOUT)
    expect(_header(page).get_by_role("button", name="Edit spec")).to_have_count(0)
    card = page.locator(f".react-flow__node[data-id='{rows}']")
    expect(card.get_by_role("button", name="Edit spec")).to_have_count(0)


def test_the_card_edit_button_opens_the_spec(page):
    editor, _rows, chart = _serve(page)
    card = page.locator(f".react-flow__node[data-id='{chart}']")
    stable_box(card, page)
    card.hover()
    edit = card.get_by_role("button", name="Edit spec")
    expect(edit).to_be_visible(timeout=TIMEOUT)

    edit.click()
    expect(page.locator(".fd-code-panel .fd-spec-form")).to_be_visible(timeout=TIMEOUT)
    wait_until(lambda: editor.code_panel.instance_id == chart, page, timeout=TIMEOUT)


def test_the_card_edit_button_takes_the_keyboard(page):
    editor, _rows, chart = _serve(page)
    edit = page.locator(f".react-flow__node[data-id='{chart}']").get_by_role(
        "button", name="Edit spec"
    )
    edit.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".fd-code-panel .fd-spec-form")).to_be_visible(timeout=TIMEOUT)
    wait_until(lambda: editor.code_panel.instance_id == chart, page, timeout=TIMEOUT)


def test_the_settings_toggles_are_one_line_at_360px(page):
    _serve(page, width=360)
    page.get_by_role("button", name="More actions").filter(visible=True).first.click()
    page.get_by_role("menuitem", name="Settings").click()
    dialog = page.get_by_role("dialog")
    heights = {}
    for name in ("Above", "Left", "Compact", "Full"):
        button = dialog.get_by_role("button", name=name, exact=True)
        expect(button).to_be_visible(timeout=TIMEOUT)
        heights[name] = stable_box(button, page)["height"]
    # A label wrapped onto a second line makes its button about 70 px tall.
    assert len(set(heights.values())) == 1 and heights["Above"] < 48, heights
