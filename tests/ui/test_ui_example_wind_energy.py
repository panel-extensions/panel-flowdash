"""UI tests for the wind energy example: its seeded dashboards open and respond."""

import importlib.util
import sys
from pathlib import Path

import pytest
from panel.tests.util import serve_component

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.registry import build_registry

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

EXAMPLE = Path(__file__).parents[2] / "examples" / "wind_energy"

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(
        not (EXAMPLE / "_data" / "turbines.csv.gz").exists(), reason="Example data not available."
    ),
    pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index"),
    pytest.mark.filterwarnings("ignore:reference already known"),
]


@pytest.fixture
def serve_example(page, tmp_path, monkeypatch):
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    project.store.close()
    store = MemoryDashboardStore()
    project.seed_dashboards(store, "demo")
    registry = build_registry(EXAMPLE)

    def serve(dashboard_id):
        def app():
            return FlowDashApp(
                registry=registry,
                store=store,
                home_dashboard=dashboard_id,
                notifications=False,
                propagate_failures=project.propagate_failures,
                settings_class=project.settings_class,
                editor_options=project.editor_options,
                page_options=project.page_options,
            )

        serve_component(page, app)

    yield serve
    for name in list(sys.modules):
        if name.split(".")[0] in {"Sources", "Controls", "Transforms", "Views", "Explore"}:
            del sys.modules[name]


def test_overview_shows_kpis_and_the_largest_projects(page, serve_example):
    serve_example("wind-overview")

    def shown(text):
        # Until the canvas shows compact node cards, the hidden canvas holds a copy of each view.
        return page.get_by_text(text, exact=True).filter(visible=True).first

    expect(shown("Installed capacity")).to_be_visible(timeout=20000)
    expect(shown("71,790")).to_be_visible()
    expect(shown("138.2 GW")).to_be_visible()
    expect(shown("Largest projects")).to_be_visible()
    expect(shown("Western Spirit")).to_be_visible()


def test_deep_dive_waits_for_a_project_then_shows_it(page, serve_example):
    serve_example("wind-state-deep-dive")

    placeholder = page.get_by_text("Pick a project in the sidebar to see its turbines")
    expect(placeholder).to_be_visible(timeout=20000)

    project = page.get_by_placeholder("Type a project name")
    project.click()
    project.fill("Roscoe")
    page.get_by_role("option").first.click()

    expect(placeholder).to_be_hidden()
    expect(page.get_by_text("Fisher County, TX")).to_be_visible()
