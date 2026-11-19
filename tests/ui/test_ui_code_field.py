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
