"""UI tests for the combined dashboard settings and sharing dialog."""

import typing as t

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import DashboardItem, MemoryDashboardStore, Permission, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import select_option, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# A click on a dialog that opened after the page loaded can reach Bokeh as a
# model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Meter")
class Meter(Viewer):
    reading = param.Integer(default=3)

    def __panel__(self):
        return "meter tile"


REGISTRY = normalize_components({"Test/meter": Meter})


class RecordingApp(FlowDashApp):
    instances: t.ClassVar[list] = []

    def __init__(self, **params):
        super().__init__(**params)
        RecordingApp.instances.append(self)


AUTH = AuthConfig(user_groups={"erin": ["sales"], "frank": ["sales"]})


def _serve_edit(page, tmp_path, store, dashboard, suffix=None):
    """Serve the app's routes, opened on *dashboard* in edit mode; return the apps created."""
    RecordingApp.instances = []
    routes = RecordingApp.build_routes(
        tmp_path, registry=REGISTRY, store=store, auth_config=AUTH, notifications=False
    )
    if suffix is None:
        suffix = f"/dash/{dashboard.dashboard_id}?edit=true"
    serve_component(page, routes, suffix=suffix)
    return RecordingApp.instances


def _app(store, **params):
    return FlowDashApp(
        registry=REGISTRY,
        store=store,
        auth_config=AUTH,
        notifications=False,
        **params,
    )


def _role(dialog, name):
    return dialog.locator(".share-entry").filter(has_text=name).get_by_role("combobox")


def test_roles_and_the_everyone_switch_map_to_the_permission(page):
    store = MemoryDashboardStore()

    def app():
        app = _app(store)
        dashboard = store.create_dashboard(app._user_id, "Plant")
        store.set_permission(
            dashboard.dashboard_id, Permission.from_spec(allow_edit_users=["bob"])
        )
        app._open_share_dialog(dashboard.dashboard_id, "Plant")
        return app._share_dialog

    serve_component(page, app)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Plant, owned by you")).to_be_visible()
    expect(_role(dialog, "bob")).to_have_text("Can edit")

    add = dialog.get_by_role("combobox", name="Add people or groups")
    add.fill("sales")
    add.press("Enter")
    expect(_role(dialog, "sales")).to_have_text("Can view")
    expect(dialog.get_by_text("Group, 2 members")).to_be_visible()

    _role(dialog, "bob").click()
    select_option(page, "Blocked").click()
    expect(dialog.get_by_text("always blocked, even if listed above")).to_be_visible()

    dialog.get_by_role("switch").click()
    dialog.get_by_role("button", name="Save settings").click()

    (dashboard,) = store._all_dashboards()
    wait_until(
        lambda: (
            store._load_any(dashboard.dashboard_id).permission
            == Permission.from_spec(allow_groups=["sales"], deny_users=["bob"])
        ),
        page,
    )
    expect(dialog).to_be_hidden()


def test_share_and_settings_open_the_same_dialog_on_their_sections(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard(resolve_identity(AUTH).user, "Plant")
    _serve_edit(page, tmp_path, store, dashboard, suffix=f"/dash/{dashboard.dashboard_id}")

    dialog = page.get_by_role("dialog")
    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Settings").click()
    expect(dialog.get_by_label("Name")).to_have_value("Plant")
    expect(dialog.get_by_label("Link")).to_be_hidden()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()

    # Sharing is an edit action.
    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name="Share").click()
    expect(dialog.get_by_label("Link")).to_have_value(
        f"{page.url.split('/dash/')[0]}/dash/{dashboard.dashboard_id}"
    )
    expect(dialog.get_by_role("button", name="Copy link")).to_be_visible()
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden()

    page.get_by_role("button", name="Settings").click()
    expect(dialog.get_by_label("Name")).to_have_value("Plant")
    expect(dialog.get_by_label("Link")).to_be_hidden()


def test_launcher_share_opens_the_sharing_section(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard(resolve_identity(AUTH).user, "Plant")
    _serve_edit(page, tmp_path, store, dashboard, suffix="/")

    page.get_by_role("button", name="More actions for Plant").click()
    page.get_by_role("menuitem", name="Share").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Plant, owned by you")).to_be_visible()
    expect(dialog.get_by_label("Link")).to_have_value(
        f"{page.url.rstrip('/')}/dash/{dashboard.dashboard_id}"
    )


def test_tile_switches_change_the_tile_settings(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard(resolve_identity(AUTH).user, "Plant")
    dashboard.items = [DashboardItem(instance_id="m1", component_id="Test/meter")]
    store.save_dashboard(dashboard)
    _serve_edit(page, tmp_path, store, dashboard)

    page.get_by_role("button", name="Settings").click()
    dialog = page.get_by_role("dialog")
    titles = dialog.get_by_role("switch", name="Show tile titles")
    expect(titles).to_be_enabled()
    dialog.get_by_role("switch", name="Frame tiles").click()
    expect(titles).to_be_disabled()
    dialog.get_by_role("button", name="Save settings").click()

    wait_until(
        lambda: store._load_any(dashboard.dashboard_id).settings.get("tile_cards") is False,
        page,
    )


def test_refresh_presets_set_the_period_and_save_it(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard(resolve_identity(AUTH).user, "Plant")
    dashboard.items = [DashboardItem(instance_id="m1", component_id="Test/meter")]
    store.save_dashboard(dashboard)
    apps = _serve_edit(page, tmp_path, store, dashboard)

    page.get_by_role("button", name="Settings").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_text("Refresh", exact=True).first.click()
    dialog.get_by_role("button", name="5 min", exact=True).click()
    expect(dialog.get_by_text("The new schedule starts when you save.")).to_be_visible()
    dialog.get_by_role("button", name="Save settings").click()

    wait_until(
        lambda: store._load_any(dashboard.dashboard_id).settings.get("refresh_period") == 300,
        page,
    )
    assert apps[-1]._editor.settings.refresh_period == 300
    assert not apps[-1]._editor.dirty


def test_editor_who_may_not_share_gets_no_sharing_or_delete(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard("someone-else", "Plant")
    store.set_permission(
        dashboard.dashboard_id,
        Permission.from_spec(allow_edit_users=sorted(resolve_identity(AUTH).user_names)),
    )
    apps = _serve_edit(page, tmp_path, store, dashboard)

    page.get_by_role("button", name="Settings").click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Refresh", exact=True).first).to_be_visible()
    view = apps[-1]._editor._settings_view
    assert [item["label"] for item in view._nav.items] == ["General", "Refresh"]
    expect(dialog.get_by_text("Sharing", exact=True).filter(visible=True)).to_have_count(0)
    expect(dialog.get_by_role("button", name="Delete")).to_be_hidden()
    expect(dialog.get_by_label("Name")).to_be_disabled()


def test_delete_asks_for_the_typed_name(page, tmp_path):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard(resolve_identity(AUTH).user, "Plant")
    _serve_edit(page, tmp_path, store, dashboard)

    page.get_by_role("button", name="Settings").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete").click()
    confirm = page.get_by_role("dialog").filter(has_text="Delete Plant?")
    expect(confirm.get_by_label("Type the name to confirm")).to_be_visible()
    confirm.get_by_label("Type the name to confirm").fill("Plant")
    confirm.get_by_role("button", name="Delete dashboard").click()
    wait_until(lambda: store._all_dashboards() == [], page)
