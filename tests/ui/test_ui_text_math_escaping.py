"""UI test: user and data text never becomes links, HTML or MathJax math in the browser.

Every surface shows the whole payload corpus. The page must request nothing from
evil.test, run no payload script (also after clicking whatever MathJax typeset), and
contain no typeset math, no image and no link pointing at a payload.
"""

import asyncio

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.placeholders import Placeholder, broken_edges_panel, placeholder_view
from panel_flowdash.ui.text import (
    alert_pane,
    escape_markdown,
    markdown_pane,
    text_alert,
    text_typography,
)

from ..untrusted_payloads import ALL, PAYLOADS
from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Payload", config=["title"])
class Payload(Viewer):
    title = param.String(default="")

    @param.output(param.DataFrame)
    def frame(self):
        return pd.DataFrame({name: [payload] for name, payload in PAYLOADS.items()})

    def __flowdash_inspect__(self, port):
        raise RuntimeError(ALL)

    def __panel__(self):
        return pn.pane.Markdown("payload tile", height=120)


def _editor(**params):
    return FlowDash(
        {"Test/payload": Payload},
        notifications=False,
        include_builtin_components=False,
        inspect_outputs=True,
        **params,
    )


def _dashboard():
    editor = _editor(header_layout="full")
    editor.load_model(DashboardModel(dashboard_id="d", user_id="u", title=ALL))
    editor.settings.description = ALL
    editor.settings.tile_titles = True
    editor.add_component("Test/payload", config={"title": ALL})
    editor.editable = False
    return editor


def _canvas():
    # Components and connections that cannot be restored are named by the saved file.
    items = [DashboardItem("p", "Test/payload", 0, 0, {"title": ALL})]
    edges = []
    for i, (name, payload) in enumerate(PAYLOADS.items()):
        items.append(DashboardItem(f"m{i}", f"Views/{name} {payload}", 400, 150 * i, {}))
        edges.append(DashboardEdge("p", f"{name} {payload}", f"gone{i}", "x"))
    editor = _editor()
    editor.load_model(DashboardModel("d", "u", ALL, items=items, edges=edges))
    return editor


def _value_popups():
    editor = _editor()
    frame = Payload().frame()
    return pn.Column(
        editor._build_value_popup(ALL, ALL, frame),
        editor._build_value_popup(ALL, None, ALL),
        sizing_mode="stretch_width",
    )


def _import_message():
    editor = _editor()
    editor.add_component("Test/payload")
    editor._on_files_dropped([{"name": f"{ALL}.json", "content": '{"items": []}'}])
    return editor._import_message


def _inspect_error():
    editor = _editor()
    instance_id = editor.add_component("Test/payload")
    return editor._inspect_hook_content(instance_id, "frame")


def _placeholders():
    return pn.Column(
        placeholder_view(Placeholder("missing", ALL, ALL)),
        broken_edges_panel(list(PAYLOADS.values()), 0, lambda: None),
    )


def _page_error():
    app = FlowDashApp(registry={}, store=MemoryDashboardStore(), notifications=False)

    async def fail(*_args, **_kwargs):
        raise RuntimeError(ALL)

    app._entry_from_key = lambda key: object()
    app._unresolved = lambda entry: False
    app._can_access_entry = lambda entry: True
    app._instantiate_entry = fail
    return asyncio.run(app._render_page(("x", "y")))


def _helpers():
    # Unescaped TeX without URLs, which Markdown would turn into links of its own.
    unescaped = f"paren_href {PAYLOADS['dd_href']}\n\n$$x^2$$"
    return pn.Column(
        text_typography(ALL),
        text_alert(ALL),
        markdown_pane(escape_markdown(ALL)),
        # The math switch itself must reach the browser, even for unescaped TeX.
        markdown_pane(unescaped),
        alert_pane(unescaped),
    )


SURFACES = {
    "dashboard": _dashboard,
    "canvas": _canvas,
    "value_popup": _value_popups,
    "import_message": _import_message,
    "inspect_error": _inspect_error,
    "placeholders": _placeholders,
    "page_error": _page_error,
    "helpers": _helpers,
}

# Collects what the payloads produced, in the page and in every shadow root.
_FOUND = """() => {
  const roots = [document];
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot);
    }
  }
  const found = {math: 0, images: [], links: [], text: ""};
  for (const root of roots) {
    found.math += root.querySelectorAll("mjx-container").length;
    for (const img of root.querySelectorAll("img")) {
      if ((img.getAttribute("src") || "").includes("evil")) found.images.push(img.getAttribute("src"));
    }
    for (const a of root.querySelectorAll("a[href]")) {
      const href = a.getAttribute("href");
      if (/evil|javascript|data:|vbscript/i.test(href)) found.links.push(href);
    }
    for (const a of root.querySelectorAll("mjx-container a")) {
      a.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}));
    }
  }
  return found;
}"""


def _serve(page, build):
    requests = []
    page.on("request", lambda r: requests.append(r.url) if "evil.test" in r.url else None)
    page.route("**/*evil.test*/**", lambda route: route.abort())

    def app():
        # Load MathJax with the page, as any page with math or a LaTeX pane would.
        pn.extension("mathjax")
        return build()

    serve_component(page, app)
    return requests


@pytest.mark.parametrize("surface", list(SURFACES))
def test_payloads_stay_text(page, surface):
    requests = _serve(page, SURFACES[surface])
    expect(page.get_by_text("paren_href", exact=False).first).to_be_attached()
    page.wait_for_timeout(1500)
    found = page.evaluate(_FOUND)
    page.wait_for_timeout(300)
    assert found["math"] == 0
    assert found["images"] == []
    assert found["links"] == []
    assert requests == []
    assert page.evaluate("() => window.__pwned || []") == []


def test_mathjax_runs_on_the_test_page(page):
    # Guards the test above: without MathJax on the page it would pass vacuously.
    _serve(page, lambda: pn.pane.Markdown("$$x^2$$"))
    expect(page.locator("mjx-container").first).to_be_attached()
