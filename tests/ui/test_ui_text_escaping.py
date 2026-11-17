"""UI test: stored and data text is shown as text and never runs as HTML in the browser."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

PAYLOAD = "Q3 <img src=x onerror=\"window.__xss=1;document.title='pwned'\">"


@register(page=False, component=True, title="Payload", config=["title"])
class Payload(Viewer):
    title = param.String(default="")

    @param.output(param.DataFrame)
    def frame(self):
        return pd.DataFrame({PAYLOAD: [1, 2]})

    def __panel__(self):
        return pn.pane.Markdown("payload tile", height=120)


# Counts the payload's elements in the page and in every shadow root under it.
_INJECTED = """() => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot);
    }
  }
  return roots.reduce((n, root) => n + root.querySelectorAll('img[src="x"]').length
    + [...root.querySelectorAll("script")].filter(s => s.text.includes("__xss")).length, 0);
}"""


def _assert_not_injected(page, title):
    page.wait_for_timeout(500)
    assert page.title() == title
    assert page.evaluate("() => window.__xss") is None
    assert page.evaluate(_INJECTED) == 0


def test_dashboard_tile_and_column_text_is_not_rendered_as_html(page):
    editor = FlowDash(
        {"Test/payload": Payload},
        notifications=False,
        include_builtin_components=False,
        inspect_outputs=True,
        # The full layout shows the description as text rather than in a tooltip.
        header_layout="full",
    )
    editor.load_model(DashboardModel(dashboard_id="d", user_id="u", title=PAYLOAD))
    editor.settings.description = PAYLOAD
    editor.settings.tile_titles = True
    instance_id = editor.add_component("Test/payload", config={"title": PAYLOAD})
    editor.editable = False
    serve_component(page, editor)
    title = page.title()

    expect(page.get_by_text(PAYLOAD, exact=True).first).to_be_visible()
    # Title bar, description and tile title.
    expect(page.get_by_text(PAYLOAD, exact=True)).to_have_count(3)
    _assert_not_injected(page, title)

    editor._on_inspect_clicked(instance_id, editor._inspect_tiles[instance_id])
    expect(page.locator(".flowdash-inspect-header").get_by_text(PAYLOAD)).to_be_visible()
    expect(page.get_by_text(f"{PAYLOAD} int")).to_be_visible()
    _assert_not_injected(page, title)
