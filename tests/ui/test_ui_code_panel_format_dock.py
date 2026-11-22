"""UI tests for formatting the code panel's text."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash
from tests.test_code_panel import make_editor, make_format_editor
from tests.test_spec_settings import COMPONENTS as SPEC_COMPONENTS
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

VIEWPORT = {"width": 1440, "height": 900}
# Slow runners, e.g. Windows, take seconds for a round trip.
TIMEOUT = 15000


def _open_node(page, node, text):
    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    panel = page.locator(".fd-code-panel")
    expect(panel).to_be_visible(timeout=TIMEOUT)
    return panel


def _ace_text(page, selector):
    return page.evaluate(
        """(selector) => {
          function find(root) {
            for (const el of root.querySelectorAll('*')) {
              if (el.classList.contains('ace_editor') && el.env && root.host
                  && root.host.classList.contains(selector)) return el.env.editor.getValue()
              if (el.shadowRoot) { const hit = find(el.shadowRoot); if (hit != null) return hit }
            }
            return null
          }
          return find(document)
        }""",
        selector,
    )


def _ace_focused(page, selector):
    return page.evaluate(
        """(selector) => {
          function find(root) {
            for (const el of root.querySelectorAll('*')) {
              if (el.classList.contains('ace_editor') && el.env && root.host
                  && root.host.classList.contains(selector)) return el.env.editor.isFocused()
              if (el.shadowRoot) { const hit = find(el.shadowRoot); if (hit != null) return hit }
            }
            return null
          }
          return find(document)
        }""",
        selector,
    )


def test_format_sql_is_an_undoable_draft(page):
    editor, node = make_format_editor()
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    panel = _open_node(page, node, "select 1; select 2")
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("select 1; select 2", timeout=TIMEOUT)
    button = page.locator(".fd-code-format button")
    expect(button).to_be_visible()
    button.hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Shift+Alt+F")

    button.click()
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-editor") == "SELECT 1\nSELECT 2",
        page,
        timeout=TIMEOUT,
    )
    wait_until(lambda: editor.code_panel.dirty, page, timeout=TIMEOUT)
    expect(panel.get_by_text("Edited since the last run")).to_be_visible()
    state = editor._dataflow_graph.get_config_state(node)
    assert state.sql == "select 1; select 2"

    code.locator(".ace_content").click()
    wait_until(lambda: _ace_focused(page, "fd-code-panel-editor"), page, timeout=TIMEOUT)
    page.keyboard.press("ControlOrMeta+z")
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-editor") == "select 1; select 2",
        page,
        timeout=TIMEOUT,
    )
    wait_until(lambda: not editor.code_panel.dirty, page, timeout=TIMEOUT)

    page.keyboard.press("Shift+Alt+F")
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-editor") == "SELECT 1\nSELECT 2",
        page,
        timeout=TIMEOUT,
    )
    wait_until(
        lambda: editor.code_panel.code.value_input == "SELECT 1\nSELECT 2", page, timeout=TIMEOUT
    )
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(lambda: state.sql == "SELECT 1\nSELECT 2", page, timeout=TIMEOUT)


def test_no_formatter_no_format_button(page):
    editor, query = make_editor()
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    _open_node(page, query, "p_year > 2014")
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=TIMEOUT)
    expect(page.locator(".fd-code-format button")).to_be_hidden()
    code.locator(".ace_content").click()
    page.keyboard.press("Shift+Alt+F")
    page.wait_for_timeout(300)
    assert editor.code_panel.code.value_input == "p_year > 2014"


def test_format_json_spec(page):
    editor = FlowDash(SPEC_COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/plain", position=(0, 0))
    page.set_viewport_size(VIEWPORT)
    serve_component(page, editor)
    _open_node(page, node, "")
    editor.code_panel.run_on_change = False
    expect(page.locator(".fd-code-format button")).to_be_hidden()
    page.locator(".fd-code-spec-view button", has_text="JSON").click()
    button = page.locator(".fd-code-format button")
    expect(button).to_be_visible()
    text = page.locator(".fd-code-panel-spec-json")
    text.locator(".ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text('{"title":"T","mark":"bar"}')
    compact = '{"title":"T","mark":"bar"}'
    wait_until(
        lambda: editor.code_panel.spec_editor.editor.value_input == compact, page, timeout=TIMEOUT
    )
    wait_until(lambda: editor.code_panel.dirty, page, timeout=TIMEOUT)
    button.click()
    expected = '{\n  "title": "T",\n  "mark": "bar"\n}'
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-spec-json") == expected, page, timeout=TIMEOUT
    )
    wait_until(
        lambda: editor.code_panel.spec_editor.value == {"title": "T", "mark": "bar"},
        page,
        timeout=TIMEOUT,
    )
    assert editor.code_panel.dirty
    assert editor._component_instances[node].spec != {"title": "T", "mark": "bar"}

    text.locator(".ace_content").click()
    wait_until(lambda: _ace_focused(page, "fd-code-panel-spec-json"), page, timeout=TIMEOUT)
    page.keyboard.press("ControlOrMeta+z")
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-spec-json") == compact, page, timeout=TIMEOUT
    )
    wait_until(
        lambda: editor.code_panel.spec_editor.editor.value_input == compact, page, timeout=TIMEOUT
    )

    # The shortcut formats the text in the editor with the keyboard.
    page.keyboard.press("Shift+Alt+F")
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-spec-json") == expected, page, timeout=TIMEOUT
    )
