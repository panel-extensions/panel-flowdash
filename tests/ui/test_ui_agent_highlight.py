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
    # The outline is drawn inside the tile, where the grid item does not clip it.
    expect(marked).to_have_css("outline-offset", "-2px")

    page.get_by_role("button", name="Done").first.click()
    page.get_by_role("button", name="Save and leave").click()
    page.get_by_role("button", name="Edit").wait_for()

    expect(page.locator(f".{TILE_CLASSES['add']}")).to_have_count(0)
    tiles = page.locator(".flowdash-tile:visible")
    expect(tiles).to_have_count(2, timeout=20000)
    page.mouse.move(0, 0)
    page.wait_for_timeout(500)
    for i in range(tiles.count()):
        assert _tinted_corner_pixels(page, tiles.nth(i)) == []
