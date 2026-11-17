"""UI tests for the built-in Markdown, Notes, Heading and Divider components."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


def test_text_components_render_in_tiles(page):
    editor = FlowDash({}, notifications=False)
    editor.add_component("Text/Heading", config={"text": "Fleet overview", "subtitle": "By state"})
    editor.add_component("Text/Divider", config={"label": "Details"})
    editor.add_component(
        "Text/Markdown", config={"text": "## Wind\n\nRaw <b>tags</b> stay *text*"}
    )
    editor.mode = "dashboard"
    editor.editable = False
    serve_component(page, editor)

    expect(page.get_by_text("Fleet overview")).to_be_visible()
    expect(page.get_by_text("By state")).to_be_visible()
    expect(page.get_by_text("Details", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Wind")).to_be_visible()
    expect(page.get_by_text("Raw <b>tags</b> stay")).to_be_visible()
    expect(page.locator("em", has_text="text")).to_be_visible()


def test_notes_are_typed_into_the_tile_and_saved(page):
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Text/Notes", config={"text": "<p>First</p>"})
    editor.mode = "dashboard"
    serve_component(page, editor)

    notes = page.locator(".ql-editor")
    expect(notes).to_have_attribute("contenteditable", "true")
    notes.click()
    page.keyboard.press("End")
    page.keyboard.type(" and more")
    wait_until(lambda: editor.graph.get_config_state(node).text == "<p>First and more</p>", page)
    assert editor._tile_items[0]["config"]["text"] == "<p>First and more</p>"

    page.keyboard.press("Shift+Home")
    expect(page.locator(".ql-tooltip:not(.ql-hidden) .ql-bold")).to_be_visible()

    editor.editable = False
    expect(notes).to_have_attribute("contenteditable", "false")
