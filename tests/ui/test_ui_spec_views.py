"""UI tests for a spec's Form, YAML and JSON views, "Run on change" and the stale result markers."""

import json

import pytest
from panel.tests.util import wait_until

from tests.test_code_panel import make_editor
from tests.ui.test_ui_code_panel import _open as _open_query, _type_at_end
from tests.ui.test_ui_spec_settings import SPEC, _chart_shows, _open

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

TIMEOUT = 15000

# The text and cursor row of the spec's editor, and whether it has the keyboard.
_ACE = """() => {
  function find(root) {
    for (const el of root.querySelectorAll('*')) {
      if (el.classList.contains('ace_editor') && el.env && el.offsetParent !== null
          && root.host && root.host.classList.contains('fd-spec-editor')) {
        const editor = el.env.editor
        return {text: editor.getValue(), row: editor.getCursorPosition().row, focused: editor.isFocused()}
      }
      if (el.shadowRoot) { const hit = find(el.shadowRoot); if (hit) return hit }
    }
    return null
  }
  return find(document)
}"""


def _view(page, name):
    page.locator(".fd-code-spec-view").get_by_role("button", name=name, exact=True).click()


def _ace(page):
    return page.evaluate(_ACE) or {}


def _replace_text(page, text):
    page.locator(".fd-spec-editor .ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text(text)


def test_views_edit_one_spec_and_yaml_keeps_on_keys_and_comments(page):
    editor, chart = _open(page)
    _view(page, "YAML")
    wait_until(lambda: _ace(page).get("text", "").startswith("mark: bar\n"), page, timeout=TIMEOUT)
    yaml = (
        "# Picked bars\n"
        "mark: bar  # bars\n"
        "params:\n"
        "  - name: pick\n"
        "    select: {type: point, on: click}\n"
        "encoding:\n"
        "  x: {field: year, type: ordinal}\n"
        "  y: {field: capacity, type: quantitative}\n"
        "title: From YAML\n"
    )
    _replace_text(page, yaml)
    # "Run on change" applies the text once it parses and typing pauses, without leaving the editor.
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "From YAML",
        page,
        timeout=TIMEOUT,
    )
    spec = editor._component_instances[chart].spec
    assert spec["params"][0]["select"] == {"type": "point", "on": "click"}
    assert _ace(page)["text"] == yaml.rstrip("\n") or _ace(page)["text"] == yaml
    _view(page, "JSON")
    wait_until(lambda: '"on": "click"' in _ace(page).get("text", ""), page, timeout=TIMEOUT)
    _view(page, "Form")
    expect(page.get_by_role("textbox", name="Title", exact=True)).to_have_value("From YAML")
    title = page.get_by_role("textbox", name="Title", exact=True)
    title.fill("From the form")
    title.press("Enter")
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "From the form",
        page,
        timeout=TIMEOUT,
    )
    _view(page, "YAML")
    wait_until(lambda: "title: From the form" in _ace(page).get("text", ""), page, timeout=TIMEOUT)
    text = _ace(page)["text"]
    assert text.startswith("# Picked bars\nmark: bar  # bars\n")
    assert "select: {type: point, on: click}" in text
    wait_until(lambda: _chart_shows(page, "From the form"), page, timeout=TIMEOUT)


def test_text_that_does_not_parse_is_not_run_and_marks_the_result_stale(page):
    editor, chart = _open(page)
    _view(page, "YAML")
    wait_until(lambda: _ace(page).get("text", "").startswith("mark: bar\n"), page, timeout=TIMEOUT)
    page.locator(".fd-spec-editor .ace_content").click()
    page.keyboard.press("ControlOrMeta+Home")
    page.keyboard.type("title: Fixed: x\n", delay=40)
    badge = page.locator(".fd-code-stale-badge")
    expect(badge).to_be_visible(timeout=TIMEOUT)
    expect(badge).to_contain_text("Not run")
    node = page.locator(f".react-flow__node[data-id='{chart}']")
    expect(node.locator(".fd-stale-mark")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".flowdash-tile-stale-mark:visible")).to_contain_text("Not run")
    page.wait_for_timeout(1200)
    assert "title" not in editor._component_instances[chart].spec
    assert editor.code_panel.spec_editor.error is not None
    # Typing on until the text parses runs it, and the markers clear.
    page.keyboard.press("ControlOrMeta+Home")
    page.keyboard.press("End")
    for _ in range(3):
        page.keyboard.press("Backspace")
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "Fixed",
        page,
        timeout=TIMEOUT,
    )
    expect(badge).to_be_hidden(timeout=TIMEOUT)
    expect(node.locator(".fd-stale-mark")).to_have_count(0, timeout=TIMEOUT)
    expect(page.locator(".flowdash-tile-stale-mark:visible")).to_have_count(0, timeout=TIMEOUT)


