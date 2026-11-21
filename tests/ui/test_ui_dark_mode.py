"""UI tests for the editor's canvas, split and code panel in the dark theme."""

import pytest

from tests.test_code_panel import make_editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

_STYLE = """([selector, pseudo, prop]) => {
  const find = (root) => {
    const el = root.querySelector(selector)
    if (el) return el
    for (const child of root.querySelectorAll('*')) {
      if (child.shadowRoot) { const hit = find(child.shadowRoot); if (hit) return hit }
    }
    return null
  }
  const el = find(document)
  return el ? getComputedStyle(el, pseudo)[prop] : null
}"""


def _style(page, selector, prop, pseudo=None):
    return page.evaluate(_STYLE, [selector, pseudo, prop])


def _brightness(color):
    import re

    r, g, b = (float(v) for v in re.findall(r"[\d.]+", color)[:3])
    return (r + g + b) / 3


def _serve(page, editor, dark):
    import panel_material_ui as pmui

    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, pmui.Page(main=[editor], dark_theme=dark))


def test_split_and_canvas_follow_the_dark_theme(page):
    editor, _query = make_editor(mode="split")
    _serve(page, editor, dark=True)
    expect(page.locator(".react-flow__edge-path").first).to_be_attached(timeout=15000)
    # Light lines on the dark page, not React Flow's #3e3e3e or splitjs's #e0e0e0.
    assert _style(page, ".react-flow__edge-path", "stroke") != "rgb(62, 62, 62)"
    assert _style(page, ".gutter > .divider", "backgroundImage").count("224, 224, 224") == 0
    assert _style(page, ".gutter", "backgroundColor", "::after") != "rgb(224, 224, 224)"
    assert _brightness(_style(page, ".react-flow__attribution", "backgroundColor")) < 60


def test_canvas_keeps_react_flow_light_defaults(page):
    editor, _query = make_editor()
    _serve(page, editor, dark=False)
    expect(page.locator(".react-flow__edge-path").first).to_be_attached(timeout=15000)
    assert _style(page, ".react-flow__edge-path", "stroke") == "rgb(177, 177, 183)"


@pytest.mark.parametrize("dark", [True, False])
def test_code_panel_cursor_line_is_a_neutral_tint(page, dark):
    editor, query = make_editor()
    _serve(page, editor, dark=dark)
    page.locator(f".react-flow__node[data-id='{query}']").click()
    page.locator(".fd-edit-spec").first.click(timeout=8000)
    code = page.locator(".fd-code-panel-editor")
    expect(code.locator(".ace_content")).to_contain_text("p_year > 2014", timeout=8000)
    code.locator(".ace_content").click()
    active = code.locator(".ace_marker-layer .ace_active-line")
    expect(active).to_be_attached()
    tint = "rgba(255, 255, 255, 0.08)" if dark else "rgba(0, 0, 0, 0.04)"
    assert active.evaluate("el => getComputedStyle(el).backgroundColor") == tint
