"""UI tests that the top bar's actions, search, assistant and theme toggle never overlap."""

from itertools import pairwise

import pytest

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .test_ui_header_layout import EDIT_ACTIONS, TOP_BAR, VIEW_ACTIONS
from .test_ui_header_overflow import _HITS_ITSELF, _button, _store
from .test_ui_view_header import COMPONENTS
from .util import serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# The boxes of the visible buttons, crumbs and chips in the top bar.
_TOP_BAR_ITEMS = """() => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  const bar = roots.map((r) => r.querySelector("header.MuiAppBar-root")).find(Boolean).getBoundingClientRect()
  return roots.flatMap((r) => [...r.querySelectorAll("button")]).flatMap((b) => {
    const box = b.getBoundingClientRect()
    if (box.width === 0 || box.top < bar.top || box.bottom > bar.bottom + 1) return []
    const name = b.getAttribute("aria-label") || b.textContent.trim()
    return [{name, left: box.left, right: box.right}]
  })
}"""


def _agent():
    return Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
@pytest.mark.parametrize("width", [640, 800, 1024])
def test_top_bar_items_never_overlap(page, width, edit):
    store, dash = _store()
    page.set_viewport_size({"width": width, "height": 900})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=_agent(),
        )

    suffix = f"/dash/{dash}" + ("?edit=true" if edit else "")
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix)
    names = (EDIT_ACTIONS if edit else VIEW_ACTIONS) + TOP_BAR + ["Assistant"]
    for name in names:
        expect(_button(page, name)).to_be_visible()
        stable_box(_button(page, name), page)

    items = sorted(page.evaluate(_TOP_BAR_ITEMS), key=lambda item: item["left"])
    assert items[0]["left"] >= 0
    assert items[-1]["right"] <= width
    for before, after in pairwise(items):
        assert before["right"] <= after["left"] + 1, f"{before['name']} overlaps {after['name']}"
    for name in names:
        assert _button(page, name).evaluate(_HITS_ITSELF), f"{name} is covered"