def test_a_draft_not_run_dims_the_result_until_run(page):
    editor, chart = _open(page)
    switch = page.locator(".fd-code-run-on-change input")
    expect(switch).to_be_checked()
    switch.click()
    wait_until(lambda: not editor.code_panel.run_on_change, page, timeout=TIMEOUT)
    title = page.get_by_role("textbox", name="Title", exact=True)
    title.fill("Draft")
    title.press("Enter")
    badge = page.locator(".fd-code-stale-badge")
    expect(badge).to_contain_text("Not run: showing the previous version", timeout=TIMEOUT)
    expect(page.locator(".fd-code-result.fd-code-result-dimmed")).to_be_visible()
    node = page.locator(f".react-flow__node[data-id='{chart}']")
    expect(node.locator(".fd-stale-mark")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".flowdash-tile-stale-mark:visible")).to_be_visible()
    page.wait_for_timeout(800)
    assert "title" not in editor._component_instances[chart].spec
    badge.get_by_role("button", name="Run").click()
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "Draft",
        page,
        timeout=TIMEOUT,
    )
    expect(badge).to_be_hidden(timeout=TIMEOUT)
    expect(page.locator(".fd-code-result.fd-code-result-dimmed")).to_have_count(0)
    expect(node.locator(".fd-stale-mark")).to_have_count(0, timeout=TIMEOUT)
    expect(page.locator(".flowdash-tile-stale-mark:visible")).to_have_count(0)


def test_a_section_the_form_cannot_show_links_to_its_line(page):
    editor, _chart = _open(page)
    row = page.locator(".fd-spec-unshown-row", has_text="transform")
    expect(row).to_be_visible(timeout=TIMEOUT)
    row.get_by_role("button", name="Edit transform in the YAML").click()
    expect(page.locator(".fd-code-spec-view button[aria-pressed='true']")).to_have_text("YAML")
    wait_until(lambda: _ace(page).get("focused"), page, timeout=TIMEOUT)
    ace = _ace(page)
    assert ace["text"].splitlines()[ace["row"]].startswith("transform:")
    assert editor.code_panel.spec_view == "yaml"


def test_text_that_does_not_parse_stays_in_its_view(page):
    _editor, _chart = _open(page)
    _view(page, "JSON")
    wait_until(lambda: _ace(page).get("text", "").startswith("{"), page, timeout=TIMEOUT)
    _replace_text(page, '{"mark": ')
    _view(page, "YAML")
    expect(page.locator(".fd-code-spec-view button[aria-pressed='true']")).to_have_text("JSON")
    expect(page.locator(".fd-spec-status")).to_contain_text("Not switched to YAML")
    assert _ace(page)["text"] == '{"mark": '
    _replace_text(page, json.dumps({**SPEC, "title": "Pasted"}))
    _view(page, "YAML")
    wait_until(lambda: "title: Pasted" in _ace(page).get("text", ""), page, timeout=TIMEOUT)


def test_code_keeps_manual_run(page):
    editor, query = make_editor()
    _panel, code = _open_query(page, editor, query)
    expect(page.locator(".fd-code-run-on-change input")).not_to_be_checked()
    _type_at_end(page, code, " and p_cap > 2")
    expect(page.locator(".fd-code-stale-badge")).to_contain_text("Not run", timeout=TIMEOUT)
    node = page.locator(f".react-flow__node[data-id='{query}']")
    expect(node.locator(".fd-stale-mark")).to_be_visible(timeout=TIMEOUT)
    page.wait_for_timeout(1500)
    assert editor._component_instances[query].expr == "p_year > 2014"
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap > 2",
        page,
        timeout=TIMEOUT,
    )
    expect(page.locator(".fd-code-stale-badge")).to_be_hidden(timeout=TIMEOUT)
    expect(node.locator(".fd-stale-mark")).to_have_count(0, timeout=TIMEOUT)


def test_escape_then_tab_leaves_the_spec_text(page):
    _open(page)
    _view(page, "JSON")
    wait_until(lambda: '"mark": "bar"' in _ace(page).get("text", ""), page, timeout=TIMEOUT)
    before = _ace(page)["text"]
    page.locator(".fd-spec-editor .ace_content").click()
    wait_until(lambda: _ace(page).get("focused"), page, timeout=TIMEOUT)
    page.keyboard.press("Escape")
    page.keyboard.press("Tab")
    wait_until(lambda: not _ace(page).get("focused"), page, timeout=TIMEOUT)
    assert _ace(page)["text"] == before, "Tab inserted nothing"
