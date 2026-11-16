"""UI tests for component-level authorization."""

import pytest
from panel.tests.util import serve_component

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


@register(page=False, component=True, title="Ledger", allow_groups=["finance"])
def ledger(config):
    return "ledger tile"


@register(page=False, component=True, title="Notes")
def notes(config):
    return "notes tile"


REGISTRY = normalize_components({"Test/ledger": ledger, "Test/notes": notes})


@pytest.mark.filterwarnings("ignore:reference already known")
def test_restricted_component_renders_as_a_locked_tile(page):
    store = MemoryDashboardStore()
    dashboard = store.create_dashboard("someone-else", "Books")
    dashboard.items = [
        DashboardItem(instance_id="l1", component_id="Test/ledger"),
        DashboardItem(instance_id="n1", component_id="Test/notes", x=300),
    ]
    store.save_dashboard(dashboard)

    def app():
        return FlowDashApp(
            registry=REGISTRY,
            store=store,
            home_dashboard="Books",
            include_builtin_components=False,
            notifications=False,
        )

    serve_component(page, app)

    page.get_by_text("notes tile").wait_for()
    expect(page.get_by_text("You do not have access to this component.")).to_be_visible()
    expect(page.get_by_text("ledger tile")).to_have_count(0)
    expect(page.get_by_text("Ledger")).to_have_count(0)
