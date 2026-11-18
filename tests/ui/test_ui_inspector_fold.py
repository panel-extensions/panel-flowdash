"""UI test: the inspector folded beside the assistant lays its text out at full width once unfolded."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.auth import resolve_identity

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# The narrowest box of a text of four or more characters below the top bar and
# right of *left*, leaving out icon ligatures.
_NARROWEST_TEXT = """(left) => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  let narrowest = null
  for (const root of roots) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent.trim()
      if (text.length < 4) continue
      const range = document.createRange()
      range.selectNodeContents(node)
      const box = range.getBoundingClientRect()
      if (box.width === 0 || box.left < left || box.top < 64) continue
      if (node.parentElement.closest(".material-icons, .MuiIcon-root")) continue
      if (narrowest === null || box.width < narrowest.width) narrowest = {text, width: box.width}
    }
  }
  return narrowest
}"""


@pytest.mark.xfail(
    reason="Panel sizes an ESM component's container only when rendering it (holoviz/panel#9108)",
    strict=False,
)
def test_inspector_text_is_not_squeezed_after_folding_beside_the_assistant(page):
    page.set_viewport_size({"width": 1600, "height": 1000})
    store = MemoryDashboardStore()
    dash = store.create_dashboard(resolve_identity().user, "Wind fleet").dashboard_id
    apps = []
    agent = Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps, agent=agent)
    inspector = apps[0]._editor._inspector

    page.get_by_role("button", name="Assistant", exact=True).click()
    wait_until(lambda: inspector.collapsed, page, timeout=10000)
    apps[0]._editor.add_component("Test/note")
    page.wait_for_timeout(1000)
    page.get_by_role("button", name="Assistant", exact=True).click()
    wait_until(lambda: not inspector.collapsed, page, timeout=10000)
    expect(page.get_by_text("Issues", exact=True)).to_be_visible()
    page.wait_for_timeout(500)

    narrowest = page.evaluate(_NARROWEST_TEXT, 1600 - 360)
    assert narrowest["width"] >= 30, narrowest
