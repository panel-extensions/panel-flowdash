"""Helpers shared by the UI tests."""

import re

from panel.tests.util import serve_component as _serve_component, wait_until

# Whether every stylesheet linked into a shadow root has loaded. Panel's own
# test utilities only gained this helper after the oldest Panel tested here.
_STYLESHEETS_SETTLED = """() => {
    const settled = (root) => [...root.querySelectorAll('*')].every((el) => el.shadowRoot == null || (
        [...el.shadowRoot.querySelectorAll('link[rel="stylesheet"]')].every((link) => link.sheet != null)
        && settled(el.shadowRoot)
    ))
    return settled(document)
}"""

# Whether every Bokeh document on the page has rendered and gone idle.
_DOCUMENTS_IDLE = (
    "() => window.Bokeh?.documents?.length > 0 && Bokeh.documents.every((d) => d.is_idle)"
)

# Opening a dashboard builds the models of the whole editor in one go, which on
# a busy CI runner takes longer than the 5 s that ``expect`` waits by default.
DASHBOARD_LOAD_TIMEOUT = 20000

# A scripted agent run of a few tool calls takes about 1.5 s on a desktop and
# over 20 s on a Windows runner, most of it in the first tool call of a test
# worker, so waits for a run to end get this much.
AGENT_RUN_TIMEOUT = 60000

# The number of inert elements on the page, across shadow roots.
INERT_COUNT = """() => {
  const found = []
  const walk = (root) => root.querySelectorAll("*").forEach((el) => {
    if (el.inert) found.push(el)
    if (el.shadowRoot) walk(el.shadowRoot)
  })
  walk(document)
  return found.length
}"""


def ask_button(page):
    """The dashboard bar's Ask toggle, named by its description.

    In view mode it docks the Explore feed beside the dashboard or the
    report; in edit mode it opens and closes the assistant's drawer.
    """
    return page.get_by_role("button", name="Ask about this dashboard", exact=True).filter(
        visible=True
    )


def open_assistant(page):
    """Open the assistant's drawer on a dashboard: Edit if in view mode, then Ask.

    The top bar's Assistant button is hidden on dashboard pages, where the
    bar's Ask opens the drawer in edit mode. Ask left on in view mode opens
    it as edit mode starts.
    """
    from playwright.sync_api import expect

    edit = page.get_by_role("button", name="Edit", exact=True).filter(visible=True)
    done = page.get_by_role("button", name="Done", exact=True).filter(visible=True)
    expect(edit.or_(done).first).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    if edit.count():
        edit.click()
        expect(done).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    ask = ask_button(page)
    if ask.get_attribute("aria-pressed") != "true":
        ask.click()
        # Off Ask, whose tooltip would otherwise stay over the edit actions beside it.
        page.mouse.move(0, 0)


def ask_from_palette(page, text):
    """Ask the assistant *text* from the command palette, which opens its drawer in view mode too."""
    from panel_flowdash.ui.command_palette import PLACEHOLDER

    page.keyboard.press("Control+k")
    page.get_by_role("combobox", name=PLACEHOLDER).press_sequentially(text)
    page.get_by_role("option", name=re.compile("^Ask the assistant")).click()


def serve_component(page, app, suffix="", wait=True, **kwargs):
    """Serve *app* and, with *wait*, return once the page takes input.

    Panel's ``serve_component`` gives the first render five seconds, while
    the first page a test worker opens can take longer as the browser
    compiles the component bundles. Its page template also covers the page
    with a loading overlay until the root view reports idle; locator actions
    wait for it, but raw ``page.mouse`` and ``page.keyboard`` input lands on
    the overlay instead.
    """
    result = _serve_component(page, app, suffix=suffix, wait=False, **kwargs)
    if wait:
        page.wait_for_function(_DOCUMENTS_IDLE, timeout=30000)
        page.locator("#loader").wait_for(state="hidden", timeout=30000)
        page.wait_for_function(_STYLESHEETS_SETTLED, timeout=15000)
    return result


def stable_box(locator, page=None, timeout=8000):
    """Return the bounding box of *locator* once it stops moving.

    Nodes on the canvas grow as their views render and the viewport fits
    them, so a box read too early points at where a handle used to be.
    """
    locator.wait_for(state="visible", timeout=timeout)
    boxes = []

    def settled():
        boxes.append(locator.bounding_box())
        return len(boxes) >= 3 and boxes[-1] is not None and boxes[-1] == boxes[-2] == boxes[-3]

    wait_until(settled, page, timeout=timeout, interval=100)
    return boxes[-1]


def center(box):
    """The center point of a bounding box."""
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


# The width and height each tile on the grid is drawn at, by the key class of its tile.
_DRAWN_SIZES = """() => {
  const out = {}
  const walk = (root) => root.querySelectorAll("*").forEach((el) => {
    if (el.classList?.contains("muuri-grid-item")) {
      const tile = el.querySelector('[class*="flowdash-tile-key-"]')
      const key = tile && [...tile.classList].find((c) => c.startsWith("flowdash-tile-key-"))
      if (key) out[key] = [parseFloat(el.getAttribute("data-width")), parseFloat(el.style.height)]
    }
    if (el.shadowRoot) walk(el.shadowRoot)
  })
  walk(document)
  return out
}"""


def tile_sizes_match(page, editor):
    """Whether every tile is drawn at the width and height its layout entry gives it."""
    keys = {
        next(c for c in tile.css_classes if c.startswith("flowdash-tile-key-")): iid
        for iid, tile in editor._inspect_tiles.items()
        if iid in editor._grid_ids
    }
    if editor._deferred_grid_layout is not None:
        return False
    # What the grid was told to draw, one entry per tile in its order.
    entries = dict(zip(editor._grid_ids, editor._tile_grid.layout, strict=False))
    drawn = page.evaluate(_DRAWN_SIZES)
    shown = [
        k
        for k, iid in keys.items()
        if entries.get(iid, {}).get("visible", True)
        and {"width", "height"} <= entries.get(iid, {}).keys()
    ]
    if not shown or any(k not in drawn for k in shown):
        return False
    return all(
        abs(drawn[k][0] - entries[keys[k]]["width"]) < 0.01
        and abs(drawn[k][1] - entries[keys[k]]["height"]) < 0.5
        for k in shown
    )


def select_option(page, name):
    """An option of an open panel-material-ui Select menu.

    The options are menuitems up to panel-material-ui 0.17 and listbox
    options from panel-material-ui#836 on, so this matches either role.
    """
    return page.get_by_role("option", name=name).or_(page.get_by_role("menuitem", name=name))
