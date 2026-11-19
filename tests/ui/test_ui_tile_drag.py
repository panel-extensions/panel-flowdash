"""UI tests for dragging components between the tile grid, the top bar and the sidebar."""

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import FlowDash, register
from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.ui.tile_layout import GRID, SIDEBAR, TOP, area_items

from .util import center, serve_component, stable_box, tile_sizes_match

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui


@register(page=False, component=True, sidebar=True, title="Region")
def region(config):
    return pmui.Select(label="Region", options=["North", "South"])


@register(page=False, component=True, title="Chart", default_size={"width": 600, "height": 300})
def chart(config):
    return pn.pane.Markdown("A chart", sizing_mode="stretch_both")


COMPONENTS = {"T/region": region, "T/chart": chart}
PLACED = [
    ("T/region", "region"),
    ("Widgets/KPI", "kpi"),
    ("Text/Heading", "heading"),
    ("T/chart", "chart"),
]


def _editor(store=None, **params):
    params.setdefault("history_coalesce", 0)
    editor = FlowDash(
        components=COMPONENTS, notifications=False, store=store, user="alice", **params
    )
    editor.new_dashboard("Drag")
    for component, instance_id in PLACED:
        editor.add_component(component, instance_id=instance_id)
    editor.mode = "dashboard"
    return editor


def _areas(editor):
    return {area: [i["instance_id"] for i in area_items(editor, area)] for area in AREAS}


AREAS = (GRID, TOP, SIDEBAR)


def _published(editor) -> list[dict]:
    """Return the metadata of the published versions.

    Unlike ``edit_history.versions``, this does not publish the edit in
    progress, which from the test's thread could split an edit the server is
    still recording.
    """
    artifact = editor.edit_history.artifact
    return [v.metadata for v in artifact.versions] if artifact is not None else []


def _tile(page, text):
    return page.locator(".muuri-grid-item").filter(has_text=text)


def _drag(page, start, end, *, steps=20, before_release=None):
    page.mouse.move(*start)
    page.mouse.down()
    for k in range(1, steps + 1):
        page.mouse.move(
            start[0] + (end[0] - start[0]) * k / steps, start[1] + (end[1] - start[1]) * k / steps
        )
        page.wait_for_timeout(20)
    page.wait_for_timeout(200)
    if before_release is not None:
        before_release()
    page.mouse.up()


def _drag_tile(page, text, end, **kwargs):
    tile = _tile(page, text)
    tile.hover()
    handle = tile.locator(".muuri-handle.drag")
    _drag(page, center(stable_box(handle, page)), end, **kwargs)


def _drag_control(page, handle, end, **kwargs):
    _drag(page, center(stable_box(handle, page)), end, **kwargs)


# The role of the element with the keyboard focus, across shadow roots.
_FOCUSED_ROLE = """() => {
  let el = document.activeElement
  while (el?.shadowRoot?.activeElement) { el = el.shadowRoot.activeElement }
  return el?.getAttribute("role") ?? el?.tagName
}"""


def _grid_box(page):
    return stable_box(page.locator(".muuri-grid").first, page)


