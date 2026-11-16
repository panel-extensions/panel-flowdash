"""UI tests for the launcher: dashboard cards, their actions menu, filters and empty state."""

import json
import re

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, DashboardModel, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, resolve_identity
from panel_flowdash.component_library import normalize_components
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.editor import export_model

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# Opening a dialog or menu after the page loaded can reach Bokeh as a model it
# already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Gauge", icon="speed")
def gauge(config):
    return "gauge"


def _user():
    return resolve_identity(AuthConfig()).user


def _serve(page, tmp_path, dashboards=()):
    """Serve the app's launcher over a store holding *dashboards* (title, owner) pairs.

    The dashboards are stored as last edited one minute apart, the first most recently.
    """
    store = DashboardStore(tmp_path / "launcher.db")
    for age, (title, owner) in enumerate(dashboards):
        model = store.create_dashboard(owner or _user(), title)
        model.items = [DashboardItem(instance_id="g1", component_id="Test/gauge")]
        store.save_dashboard(model)
        with store._get_conn() as conn:
            conn.execute(
                "UPDATE dashboards SET updated_at = datetime('now', ?) WHERE dashboard_id = ?",
                (f"-{age} minutes", model.dashboard_id),
            )
    routes = FlowDashApp.build_routes(
        tmp_path,
        registry=normalize_components({"Test/gauge": gauge}),
        store=store,
        notifications=False,
    )
    serve_component(page, routes)
    return store


def _cards(page):
    return page.locator(".flowdash-dashboard-card")


def _open_menu(page, title):
    page.get_by_role("button", name=f"More actions for {title}").click()
    return page.get_by_role("menu")


def test_card_menu_lists_actions(page, tmp_path):
    _serve(page, tmp_path, [("Sales", None)])

    menu = _open_menu(page, "Sales")

    # Each item's text starts with its icon's ligature name.
    labels = ["Open", "Edit", "Rename", "Duplicate", "Share", "Download as file", "Delete"]
    expect(menu.get_by_role("menuitem")).to_have_text(
        [re.compile(f"{label}$") for label in labels]
    )
    expect(menu.get_by_role("separator")).to_have_count(1)
    color = menu.get_by_role("menuitem", name="Delete").evaluate("e => getComputedStyle(e).color")
    open_color = menu.get_by_role("menuitem", name="Open").evaluate(
        "e => getComputedStyle(e).color"
    )
    assert color != open_color


def test_duplicate_adds_a_card(page, tmp_path):
    store = _serve(page, tmp_path, [("Sales", None)])

    _open_menu(page, "Sales").get_by_role("menuitem", name="Duplicate").click()

    expect(_cards(page)).to_have_count(2)
    expect(_cards(page).get_by_text("Sales copy", exact=True)).to_be_visible()
    wait_until(lambda: "Sales copy" in [m.title for m in store.list_dashboards(_user())], page)


def test_download_saves_the_dashboard_file(page, tmp_path):
    store = _serve(page, tmp_path, [("Sales", None)])
    (model,) = store.list_dashboards(_user())

    for _ in range(2):
        with page.expect_download() as info:
            _open_menu(page, "Sales").get_by_role("menuitem", name="Download as file").click()
        download = info.value
        assert download.suggested_filename == "Sales.json"
        with open(download.path(), encoding="utf-8") as f:
            assert json.load(f) == export_model(model)


def test_rename_opens_rename_dialog(page, tmp_path):
    _serve(page, tmp_path, [("Sales", None)])

    _open_menu(page, "Sales").get_by_role("menuitem", name="Rename").click()

    expect(page.get_by_role("dialog")).to_be_visible()


def test_card_opens_dashboard(page, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    store = _serve(page, tmp_path, [("Sales", None)])
    (model,) = store.list_dashboards(_user())

    _cards(page).get_by_text("Sales", exact=True).click()

    wait_until(lambda: page.url.endswith(f"/dash/{model.dashboard_id}"), page)
    expect(_cards(page)).to_have_count(0)
    expect(page.get_by_text("gauge", exact=True)).to_be_visible()
    # The breadcrumb switcher is created by the click and renamed by the load.
    expect(page.locator(".header").get_by_role("button", name="Sales")).to_be_visible()
    assert errors == []


def test_filters_and_sort(page, tmp_path):
    _serve(page, tmp_path, [("beta", None), ("Alpha", "maya"), ("gamma", None)])
    titles = page.locator(".flowdash-dashboard-card .title")

    expect(page.get_by_role("button", name="All 3")).to_be_visible()
    expect(titles).to_have_text(["beta", "Alpha", "gamma"])

    page.get_by_role("button", name="Last edited").click()
    page.get_by_role("menuitem", name="Name").click()
    expect(titles).to_have_text(["Alpha", "beta", "gamma"])

    page.get_by_role("button", name="Owned by me").click()
    expect(titles).to_have_text(["beta", "gamma"])

    page.get_by_role("button", name="Shared with me").click()
    expect(titles).to_have_text(["Alpha"])
    expect(page.get_by_text("Edited 1 minute ago by maya")).to_be_visible()


def test_list_view(page, tmp_path):
    _serve(page, tmp_path, [("Sales", None)])

    page.get_by_role("button", name="view list").click()

    expect(page.locator(".flowdash-dashboard-card .card.row")).to_have_count(1)
    _open_menu(page, "Sales")
    expect(page.get_by_role("menuitem", name="Open")).to_be_visible()


def test_long_title_does_not_reach_the_menu(page, tmp_path):
    title = "Fleet Repowering Candidates Across Texas and Oklahoma Wind Farms"
    _serve(page, tmp_path, [(title, None)])

    title_box = page.locator(".flowdash-dashboard-card .title").bounding_box()
    menu_box = page.get_by_role("button", name=f"More actions for {title}").bounding_box()

    assert title_box["x"] + title_box["width"] <= menu_box["x"]


def test_empty_state(page, tmp_path):
    _serve(page, tmp_path)

    expect(page.get_by_text("Build your first dashboard")).to_be_visible()
    expect(page.get_by_role("button", name=re.compile(r"^Browse \d+ components$"))).to_be_visible()

    page.get_by_role("button", name="New dashboard").click()

    expect(page.get_by_role("dialog")).to_be_visible()


def test_import_a_file(page, tmp_path):
    store = _serve(page, tmp_path)
    model = DashboardModel(
        dashboard_id="x",
        user_id="someone",
        title="Imported",
        items=[DashboardItem(instance_id="g1", component_id="Test/gauge")],
    )
    content = json.dumps(export_model(model)).encode()

    page.locator(".flowdash-import input[type=file]").set_input_files(
        {"name": "imported.json", "mimeType": "application/json", "buffer": content}
    )

    wait_until(lambda: [m.title for m in store.list_dashboards(_user())] == ["Imported"], page)
    (stored,) = store.list_dashboards(_user())
    wait_until(lambda: page.url.endswith(f"/dash/{stored.dashboard_id}"), page)
    expect(_cards(page)).to_have_count(0)
