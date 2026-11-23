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


def test_the_templates_are_a_labelled_radio_group(page):
    _serve(page)
    dialog = _open_dialog(page)
    name = dialog.get_by_label("Name", exact=True)
    group = dialog.get_by_role("radiogroup", name="Start from")
    expect(group).to_be_visible()
    radios = group.get_by_role("radio")
    expect(radios).to_have_count(4)
    blank = group.get_by_role("radio", name=re.compile("^Blank"))
    kpi = group.get_by_role("radio", name=re.compile("^KPI overview"))
    explorer = group.get_by_role("radio", name=re.compile("^Table explorer"))
    expect(blank).to_have_attribute("aria-checked", "true")
    expect(kpi).to_have_attribute("aria-checked", "false")

    # One Tab stop: the chosen option; the arrow keys, Home and End move and choose.
    name.focus()
    page.keyboard.press("Tab")
    expect(blank).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(kpi).to_be_focused()
    expect(kpi).to_have_attribute("aria-checked", "true")
    expect(name).to_have_value("KPI overview")
    page.keyboard.press("End")
    expect(explorer).to_have_attribute("aria-checked", "true")
    page.keyboard.press("Home")
    expect(blank).to_have_attribute("aria-checked", "true")
    page.keyboard.press("ArrowUp")
    expect(explorer).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(blank).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(kpi).to_have_attribute("aria-checked", "true")
    outline = kpi.evaluate("e => getComputedStyle(e).outlineStyle")
    assert outline == "solid"

    next_button = dialog.get_by_role("button", name="Next", exact=True)
    next_button.focus()
    page.keyboard.press("Enter")
    # Every slot is a labelled combobox, its description part of its accessible name.
    data = dialog.get_by_role("combobox", name=re.compile("^Data A table with"))
    expect(data).to_be_visible()
    expect(dialog.get_by_role("combobox", name=re.compile("^Measure "))).to_be_disabled()
    expect(dialog.get_by_text("Choose Data first.").first).to_be_visible()
    page.keyboard.press("Shift+Tab")
    back = dialog.get_by_role("button", name="Back", exact=True)
    back.focus()
    page.keyboard.press("Enter")
    expect(kpi).to_be_visible()


def _select_opens_by_keyboard() -> bool:
    """Whether the installed panel-material-ui Select opens from the keyboard (it sets open via onOpen)."""
    from pathlib import Path

    import panel_material_ui

    jsx = Path(panel_material_ui.__file__).parent / "widgets" / "Select.jsx"
    return jsx.exists() and "onOpen" in jsx.read_text()


@pytest.mark.skipif(
    not _select_opens_by_keyboard(),
    reason="panel-material-ui's Select only opens on click (no onOpen), fixed in panel-material-ui",
)
def test_a_slot_is_chosen_by_keyboard(page):
    _serve(page)
    dialog = _open_dialog(page)
    dialog.get_by_role("radio", name=re.compile("^KPI overview")).click()
    dialog.get_by_role("button", name="Next", exact=True).click()
    data = dialog.get_by_role("combobox", name=re.compile("^Data"))
    data.focus()
    page.keyboard.press("ArrowDown")
    expect(page.get_by_role("listbox")).to_be_visible()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(data).to_have_text(re.compile("Sales"))


def test_escape_on_the_slot_step_then_reopen(page):
    _serve(page)
    dialog = _open_dialog(page)
    dialog.get_by_role("radio", name=re.compile("^KPI overview")).click()
    dialog.get_by_role("button", name="Next", exact=True).click()
    expect(dialog.get_by_role("combobox", name=re.compile("^Data"))).to_be_visible()
    dialog.get_by_role("button", name="Back", exact=True).focus()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    page.get_by_role("button", name="New dashboard").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_role("heading", name="Create dashboard", exact=True)).to_be_visible()
    expect(dialog.get_by_role("radio", name=re.compile("^Blank"))).to_have_attribute(
        "aria-checked", "true"
    )


def test_templates_turned_off_show_only_the_name(page):
    _serve(page, templates=False)
    dialog = _open_dialog(page)
    expect(dialog.get_by_label("Name", exact=True)).to_be_visible()
    expect(dialog.get_by_text("KPI overview", exact=True)).to_have_count(0)