def test_drag_tiles_to_the_sidebar_and_top_bar_and_back(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    expect(page.locator(".muuri-grid-item")).to_have_count(3)

    # The sidebar is empty, so it is offered along the grid's left edge while dragging.
    grid = _grid_box(page)
    target = (grid["x"] + 30, grid["y"] + grid["height"] - 40)

    def sidebar_offered():
        expect(page.locator(".fd-tile-drag-layer [data-area='sidebar']")).to_have_text("Sidebar")

    _drag_tile(page, "Heading", target, before_release=sidebar_offered)
    wait_until(lambda: _areas(editor)[SIDEBAR] == ["heading"], page, timeout=8000)
    expect(page.locator(".fd-tile-drag-layer")).to_have_count(0)
    expect(page.locator(".fd-controls-panel")).to_be_visible()
    expect(page.locator(".muuri-grid-item")).to_have_count(2)

    top = stable_box(page.locator(".fd-controls-bar"), page)
    _drag_tile(page, "KPI", (top["x"] + top["width"] - 40, top["y"] + top["height"] / 2))
    wait_until(lambda: _areas(editor)[TOP] == ["region", "kpi"], page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(1)

    # Back onto the grid, in front of the chart.
    chart = stable_box(_tile(page, "A chart"), page)
    heading = page.locator(".fd-controls-panel .fd-control-handle")
    _drag_control(page, heading, (chart["x"] + 20, chart["y"] + chart["height"] / 2))
    wait_until(lambda: _areas(editor)[GRID] == ["heading", "chart"], page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(2)
    expect(page.locator(".fd-controls-panel")).to_be_hidden()


def test_reorder_the_top_bar_and_undo(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    top = stable_box(page.locator(".fd-controls-bar"), page)
    _drag_tile(page, "KPI", (top["x"] + top["width"] - 40, top["y"] + top["height"] / 2))
    moved = "Moved kpi to the top bar"
    wait_until(
        lambda: [m["label"] for m in _published(editor)[-1:]] == [moved], page, timeout=8000
    )
    versions = len(_published(editor))

    handles = page.locator(".fd-controls-bar .fd-control-handle")
    region = stable_box(handles.nth(0), page)
    _drag_control(page, handles.nth(1), (region["x"] + 24, region["y"] + region["height"] / 2))
    wait_until(lambda: len(_published(editor)) > versions, page, timeout=8000)
    page.wait_for_timeout(500)
    assert len(_published(editor)) == versions + 1
    assert _areas(editor)[TOP] == ["kpi", "region"]

    editor.undo()
    wait_until(lambda: _areas(editor)[TOP] == ["region", "kpi"], page, timeout=8000)
    editor.undo()
    wait_until(lambda: _areas(editor)[TOP] == ["region"], page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(3)


def test_a_chart_is_refused_beside_the_grid(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    serve_component(page, editor)
    versions = len(editor.edit_history.versions)
    top = stable_box(page.locator(".fd-controls-bar"), page)

    def refused():
        over = page.locator(".fd-tile-drag-layer [data-area='top']")
        expect(over).to_have_attribute("data-refused", "true")
        expect(page.locator(".fd-tile-drag-hint")).to_have_text(
            "Charts and tables stay on the grid"
        )

    _drag_tile(
        page,
        "A chart",
        (top["x"] + top["width"] - 40, top["y"] + top["height"] / 2),
        before_release=refused,
    )
    page.wait_for_timeout(500)
    assert "chart" in _areas(editor)[GRID]
    assert all(
        v.metadata.get("args", {}).get("area") is None
        for v in editor.edit_history.versions[versions:]
    )


def test_a_moved_tile_is_saved_and_reloaded(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    store = MemoryDashboardStore()
    editor = _editor(store)
    serve_component(page, editor)
    grid = _grid_box(page)
    _drag_tile(page, "KPI", (grid["x"] + 30, grid["y"] + grid["height"] - 40))
    wait_until(lambda: _areas(editor)[SIDEBAR] == ["kpi"], page, timeout=8000)
    page.get_by_role("button", name="Save").click()
    wait_until(lambda: not editor.dirty, page, timeout=8000)

    reloaded = FlowDash(components=COMPONENTS, notifications=False, store=store, user="alice")
    reloaded.load_model(store.load_dashboard("alice", editor.dashboard.dashboard_id))
    reloaded.editable = False
    serve_component(page, reloaded)
    panel = page.locator(".fd-controls-panel")
    expect(panel).to_be_visible()
    expect(panel).to_contain_text("KPI")
    expect(page.locator(".muuri-grid-item")).to_have_count(2)
    expect(page.locator(".fd-control-handle").first).to_be_hidden()


@pytest.mark.parametrize("coalesce", [0, 1])
def test_a_drag_off_the_grid_is_one_version(page, coalesce):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor(history_coalesce=coalesce)
    serve_component(page, editor)
    versions = len(editor.edit_history.versions)
    before = _areas(editor)
    grid = _grid_box(page)
    _drag_tile(page, "Heading", (grid["x"] + 30, grid["y"] + grid["height"] - 40))
    move = {"op": "layout", "args": {"name": "heading", "area": SIDEBAR}}
    wait_until(
        lambda: any(move in m.get("ops", []) for m in _published(editor)[versions:]),
        page,
        timeout=8000,
    )
    page.wait_for_timeout(1500)
    assert len(_published(editor)) == versions + 1
    assert _areas(editor)[SIDEBAR] == ["heading"]

    editor.undo()
    wait_until(lambda: _areas(editor) == before, page, timeout=8000)
    expect(page.locator(".muuri-grid-item")).to_have_count(3)


def test_a_tile_dropped_above_the_grid_goes_to_an_empty_top_bar(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor(controls_position="left")
    serve_component(page, editor)
    assert _areas(editor)[TOP] == []
    grid = _grid_box(page)
    _drag_tile(page, "Heading", (grid["x"] + grid["width"] / 2, grid["y"] - 20))
    wait_until(lambda: _areas(editor)[TOP] == ["heading"], page, timeout=8000)


def test_tiles_keep_their_sizes_when_one_moves_to_the_top_bar(page):
    page.set_viewport_size({"width": 1400, "height": 900})
    editor = _editor()
    editor.add_component("Widgets/Select", instance_id="select")
    serve_component(page, editor)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=8000)
    # The KPI leads the grid, so its removal shifts every tile after it.
    editor._layout_panel.tile_select.value = "kpi"
    # The panel's buttons are rebuilt for the selected tile; a slow browser may still show the old ones.
    page.wait_for_timeout(1500)
    page.get_by_text("Move to top bar").first.click()
    wait_until(lambda: _areas(editor)[TOP] == ["region", "kpi"], page, timeout=8000)
    page.wait_for_timeout(500)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=8000)
    # The pressed button left with the tile, so the focus moves to the tile picker.
    wait_until(lambda: page.evaluate(_FOCUSED_ROLE) == "combobox", page, timeout=8000)


def test_tiles_keep_their_sizes_when_a_component_is_removed_in_the_split(page):
    page.set_viewport_size({"width": 2000, "height": 900})
    editor = _editor()
    editor.add_component("Widgets/Select", instance_id="select")
    serve_component(page, editor)
    # The Split tab shows the layout for the pane's width, so a pane as wide as
    # Desktop shows the tiles at their own sizes.
    editor._split_view.preferences.sizes = (20.0, 80.0)
    editor._split_view.restore()
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=8000)
    # The KPI leads the grid, so removing it shifts every tile after it.
    page.locator(".react-flow__node").filter(has_text="KPI").first.click()
    wait_until(lambda: (editor._flow.selection or {}).get("nodes") == ["kpi"], page, timeout=8000)
    page.keyboard.press("Delete")
    wait_until(lambda: "kpi" not in editor._grid_ids, page, timeout=8000)
    page.wait_for_timeout(500)
    wait_until(lambda: tile_sizes_match(page, editor), page, timeout=8000)
