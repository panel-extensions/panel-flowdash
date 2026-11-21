"""UI tests for the inspector's Data tab: a component's own preview shown as a table that fits the inspector."""

import pandas as pd
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

WIDE = pd.DataFrame(
    {
        "case_id": range(3000400, 3000460),
        "t_state": ["AK", "CA", "TX"] * 20,
        "t_county": ["Northwest Arctic Borough", "Kern County", "Story County"] * 20,
        "p_name": ["Alpine Wind", "Big Sky Ridge", "Cedar Creek"] * 20,
        "p_year": [2001, 2010, 2020] * 20,
        "t_cap": [1500, 2300, 3600] * 20,
    }
)


class Pipeline:
    """Stands in for a lazy pipeline: a table without ``columns`` or ``head``."""


@register(
    page=False,
    component=True,
    title="Lazy Table",
    provides=[{"key": "pipeline", "type": "demo.pipeline"}],
)
class LazyTable(Viewer):
    @param.output(param.Parameter)
    def pipeline(self):
        return Pipeline()

    def __flowdash_preview__(self, output=None):
        return WIDE

    def __panel__(self):
        return "lazy"


# Sizes of the table's host, the table inside it and the inspector's tab content.
_SIZES = """(host) => {
  const table = host.shadowRoot.querySelector('table');
  return {
    hostWidth: host.clientWidth, hostScroll: host.scrollWidth,
    hostHeight: host.getBoundingClientRect().height, tableHeight: table.getBoundingClientRect().height,
    rows: table.querySelectorAll('tbody tr').length,
    headers: [...table.querySelectorAll('thead th')].map(th => th.textContent.trim()),
  };
}"""


def test_data_tab_shows_a_components_preview_as_a_fitted_table(page):
    editor = FlowDash(
        {"Demo/lazy": LazyTable}, notifications=False, include_builtin_components=False
    )
    node = editor.add_component("Demo/lazy", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.get_by_role("tab", name="Data").click()

    heading = page.locator(".fd-data-heading")
    expect(heading).to_have_text("Pipeline")
    # The type id is only in the heading's tooltip.
    heading.hover()
    expect(page.get_by_role("tooltip")).to_have_text("Type: demo.pipeline")
    expect(page.get_by_text("60 rows \u00d7 6 columns, first 15 shown")).to_be_visible()

    host = page.locator(".fd-data-table")
    expect(host).to_be_visible()
    sizes = host.evaluate(_SIZES)
    assert sizes["headers"] == list(WIDE.columns)
    assert sizes["rows"] == 15
    # The table scrolls sideways inside its own box, never the inspector.
    assert sizes["hostScroll"] > sizes["hostWidth"]
    box, header = host.bounding_box(), page.locator(".fd-inspector-header").bounding_box()
    assert header["x"] <= box["x"] and box["x"] + box["width"] <= header["x"] + header["width"]
    # Fitted to its rows, with no empty area below them.
    assert abs(sizes["hostHeight"] - sizes["tableHeight"]) <= 4
