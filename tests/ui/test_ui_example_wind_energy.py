"""UI tests for the wind energy example: its seeded dashboards open and respond."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from panel.tests.util import wait_until

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.registry import build_registry

from .util import serve_component, stable_box, tile_sizes_match

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

EXAMPLE = Path(__file__).parents[2] / "examples" / "wind_energy"

# The example's dashboards draw maps and charts of 72,000 turbines. The server
# has them ready in a second or two, but the browser takes over 20 s to render
# them on a busy Windows runner.
EXAMPLE_LOAD_TIMEOUT = 45000

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

    # The tiles show as their own views render, not all at once.
    for text in ["Installed capacity", "71,790", "138.2 GW", "Largest projects", "Western Spirit"]:
        expect(shown(text)).to_be_visible(timeout=EXAMPLE_LOAD_TIMEOUT)


def test_deep_dive_waits_for_a_project_then_shows_it(page, serve_example):
    serve_example("wind-state-deep-dive")

    placeholder = page.get_by_text("Pick a project in the sidebar to see its turbines")
    expect(placeholder).to_be_visible(timeout=EXAMPLE_LOAD_TIMEOUT)

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
    expect(gate).to_be_visible(timeout=EXAMPLE_LOAD_TIMEOUT)
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


def test_view_actions_lead_with_the_output_switch(page, serve_example):
    serve_example("wind-data-quality")
    expect(_bar_button(page, "Dashboard")).to_be_visible(timeout=EXAMPLE_LOAD_TIMEOUT)

    boxes = [
        stable_box(_bar_button(page, label), page)
        for label in ["Dashboard", "Ask", "Edit", "Refresh", "Off", "Share", "more_vert"]
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
    _bar_button(page, "Edit").click(timeout=EXAMPLE_LOAD_TIMEOUT)
    search = page.get_by_placeholder("Search components")
    # The editor is built once Edit is pressed, which takes as long as the first render.
    expect(search).to_be_visible(timeout=EXAMPLE_LOAD_TIMEOUT)
    if query:
        search.fill(query)
        expect(page.get_by_text("Data Table", exact=True)).to_have_count(0)

    # The hint's text can render after the list, shrinking the list's region and
    # leaving an entry scrolled to before it below the region's end.
    hint = stable_box(page.locator(".MuiPaper-root").filter(has_text="Drag onto the canvas"), page)
    last = page.locator(".MuiListItemButton-root").last
    last.scroll_into_view_if_needed()
    box = stable_box(last, page)
    region = last.evaluate(_SCROLL_REGION)
    # Entries scroll in a region of their own, which ends above the hint.
    assert region is not None
    assert _overlap(region, hint) == 0
    # Within a pixel, as scrolling to the end lands on a fractional offset.
    assert box["y"] >= region["y"] - 1
    assert box["y"] + box["height"] <= min(region["y"] + region["height"], hint["y"]) + 1


def _tile_by_id(page, editor, instance_id):
    key = next(
        c
        for c in editor._inspect_tiles[instance_id].css_classes
        if c.startswith("flowdash-tile-key-")
    )
    return page.locator(".muuri-grid-item").filter(has=page.locator(f".{key}"))


def test_tiles_keep_their_sizes_after_a_top_bar_move_its_undo_and_a_reorder(
    page, serve_example, monkeypatch
):
    apps = []
    init = FlowDashApp.__init__

    def remember(self, *args, **kwargs):
        init(self, *args, **kwargs)
        apps.append(self)

    monkeypatch.setattr(FlowDashApp, "__init__", remember)
    page.set_viewport_size({"width": 1600, "height": 1000})
    serve_example("wind-overview")
    editor = apps[0]._editor
    page.get_by_role("button", name="Edit", exact=True).click(timeout=EXAMPLE_LOAD_TIMEOUT)
    wait_until(lambda: editor.editable, page, timeout=8000)
    page.get_by_role("button", name="Layout").click()
    wait_until(
        lambda: editor.mode == "dashboard" and editor._tile_grid.editable, page, timeout=8000
    )
    expect(page.locator(".muuri-grid-item").filter(visible=True).first).to_be_visible(
        timeout=EXAMPLE_LOAD_TIMEOUT
    )
    sizes = {e["instance_id"]: (e["width"], e["height"]) for e in editor.layout if "width" in e}
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=EXAMPLE_LOAD_TIMEOUT)

    _tile_by_id(page, editor, "kpi_capacity").click()
    wait_until(lambda: editor._inspector._shown == ("node", "kpi_capacity"), page, timeout=8000)
    # The panel's buttons are rebuilt for the selected tile; a slow browser may still show the old ones.
    page.wait_for_timeout(1500)
    page.get_by_text("Move to top bar").first.click()
    wait_until(lambda: "kpi_capacity" not in editor._grid_ids, page, timeout=20000)
    page.wait_for_timeout(500)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=20000)

    # Undo puts the tile back in the middle of the grid, and redo takes it out again.
    page.evaluate("document.activeElement?.blur()")
    page.keyboard.press("Control+z")
    wait_until(lambda: "kpi_capacity" in editor._grid_ids, page, timeout=20000)
    page.wait_for_timeout(500)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=20000)
    page.keyboard.press("Control+Shift+z")
    wait_until(lambda: "kpi_capacity" not in editor._grid_ids, page, timeout=20000)
    page.wait_for_timeout(500)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=20000)

    # A reorder in the browser sends the sizes the grid draws back to the editor.
    year = _tile_by_id(page, editor, "kpi_year")
    year.hover()
    handle = stable_box(year.locator(".muuri-handle.drag"), page)
    first = stable_box(page.locator(".muuri-grid-item").filter(visible=True).first, page)
    page.mouse.move(handle["x"] + 4, handle["y"] + 4)
    page.mouse.down()
    for k in range(1, 21):
        x = handle["x"] + (first["x"] + 10 - handle["x"]) * k / 20
        y = handle["y"] + (first["y"] + 20 - handle["y"]) * k / 20
        page.mouse.move(x, y)
        page.wait_for_timeout(20)
    page.mouse.up()
    page.wait_for_timeout(1000)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=20000)
    after = {e["instance_id"]: (e["width"], e["height"]) for e in editor.layout if "width" in e}
    assert {iid: after[iid] for iid in sizes if iid in after} == {
        iid: size for iid, size in sizes.items() if iid in after
    }


# The on-screen rectangles of the visible tiles of the grid.
_TILE_RECTS = """() => {
  const out = []
  const walk = (root) => root.querySelectorAll("*").forEach((el) => {
    if (el.classList?.contains("muuri-grid-item")) {
      const r = el.getBoundingClientRect()
      if (r.width > 0 && r.height > 0 && getComputedStyle(el).display !== "none") {
        out.push([r.left, r.top, r.right, r.bottom])
      }
    }
    if (el.shadowRoot) walk(el.shadowRoot)
  })
  walk(document)
  return out
}"""


def _tiles_overlap(page) -> bool:
    rects = page.evaluate(_TILE_RECTS)
    return any(
        a[0] < b[2] - 1 and b[0] < a[2] - 1 and a[1] < b[3] - 1 and b[1] < a[3] - 1
        for i, a in enumerate(rects)
        for b in rects[i + 1 :]
    )


def test_moving_a_tile_from_the_middle_of_a_row_leaves_no_overlap(
    page, serve_example, monkeypatch
):
    apps = []
    init = FlowDashApp.__init__

    def remember(self, *args, **kwargs):
        init(self, *args, **kwargs)
        apps.append(self)

    monkeypatch.setattr(FlowDashApp, "__init__", remember)
    page.set_viewport_size({"width": 1600, "height": 1000})
    serve_example("wind-data-quality")
    editor = apps[0]._editor
    page.get_by_role("button", name="Edit", exact=True).click(timeout=EXAMPLE_LOAD_TIMEOUT)
    wait_until(lambda: editor.editable, page, timeout=8000)
    page.get_by_role("button", name="Layout").click()
    wait_until(
        lambda: editor.mode == "dashboard" and editor._tile_grid.editable, page, timeout=8000
    )
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=EXAMPLE_LOAD_TIMEOUT)
    page.wait_for_timeout(1000)
    assert not _tiles_overlap(page)

    _tile_by_id(page, editor, "kpi_checked").click()
    wait_until(lambda: editor._inspector._shown == ("node", "kpi_checked"), page, timeout=8000)
    page.wait_for_timeout(1500)
    page.get_by_text("Move to top bar").first.click()
    wait_until(lambda: "kpi_checked" not in editor._grid_ids, page, timeout=20000)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=20000)
    page.wait_for_timeout(1500)
    assert not _tiles_overlap(page)
