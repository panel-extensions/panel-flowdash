"""UI tests of a chart of one dashboard shown live in another through a reference."""

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import (
    DashboardEdge,
    DashboardItem,
    DashboardModel,
    MemoryDashboardStore,
    register,
)
from panel_flowdash.editor import FlowDash
from panel_flowdash.references import REFERENCE_COMPONENT

from .util import DASHBOARD_LOAD_TIMEOUT, select_option, serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Numbers", config=["scale"])
class Numbers(Viewer):
    scale = param.Integer(default=1)

    @param.output(param.DataFrame)
    @param.depends("scale")
    def table(self):
        return pd.DataFrame({"x": ["a", "b", "c"], "y": [v * self.scale for v in (1, 2, 3)]})

    def __panel__(self):
        return pn.pane.Markdown("numbers")


@register(
    page=False,
    component=True,
    title="Bars",
    config=["color"],
    extensions=["vega"],
    default_size={"width": 360, "height": 300},
)
class Bars(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    color = param.Selector(
        default="steelblue",
        objects=["steelblue", "seagreen"],
        metadata={"flowdash": {"two_way": True}},
    )

    def _spec(self, table, color):
        return {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "data": {"values": [] if table is None else table.to_dict(orient="records")},
            "mark": {"type": "bar", "color": color},
            "encoding": {
                "x": {"field": "x", "type": "nominal"},
                "y": {"field": "y", "type": "quantitative"},
            },
            "width": 240,
            "height": 120,
        }

    def _caption(self, table, color):
        return "No data" if table is None else f"Total {int(table['y'].sum())} in {color}"

    def __panel__(self):
        return pn.Column(
            pmui.Select.from_param(self.param.color, label="Color", margin=(4, 8)),
            pn.pane.Vega(pn.bind(self._spec, self.param.table, self.param.color)),
            pn.pane.Markdown(pn.bind(self._caption, self.param.table, self.param.color)),
            sizing_mode="stretch_width",
        )


COMPONENTS = {"Test/numbers": Numbers, "Test/bars": Bars}

# Ids of the form the store makes, which cannot be guessed, so a dashboard
# someone may not open is said to be out of reach rather than missing.
IDS = {"exec": "0e1ec0da5b0a", "ops": "0b5da5b0a4d1"}

# How many Vega charts are drawn on the page.
CHARTS = """() => Bokeh.documents.flatMap((doc) => [...doc.all_models])
  .filter((model) => model.type.endsWith("VegaPlot"))
  .map((model) => Bokeh.index.find_one_by_id(model.id))
  .filter((view) => view && view.vega_view && view.el.isConnected).length"""


def _store():
    source = DashboardModel(
        dashboard_id=IDS["exec"],
        user_id="alice",
        title="Exec dashboard",
        items=[
            DashboardItem(instance_id="numbers", component_id="Test/numbers", config={"scale": 2}),
            DashboardItem(instance_id="chart", component_id="Test/bars"),
        ],
        edges=[
            DashboardEdge(
                source="numbers", source_port="table", target="chart", target_port="table"
            )
        ],
    )
    importer = DashboardModel(
        dashboard_id=IDS["ops"],
        user_id="alice",
        title="Ops dashboard",
        items=[
            DashboardItem(
                instance_id="ref",
                component_id=REFERENCE_COMPONENT,
                config={"dashboard": IDS["exec"], "node": "chart"},
            )
        ],
    )
    return MemoryDashboardStore({IDS["exec"]: source, IDS["ops"]: importer})


def _app(store, editors):
    def app():
        args = pn.state.session_args
        dashboard = args.get("dashboard", [b"ops"])[0].decode()
        denied = args.get("user", [b"alice"])[0].decode() != "alice"
        editor = FlowDash(
            COMPONENTS,
            store=store,
            user="alice",
            dashboard=IDS[dashboard],
            mode="dashboard",
            notifications=False,
            authorize_dashboard=(lambda model, access: model.dashboard_id != IDS["exec"])
            if denied
            else None,
        )
        editors[dashboard] = editor
        return editor

    return app


def _open(context, port, query):
    tab = context.new_page()
    tab.goto(f"http://127.0.0.1:{port}/?{query}")
    tab.wait_for_function(
        "() => window.Bokeh?.documents?.length > 0 && Bokeh.documents.every((d) => d.is_idle)"
    )
    return tab


def test_a_chart_shown_through_a_reference_follows_its_dashboard(page):
    store = _store()
    editors = {}
    _, port = serve_component(page, _app(store, editors), suffix="/?dashboard=ops")

    tile = page.locator(".flowdash-tile")
    expect(tile.get_by_text("Total 12 in steelblue")).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(page.locator(".flowdash-provenance-chip")).to_contain_text("From Exec dashboard")
    wait_until(lambda: page.evaluate(CHARTS) == 1, page, timeout=10000)

    # The source open in another tab, live at the same time as the reference.
    source = _open(page.context, port, "dashboard=exec")
    expect(source.get_by_text("Total 12 in steelblue")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    wait_until(lambda: source.evaluate(CHARTS) == 1, source, timeout=10000)

    source.get_by_role("combobox", name="Color").click()
    select_option(source, "seagreen").click()
    expect(source.get_by_text("Total 12 in seagreen")).to_be_visible()
    # Unsaved, so the reference still shows what is saved.
    expect(tile.get_by_text("Total 12 in steelblue")).to_be_visible()
    source.get_by_role("button", name="Save", exact=True).click()
    wait_until(lambda: not editors["exec"].dirty, source)

    expect(tile.get_by_text("Total 12 in seagreen")).to_be_visible(timeout=10000)
    wait_until(lambda: page.evaluate(CHARTS) == 1, page, timeout=10000)

    # The source dashboard is deleted, e.g. from the launcher of a third session.
    store.delete_dashboard("alice", IDS["exec"])
    expect(tile.get_by_text("Missing node: chart").first).to_be_visible(timeout=10000)
    expect(page.locator(".flowdash-provenance-chip")).to_contain_text("Missing node: chart")
    source.close()


def test_a_viewer_who_may_not_open_the_source_sees_no_access(page):
    store = _store()
    serve_component(page, _app(store, {}), suffix="/?dashboard=ops&user=bob")

    tile = page.locator(".flowdash-tile")
    expect(tile.get_by_text("No access").first).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    expect(tile.get_by_text("Total 12")).to_have_count(0)


def test_two_editors_on_one_page_show_the_chart_at_once(page):
    page.set_viewport_size({"width": 1600, "height": 900})
    store = _store()
    editors = {}

    def app():
        for dashboard in ("exec", "ops"):
            editors[dashboard] = FlowDash(
                COMPONENTS,
                store=store,
                user="alice",
                dashboard=IDS[dashboard],
                mode="dashboard",
                notifications=False,
            )
        return pn.Row(editors["exec"], editors["ops"], sizing_mode="stretch_width")

    serve_component(page, app)
    source, importer = page.locator(".flowdash-tile").filter(has_text="Total 12").all()
    expect(source).to_contain_text("Total 12 in steelblue")
    expect(importer).to_contain_text("Total 12 in steelblue")
    wait_until(lambda: page.evaluate(CHARTS) == 2, page, timeout=10000)
    # One view each: the reference renders its own, so the source's tile keeps its chart.
    assert editors["ops"]._tile_objects[0] is not editors["exec"]._tile_objects[1]
