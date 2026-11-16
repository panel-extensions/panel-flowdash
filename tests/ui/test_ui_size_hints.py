"""UI tests for the ``default_size``, ``min_size`` and ``max_size`` tile hints."""

import panel as pn
import pytest
from panel.tests.util import wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

from .util import center, serve_component, stable_box

pytest.importorskip("playwright")

pytestmark = pytest.mark.ui


@register(
    page=False,
    component=True,
    title="Sized",
    default_size={"width": 600, "height": 300},
    min_size={"width": 240, "height": 200},
    max_size={"width": 960, "height": 400},
)
def sized_tile(config):
    return pn.pane.Markdown("sized tile")


def _tile_sizes(page):
    """Return [authored width, inline height, min width attribute] for each tile."""
    return page.locator(".muuri-grid-item").evaluate_all(
        """els => els.map(el => [
            parseFloat(el.getAttribute('data-width')),
            el.style.height,
            el.getAttribute('data-min-width'),
        ])"""
    )


def test_new_tile_starts_at_default_size_and_resizing_stays_in_bounds(page):
    editor = FlowDash(
        {"Test/sized": sized_tile}, notifications=False, include_builtin_components=False
    )
    editor.add_component("Test/sized")
    editor.mode = "dashboard"
    serve_component(page, editor)

    wait_until(lambda: _tile_sizes(page) == [[50, "300px", "240"]], page=page, timeout=8000)

    handle = page.locator(".muuri-grid-item .muuri-handle.resize")
    x, y = center(stable_box(handle, page))
    page.mouse.move(x, y)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(x, y + 30 * step)
    page.mouse.up()

    wait_until(
        lambda: editor.layout and editor.layout[0]["height"] == 400, page=page, timeout=8000
    )
    wait_until(lambda: _tile_sizes(page)[0][1] == "400px", page=page, timeout=8000)
