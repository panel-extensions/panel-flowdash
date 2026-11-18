"""Helpers shared by the UI tests."""

from panel.tests.util import serve_component as _serve_component, wait_until

# Opening a dashboard builds the models of the whole editor in one go, which on
# a busy CI runner takes longer than the 5 s that ``expect`` waits by default.
DASHBOARD_LOAD_TIMEOUT = 20000


def serve_component(page, app, suffix="", wait=True, **kwargs):
    """Serve *app* and, with *wait*, return once the page takes input.

    Panel's page template covers the page with a loading overlay until the
    root view reports idle, which on a slow runner is well after Panel's
    ``serve_component`` returns. Locator actions wait for it, but raw
    ``page.mouse`` and ``page.keyboard`` input lands on the overlay instead.
    """
    result = _serve_component(page, app, suffix=suffix, wait=wait, **kwargs)
    if wait:
        page.locator("#loader").wait_for(state="hidden", timeout=30000)
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
