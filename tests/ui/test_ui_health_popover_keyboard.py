"""UI tests for using the health chip's popover with the keyboard and a screen reader."""

import pytest
from panel.tests.util import wait_until

from .test_ui_view_header import _serve, _store

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# Whether the focused element, found through the shadow roots, lies in the given one.
_HAS_FOCUS = """
(root) => {
  let el = document.activeElement
  while (el && el.shadowRoot && el.shadowRoot.activeElement) { el = el.shadowRoot.activeElement }
  for (let node = el; node; node = node.parentNode || node.host) {
    if (node === root) return true
  }
  return false
}
"""


def _chip(page):
    return page.locator(".fd-health-chip").get_by_role("button")


def _dialog(page):
    return page.get_by_role("dialog", name="Dashboard health")


def _open_with_enter(page):
    chip = _chip(page)
    chip.focus()
    page.keyboard.press("Enter")
    dialog = _dialog(page)
    expect(dialog).to_be_visible()
    expect(chip).to_have_attribute("aria-expanded", "true")
    return chip, dialog


def test_the_chip_announces_its_dialog(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    chip = _chip(page)
    expect(chip).to_have_attribute("aria-haspopup", "dialog")
    expect(chip).to_have_attribute("aria-expanded", "false")


def test_enter_opens_the_dialog_with_the_focus_inside(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    _, dialog = _open_with_enter(page)
    assert dialog.evaluate(_HAS_FOCUS)
    expect(dialog.get_by_text("Unit Picker")).to_be_visible()


def test_tab_never_reaches_the_page_behind_the_dialog(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    chip, dialog = _open_with_enter(page)
    page.keyboard.press("Tab")
    expect(dialog.get_by_role("button", name="Show issues")).to_be_focused()
    # Past the last action the focus either wraps inside the dialog, or the
    # dialog closes and the focus is back on the chip.
    for _ in range(3):
        page.keyboard.press("Tab")
        wait_until(
            lambda: (
                dialog.evaluate(_HAS_FOCUS) if dialog.is_visible() else chip.evaluate(_HAS_FOCUS)
            ),
            page,
        )
        if not dialog.is_visible():
            expect(chip).to_have_attribute("aria-expanded", "false")
            break


def test_escape_closes_and_returns_the_focus_to_the_chip(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    chip, dialog = _open_with_enter(page)
    page.keyboard.press("Tab")
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(chip).to_be_focused()
    expect(chip).to_have_attribute("aria-expanded", "false")


def test_the_chip_closes_its_own_dialog(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    chip = _chip(page)
    chip.click()
    expect(_dialog(page)).to_be_visible()
    chip.click(timeout=3000)
    expect(_dialog(page)).to_be_hidden()
    expect(chip).to_have_attribute("aria-expanded", "false")


def test_pressing_outside_closes_the_dialog(page):
    store, dash = _store()
    _serve(page, store, dash, [])

    _chip(page).click()
    expect(_dialog(page)).to_be_visible()
    page.get_by_text("output chart tile").first.click()
    expect(_dialog(page)).to_be_hidden()
