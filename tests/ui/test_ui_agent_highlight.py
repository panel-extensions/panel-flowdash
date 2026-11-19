"""UI test: the canvas and grid outline what the assistant's edit added."""

import io

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from PIL import Image
from pydantic_ai import Agent

from panel_flowdash.agent.highlight import TILE_CLASSES
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

from ..test_agent_tools import scripted
from .test_ui_shell import _serve_app, _store_with

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _pixel(page, x, y):
    png = page.screenshot(clip={"x": x, "y": y, "width": 1, "height": 1})
    return tuple(Image.open(io.BytesIO(png)).convert("RGB").getpixel((0, 0)))


def _edge_pixels(page, locator):
    """Return the pixels 1px inside the middle of each edge of an element's box."""
    box = locator.bounding_box()
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    points = [
        (x + w / 2, y + 1),
        (x + w - 2, y + h / 2),
        (x + w / 2, y + h - 2),
        (x + 1, y + h / 2),
    ]
    return [_pixel(page, px, py) for px, py in points]


def _tinted_corner_pixels(page, locator, size=6):
    """Return the coloured pixels in the corners of an element's box."""
    box = locator.bounding_box()
    corners = [
        (box["x"], box["y"]),
        (box["x"] + box["width"] - size, box["y"]),
        (box["x"], box["y"] + box["height"] - size),
        (box["x"] + box["width"] - size, box["y"] + box["height"] - size),
    ]
    tinted = []
    for x, y in corners:
        png = page.screenshot(clip={"x": x, "y": y, "width": size, "height": size})
        data = Image.open(io.BytesIO(png)).convert("RGB").tobytes()
        pixels = [data[i : i + 3] for i in range(0, len(data), 3)]
        tinted += [tuple(p) for p in pixels if max(p) - min(p) > 40]
    return tinted


def test_the_added_node_is_outlined_until_the_next_run(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_role("textbox").last
    chat.fill("Add another note")
    chat.press("Enter")

    wait_until(lambda: len(apps[0]._agent_session.highlighter.marked) == 1, page, timeout=20000)
    (instance_id,) = apps[0]._agent_session.highlighter.marked
    node = page.locator(f'.react-flow__node[data-id="{instance_id}"]')
    expect(node).to_be_visible(timeout=20000)
    expect(node).to_have_css("border-color", "rgb(46, 125, 50)", timeout=20000)
    page.get_by_role("button", name="Layout").first.click()
    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(1, timeout=20000)


def test_leaving_edit_mode_leaves_no_outline_on_the_tiles(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_role("textbox").last
    chat.fill("Add another note")
    chat.press("Enter")

    wait_until(lambda: len(apps[0]._agent_session.highlighter.marked) == 1, page, timeout=20000)
    page.get_by_role("button", name="Layout").first.click()
    marked = page.locator(f".{TILE_CLASSES['add']}")
    expect(marked).to_have_count(1, timeout=20000)
    page.mouse.move(0, 0)
    # The outline is drawn just inside the edge of the grid item that holds the tile.
    # The grid may still be laying out, so the pixels are sampled until it settles.
    wait_until(
        lambda: all(g > r + 30 and g > b + 30 for r, g, b in _edge_pixels(page, marked)),
        page,
        timeout=10000,
    )

    page.get_by_role("button", name="Done").first.click()
    page.get_by_role("button", name="Save and leave").click()
    page.get_by_role("button", name="Edit", exact=True).wait_for()

    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(0)
    tiles = page.locator(".flowdash-tile:visible")
    expect(tiles).to_have_count(2, timeout=20000)
    page.mouse.move(0, 0)
    page.wait_for_timeout(500)
    for i in range(tiles.count()):
        assert _tinted_corner_pixels(page, tiles.nth(i)) == []


# The ring's box and radius, and those of the tile's visible edge: the grid
# item's card while the tiles are arranged, otherwise the tile itself, whose
# border is the frame of a framed tile.
_RING_AND_EDGE = """
(el) => {
  const item = el.closest('.muuri-grid-item');
  const ring = getComputedStyle(item, '::after');
  const card = getComputedStyle(item).boxShadow !== 'none';
  const edge = card ? item : el;
  const b = item.getBoundingClientRect();
  const e = edge.getBoundingClientRect();
  const px = (v) => parseFloat(v);
  return {
    framed: el.classList.contains('flowdash-tile-framed'),
    card,
    ring: [
      b.x + px(ring.left),
      b.y + px(ring.top),
      b.width - px(ring.left) - px(ring.right),
      b.height - px(ring.top) - px(ring.bottom),
    ],
    ringRadius: ring.borderTopLeftRadius,
    ringWidth: ring.borderTopWidth,
    edge: [e.x, e.y, e.width, e.height],
    edgeRadius: getComputedStyle(edge).borderTopLeftRadius,
  };
}
"""


def _rings_on_edges(page, preview):
    """Return the rings, and whether each sits on its tile's edge in the mode asked for.

    The tiles must be laid out in that mode: drawn as cards only while
    arranging, both frames shown in view mode, and at a real size, since the
    ring also matches the edge of a card the grid has not sized yet.
    """
    marked = page.locator(f".{TILE_CLASSES['add']}")
    rings = [marked.nth(i).evaluate(_RING_AND_EDGE) for i in range(marked.count())]
    laid_out = (
        len(rings) == 2
        and all(ring["card"] is not preview for ring in rings)
        and all(ring["edge"][2] >= 100 and ring["edge"][3] >= 50 for ring in rings)
        and (not preview or sorted(ring["framed"] for ring in rings) == [False, True])
    )
    aligned = all(
        all(abs(r - e) <= 1 for r, e in zip(ring["ring"], ring["edge"], strict=True))
        and ring["ringRadius"] == ring["edgeRadius"]
        and ring["ringWidth"] == "2px"
        for ring in rings
    )
    return rings, laid_out and aligned


@pytest.mark.parametrize("preview", [False, True], ids=["arranging", "view"])
def test_the_tile_outline_sits_on_the_tiles_visible_edge(page, preview):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(
            ("add_node", {"component": "Test/note", "name": "framed"}),
            ("add_node", {"component": "Test/note", "name": "unframed"}),
            ("place_tile", {"name": "framed", "frame": "framed"}),
            ("place_tile", {"name": "unframed", "frame": "unframed"}),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)

    page.get_by_text("note tile").first.wait_for()
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_role("textbox").last
    chat.fill("Add a framed and an unframed note")
    chat.press("Enter")

    session = apps[0]._agent_session
    wait_until(
        lambda: len(session.highlighter.marked) == 2 and not session.runner.state.running,
        page,
        timeout=20000,
    )
    page.get_by_role("button", name="Layout").first.click()
    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(2, timeout=20000)
    apps[0]._editor.preview = preview

    for width in (1280, 700):
        page.set_viewport_size({"width": width, "height": 900})
        # The grid lays the tiles out again after the change.
        wait_until(lambda: _rings_on_edges(page, preview)[1], page, timeout=10000)
