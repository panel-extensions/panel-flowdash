"""Every shipped template, filled in on the wind example and on the penguins data, shows every tile."""

import importlib.util
import sys
from pathlib import Path

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps
from panel_flowdash.templates import PLACEHOLDER_COMPONENT

EXAMPLE = Path(__file__).parents[1] / "examples" / "wind_energy"
PENGUINS = Path(__file__).parent / "data" / "penguins.csv"

pytestmark = pytest.mark.filterwarnings("ignore:The inplace keyword in DataFrame.reset_index")

WIND = {
    "package:kpi-overview": {
        "data": "Sources/turbines",
        "measure": "t_cap",
        "category": "t_state",
        "trend": "Views/capacity_timeline",
        "breakdown": "Views/data_table",
        "rows": "Views/data_table",
    },
    "package:trend-and-breakdown": {
        "data": "Sources/turbines",
        "date": "p_year",
        "category": "t_state",
        "measure": "t_cap",
        "trend": "Views/capacity_timeline",
        "breakdown": "Views/data_table",
        "detail": "Views/turbine_map",
    },
    "package:table-explorer": {
        "data": "Sources/turbines",
        "table": "Views/data_table",
        "filter_1": "t_state",
        "filter_2": "t_manu",
        "filter_range": "p_year",
    },
}

PENGUIN_BINDINGS = {
    "package:kpi-overview": {
        "data": "Data/penguins",
        "measure": "body_mass_g",
        "category": "species",
        "trend": "Views/line",
        "breakdown": "Views/bars",
        "rows": "Views/table",
    },
    "package:trend-and-breakdown": {
        "data": "Data/penguins",
        "date": "year",
        "category": "island",
        "measure": "flipper_length_mm",
        "trend": "Views/line",
        "breakdown": "Views/bars",
        "detail": "Views/table",
    },
    "package:table-explorer": {
        "data": "Data/penguins",
        "table": "Views/table",
        "filter_1": "species",
        "filter_2": "sex",
        "filter_range": "year",
    },
}


@register(page=False, component=True, title="Penguins", tags=["source"])
class Penguins(Viewer):
    @param.output(param.DataFrame)
    def penguins(self):
        return pd.read_csv(PENGUINS)

    def __panel__(self):
        return pn.pane.Markdown("penguins")


@register(page=False, component=True, title="Table", tags=["view"])
class Table(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.widgets.Tabulator(self.param.table, disabled=True)


@register(page=False, component=True, title="Line", tags=["view"])
class Line(Viewer):
    """The first numeric column, as the text a chart would draw."""

    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(self._text, self.param.table))

    @staticmethod
    def _text(table):
        if table is None:
            return "No data"
        column = table.select_dtypes("number").columns[0]
        return f"{column}: {table[column].min()} to {table[column].max()}"


@register(page=False, component=True, title="Bars", tags=["view"])
class Bars(Viewer):
    """The first column's values against the second's, as the text a bar chart would draw."""

    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(self._text, self.param.table))

    @staticmethod
    def _text(table):
        if table is None:
            return "No data"
        label, value = table.columns[:2]
        return "\n".join(f"- {row[label]}: {row[value]:,.0f}" for _, row in table.iterrows())


@pytest.fixture
def wind(tmp_path, monkeypatch):
    if not (EXAMPLE / "_data" / "turbines.csv.gz").exists():
        pytest.skip("Example data not available.")
    monkeypatch.setenv("WIND_ENERGY_DB", str(tmp_path / "dashboards.db"))
    monkeypatch.setattr(sys, "path", [str(EXAMPLE), *sys.path])
    spec = importlib.util.spec_from_file_location("wind_energy_init", EXAMPLE / "__init__.py")
    project = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(project)
    editor = FlowDash(str(EXAMPLE), store=MemoryDashboardStore())
    yield editor
    editor.dispose()
    project.store.close()
    for name in list(sys.modules):
        if name.split(".")[0] in {"Sources", "Controls", "Transforms", "Views", "Explore"}:
            del sys.modules[name]


@pytest.fixture
def penguins():
    editor = FlowDash(
        components={
            "Data/penguins": Penguins,
            "Views/table": Table,
            "Views/line": Line,
            "Views/bars": Bars,
        },
        store=MemoryDashboardStore(),
    )
    yield editor
    editor.dispose()


async def _check(editor, template_id, bindings):
    ops = DashboardOps(editor)
    message = await ops.new_dashboard_from_template_async(template_id, bindings)
    assert "still need" not in message and "unbound" not in message, message
    names = [node["name"] for node in ops.nodes()]
    components = {item["instance_id"]: item["component_id"] for item in editor._tile_items}
    assert PLACEHOLDER_COMPONENT not in components.values()
    report = ops.preview()
    assert {name: entry["status"] for name, entry in report["nodes"].items()} == dict.fromkeys(
        names, "ok"
    )
    doc = ops._export()
    from panel_flowdash.agent.yaml_format import dashboard_to_dict

    problems = await ops.view_problems_async(dashboard_to_dict(editor), names)
    assert problems == {}
    graph = editor.graph
    for name, component in components.items():
        if component == "Widgets/KPI":
            value = graph.get_state(name).value
            assert isinstance(value, int | float) and value == value, (name, value)
    return doc


@pytest.mark.parametrize("template_id", list(WIND))
async def test_templates_on_the_wind_data(wind, template_id):
    await _check(wind, template_id, WIND[template_id])


@pytest.mark.parametrize("template_id", list(PENGUIN_BINDINGS))
async def test_templates_on_the_penguins_data(penguins, template_id):
    await _check(penguins, template_id, PENGUIN_BINDINGS[template_id])


async def test_a_view_that_cannot_show_the_grouped_breakdown_is_refused(wind):
    from panel_flowdash.agent import OperationError

    bindings = {**WIND["package:kpi-overview"], "breakdown": "Views/state_ranking"}
    with pytest.raises(OperationError, match=r"Breakdown chart \(Views/state_ranking\)"):
        await DashboardOps(wind).new_dashboard_from_template_async(
            "package:kpi-overview", bindings
        )
