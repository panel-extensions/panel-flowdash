"""Helpers shared by the UI tests."""

from panel.tests.util import (
    _STYLESHEETS_SETTLED,
    serve_component as _serve_component,
    wait_until,
)

# Whether every Bokeh document on the page has rendered and gone idle.
_DOCUMENTS_IDLE = (
    "() => window.Bokeh?.documents?.length > 0 && Bokeh.documents.every((d) => d.is_idle)"
)

# Opening a dashboard builds the models of the whole editor in one go, which on
# a busy CI runner takes longer than the 5 s that ``expect`` waits by default.
DASHBOARD_LOAD_TIMEOUT = 20000


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
