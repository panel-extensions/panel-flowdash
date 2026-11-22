"""UI tests for the sidecar's Spec tab, where the code panel edits a spec unless it is under the canvas."""

import json

import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash
from panel_flowdash.ui.split_view import STORAGE_KEY
from tests.test_code_panel import make_editor
from tests.test_spec_settings import COMPONENTS as SPEC_COMPONENTS
from tests.ui.test_ui_code_panel_format_dock import _ace_text
from tests.ui.test_ui_spec_settings import Chart, Rows, _chart_shows
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

WIDE = {"width": 1440, "height": 900}
NARROW = {"width": 900, "height": 800}
# Slow runners, e.g. Windows, take seconds for a round trip.
TIMEOUT = 15000


def _sidecar_tab(page, name):
    return page.locator(".fd-sidecar").get_by_role("tab", name=name, exact=True)


def _select(page, editor, node):
    page.locator(f".react-flow__node[data-id='{node}']").click()
    wait_until(lambda: editor._flow.selection.get("nodes") == [node], page, timeout=TIMEOUT)


def test_a_spec_edited_in_the_spec_tab_updates_the_tile(page):
    editor = FlowDash(
        {"T/rows": Rows, "T/chart": Chart}, notifications=False, include_builtin_components=False
    )
    rows = editor.add_component("T/rows", position=(0, 0))
    chart = editor.add_component("T/chart", position=(320, 0))
    editor.connect(rows, "data", chart, "data")
    page.set_viewport_size(WIDE)
    serve_component(page, editor)
    _select(page, editor, chart)
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=TIMEOUT)
    _sidecar_tab(page, "Spec").click(timeout=TIMEOUT)
    # Outside the canvas tabs the panel is in the sidecar, opened on the node.
    panel = page.locator(".fd-spec-tab .fd-code-panel")
    expect(panel).to_be_visible(timeout=TIMEOUT)
    assert editor.mode == "dashboard"
    title = panel.get_by_role("textbox", name="Title", exact=True)
    title.fill("Capacity by year")
    # "Run on change" is on for a spec, so the form edit runs without Run.
    title.press("Enter")
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "Capacity by year",
        page,
        timeout=TIMEOUT,
    )
    wait_until(lambda: _chart_shows(page, "Capacity by year"), page, timeout=TIMEOUT)


def test_a_draft_survives_moving_between_the_bottom_panel_and_the_spec_tab(page):
    editor = FlowDash(SPEC_COMPONENTS, notifications=False, include_builtin_components=False)
    node = editor.add_component("D/json", position=(0, 0))
    page.set_viewport_size(WIDE)
    serve_component(page, editor)
    _select(page, editor, node)
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    editor.code_panel.run_on_change = False
    page.locator(".fd-code-spec-view button", has_text="JSON").click()
    text = page.locator(".fd-code-panel-spec-json")
    text.locator(".ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text('{"title": "Draft"}')
    wait_until(lambda: editor.code_panel.dirty, page, timeout=TIMEOUT)

    toggle = page.locator(".fd-code-dock-toggle button")
    expect(toggle).to_have_attribute("aria-label", "Open in sidecar")
    toggle.click()
    expect(page.locator(".fd-spec-tab .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_have_count(0)
    expect(_sidecar_tab(page, "Spec")).to_have_attribute("aria-selected", "true")
    expect(page.locator(".fd-spec-tab .fd-code-panel-spec-json .ace_content")).to_contain_text(
        "Draft", timeout=TIMEOUT
    )
    stored = json.loads(page.evaluate(f"localStorage.getItem({STORAGE_KEY!r})"))
    assert stored["code_dock"] == "sidecar"

    # Leaving the tab and coming back keeps it too.
    _sidecar_tab(page, "Settings").click()
    _sidecar_tab(page, "Spec").click()
    expect(page.locator(".fd-spec-tab .fd-code-panel-spec-json .ace_content")).to_contain_text(
        "Draft", timeout=TIMEOUT
    )

    page.locator(".fd-spec-tab .fd-code-dock-toggle button").click()
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".fd-spec-tab .fd-code-panel")).to_have_count(0)
    wait_until(
        lambda: _ace_text(page, "fd-code-panel-spec-json") == '{"title": "Draft"}',
        page,
        timeout=TIMEOUT,
    )
    assert editor.code_panel.dirty
    assert editor._component_instances[node].spec == {"a": 1}


def test_a_narrow_window_edits_in_the_spec_tab_instead_of_a_dialog(page):
    editor, query = make_editor()
    page.set_viewport_size(NARROW)
    serve_component(page, editor)
    _select(page, editor, query)
    expect(page.locator(".fd-code-snippet")).to_have_count(0)
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    expect(_sidecar_tab(page, "Spec")).to_have_attribute("aria-selected", "true", timeout=TIMEOUT)
    code = page.locator(".fd-spec-tab .fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=TIMEOUT)
    expect(page.get_by_role("dialog")).to_have_count(0)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_have_count(0)
    # The window is too narrow for the panel under the canvas, so it is not offered.
    expect(page.locator(".fd-spec-tab .fd-code-dock-toggle button")).to_be_hidden()

    code.locator(".ace_content").click()
    page.keyboard.press("End")
    page.keyboard.type(" and p_cap > 1")
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(
        lambda: editor._component_instances[query].expr == "p_year > 2014 and p_cap > 1",
        page,
        timeout=TIMEOUT,
    )


def test_narrowing_the_window_moves_the_panel_into_the_spec_tab(page):
    editor, query = make_editor()
    page.set_viewport_size(WIDE)
    serve_component(page, editor)
    _select(page, editor, query)
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    page.set_viewport_size(NARROW)
    expect(page.locator(".fd-spec-tab .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_have_count(0)
    page.set_viewport_size(WIDE)
    expect(page.locator(".fd-code-dock .fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    expect(page.locator(".fd-spec-tab .fd-code-panel")).to_have_count(0)


def test_a_definition_only_node_shows_its_definition_read_only(page):
    editor, _query = make_editor()
    defined = editor.add_component("D/defined", position=(0, 300))
    page.set_viewport_size(WIDE)
    serve_component(page, editor)
    _select(page, editor, defined)
    _sidecar_tab(page, "Spec").click(timeout=TIMEOUT)
    # Under the canvas by default, so the tab offers the sidecar.
    page.locator(".fd-spec-open-here button").click(timeout=TIMEOUT)
    definition = page.locator(".fd-spec-tab .fd-code-definition")
    expect(definition.locator(".ace_content")).to_contain_text("tables:", timeout=TIMEOUT)
    expect(page.locator(".fd-spec-tab")).to_contain_text("Read only")
    expect(page.locator(".fd-spec-tab .fd-code-run")).to_be_hidden()
    definition.locator(".ace_content").click()
    page.keyboard.type("x")
    page.wait_for_timeout(300)
    expect(definition.locator(".ace_content")).not_to_contain_text("xtype")
    assert editor.code_panel.definition.readonly
