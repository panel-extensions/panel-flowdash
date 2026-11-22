"""UI tests: the selection survives tab switches and code panel moves, and Escape closes one thing at a time."""

import re

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardEdge
from tests.test_code_panel import COMPONENTS as CODE_COMPONENTS, make_editor
from tests.ui.test_ui_command_palette import _store_with
from tests.ui.test_ui_editor_split_mode import _editor, _item

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# Switching tabs moves the canvas into a new pane, so a click can reach Bokeh as a
# model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# Wide enough that the Split tab keeps the inspector open.
WIDE = {"width": 1700, "height": 900}
TIMEOUT = 10000


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _shows_node(page, editor, instance_id):
    wait_until(
        lambda: (editor._flow.selection or {}).get("nodes") == [instance_id],
        page,
        timeout=TIMEOUT,
    )
    wait_until(lambda: editor._inspector._shown == ("node", instance_id), page, timeout=TIMEOUT)
    expect(page.locator(".fd-inspector-header")).to_be_visible()
    expect(_node(page, instance_id)).to_have_class(re.compile(r"\bselected\b"))


def test_the_inspector_keeps_the_selection_across_tab_switches(page):
    editor, (map_id, chart_id) = _editor()
    page.set_viewport_size(WIDE)
    serve_component(page, editor)
    _node(page, map_id).click()
    _shows_node(page, editor, map_id)

    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=TIMEOUT)
    expect(page.locator(".gutter")).to_be_visible()
    _shows_node(page, editor, map_id)
    expect(_item(page, editor, map_id)).to_have_class(re.compile("muuri-selected"))

    # Layout shows no canvas, and the selection is kept on its grid.
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=TIMEOUT)
    expect(_item(page, editor, map_id)).to_have_class(re.compile("muuri-selected"))
    page.wait_for_timeout(500)
    assert editor._flow.selection["nodes"] == [map_id]

    page.get_by_role("button", name="Dataflow").click()
    wait_until(lambda: editor.mode == "wiring", page, timeout=TIMEOUT)
    _shows_node(page, editor, map_id)

    # The canvas still reports the selection as it changes.
    _node(page, chart_id).click()
    _shows_node(page, editor, chart_id)
    page.locator(".react-flow__pane").click(position={"x": 20, "y": 20})
    wait_until(lambda: editor._flow.selection["nodes"] == [], page, timeout=TIMEOUT)
    wait_until(lambda: editor._inspector._shown is None, page, timeout=TIMEOUT)


def test_the_inspector_keeps_the_selection_as_the_code_panel_moves(page):
    editor, query = make_editor()
    page.set_viewport_size({"width": 1900, "height": 900})
    serve_component(page, editor)
    _node(page, query).click()
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    expect(page.locator(".fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    _shows_node(page, editor, query)

    toggle = page.locator(".fd-code-dock-toggle button")
    toggle.click()
    wait_until(lambda: editor.code_panel.dock == "right", page, timeout=TIMEOUT)
    expect(page.locator(".fd-code-side .fd-code-panel")).to_be_visible()
    _shows_node(page, editor, query)

    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=TIMEOUT)
    _shows_node(page, editor, query)
    page.get_by_role("button", name="Dataflow").click()
    wait_until(lambda: editor.mode == "wiring", page, timeout=TIMEOUT)
    _shows_node(page, editor, query)

    page.locator(".fd-code-dock-toggle button").click()
    wait_until(lambda: editor.code_panel.dock == "bottom", page, timeout=TIMEOUT)
    _shows_node(page, editor, query)


def _serve_code_app(page, apps):
    store, (dash,) = _store_with("Plant")
    model = store._load_any(dash)
    model.items = [
        DashboardItem(instance_id="src", component_id="D/turbines", x=0, y=0),
        DashboardItem(instance_id="q", component_id="D/query", x=0, y=4),
    ]
    model.edges = [DashboardEdge("src", "data", "q", "data")]
    store.save_dashboard(model)

    def factory():
        app = FlowDashApp(
            registry=normalize_components(CODE_COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    routes = {"/": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=f"/dash/{dash}?edit=true")
    page.get_by_role("button", name="Done").wait_for()


def _still_editing(page, editor):
    page.wait_for_timeout(500)
    assert "edit=true" in page.url and editor.editable


def test_escape_closes_the_innermost_thing_before_leaving_edit_mode(page):
    page.set_viewport_size(WIDE)
    apps = []
    _serve_code_app(page, apps)
    editor = apps[0]._editor
    page.get_by_role("button", name="Dataflow").click()
    wait_until(lambda: editor.mode == "wiring", page, timeout=TIMEOUT)

    # An open menu first.
    page.get_by_role("button", name="More actions").click()
    expect(page.get_by_role("menu")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("menu")).to_have_count(0)
    _still_editing(page, editor)

    # Then focus in the code panel.
    _node(page, "q").click()
    page.locator(".fd-edit-spec").first.click(timeout=TIMEOUT)
    expect(page.locator(".fd-code-panel")).to_be_visible(timeout=TIMEOUT)
    page.locator(".fd-code-dock-toggle button").focus()
    page.keyboard.press("Escape")
    _still_editing(page, editor)
    assert editor._flow.selection["nodes"] == ["q"]

    # Then the selection.
    page.keyboard.press("Escape")
    wait_until(lambda: editor._flow.selection["nodes"] == [], page, timeout=TIMEOUT)
    _still_editing(page, editor)

    # A selection kept in the Layout tab, where no canvas is shown, comes before edit mode too.
    page.get_by_role("button", name="Dataflow").click()
    _node(page, "q").click()
    wait_until(lambda: editor._flow.selection["nodes"] == ["q"], page, timeout=TIMEOUT)
    page.get_by_role("button", name="Layout").click()
    wait_until(lambda: editor.mode == "dashboard", page, timeout=TIMEOUT)
    page.wait_for_timeout(500)
    page.keyboard.press("Escape")
    wait_until(lambda: editor._flow.selection["nodes"] == [], page, timeout=TIMEOUT)
    wait_until(lambda: editor._selected_tile_id() is None, page, timeout=TIMEOUT)
    _still_editing(page, editor)

    # With nothing open, Escape leaves edit mode.
    page.keyboard.press("Escape")
    wait_until(lambda: "edit=true" not in page.url, page, timeout=TIMEOUT)
    assert not editor.editable
