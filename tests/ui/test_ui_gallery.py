"""UI tests for the component gallery at /components."""

from urllib.parse import urlparse

import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(
    page=False,
    component=True,
    title="Turbines",
    description="Loads the turbine table.",
    provides=[{"key": "table", "type": "DataFrame"}],
)
def source(config):
    return "source tile"


@register(
    page=False,
    component=True,
    title="State Filter",
    description="Keeps one state.",
    tags=["geo"],
    requires=[{"key": "table", "type": "DataFrame", "label": "The turbines"}],
    provides=[{"key": "filtered", "type": "DataFrame"}],
)
def state_filter(config):
    return "filter tile"


@register(
    page=False,
    component=True,
    title="Capacity Chart",
    requires=[{"key": "filtered", "type": "DataFrame"}],
)
def chart(config):
    return "chart tile"


@register(page=False, component=True, title="Ledger", allow_groups=["finance"])
def ledger(config):
    return "ledger tile"


COMPONENTS = {
    "Test/source": source,
    "Test/filter": state_filter,
    "Test/chart": chart,
    "Test/ledger": ledger,
}


def _path(page):
    return urlparse(page.url).path


def _card(page, title):
    return page.locator(".flowdash-gallery-card").filter(has=page.get_by_text(title, exact=True))


def _serve(page, store=None, apps=None):
    store = store or MemoryDashboardStore()

    def factory():
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            include_builtin_components=False,
        )
        if apps is not None:
            apps.append(app)
        return app

    routes = {"/": factory, "/components": factory, "/dash/[^/]+": factory}
    serve_component(page, routes, suffix="/components")
    page.get_by_text("Building blocks for dashboards").wait_for()
    return store


def _seed(store, title, components):
    model = store.create_dashboard(resolve_identity().user, title)
    model.items = [
        DashboardItem(instance_id=f"n{i}", component_id=cid) for i, cid in enumerate(components)
    ]
    store.save_dashboard(model)
    return model


def test_cards_hide_restricted_components(page):
    _serve(page)

    expect(page.locator(".flowdash-gallery-card")).to_have_count(3)
    expect(_card(page, "State Filter")).to_contain_text("Transform")
    expect(_card(page, "State Filter")).to_contain_text("in: The turbines")
    expect(_card(page, "State Filter")).to_contain_text("out: filtered")
    expect(_card(page, "State Filter")).to_contain_text("Not used yet")
    expect(page.get_by_text("Ledger")).to_have_count(0)


def test_role_chips_search_and_accepts_filter(page):
    _serve(page)

    expect(page.locator(".role-all")).to_contain_text("All 3")
    page.locator(".role-view").click()
    expect(_card(page, "Capacity Chart")).to_be_visible()
    expect(_card(page, "Turbines")).to_be_hidden()
    expect(_card(page, "State Filter")).to_be_hidden()

    page.locator(".role-all").click()
    page.get_by_placeholder("Search by name, description, tag or port").fill("geo")
    expect(_card(page, "State Filter")).to_be_visible()
    expect(_card(page, "Turbines")).to_be_hidden()
    expect(page.locator(".role-all")).to_contain_text("All 1")

    page.get_by_placeholder("Search by name, description, tag or port").fill("")
    page.get_by_role("button", name="Accepts").click()
    page.get_by_role("menuitem", name="DataFrame").click()
    expect(page.get_by_role("button", name="Accepts DataFrame")).to_be_visible()
    expect(_card(page, "State Filter")).to_be_visible()
    expect(_card(page, "Capacity Chart")).to_be_visible()
    expect(_card(page, "Turbines")).to_be_hidden()

    page.get_by_placeholder("Search by name, description, tag or port").fill("nothing like this")
    expect(page.get_by_text("No components match.")).to_be_visible()


def test_detail_panel_shows_ports_and_usage(page):
    store = MemoryDashboardStore()
    _seed(store, "Plant", ["Test/filter"])
    _serve(page, store)

    detail = page.locator(".flowdash-gallery-detail")
    expect(detail).to_be_hidden()
    _card(page, "State Filter").click()
    expect(detail).to_be_visible()
    expect(detail).to_contain_text("Keeps one state.")
    expect(detail).to_contain_text("The turbines")
    expect(detail).to_contain_text("Required")
    expect(detail).to_contain_text("Used in")
    expect(detail.get_by_text("Plant")).to_be_visible()
    expect(page.get_by_role("button", name="Open as page")).to_be_hidden()

    page.get_by_role("button", name="Close details").click()
    expect(detail).to_be_hidden()


def test_used_in_link_opens_the_dashboard(page):
    store = MemoryDashboardStore()
    model = _seed(store, "Plant", ["Test/filter"])
    apps = []
    _serve(page, store, apps)

    _card(page, "State Filter").click()
    page.locator(".flowdash-gallery-detail").get_by_text("Plant").click()
    wait_until(lambda: _path(page) == f"/dash/{model.dashboard_id}", page, timeout=10000)
    wait_until(lambda: apps[0]._current_dashboard is not None, page, timeout=10000)
    # Checked on the server: slow runners can take long to repaint the page.
    assert apps[0]._current_dashboard.dashboard_id == model.dashboard_id


def test_add_to_dashboard_places_the_component(page):
    store = MemoryDashboardStore()
    model = _seed(store, "Plant", ["Test/source"])
    apps = []
    _serve(page, store, apps)

    _card(page, "State Filter").click()
    page.get_by_role("button", name="Add to dashboard").click()
    page.get_by_role("menuitem", name="Plant").click()

    wait_until(lambda: _path(page) == f"/dash/{model.dashboard_id}", page, timeout=10000)
    wait_until(lambda: page.url.endswith("?edit=true"), page, timeout=10000)
    page.get_by_role("button", name="Done").wait_for()
    editor = apps[0]._editor
    wait_until(
        lambda: {i["component_id"] for i in editor._tile_items} == {"Test/source", "Test/filter"},
        page,
        timeout=10000,
    )
    assert editor.dirty


def test_add_to_a_new_dashboard(page):
    apps = []
    _serve(page, apps=apps)

    _card(page, "Turbines").click()
    page.get_by_role("button", name="Add to dashboard").click()
    page.get_by_role("menuitem", name="New dashboard").click()

    wait_until(lambda: _path(page).startswith("/dash/"), page, timeout=10000)
    page.get_by_role("button", name="Done").wait_for()
    app = apps[0]
    wait_until(lambda: app._current_dashboard is not None, page, timeout=10000)
    assert app._current_dashboard.title == "Turbines dashboard"
    assert [i["component_id"] for i in app._editor._tile_items] == ["Test/source"]


def test_detail_panel_covers_the_page_on_narrow_screens(page):
    page.set_viewport_size({"width": 400, "height": 800})
    _serve(page)

    _card(page, "Turbines").click()
    detail = page.locator(".flowdash-gallery-detail")
    expect(detail).to_be_visible()
    box = detail.bounding_box()
    assert box["x"] == 0
    assert box["width"] == 400
    page.get_by_role("button", name="Close details").click()
    expect(detail).to_be_hidden()
