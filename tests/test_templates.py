"""Dashboard templates: the file format, loading, binding slots, the create dialog and the agent's tools."""

import asyncio
import sys
import textwrap

import pandas as pd
import panel as pn
import param
import pytest
import yaml
from panel.viewable import Viewer

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.admin.sections import GENERAL_GROUPS
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.app import FlowDashApp
from panel_flowdash.templates import (
    DataBinding,
    TemplateError,
    check_bindings,
    check_bound_text,
    column_fits,
    fill_template,
    find_templates,
    load_template_dir,
    offered_templates,
    package_templates,
    parse_template,
    slot_choices,
    template_model,
    to_dashboard_dict,
)
from tests.test_navigation import FakeLocation

FRAME = pd.DataFrame(
    {
        "day": pd.date_range("2026-01-01", periods=4),
        "region": ["north", "south", "north", "east"],
        "revenue": [10.0, 20.0, 30.0, 40.0],
    }
)


@register(
    page=False,
    component=True,
    title="Sales",
    tags=["source"],
    provides=[{"key": "sales", "type": "DataFrame"}],
    palette_presets=lambda context: [{"title": "Sales (EU)", "config": {"market": "eu"}}],
)
class Sales(Viewer):
    market = param.String(default="all")

    @param.output(param.DataFrame)
    def sales(self):
        return FRAME

    def __panel__(self):
        return pn.pane.Markdown("sales")


