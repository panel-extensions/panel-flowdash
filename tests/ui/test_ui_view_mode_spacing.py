"""UI tests: the tile grid in view mode, after editing and beside a right-hand sidebar."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash import DashboardItem, MemoryDashboardStore
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.auth import resolve_identity

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# The edges of the visible tile grid and its tiles, the left edge of the open
# right-hand drawer (or None), and the edit-only decorations left on the grid.
_GRID_STATE = """() => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  const all = (sel) => roots.flatMap((r) => [...r.querySelectorAll(sel)])
  const shown = (el) => el.getBoundingClientRect().width > 0
  const grid = all(".muuri-grid").find(shown)
  if (!grid) return null
  const items = [...grid.children].filter((c) => c.classList.contains("muuri-grid-item") && shown(c))
  const drawer = all(".MuiDrawer-paper").find((d) => {
    const box = d.getBoundingClientRect()
    return box.width > 0 && box.left < window.innerWidth - 1
  })
  const handles = [...grid.querySelectorAll(".muuri-handle")].filter((h) => getComputedStyle(h).display !== "none")
  const edges = (el) => {
    const box = el.getBoundingClientRect()
    return [box.left, box.right]
  }
  return {
    grid: edges(grid),
    tiles: items.map(edges),
    drawer: drawer ? drawer.getBoundingClientRect().left : null,
    gridClasses: [...grid.classList],
    shownHandles: handles.length,
    outlines: items.map((i) => getComputedStyle(i).outlineStyle).filter((s) => s !== "none"),
    itemBackgrounds: items.map((i) => getComputedStyle(i).backgroundColor),
  }
}"""


def _agent():
    return Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


def _store():
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        DashboardItem(instance_id=f"n{i}", component_id="Test/note", x=0, y=0) for i in range(3)
    ]
    model.tile_layout = [
        {"instance_id": "n0", "index": 0, "width": 50, "height": 200, "visible": True},
        {"instance_id": "n1", "index": 1, "width": 50, "height": 200, "visible": True},
        {"instance_id": "n2", "index": 2, "width": 100, "height": 200, "visible": True},
    ]
    store.save_dashboard(model)
    return store, model.dashboard_id


def _settled_state(page):
    states = []

    def settled():
        states.append(page.evaluate(_GRID_STATE))
        return (
            len(states) >= 3 and states[-1] is not None and states[-1] == states[-2] == states[-3]
        )

    wait_until(settled, page, timeout=10000, interval=200)
    return states[-1]


@pytest.mark.parametrize("width", [1440, 1920])
def test_tiles_keep_the_same_spacing_from_the_assistant_as_from_the_rail(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    store, dash = _store()
    _serve_app(page, store, f"/dash/{dash}", agent=_agent())
    page.get_by_text("note tile").first.wait_for()

    state = _settled_state(page)
    inset = min(left for left, _ in state["tiles"]) - state["grid"][0]
    assert inset > 0
    assert state["grid"][1] == pytest.approx(width, abs=1)
    assert width - max(right for _, right in state["tiles"]) == pytest.approx(inset, abs=1.5)

    page.get_by_role("button", name="Assistant", exact=True).click()
    wait_until(
        lambda: (page.evaluate(_GRID_STATE) or {}).get("drawer") is not None, page, timeout=10000
    )
    state = _settled_state(page)
    assert state["grid"][1] == pytest.approx(state["drawer"], abs=1)
    right = max(right for _, right in state["tiles"])
    assert state["drawer"] - right == pytest.approx(inset, abs=1.5)


@pytest.mark.parametrize("tab", ["Layout", "Split"])
def test_tiles_lose_their_edit_decorations_when_editing_is_done(page, tab):
    page.set_viewport_size({"width": 1440, "height": 900})
    store, dash = _store()
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps)
    page.get_by_text("note tile").first.wait_for()
    viewed = _settled_state(page)

    page.get_by_role("button", name="Edit").click()
    page.get_by_role("button", name=tab).click()
    # The Split tab shows the dashboard at the pane's width, without handles.
    wait_until(
        lambda: (
            apps[0]._editor.mode == ("dashboard" if tab == "Layout" else "split")
            and ((page.evaluate(_GRID_STATE) or {}).get("shownHandles", 0) > 0)
            == (tab == "Layout")
        ),
        page,
        timeout=10000,
    )
    page.locator(".muuri-grid-item").first.click(position={"x": 30, "y": 60})
    wait_until(lambda: apps[0]._editor._tile_grid.selected is not None, page, timeout=10000)

    page.get_by_role("button", name="Done").click()
    page.get_by_role("button", name="Refresh").wait_for()
    page.mouse.move(1, 1)
    state = _settled_state(page)
    assert {"muuri-no-handles", "muuri-no-card"} <= set(state["gridClasses"])
    assert state["shownHandles"] == 0
    assert state["outlines"] == []
    assert state["itemBackgrounds"] == viewed["itemBackgrounds"]
    assert state["tiles"] == viewed["tiles"]
