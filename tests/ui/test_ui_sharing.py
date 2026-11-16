"""UI tests for the editor role in the share dialog and for who may inspect outputs."""

import re

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import DashboardItem, MemoryDashboardStore, Permission, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig
from panel_flowdash.component_library import normalize_components

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


@register(page=False, component=True, title="Meter")
class Meter(Viewer):
    reading = param.Integer(default=3)

    @param.output(param.Integer)
    @param.depends("reading")
    def tripled(self):
        return self.reading * 3

    def __panel__(self):
        return "meter tile"


REGISTRY = normalize_components({"Test/meter": Meter})


def test_share_dialog_sets_editors(page):
    store = MemoryDashboardStore()

    def app():
        app = FlowDashApp(
            registry=REGISTRY,
            store=store,
            auth_config=AuthConfig(user_groups={"erin": ["sales"]}),
            notifications=False,
        )
        dashboard = store.create_dashboard(app._user_id, "Plant")
        store.set_permission(
            dashboard.dashboard_id, Permission.from_spec(allow_edit_users=["bob"])
        )
        app._open_share_dialog(dashboard.dashboard_id, "Plant")
        return app._share_dialog

    serve_component(page, app)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("bob", exact=True)).to_be_visible()
    add = dialog.get_by_role("combobox", name="Add people or groups")
    add.fill("erin")
    add.press("Enter")
    dialog.locator(".share-entry").filter(has_text="erin").get_by_role("combobox").click()
    page.get_by_role("menuitem", name="Can edit").click()
    dialog.get_by_role("button", name="Save settings").click()

    (dashboard,) = store._all_dashboards()
    wait_until(
        lambda: (
            store._load_any(dashboard.dashboard_id).permission.allow_edit_users
            == frozenset({"bob", "erin"})
        ),
        page,
    )


@pytest.mark.filterwarnings("ignore:reference already known")
@pytest.mark.parametrize("allowed", [False, True], ids=["default", "opened-to-viewers"])
def test_viewer_inspects_only_when_the_dashboard_allows_it(page, allowed):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard("someone-else", "Plant")
    dashboard.items = [DashboardItem(instance_id="m1", component_id="Test/meter")]
    dashboard.settings = {"viewers_can_inspect": allowed}
    store.save_dashboard(dashboard)

    def app():
        return FlowDashApp(
            registry=REGISTRY,
            store=store,
            home_dashboard="Plant",
            editor_options={"inspect_outputs": True},
            notifications=False,
        )

    serve_component(page, app)

    page.get_by_text("meter tile").wait_for()
    button = page.get_by_role("button", name=re.compile("^Inspect data for "))
    if allowed:
        button.click()
        page.locator(".MuiDrawer-paper").get_by_text("9", exact=True).wait_for()
    else:
        expect(button).to_be_hidden()