@register(page=False, component=True, title="Rows", tags=["view"])
class Rows(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown("rows")


@register(page=False, component=True, title="Secret", tags=["source"])
class Secret(Viewer):
    @param.output(param.DataFrame)
    def secret(self):
        return FRAME

    def __panel__(self):
        return pn.pane.Markdown("secret")


@register(page=False, component=True, title="Fleet", tags=["source"])
class Fleet(Viewer):
    """A source whose first output is a list, not a table."""

    @param.output(param.List)
    def region_names(self):
        return ["north"]

    @param.output(param.DataFrame)
    def telemetry(self):
        return FRAME

    def __panel__(self):
        return pn.pane.Markdown("fleet")


@register(page=False, component=True, title="Indicator", tags=["view", "kpi"])
class Indicator(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown("kpi")


@register(page=False, component=True, title="Status", tags=["view"])
class Status(Viewer):
    checked = param.Parameter(default=None)

    def __panel__(self):
        return pn.pane.Markdown("status")


COMPONENTS = {
    "Data/sales": Sales,
    "Data/secret": Secret,
    "Data/fleet": Fleet,
    "Views/rows": Rows,
    "Views/indicator": Indicator,
    "Views/status": Status,
}


def _editor(**params):
    params.setdefault("authorize_component", lambda entry: entry.app_id != "Data/secret")
    editor = FlowDash(components=COMPONENTS, store=MemoryDashboardStore(), **params)
    editor.ensure_components_loaded()
    return editor


TEMPLATE = """
flowdash: 1
title: Sales
settings:
  description: Revenue by [[category]]
x-template:
  name: Sales overview
  description: A KPI and a table.
  slots:
    data: {kind: data, label: Data}
    rows: {kind: view, of: data, label: Table}
    measure: {kind: column, of: data, type: number, label: Measure}
    category: {kind: column, of: data, type: category, label: Category, required: false}
nodes:
  data:
    component: flowdash:slot
  total:
    component: Widgets/KPI
    config: {label: "Total [[measure]]"}
  note:
    component: Text/Markdown
    config: {text: "By **[[category]]**"}
  rows:
    component: flowdash:slot
edges:
  - from: data.table
    to: total.value
    transform: [{op: column, column: "[[measure]]"}, {op: aggregate, how: sum}]
  - from: data.table
    to: rows.table
filters:
  - column: "[[category]]"
    widget: select
layout:
  tiles:
    - {node: total, width: 50, height: 130}
    - {node: note, width: 50, height: 130}
    - {node: rows, width: 100, height: 400}
    - {node: data, visible: false}
"""


def _template():
    return parse_template(TEMPLATE, template_id="project:sales")


class TestFormat:
    def test_package_templates_load_in_order(self):
        templates = package_templates()
        assert [t.id for t in templates] == [
            "package:kpi-overview",
            "package:trend-and-breakdown",
            "package:table-explorer",
        ]
        assert [t.name for t in templates] == [
            "KPI overview",
            "Trend and breakdown",
            "Table explorer with filters",
        ]

    @pytest.mark.parametrize("template", package_templates(), ids=lambda t: t.id)
    def test_package_templates_fill_in_without_bindings(self, template):
        doc = fill_template(template, {})
        assert to_dashboard_dict(doc)["items"]
        components = {node["component"] for node in doc["nodes"].values()}
        assert "flowdash:slot" not in components

    def test_a_template_is_a_dashboard_file(self):
        from panel_flowdash.ui.launcher import parse_dashboard_file

        model = parse_dashboard_file(TEMPLATE, "alice")
        assert model.title == "Sales"

    @pytest.mark.parametrize(
        ("change", "path"),
        [
            (lambda t: t.replace("x-template:", "x-other:"), "x-template"),
            (lambda t: t.replace("name: Sales overview", "name: ''"), "x-template.name"),
            (
                lambda t: t.replace("description: A KPI", "author: x\n  description: A KPI"),
                "x-template.author",
            ),
            (
                lambda t: t.replace("kind: view, of: data", "kind: chart, of: data"),
                "x-template.slots.rows.kind",
            ),
            (lambda t: t.replace("type: number", "type: money"), "x-template.slots.measure.type"),
            (
                lambda t: t.replace("of: data, type: number", "of: rows, type: number"),
                "x-template.slots.measure.of",
            ),
            (lambda t: t.replace("Total [[measure]]", "Total [[nope]]"), "document"),
            (
                lambda t: t.replace(
                    "  rows:\n    component: flowdash:slot", "  rows:\n    component: Views/rows"
                ),
                "x-template.slots.rows",
            ),
            (
                lambda t: t.replace(
                    "  total:\n", "  stray:\n    component: flowdash:slot\n  total:\n"
                ),
                "nodes.stray.component",
            ),
            (lambda t: t.replace("flowdash: 1", "flowdash: 1\nextra: !!binary aGk="), "document"),
            (
                lambda t: t.replace(
                    "width: 50, height: 130}\n    - {node: note",
                    "width: 50, height: 130}\n    - {node: ghost",
                ),
                "layout.tiles[1].node",
            ),
        ],
    )
    def test_malformed_templates_are_refused_whole(self, change, path):
        with pytest.raises(TemplateError) as info:
            parse_template(change(TEMPLATE), template_id="project:bad")
        assert info.value.path == path

    def test_a_folder_skips_files_that_are_not_templates(self, tmp_path, caplog):
        (tmp_path / "good.yaml").write_text(TEMPLATE)
        (tmp_path / "bad.yaml").write_text(TEMPLATE.replace("kind: data", "kind: sql"))
        (tmp_path / "notes.txt").write_text("not read")
        (tmp_path / "dashboard.yml").write_text("flowdash: 1\ntitle: plain dashboard\n")
        templates = load_template_dir(tmp_path)
        assert [t.id for t in templates] == ["project:good"]
        assert "bad.yaml" in caplog.text and "dashboard.yml" in caplog.text

    def test_find_templates_adds_the_project_folder(self, tmp_path):
        (tmp_path / "templates").mkdir()
        (tmp_path / "templates" / "sales.yaml").write_text(TEMPLATE)
        ids = [t.id for t in find_templates(tmp_path)]
        assert ids[-1] == "project:sales" and ids[0] == "package:kpi-overview"
        assert [t.id for t in find_templates(tmp_path / "missing")] == ids[:-1]


class TestFill:
    def test_bound_slots_fill_nodes_edges_filters_and_text(self):
        doc = fill_template(
            _template(),
            {
                "data": DataBinding("Data/sales", {"market": "eu"}, "sales"),
                "rows": DataBinding("Views/rows", {}, "table"),
                "measure": "revenue",
                "category": "region",
            },
        )
        assert doc["nodes"]["data"] == {"component": "Data/sales", "config": {"market": "eu"}}
        assert doc["nodes"]["rows"]["component"] == "Views/rows"
        assert doc["nodes"]["total"]["config"] == {"label": "Total revenue"}
        assert doc["edges"][0] == {
            "from": "data.sales",
            "to": "total.value",
            "transform": [
                {"op": "column", "column": "revenue"},
                {"op": "aggregate", "how": "sum"},
            ],
        }
        assert doc["edges"][1] == {"from": "data.sales", "to": "rows.table"}
        assert doc["filters"] == [{"column": "region", "widget": "select"}]
        assert doc["settings"]["description"] == "Revenue by region"

    def test_unbound_slots_leave_placeholder_tiles_in_place(self):
        doc = fill_template(_template(), {"data": DataBinding("Data/sales", {}, "sales")})
        nodes = doc["nodes"]
        assert nodes["data"]["component"] == "Data/sales"
        for name, missing in (("total", "Measure"), ("note", "Category"), ("rows", "Table")):
            assert nodes[name]["component"] == "Text/Markdown"
            assert f"**{missing}**" in nodes[name]["config"]["text"]
        assert doc["edges"] == []
        assert doc["filters"] == []
        assert doc["settings"]["description"] == "Revenue by "
        assert [t["node"] for t in doc["layout"]["tiles"]] == ["total", "note", "rows", "data"]

    def test_unbound_data_reaches_every_node_it_feeds(self):
        doc = fill_template(
            _template(), {"rows": DataBinding("Views/rows", {}, "table"), "measure": "revenue"}
        )
        text = doc["nodes"]["rows"]["config"]["text"]
        assert "**Data**" in text and "**Table**" not in text
        assert "**Data** and **Measure**" not in doc["nodes"]["total"]["config"]["text"]

    def test_column_names_are_escaped_in_text_components(self):
        doc = fill_template(
            _template(),
            {"data": DataBinding("Data/sales", {}, "sales"), "category": "<b>x</b> *y*"},
        )
        text = doc["nodes"]["note"]["config"]["text"]
        assert "<b>" not in text and "&lt;b&gt;" in text and "\\*y\\*" in text

    @pytest.mark.parametrize("value", ["{{ value }}", "[[measure]]", "a\nb", "x" * 201, "", 3])
    def test_bound_text_that_could_inject_is_refused(self, value):
        with pytest.raises(TemplateError):
            check_bound_text(value, "bindings.x")

    def test_bindings_must_name_slots_of_the_right_kind(self):
        with pytest.raises(TemplateError, match="names no slot"):
            fill_template(_template(), {"nope": "x"})
        with pytest.raises(TemplateError, match="bound to a component"):
            fill_template(_template(), {"data": "Data/sales"})

    def test_column_fits(self):
        assert column_fits("float64", "number") and not column_fits("object", "number")
        assert column_fits("datetime64[ns]", "date") and column_fits("int64", "date", "p_year")
        assert not column_fits("int64", "date", "case_id")
        assert column_fits("object", "category") and not column_fits("float64", "category")
        assert not column_fits("int64", "category", "case_id")
        assert not column_fits("datetime64[ns]", "category")
        assert column_fits("float64", "any")


class TestBinding:
    def test_choices_are_components_the_user_may_place_with_presets(self):
        editor = _editor()
        template = _template()
        data = {c.key: c for c in slot_choices(editor, template.slots["data"])}
        assert "Data/secret" not in data
        assert data["Data/sales"].binding == DataBinding("Data/sales", {}, "sales", "Sales")
        preset = next(c for c in data.values() if c.label == "Sales (EU)")
        assert preset.binding.config == {"market": "eu"}
        views = [c.key for c in slot_choices(editor, template.slots["rows"], preset.binding)]
        assert views == ["Views/rows"]

    def test_check_bindings_resolves_keys_and_checks_columns(self):
        editor = _editor()
        template = _template()
        choices = {
            "data": slot_choices(editor, template.slots["data"]),
            "rows": slot_choices(editor, template.slots["rows"]),
        }
        columns = {"data": {"day": "datetime64[ns]", "region": "object", "revenue": "float64"}}
        resolved = check_bindings(
            template, {"data": "Data/sales", "measure": "revenue"}, choices, columns
        )
        assert resolved["data"].component == "Data/sales" and resolved["measure"] == "revenue"
        with pytest.raises(TemplateError, match="'Data/secret' is not offered"):
            check_bindings(template, {"data": "Data/secret"}, choices, columns)
        with pytest.raises(TemplateError, match="the columns are day, region, revenue"):
            check_bindings(template, {"measure": "profit"}, choices, columns)
        with pytest.raises(TemplateError, match="does not suit a number column"):
            check_bindings(template, {"measure": "region"}, choices, columns)

    def test_templates_whose_components_are_disabled_are_not_offered(self):
        templates = [_template()]
        assert offered_templates(_editor(), templates) == templates
        editor = _editor(authorize_component=lambda entry: entry.app_id != "Widgets/KPI")
        assert offered_templates(editor, templates) == []

    def test_model_keeps_only_what_the_session_may_use(self):
        editor = _editor()
        template = _template()
        model = template_model(
            template, {"data": DataBinding("Data/secret", {}, "secret")}, "alice", "Mine"
        )
        assert model.title == "Mine" and model.user_id == "alice"
        editor._guard_restricted(model, None)
        assert "data" not in {item.instance_id for item in model.items}


class TestAgent:
    def test_tools_are_in_the_assistant_set_only(self):
        pytest.importorskip("pydantic_ai")
        from panel_flowdash.agent.assistant import EXPLORE_TOOLS
        from panel_flowdash.agent.chrome import TOOL_LABELS
        from panel_flowdash.agent.tools import TOOLS

        names = [tool.__name__ for tool in TOOLS]
        for name in ("list_templates", "new_dashboard_from_template"):
            assert name in names and name not in EXPLORE_TOOLS and name in TOOL_LABELS
        label = TOOL_LABELS["new_dashboard_from_template"]({"template": "package:kpi-overview"})
        assert label == "Start a dashboard from the kpi overview template"

    async def test_list_templates_offers_choices(self):
        ops = DashboardOps(_editor())
        listed = {t["id"]: t for t in await ops.list_templates_async()}
        slots = listed["package:kpi-overview"]["slots"]
        assert set(slots["data"]["choices"]) >= {"Data/sales"}
        assert "Data/secret" not in slots["data"]["choices"]
        assert slots["trend"]["choices"] == {"Views/rows": "Rows"}
        assert slots["measure"] == {
            "kind": "column",
            "label": "Measure",
            "description": "The number the headline figures add up, e.g. revenue.",
            "of": "data",
            "type": "number",
            "required": True,
        }

    async def test_fills_an_empty_canvas_and_checks_columns(self):
        editor = _editor()
        ops = DashboardOps(editor)
        with pytest.raises(OperationError, match="'profit' is not a column"):
            await ops.new_dashboard_from_template_async(
                "package:kpi-overview", {"data": "Data/sales", "measure": "profit"}
            )
        message = await ops.new_dashboard_from_template_async(
            "package:kpi-overview",
            {"data": "Data/sales", "measure": "revenue", "trend": "Views/rows"},
        )
        assert message.startswith("Filled in the template KPI overview.")
        assert "breakdown, rows" in message and "kpi_categories" in message
        names = {node["name"] for node in ops.nodes()}
        assert {"data", "kpi_total", "trend"} <= names
        preview = ops.preview()["nodes"]
        assert preview["kpi_total"]["outputs"] == {} or preview["kpi_total"]["status"] == "ok"
        with pytest.raises(OperationError, match="not empty"):
            await ops.new_dashboard_from_template_async("package:kpi-overview", {})

    async def test_unknown_template(self):
        with pytest.raises(OperationError, match="No template 'nope'"):
            await DashboardOps(_editor()).new_dashboard_from_template_async("nope", {})


def _project(tmp_path):
    section = tmp_path / "Data"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "sales.py").write_text(
        textwrap.dedent(
            """
            import pandas as pd
            import panel as pn
            import param
            from panel.viewable import Viewer
            from panel_flowdash import register

            @register(page=False, component=True, title="Sales", tags=["source"],
                      provides=[{"key": "sales", "type": "DataFrame"}])
            class app(Viewer):
                @param.output(param.DataFrame)
                def sales(self):
                    return pd.DataFrame({"region": ["north", "south"], "revenue": [1.0, 2.0]})

                def __panel__(self):
                    return pn.pane.Markdown("sales")
            """
        )
    )
    views = tmp_path / "Views"
    views.mkdir()
    (views / "__init__.py").write_text("")
    (views / "rows.py").write_text(
        textwrap.dedent(
            """
            import panel as pn
            import param
            from panel.viewable import Viewer
            from panel_flowdash import register

            @register(page=False, component=True, title="Rows", tags=["view"])
            class app(Viewer):
                table = param.DataFrame(default=None, allow_None=True)

                def __panel__(self):
                    return pn.pane.Markdown("rows")
            """
        )
    )


@pytest.fixture
async def app(tmp_path):
    _project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    try:
        instance = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
        await instance._ensure_components_loaded()
        instance._notified = []
        instance._notify = lambda *args, **kwargs: instance._notified.append(args)
        yield instance
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))
        for name in [m for m in sys.modules if m.split(".")[0] in ("Data", "Views")]:
            del sys.modules[name]


