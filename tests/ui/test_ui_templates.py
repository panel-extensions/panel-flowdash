"""UI tests for starting a dashboard from a template in the "Create dashboard" dialog."""

import re

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import MemoryDashboardStore, register

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(
    page=False,
    component=True,
    title="Sales",
    tags=["source"],
    provides=[{"key": "sales", "type": "DataFrame"}],
)
class Sales(Viewer):
    @param.output(param.DataFrame)
    def sales(self):
        return pd.DataFrame(
            {
                "day": pd.date_range("2026-01-01", periods=3),
                "region": ["north", "south", "east"],
                "revenue": [1.0, 2.0, 3.0],
            }
        )

    def __panel__(self):
        return pn.pane.Markdown("sales")


@register(page=False, component=True, title="Rows", tags=["view"])
class Rows(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown(
            param.bind(lambda t: f"{0 if t is None else len(t)} rows", self.param.table)
        )


def _serve(page, **params):
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.component_library import normalize_components

    store = MemoryDashboardStore()

    def app():
        return FlowDashApp(
            registry=normalize_components({"Data/sales": Sales, "Views/rows": Rows}),
            store=store,
            notifications=False,
            **params,
        )

    serve_component(page, app)
    return store


def _open_dialog(page):
    page.get_by_role("button", name="New dashboard").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Create dashboard")).to_be_visible()
    return dialog


def _pick(page, dialog, label, option):
    dialog.get_by_label(label, exact=True).click()
    page.get_by_role("menuitem", name=option, exact=True).click()
    expect(page.get_by_role("listbox")).to_have_count(0)


def test_blank_is_chosen_and_creates_as_before(page):
    store = _serve(page)
    dialog = _open_dialog(page)
    expect(dialog.get_by_text("Blank", exact=True)).to_be_visible()
    expect(dialog.get_by_text("Start from an empty canvas")).to_be_visible()
    dialog.get_by_label("Name", exact=True).fill("Fresh")
    dialog.get_by_role("button", name="Create", exact=True).click()
    wait_until(lambda: [m.title for m in store._all_dashboards()] == ["Fresh"], page)


def test_choose_a_template_bind_its_slots_and_create(page):
    store = _serve(page)
    dialog = _open_dialog(page)

    dialog.get_by_text("KPI overview", exact=True).click()
    expect(dialog.get_by_label("Name", exact=True)).to_have_value("KPI overview")
    expect(dialog.locator(".thumb .tile")).to_have_count(7)
    expect(dialog.get_by_text("A row of headline numbers")).to_have_count(2)
    dialog.get_by_role("button", name="Next", exact=True).click()

    expect(dialog.get_by_text("Create dashboard: KPI overview")).to_be_visible()
    create = dialog.get_by_role("button", name="Create", exact=True)
    expect(create).to_be_disabled()
    _pick(page, dialog, "Data", "Sales")
    expect(create).to_be_enabled()
    expect(dialog.get_by_label("Measure", exact=True)).to_be_enabled(timeout=15000)
    _pick(page, dialog, "Measure", "revenue")
    _pick(page, dialog, "Trend chart", "Rows")
    create.click()

    def created():
        models = store._all_dashboards()
        return len(models) == 1 and models[0].title == "KPI overview"

    wait_until(created, page)
    model = store._all_dashboards()[0]
    components = {item.instance_id: item.component_id for item in model.items}
    assert components["data"] == "Data/sales" and components["trend"] == "Views/rows"
    assert components["breakdown"] == "Text/Markdown"
    expect(page.get_by_role("dialog")).to_have_count(0, timeout=15000)
    page.get_by_role("button", name="Layout", exact=True).click()
    expect(page.get_by_text("This tile needs").first).to_be_visible(timeout=15000)


def test_back_returns_to_the_templates(page):
    _serve(page)
    dialog = _open_dialog(page)
    dialog.get_by_text("Table explorer with filters", exact=True).click()
    dialog.get_by_role("button", name="Next", exact=True).click()
    expect(dialog.get_by_label("Table", exact=True)).to_be_visible()
    dialog.get_by_role("button", name="Back", exact=True).click()
    expect(dialog.get_by_text("Trend and breakdown", exact=True)).to_be_visible()
    expect(dialog.get_by_role("button", name="Next", exact=True)).to_be_visible()


def test_the_templates_are_reached_by_keyboard(page):
    _serve(page)
    dialog = _open_dialog(page)
    name = dialog.get_by_label("Name", exact=True)
    templates = dialog.get_by_role("navigation")
    blank = templates.get_by_role("button", name="Blank An empty dashboard")
    kpi = templates.get_by_role("button", name=re.compile("^KPI overview A row of headline"))
    name.focus()
    page.keyboard.press("Tab")
    expect(blank).to_be_focused()
    page.keyboard.press("Tab")
    expect(kpi).to_be_focused()
    page.keyboard.press("Enter")
    expect(name).to_have_value("KPI overview")
    next_button = dialog.get_by_role("button", name="Next", exact=True)
    expect(next_button).to_be_visible()
    next_button.focus()
    page.keyboard.press("Enter")
    # Every slot is a labelled combobox, its description part of its accessible name.
    data = dialog.get_by_role("combobox", name=re.compile("^Data A table with"))
    expect(data).to_be_visible()
    expect(dialog.get_by_role("combobox", name=re.compile("^Measure "))).to_be_disabled()
    expect(dialog.get_by_text("Choose Data first.").first).to_be_visible()
    back = dialog.get_by_role("button", name="Back", exact=True)
    back.focus()
    page.keyboard.press("Enter")
    expect(kpi).to_be_visible()


def test_templates_turned_off_show_only_the_name(page):
    _serve(page, templates=False)
    dialog = _open_dialog(page)
    expect(dialog.get_by_label("Name", exact=True)).to_be_visible()
    expect(dialog.get_by_text("KPI overview", exact=True)).to_have_count(0)
