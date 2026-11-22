"""UI tests of global filters: the filter bar, the notes on tiles and the opt-out in the Tile tab."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import FlowDash, register

from .util import DASHBOARD_LOAD_TIMEOUT, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

FLEET = pd.DataFrame(
    {"state": ["TX", "OK", "TX", "CA"], "year": [2001, 2005, 2010, 2020], "mw": [1.0, 2, 3, 4]}
)
REGIONS = pd.DataFrame({"region": ["South", "West"], "mw": [6.0, 4.0]})


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
class GFFleet(Viewer):
    data = param.DataFrame(default=FLEET)

    def __panel__(self):
        return pn.pane.Str("fleet")


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
class GFRegions(Viewer):
    data = param.DataFrame(default=REGIONS)

    def __panel__(self):
        return pn.pane.Str("regions")


@register(
    component=True,
    page=False,
    tags=["view"],
    config=["label"],
    requires=[{"key": "table", "type": "DataFrame"}],
)
class GFRows(Viewer):
    table = param.DataFrame(default=None, allow_None=True)
    label = param.String(default="Rows")

    def _text(self, table, label):
        return f"{label}: {0 if table is None else len(table)} rows"

    def __panel__(self):
        return pn.pane.Str(pn.bind(self._text, self.param.table, self.param.label))


DOC = """\
flowdash: 1
title: Fleet
filters:
- {column: state, label: State, widget: select}
nodes:
  fleet: {component: T/Fleet, position: [0, 0]}
  regions: {component: T/Regions, position: [0, 300]}
  by_state: {component: T/Rows, config: {label: By state}, position: [400, 0]}
  everything: {component: T/Rows, config: {label: Whole fleet}, position: [400, 150]}
  regional: {component: T/Rows, config: {label: Regional}, position: [400, 300]}
edges:
- {from: fleet.data, to: by_state.table}
- {from: fleet.data, to: everything.table}
- {from: regions.data, to: regional.table}
"""


def _editor(**params):
    editor = FlowDash(
        {"T/Fleet": GFFleet, "T/Regions": GFRegions, "T/Rows": GFRows},
        notifications=False,
        **params,
    )
    editor.import_dashboard(DOC)
    return editor


def _choose(page, label, option):
    page.get_by_role("combobox", name=label).click()
    page.get_by_role("listbox").get_by_text(option, exact=True).click()
    page.keyboard.press("Escape")


def test_the_filter_bar_filters_matching_views_and_hints_on_the_rest(page):
    editor = _editor(editable=False)
    serve_component(page, editor)
    bar = page.locator(".fd-filter-bar")
    expect(bar.get_by_role("combobox", name="State")).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(page.get_by_text("By state: 4 rows")).to_be_visible()

    edits = editor._edit_count
    _choose(page, "State", "TX")
    expect(page.get_by_text("By state: 2 rows")).to_be_visible()
    expect(page.get_by_text("Whole fleet: 2 rows")).to_be_visible()
    expect(page.get_by_text("Regional: 2 rows")).to_be_visible()
    hint = page.locator(".flowdash-tile-filter-hint")
    expect(hint).to_have_count(1)
    expect(hint).to_contain_text("Not filtered by State")
    assert editor._global_filters.values == {"state": ["TX"]}
    # Choosing a value is the viewer's, not an edit of the dashboard.
    assert editor._edit_count == edits


def test_a_view_opts_out_in_the_tile_tab(page):
    editor = _editor(editable=True)
    serve_component(page, editor)
    editor.set_filter_value("state", ["TX"])
    editor.mode = "dashboard"
    expect(page.get_by_text("Whole fleet: 2 rows")).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)

    editor._show_in_sidecar("everything", "Tile")
    switch = page.locator(".fd-tile-filter-switch").get_by_role("switch")
    expect(switch).to_be_checked()
    switch.click()
    expect(page.get_by_text("Whole fleet: 4 rows")).to_be_visible()
    wait_until(lambda: editor.filters[0].get("exclude") == ["everything"], page)
    expect(page.get_by_text("By state: 2 rows")).to_be_visible()


def test_filters_are_added_in_edit_mode_and_shown_in_the_report(page):
    editor = _editor(editable=True)
    editor.set_filters([])
    serve_component(page, editor)
    editor.mode = "dashboard"
    add = page.locator(".fd-filter-add").get_by_role("button")
    expect(add).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    add.click()
    page.get_by_role("menuitem", name="state", exact=True).click()
    wait_until(lambda: [f["column"] for f in editor.filters] == ["state"], page)
    editor.set_filter_value("state", ["CA"])

    page.get_by_role("button", name="Report", exact=True).click()
    report = page.locator(".flowdash-report")
    expect(report.locator(".fd-filter-bar").get_by_role("combobox", name="state")).to_be_visible()
    expect(report.get_by_text("By state: 1 rows")).to_be_visible()
