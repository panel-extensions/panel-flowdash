"""UI tests for the vertical alignment of the breadcrumb's separator and switcher arrow."""

import re

import pytest

from panel_flowdash import DashboardItem, MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .test_ui_view_header import COMPONENTS
from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

LONG_TITLE = "Quarterly turbine availability and curtailment review for every plant in the fleet"

# The vertical extent of an element's own text, not of its padded box.
_TEXT_BOX = """
(el) => {
  const node = [...el.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim())
  const range = document.createRange()
  range.selectNodeContents(node || el)
  const box = range.getBoundingClientRect()
  return {top: box.top, bottom: box.bottom}
}
"""


def _serve(page, title, width, scheme, header_layout):
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": width, "height": 900})
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, title)
    model.items = [DashboardItem(instance_id="chart", component_id="Test/chart")]
    store.save_dashboard(model)

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            header_layout=header_layout,
        )

    serve_component(
        page, {"/": factory, "/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}"
    )
    page.locator(".flowdash-tile").filter(visible=True).first.wait_for()


def _middle(top, bottom):
    return (top + bottom) / 2


def _text_middle(locator):
    box = locator.evaluate(_TEXT_BOX)
    return _middle(box["top"], box["bottom"])


def _box_middle(locator, page):
    box = stable_box(locator, page)
    return box["y"] + box["height"] / 2


@pytest.mark.parametrize("header_layout", ["compact", "full"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("width", "title"), [(1440, "Data Quality"), (1024, LONG_TITLE)])
def test_separator_and_switcher_arrow_are_centred_on_the_text(
    page, width, title, scheme, header_layout
):
    _serve(page, title, width, scheme, header_layout)

    header = page.locator("header")
    switcher = header.get_by_role("button", name=title).filter(visible=True).first
    crumb = header.get_by_role("button", name=re.compile("^Dashboards$")).filter(visible=True)
    separator = header.get_by_text(
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}", exact=True
    ).filter(visible=True)
    arrow = switcher.locator(".MuiButton-endIcon svg")

    text = _text_middle(switcher)
    assert abs(_box_middle(arrow, page) - text) <= 1
    if header_layout == "compact" and width < 1280:
        # Beside the dashboard's actions the crumbs before the switcher give way.
        expect(crumb).to_have_count(0)
        expect(separator).to_have_count(0)
    else:
        assert abs(_box_middle(separator, page) - text) <= 1
        assert abs(_text_middle(crumb) - text) <= 1

    if title == LONG_TITLE:
        truncated = switcher.evaluate(
            "(el) => el.scrollWidth > el.clientWidth"
            " && getComputedStyle(el).textOverflow === 'ellipsis'"
        )
        assert truncated
        arrow_box, switcher_box = stable_box(arrow, page), stable_box(switcher, page)
        assert arrow_box["x"] + arrow_box["width"] <= switcher_box["x"] + switcher_box["width"]
