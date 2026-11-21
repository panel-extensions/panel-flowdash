"""UI tests: accessible names and states in the top and status bars, dialogs that Escape
dismisses, the canvas selection ring, and collapsing cards as view state."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import DASHBOARD_LOAD_TIMEOUT, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The app re-renders the editor as it loads a dashboard, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note", description="A short note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/note": note}


def _serve(page, width=1440):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id="n1", component_id="Test/note", x=0, y=0),
        DashboardItem(instance_id="n2", component_id="Test/note", x=300, y=0),
    ]
    store.save_dashboard(model)
    apps = []

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        apps.append(app)
        return app

    page.set_viewport_size({"width": width, "height": 900})
    routes = {"/": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=f"/dash/{model.dashboard_id}?edit=true")
    expect(page.get_by_role("button", name="Done")).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    return apps[0], store, model.dashboard_id


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _selected(editor):
    return [n["id"] for n in editor._flow.nodes if n.get("selected")]


def test_collapsing_a_card_neither_selects_it_nor_marks_changes(page):
    app, store, dashboard_id = _serve(page)
    editor = app._editor
    _node(page, "n1").locator('[data-fd-action="toggle_collapse"]').click()
    wait_until(lambda: editor._node_cards()["n1"].collapsed, page, timeout=8000)
    expect(page.get_by_text("All changes saved")).to_be_visible()
    assert _selected(editor) == []
    assert not app._has_unsaved_changes()
    # Kept with the stored dashboard all the same.
    stored = {item.instance_id: item for item in store._load_any(dashboard_id).items}
    assert stored["n1"].collapsed and not stored["n2"].collapsed


def test_the_selection_ring_differs_from_the_agent_outline(page):
    app, *_ = _serve(page)
    editor = app._editor
    node = _node(page, "n1")
    node.click(position={"x": 60, "y": 20})
    wait_until(lambda: _selected(editor) == ["n1"], page, timeout=8000)
    # A clicked node has the focus, whose outline React Flow removes.
    style = "el => [getComputedStyle(el).outlineStyle, el.matches(':focus')]"
    wait_until(lambda: node.evaluate(style) == ["dashed", True], page, timeout=8000)


def test_the_collapse_all_toggle_keeps_its_name(page):
    app, *_ = _serve(page)
    collapse = page.locator(".fd-status-bar .MuiToggleButton-root", has_text="Collapse all")
    expect(collapse).to_have_attribute("aria-label", "Collapse all nodes (C)")
    collapse.click()
    wait_until(lambda: all(c.collapsed for c in app._editor._node_cards().values()), page)
    expect(collapse).to_have_attribute("aria-pressed", "true")
    expect(collapse).to_have_attribute("aria-label", "Collapse all nodes (C)")


def test_hidden_key_hints_leave_no_gap(page):
    _serve(page, width=760)
    hints = page.locator(".fd-keys")
    host = "el => getComputedStyle(el.getRootNode().host).display"
    wait_until(lambda: hints.evaluate(host) == "none", page, timeout=8000)


def test_more_actions_says_it_opens_a_menu(page):
    _serve(page)
    button = page.get_by_role("button", name="More actions")
    expect(button).to_have_attribute("aria-haspopup", "menu")
    expect(button).to_have_attribute("aria-expanded", "false")
    button.click()
    menu = page.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(button).to_have_attribute("aria-expanded", "true")
    expect(button).to_have_attribute("aria-controls", menu.get_attribute("id"))
    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    expect(button).to_have_attribute("aria-expanded", "false")


def test_escape_dismisses_the_clear_canvas_dialog(page):
    app, *_ = _serve(page)
    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Clear canvas").click()
    dialog = page.get_by_role("dialog").filter(has_text="Remove every component")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert not app._editor._clear_dialog.open
    assert len(app._editor._tile_items) == 2


def test_escape_dismisses_the_import_dialog_and_drops_the_file(page):
    app, *_ = _serve(page)
    editor = app._editor
    editor._pending_import = editor.to_model()
    editor._import_dialog.open = True
    dialog = page.get_by_role("dialog").filter(has_text="Load dashboard file")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    wait_until(lambda: editor._pending_import is None, page)


def test_escape_on_the_unsaved_changes_dialog_keeps_editing(page):
    app, *_ = _serve(page)
    app._editor.add_component("Test/note")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()
    left = []
    app._confirm_unsaved(lambda: left.append(True))
    dialog = page.get_by_role("dialog").filter(has_text="Save changes before leaving?")
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    wait_until(lambda: app._pending_action is None, page)
    assert not left
    assert app._has_unsaved_changes()


@pytest.mark.parametrize("width", [960, 1023, 1440])
def test_done_keeps_its_label_when_there_is_room(page, width):
    _serve(page, width=width)
    done = page.get_by_role("button", name="Done")
    expect(done).to_have_attribute("aria-label", "Done")
    font = "el => parseFloat(getComputedStyle(el).fontSize)"
    wait_until(lambda: done.evaluate(font) > 0, page, timeout=8000)


def test_done_is_named_when_it_shows_only_its_icon(page):
    _serve(page, width=860)
    done = page.get_by_role("button", name="Done")
    font = "el => parseFloat(getComputedStyle(el).fontSize)"
    wait_until(lambda: done.evaluate(font) == 0, page, timeout=8000)
    expect(done).to_have_attribute("aria-label", "Done")
