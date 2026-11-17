"""UI tests for the launcher's create, rename and delete dashboard dialogs."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardEdge, DashboardItem, MemoryDashboardStore, register

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# A click on a dialog that opened after the page loaded can reach Bokeh as a
# model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note"


def _serve_launcher(page):
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.component_library import normalize_components

    store = MemoryDashboardStore()

    def app():
        app = FlowDashApp(
            registry=normalize_components({"Test/note": note}), store=store, notifications=False
        )
        if not store.list_dashboards(app._user_id):
            model = store.create_dashboard(app._user_id, "Sales")
            model.items = [
                DashboardItem(instance_id="a", component_id="Test/note"),
                DashboardItem(instance_id="b", component_id="Test/note"),
            ]
            model.edges = [DashboardEdge("a", "value", "b", "value")]
            store.save_dashboard(model)
        return app

    serve_component(page, app)
    return store


def _card_action(page, action):
    page.locator(".flowdash-card-menu button").last.click()
    page.get_by_role("menuitem", name=action).last.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    return dialog


def _titles(store):
    return sorted(m.title for m in store._all_dashboards())


def test_create_validates_after_typing_and_submits_on_enter(page):
    store = _serve_launcher(page)

    page.get_by_text("New Dashboard").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Create dashboard")).to_be_visible()
    create = dialog.get_by_role("button", name="Create", exact=True)
    expect(create).to_be_disabled()
    expect(dialog.get_by_text("Name cannot be empty.")).to_have_count(0)

    name = dialog.get_by_label("Name", exact=True)
    name.fill("Sales")
    expect(dialog.get_by_text("A dashboard with this name already exists.")).to_be_visible()
    expect(create).to_be_disabled()

    name.fill("Fresh")
    expect(create).to_be_enabled()
    name.press("Enter")

    wait_until(lambda: _titles(store) == ["Fresh", "Sales"], page)
    # The dialog closes in the same update that mounts the editor, which on a
    # loaded Windows runner takes longer than the default five seconds.
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=15000)


def test_rename_accepts_the_unchanged_name(page):
    store = _serve_launcher(page)

    dialog = _card_action(page, "Rename")
    expect(dialog.get_by_text("Rename dashboard")).to_be_visible()
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("Sales")
    expect(dialog.get_by_text("A dashboard with this name already exists.")).to_have_count(0)

    cancel = dialog.get_by_role("button", name="Cancel")
    rename = dialog.get_by_role("button", name="Rename", exact=True)
    expect(rename).to_be_enabled()
    assert cancel.bounding_box()["x"] < rename.bounding_box()["x"]

    dialog.get_by_label("Name", exact=True).fill("Revenue")
    rename.click()

    expect(page.get_by_role("dialog")).to_have_count(0)
    wait_until(lambda: _titles(store) == ["Revenue"], page)


def test_delete_requires_typing_the_name(page):
    store = _serve_launcher(page)

    dialog = _card_action(page, "Delete")
    expect(dialog.get_by_text("Delete Sales?")).to_be_visible()
    expect(
        dialog.get_by_text(
            "Its 2 components and 1 connection are removed for everyone it is shared with. "
            "This can't be undone."
        )
    ).to_be_visible()
    delete = dialog.get_by_role("button", name="Delete dashboard")
    expect(delete).to_be_disabled()

    confirm = dialog.get_by_label("Type the name to confirm")
    confirm.fill("sales")
    expect(delete).to_be_disabled()
    confirm.fill("Sales")
    expect(delete).to_be_enabled()
    delete.click()

    expect(page.get_by_role("dialog")).to_have_count(0)
    wait_until(lambda: _titles(store) == [], page)
