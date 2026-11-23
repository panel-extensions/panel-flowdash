"""UI tests for the built-in Markdown, Notes, Heading and Divider components."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import register
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


def test_notes_the_editor_rewrites_on_render_stay_saved_as_they_are(page):
    stored = "<p>Line one</p>\n<p>Hello   world</p><p></p><h1>T</h1>x"
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Text/Notes", config={"text": stored})
    editor.load_model(editor.to_model(title="Notes"))
    editor.mode = "dashboard"
    assert not editor.dirty
    serve_component(page, editor)

    notes = page.locator(".ql-editor")
    expect(notes).to_have_attribute("contenteditable", "true")
    notes_widget = editor._component_instances[node]._editor
    shown = "<p>Line one</p><p>Hello world</p><p></p><h1>T</h1><p>x</p>"
    wait_until(lambda: notes_widget.value == shown, page)
    page.wait_for_timeout(300)
    assert editor.graph.get_config_state(node).text == stored
    assert not editor.dirty

    notes.get_by_text("x", exact=True).click()
    page.keyboard.press("End")
    page.keyboard.type("yz")
    edited = shown.replace("x</p>", "xyz</p>")
    wait_until(lambda: editor.graph.get_config_state(node).text == edited, page)
    assert editor.dirty


def test_notes_are_not_editable_in_preview(page):
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Text/Notes", config={"text": "<p>First</p>"})
    editor.mode = "dashboard"
    editor.preview = True
    serve_component(page, editor)

    expect(page.locator(".ql-editor")).to_have_attribute("contenteditable", "false")
    editor.preview = False
    expect(page.locator(".ql-editor")).to_have_attribute("contenteditable", "true")
    assert editor.graph.get_config_state(node).text == "<p>First</p>"


@register(
    page=False,
    component=True,
    title="Region query",
    requires=[{"key": "period", "required": False}],
    provides=[{"key": "data", "type": "DataFrame"}],
)
class RegionQuery(Viewer):
    """A query node like Lumen's SQL node: a period in, a table of shares by region out."""

    period = param.Parameter(default=None)

    @param.output(param.DataFrame)
    @param.depends("period")
    def data(self):
        if self.period == "Week":
            return pd.DataFrame({"region": ["APAC", "EMEA"], "share": [0.61, 0.39]})
        return pd.DataFrame({"region": ["EMEA", "APAC", "<b>AMER</b>"], "share": [0.5, 0.3, 0.2]})

    def __panel__(self):
        return pn.pane.Str("query")


def test_a_text_tile_fills_from_a_table_and_follows_it(page):
    editor = FlowDash({"T/query": RegionQuery}, notifications=False)
    period = editor.add_component(
        "Widgets/RadioGroup", config={"label": "Period", "default_options": ["Day", "Week"]}
    )
    query = editor.add_component("T/query")
    text = (
        "Leader: **{{ table.region }}** at {{ table.share:.0% }}\n\n"
        "{{#each table limit=5}}\n- {{ .region }}: {{ .share:.0% }}\n{{/each}}"
    )
    note = editor.add_component("Text/Markdown", config={"text": text})
    assert editor.connect(period, "selected", query, "period") is True
    assert editor.connect(query, "data", note, "table") is True
    editor.mode = "dashboard"
    editor.editable = False
    serve_component(page, editor)

    tile = page.locator(".flowdash-tile").nth(
        [item["instance_id"] for item in editor._tile_items].index(note)
    )
    expect(tile.locator("strong", has_text="EMEA")).to_be_visible()
    expect(tile.locator("li")).to_have_text(["EMEA: 50%", "APAC: 30%", "<b>AMER</b>: 20%"])
    expect(tile.locator("b")).to_have_count(0)

    page.get_by_role("radio", name="Week").check()
    expect(tile.locator("strong", has_text="APAC")).to_be_visible()
    expect(tile.get_by_text("Leader: APAC at 61%")).to_be_visible()
    expect(tile.locator("li")).to_have_text(["APAC: 61%", "EMEA: 39%"])
