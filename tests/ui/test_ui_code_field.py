"""UI tests for config fields edited in a code editor."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.test_code_field import COMPONENTS
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def test_sql_is_edited_over_several_lines_and_applied_on_blur(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/query", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    field = page.locator(".fd-code-field").first
    expect(field.locator(".ace_content")).to_contain_text("SELECT 1", timeout=8000)
    expect(field.locator(".ace_keyword").first).to_have_text("SELECT")
    field.locator(".ace_content").click()
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    page.keyboard.type("FROM turbines")
    page.wait_for_timeout(300)
    assert editor._component_instances[node].sql == "SELECT 1", "not applied while typing"
    page.locator(".react-flow__pane").click(position={"x": 5, "y": 5})
    wait_until(
        lambda: editor._component_instances[node].sql.replace(" ", "") == "SELECT1\nFROMturbines",
        timeout=8000,
    )


def test_the_expand_button_opens_a_large_editor(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/query", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.locator(".fd-code-expand").first.click(timeout=8000)
    large = page.locator(".fd-code-field-large")
    expect(large).to_have_count(1, timeout=8000)
    expect(large.locator(".ace_content")).to_contain_text("SELECT 1", timeout=8000)
    box = large.bounding_box()
    assert box["height"] >= 400 and box["width"] >= 600


def _saved_editor():
    from panel_flowdash import MemoryDashboardStore

    editor = FlowDash(
        COMPONENTS,
        notifications=False,
        include_builtin_components=False,
        store=MemoryDashboardStore(),
    )
    editor.new_dashboard("Queries")
    return editor


def test_save_while_typing_stores_the_checked_text(page):
    editor = _saved_editor()
    node = editor.add_component("D/checked", position=(0, 0))
    editor.save()
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    field = page.locator(".fd-code-field").first
    expect(field.locator(".ace_content")).to_contain_text("SELECT 1", timeout=8000)
    field.locator(".ace_content").click()
    page.keyboard.press("End")
    page.keyboard.type("0")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled(timeout=8000)
    save.click()
    wait_until(
        lambda: (
            editor.store.load_dashboard(editor.user, editor.dashboard.dashboard_id)
            .items[0]
            .config.get("sql")
            == "SELECT 10"
        ),
        timeout=8000,
    )
    wait_until(lambda: not editor.dirty, timeout=8000)


def test_escape_then_tab_leaves_the_editor(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/query", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    field = page.locator(".fd-code-field").first
    expect(field.locator(".ace_content")).to_contain_text("SELECT 1", timeout=8000)
    field.locator(".ace_content").click()
    page.wait_for_timeout(500)
    page.keyboard.press("Escape")
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)
    inside = page.evaluate(
        """() => {
          let el = document.activeElement
          while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement
          return !!(el && el.closest && el.closest('.ace_editor'))
        }"""
    )
    assert not inside
    assert editor._component_instances[node].sql == "SELECT 1", "Tab inserted nothing"


def test_the_large_editor_closes_on_escape(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/query", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.locator(".fd-code-expand").first.click(timeout=8000)
    large = page.locator(".fd-code-field-large")
    expect(large).to_have_count(1, timeout=8000)
    page.wait_for_timeout(500)
    page.keyboard.type("-- note\n")
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")
    expect(large).to_have_count(0, timeout=8000)
    # The cursor starts at the end of the text.
    wait_until(
        lambda: editor._component_instances[node].sql.startswith("SELECT 1-- note"),
        timeout=8000,
    )


def test_typing_then_deleting_leaves_save_disabled(page):
    editor = _saved_editor()
    node = editor.add_component("D/query", position=(0, 0))
    editor.save()
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    field = page.locator(".fd-code-field").first
    expect(field.locator(".ace_content")).to_contain_text("SELECT 1", timeout=8000)
    field.locator(".ace_content").click()
    page.keyboard.press("End")
    page.keyboard.type("x")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_enabled(timeout=8000)
    page.keyboard.press("Backspace")
    page.locator(".react-flow__pane").click(position={"x": 5, "y": 5})
    expect(save).to_be_disabled(timeout=8000)
