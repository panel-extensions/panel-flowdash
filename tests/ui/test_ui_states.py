"""UI tests for the empty, error and system states."""

from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import Permission, resolve_identity
from panel_flowdash.command.serve import _NotFoundHandler
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardModel

from .util import DASHBOARD_LOAD_TIMEOUT, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Feed Source", provides=["rows"])
def feed(config):
    return "feed tile"


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/feed": feed, "Test/note": note}


def _path(page):
    return urlparse(page.url).path


def _store():
    store = MemoryDashboardStore()
    user = resolve_identity().user
    wind = store.create_dashboard(user, "US Wind Turbines")
    wind.items = [DashboardItem(instance_id="n1", component_id="Test/note", x=0, y=0)]
    store.save_dashboard(wind)
    empty = store.create_dashboard(user, "Texas Repowering")
    private = DashboardModel(dashboard_id="f1ee7f1a2c90", user_id="maya", title="Fleet Budget")
    private.permission = Permission(allow_users=frozenset({"maya"}))
    store.save_dashboard(private)
    return store, wind.dashboard_id, empty.dashboard_id, private.dashboard_id


def _serve_app(page, store, suffix, apps=None, **kwargs):
    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )
        if apps is not None:
            apps.append(app)
        return app

    routes = {"/": factory, "/components": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix=suffix, **kwargs)


def test_denied_dashboard_is_not_reported_as_missing(page):
    store, _, _, private = _store()
    _serve_app(page, store, f"/dash/{private}")

    # The app looks the dashboard up as it loads one, after the page first renders.
    expect(page.get_by_text("You don't have access to this dashboard")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    expect(page.get_by_text("Ask the owner, maya, to add you.", exact=False)).to_be_visible()
    expect(page.get_by_text("Fleet Budget")).to_have_count(0)
    page.get_by_role("button", name="Go home").click()
    wait_until(lambda: _path(page) == "/", page, timeout=10000)


def test_missing_dashboard_suggests_a_similar_one(page):
    store, wind, _, _ = _store()
    typo = wind[:-1] + ("0" if wind[-1] != "0" else "1")
    _serve_app(page, store, f"/dash/{typo}")

    expect(page.get_by_text("This dashboard does not exist")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    expect(page.get_by_text("Similar dashboards")).to_be_visible()
    page.get_by_text("US Wind Turbines").click()
    wait_until(lambda: _path(page) == f"/dash/{wind}", page, timeout=10000)
    page.get_by_text("note tile").wait_for()


def test_unknown_url_renders_the_in_app_404(page):
    store, *_ = _store()
    _serve_app(page, store, "/reports/missing-page", default_handler_class=_NotFoundHandler)

    expect(page.get_by_text("This page does not exist")).to_be_visible()
    expect(page.get_by_text("Nothing is at /reports/missing-page.", exact=False)).to_be_visible()
    page.get_by_role("button", name="Go home").click()
    wait_until(lambda: _path(page) == "/", page, timeout=10000)


def test_empty_canvas_adds_the_first_data_source(page):
    store, _, empty, _ = _store()
    apps = []
    _serve_app(page, store, f"/dash/{empty}?edit=true", apps)

    expect(page.get_by_text("Start with a data source")).to_be_visible()
    page.get_by_role("button", name="Add Feed Source").click()
    wait_until(
        lambda: [i["component_id"] for i in apps[0]._editor._tile_items] == ["Test/feed"],
        page,
        timeout=10000,
    )
    expect(page.get_by_text("Start with a data source")).to_have_count(0)


def test_done_lists_unsaved_changes_and_saves_and_leaves(page):
    store, wind, _, _ = _store()
    apps = []
    _serve_app(page, store, f"/dash/{wind}?edit=true", apps)

    page.get_by_role("button", name="Done").wait_for()
    apps[0]._editor.add_component("Test/feed")
    expect(page.get_by_text("Unsaved changes")).to_be_visible()

    page.get_by_role("button", name="Done").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Save changes before leaving?")).to_be_visible()
    expect(dialog.get_by_text("Added Feed Source")).to_be_visible()

    dialog.get_by_role("button", name="Keep editing").click()
    expect(dialog).to_have_count(0)
    assert apps[0]._editor.editable

    page.get_by_role("button", name="Done").click()
    page.get_by_role("dialog").get_by_role("button", name="Save and leave").click()
    wait_until(lambda: "edit=true" not in page.url, page, timeout=10000)
    stored = store._load_any(wind)
    assert sorted(item.component_id for item in stored.items) == ["Test/feed", "Test/note"]
