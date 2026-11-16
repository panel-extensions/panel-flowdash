"""UI tests for saving a dashboard that another session saved in the meantime."""

import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash import DashboardItem, DashboardModel, MemoryDashboardStore, register
from panel_flowdash.editor import FlowDash

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# A click on a dialog that opened after the page loaded can reach Bokeh as a
# model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Note")
def note(config):
    return "note"


def _serve_conflict(page):
    """Serve an edited dashboard that another session saved after it was opened."""
    store = MemoryDashboardStore(
        {
            "shared": DashboardModel(
                dashboard_id="shared",
                user_id="alice",
                title="Shared",
                items=[DashboardItem(instance_id="first", component_id="Test/note")],
            )
        }
    )
    editors = []

    def app():
        editor = FlowDash(
            {"Test/note": note},
            notifications=False,
            store=store,
            user="alice",
            dashboard="shared",
            mode="dashboard",
        )
        editor.add_component("Test/note")
        other = store.load_dashboard("alice", "shared")
        other.items = []
        store.save_dashboard(other)
        editors.append(editor)
        return editor

    serve_component(page, app)

    page.get_by_role("button", name="Save", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Someone else saved this dashboard")).to_be_visible()
    return store, editors[0], dialog


def test_save_as_copy_after_conflict(page):
    store, editor, dialog = _serve_conflict(page)

    dialog.get_by_role("button", name="Save as copy").click()

    expect(page.get_by_role("dialog")).to_have_count(0)
    wait_until(lambda: editor.dashboard.title == "Shared (copy)", page)
    copy = store.load_dashboard("alice", editor.dashboard.dashboard_id)
    assert len(copy.items) == 2
    assert store.load_dashboard("alice", "shared").items == []


def test_reload_after_conflict(page):
    store, editor, dialog = _serve_conflict(page)

    dialog.get_by_role("button", name="Reload").click()

    expect(page.get_by_role("dialog")).to_have_count(0)
    wait_until(lambda: editor.dashboard.revision == 2 and not editor._tile_items, page)
    assert not editor.dirty
    assert [m.title for m in store.list_dashboards("alice")] == ["Shared"]