class TestDialog:
    async def test_blank_stays_the_plain_create(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        assert picker.visible and picker.selected == ""
        assert app._dialog_confirm_button.label == "Create"
        assert [i["label"] for i in picker._list.items] == [
            "Blank",
            "KPI overview",
            "Trend and breakdown",
            "Table explorer with filters",
        ]

    async def test_templates_can_be_turned_off(self, app):
        app.templates = False
        app._open_create_dialog()
        assert not app._template_picker.visible
        assert app._offered_templates() == []

    async def test_project_templates_are_offered(self, app, tmp_path):
        (tmp_path / "templates").mkdir()
        (tmp_path / "templates" / "sales.yaml").write_text(TEMPLATE)
        (tmp_path / "templates" / "broken.yaml").write_text("flowdash: 1\nx-template: 3\n")
        assert [t.id for t in app._offered_templates()][-1] == "project:sales"

    async def test_choose_bind_and_create(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        picker._list.index = 1
        assert picker.selected == "package:kpi-overview"
        assert app._dialog_name_input.value == "KPI overview"
        assert app._dialog_confirm_button.label == "Next"
        await picker.start_binding()
        assert picker.step == "bind" and not picker.valid
        assert app._dialog_back_button.visible and app._dialog_confirm_button.disabled
        fields = picker._fields
        assert fields["measure"].disabled
        fields["data"].value = "Data/sales"
        for _ in range(100):
            if len(fields["measure"].options) > 1:
                break
            await asyncio.sleep(0.05)
        assert list(fields["measure"].options.values()) == ["", "revenue"]
        assert list(fields["category"].options.values()) == ["", "region"]
        assert fields["category"].value == ""
        assert picker.valid and not app._dialog_confirm_button.disabled
        fields["measure"].value = "revenue"
        fields["trend"].value = "Views/rows"
        assert app._create_from_template("Revenue")
        model = next(m for m in app.store._all_dashboards() if m.title == "Revenue")
        components = {item.instance_id: item.component_id for item in model.items}
        assert components["data"] == "Data/sales" and components["trend"] == "Views/rows"
        assert components["breakdown"] == "Text/Markdown"

    async def test_back_returns_to_the_list(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        picker._list.index = 3
        await picker.start_binding()
        picker.back()
        assert picker.step == "choose" and app._dialog_name_input.visible
        assert app._dialog_confirm_button.label == "Next"


class TestDialogRobustness:
    async def test_reopening_after_leaving_the_slot_step(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        picker._list.index = 1
        await picker.start_binding()
        app._dialog.open = False
        app._open_create_dialog()
        assert app._dialog.open and picker.step == "choose" and picker.selected == ""
        assert app._dialog.title == "Create dashboard"
        assert app._dialog_confirm_button.label == "Create"

    async def test_turning_templates_off_is_rechecked_at_create(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        picker._list.index = 3
        await picker.start_binding()
        app.templates = False
        assert not app._create_from_template("Late")
        assert not app.store._all_dashboards()

    async def test_columns_of_earlier_data_never_count(self, app):
        app._open_create_dialog()
        picker = app._template_picker
        picker._list.index = 1
        await picker.start_binding()
        picker._loading["data"] = "Data/sales"
        picker._fields["data"].value = "Data/sales"
        with pytest.raises(TemplateError, match="still being read"):
            picker.bindings()


async def test_the_dialog_refuses_views_that_cannot_show_their_data(app, monkeypatch):
    from panel_flowdash.agent.ops import DashboardOps as Ops

    async def problems(self, doc, names, timeout=30):
        return {"trend": "KeyError: 'watts_per_person'"}

    monkeypatch.setattr(Ops, "view_problems_async", problems)
    app._open_create_dialog()
    picker = app._template_picker
    picker._list.index = 1
    await picker.start_binding()
    picker._fields["data"].value = "Data/sales"
    for _ in range(100):
        if picker._read_for.get("data") == "Data/sales":
            break
        await asyncio.sleep(0.05)
    picker._fields["trend"].value = "Views/rows"
    await app._confirm_template("Refused")
    assert not app.store._all_dashboards()
    assert app._dialog.open
    assert any("watts_per_person" in str(args) for args in app._notified), app._notified


def test_admin_switch():
    settings = {s.key: s for group in GENERAL_GROUPS for s in group.settings}
    assert settings["templates"].target == "templates"
    assert settings["templates"].default is True
    assert FlowDashApp.param.templates.default is True


def test_template_yaml_is_plain_data():
    for template in package_templates():
        yaml.safe_dump(template.document)


class TestReviewFixes:
    def test_data_binds_its_table_output_and_views_need_a_table_input(self):
        editor = _editor()
        template = _template()
        data = {c.key: c.binding for c in slot_choices(editor, template.slots["data"])}
        assert data["Data/fleet"].port == "telemetry"
        views = [c.key for c in slot_choices(editor, template.slots["rows"], data["Data/fleet"])]
        assert views == ["Views/rows"]

    def test_a_link_out_of_the_folder_is_not_loaded(self, tmp_path):
        outside = tmp_path / "outside.yaml"
        outside.write_text(TEMPLATE)
        folder = tmp_path / "templates"
        folder.mkdir()
        (folder / "linked.yaml").symlink_to(outside)
        (folder / "inside.yaml").write_text(TEMPLATE)
        (folder / "alias.yaml").symlink_to(folder / "inside.yaml")
        assert sorted(t.id for t in load_template_dir(folder)) == [
            "project:alias",
            "project:inside",
        ]

    def test_size_is_checked_before_reading(self, tmp_path, monkeypatch):
        from pathlib import Path

        from panel_flowdash import templates

        big = tmp_path / "big.yaml"
        with big.open("wb") as f:
            f.truncate(templates.MAX_FILE_SIZE + 1)
        read = []
        monkeypatch.setattr(Path, "read_bytes", lambda self: read.append(self) or b"")
        assert load_template_dir(tmp_path) == []
        assert read == []

    def test_preview_survives_brackets_in_labels(self):
        from panel_flowdash.ui.template_picker import preview_html

        template = parse_template(
            TEMPLATE.replace("label: Measure}", "label: 'Measure ]] {{x}} <b>'}"),
            template_id="project:x",
        )
        assert "thumb" in preview_html(template, {})

    def test_filled_text_is_never_read_again(self):
        text = """
flowdash: 1
title: "[[a]][x]] [[b]]"
settings: {description: "[[a]][x]]"}
x-template:
  name: Second
  slots:
    a: {kind: text, label: A}
    b: {kind: text, label: B}
    x: {kind: text, label: X}
nodes: {}
"""
        template = parse_template(text, template_id="project:x")
        doc = fill_template(template, {"a": "[", "b": "B"})
        assert doc["title"] == "[[x]] B"
        assert doc["settings"]["description"] == "[[x]]"

    def test_labels_read_as_words(self):
        doc = fill_template(
            parse_template(
                TEMPLATE.replace("Total [[measure]]", "Total [[measure:label]]"),
                template_id="project:x",
            ),
            {"data": DataBinding("Data/sales", {}, "sales"), "measure": "net_revenue-eur"},
        )
        assert doc["nodes"]["total"]["config"]["label"] == "Total Net revenue eur"

    def test_kpi_overview_counts_rows_and_ranks_the_breakdown(self):
        template = package_templates()[0]
        doc = fill_template(
            template,
            {
                "data": DataBinding("Data/sales", {}, "sales"),
                "breakdown": DataBinding("Views/rows", {}, "table"),
                "measure": "revenue",
                "category": "region",
            },
        )
        edges = {edge["to"]: edge.get("transform") for edge in doc["edges"]}
        assert edges["kpi_rows.value"][-1] == {"op": "aggregate", "how": "size"}
        assert edges["breakdown.table"] == [
            {"op": "group", "by": "region", "column": "revenue", "how": "sum", "top": 15}
        ]
        assert doc["nodes"]["kpi_total"]["config"]["label"] == "Total Revenue"

    def test_title_is_one_short_line(self):
        from panel_flowdash.templates import check_title

        assert check_title(" Sales\n\tby region ", "Default") == "Sales by region"
        assert check_title("  ", "Default") == "Default"
        with pytest.raises(TemplateError):
            check_title("x" * 5000, "Default")


class TestAgentRules:
    @pytest.fixture(autouse=True)
    def _agent(self):
        pytest.importorskip("pydantic_ai")

    def test_creating_needs_approval(self):
        from panel_flowdash.agent.tools import approval_policy

        ops = DashboardOps(_editor())
        for policy in (
            approval_policy(),
            approval_policy(sensitive_components=["Data/*"], ops=ops),
        ):
            assert policy.requires_approval(
                "new_dashboard_from_template", {"template": "package:kpi-overview", "bindings": {}}
            )

    async def test_one_dashboard_per_request_and_the_pin_moves(self):
        from types import SimpleNamespace

        from panel_flowdash.agent.tools import FlowDashDeps, new_dashboard_from_template

        editor = _editor()
        deps = FlowDashDeps(DashboardOps(editor))
        deps.start_request("make one")
        ctx = SimpleNamespace(deps=deps, run_id="r1")
        first = await new_dashboard_from_template(
            ctx, "package:table-explorer", {"data": "Data/sales"}
        )
        assert "Filled in the template" in str(first)
        assert deps.check_pin("r1") is None
        second = await new_dashboard_from_template(
            ctx, "package:table-explorer", {"data": "Data/sales"}
        )
        assert str(second).startswith("Refused: a request makes at most one")
        deps.end_request()
        deps.start_request("again")
        assert deps._templated is False

    async def test_viewers_are_refused(self):
        editor = _editor()
        editor.read_only = True
        with pytest.raises(PermissionError):
            await DashboardOps(editor).new_dashboard_from_template_async(
                "package:table-explorer", {"data": "Data/sales"}
            )

    async def test_long_titles_and_unreadable_columns_are_refused(self, monkeypatch):
        from panel_flowdash import templates

        ops = DashboardOps(_editor())
        with pytest.raises(OperationError, match="title"):
            await ops.new_dashboard_from_template_async(
                "package:table-explorer", {"data": "Data/sales"}, "x" * 5000
            )

        async def unreadable(*args, **kwargs):
            return None

        monkeypatch.setattr(templates, "data_columns", unreadable)
        with pytest.raises(OperationError, match="could not be read"):
            await ops.new_dashboard_from_template_async(
                "package:table-explorer", {"data": "Data/sales", "filter_1": "region"}
            )
