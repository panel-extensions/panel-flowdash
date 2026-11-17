"""UI tests for the wind energy example: its seeded dashboards open and respond."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.registry import build_registry

from .util import serve_component, stable_box

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
    # Owned by the test user, so the dashboards can be edited.
    project.seed_dashboards(store, resolve_identity().user)
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


def _overlap(a, b):
    """The height of the overlap of two boxes, 0 when they don't intersect."""
    if a["x"] >= b["x"] + b["width"] or b["x"] >= a["x"] + a["width"]:
        return 0
    return max(0, min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"]))


@pytest.mark.parametrize("width", [1440, 900])
def test_controls_bar_holds_the_state_select_above_the_tiles(page, serve_example, width):
    page.set_viewport_size({"width": width, "height": 900})
    serve_example("wind-data-quality")

    gate = page.get_by_text("Gate status", exact=True).filter(visible=True).first
    expect(gate).to_be_visible(timeout=20000)
    bar = stable_box(page.locator(".fd-controls-bar"), page)
    select = stable_box(page.locator(".MuiFormControl-root").filter(has_text="State").first, page)
    tile = stable_box(gate, page)

    # A small select, which still holds its floating label inside the bar.
    assert select["height"] >= 30
    assert bar["y"] <= select["y"]
    assert select["y"] + select["height"] <= bar["y"] + bar["height"]
    assert bar["y"] + bar["height"] <= tile["y"]
    # The top bar and one row of small inputs leave the tiles close to the top.
    tiles = page.locator(".flowdash-tile").filter(visible=True)
    assert min(tiles.nth(i).bounding_box()["y"] for i in range(tiles.count())) <= 130


def _bar_button(page, label):
    """The first shown button whose text, after any icon ligature, ends in *label*."""
    pattern = re.compile(rf"(^|[a-z_ ]){re.escape(label)}$")
    return page.locator("button").filter(has_text=pattern).filter(visible=True).first


def test_view_actions_lead_with_the_mode_toggle(page, serve_example):
    serve_example("wind-data-quality")
    expect(_bar_button(page, "View")).to_be_visible(timeout=20000)

    boxes = [
        stable_box(_bar_button(page, label), page)
        for label in ["View", "Edit", "Refresh", "Off", "Share", "more_vert"]
    ]
    lefts = [box["x"] for box in boxes]
    assert lefts == sorted(lefts)
    # All in the dashboard bar, on one line at this width.
    assert max(box["y"] for box in boxes) - min(box["y"] for box in boxes) < 8


# The box of the nearest ancestor of an element that scrolls, across shadow roots.
_SCROLL_REGION = """
(el) => {
  let node = el.parentElement || el.getRootNode().host
  while (node) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) {
      return node.getBoundingClientRect().toJSON()
    }
    node = node.parentElement || node.getRootNode().host
  }
  return null
}
"""


@pytest.mark.parametrize("height", [900, 768])
@pytest.mark.parametrize("query", ["", "map"])
def test_palette_hint_never_covers_an_entry(page, serve_example, height, query):
    page.set_viewport_size({"width": 1440, "height": height})
    serve_example("wind-data-quality")
    _bar_button(page, "Edit").click(timeout=20000)
    search = page.get_by_placeholder("Search components")
    expect(search).to_be_visible()
    if query:
        search.fill(query)
        expect(page.get_by_text("Data Table", exact=True)).to_have_count(0)

    last = page.locator(".MuiListItemButton-root").last
    last.scroll_into_view_if_needed()
    box = stable_box(last, page)
    hint = stable_box(page.locator(".MuiPaper-root").filter(has_text="Drag onto the canvas"), page)
    region = last.evaluate(_SCROLL_REGION)
    # Entries scroll in a region of their own, which ends above the hint.
    assert region is not None
    assert _overlap(region, hint) == 0
    # Within a pixel, as scrolling to the end lands on a fractional offset.
    assert box["y"] >= region["y"] - 1
    assert box["y"] + box["height"] <= min(region["y"] + region["height"], hint["y"]) + 1
