"""A spec edited through a form of curated settings and as JSON, kept in sync both ways."""

import asyncio
import json

import numpy as np
import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import MemoryDashboardStore, SpecSetting, register
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.config_choices import compute_choices, declared_choices, has_choices
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.spec_settings import (
    SpecPathError,
    SpecTextError,
    changed_paths,
    declared_settings,
    delete_path,
    format_spec,
    format_spec_yaml,
    parse_spec,
    parse_spec_yaml,
    problems,
    set_path,
    shown_in_spec,
    text_spans,
    unknown_paths,
)
from panel_flowdash.ui.choice_widgets import plain_widget
from panel_flowdash.ui.spec_field import SpecEditor
from panel_flowdash.ui.status_bar import select_on_canvas

from .test_config_choices import _settle


def _mark_type(spec):
    mark = spec.get("mark")
    return mark.get("type") if isinstance(mark, dict) else mark


def _set_mark_type(spec, value):
    if isinstance(spec.get("mark"), dict):
        spec["mark"]["type"] = value
    else:
        spec["mark"] = value


SETTINGS = [
    SpecSetting(
        "mark.type",
        label="Mark",
        widget="radio",
        options={"Bar": "bar", "Line": "line", "Point": "point"},
        group="Chart",
        get=_mark_type,
        set=_set_mark_type,
        covers=("mark",),
    ),
    SpecSetting("encoding.x.field", label="X", widget="select", choices="columns", of="data"),
    SpecSetting("encoding.y.field", label="Y", widget="select", choices="columns", of="data"),
    SpecSetting(
        "encoding.x.type",
        label="X type",
        widget="select",
        options=["nominal", "ordinal", "quantitative", "temporal"],
    ),
    SpecSetting(
        "encoding.y.type",
        label="Y type",
        widget="select",
        options=["nominal", "ordinal", "quantitative", "temporal"],
    ),
    SpecSetting("title", label="Title", group="Labels"),
    SpecSetting("encoding.x.title", label="X title", group="Labels"),
    SpecSetting("encoding.y.title", label="Y title", group="Labels"),
    SpecSetting("width", label="Width", widget="integer", group="Size"),
]

SPEC_META = {"flowdash": {"widget": "spec", "settings": SETTINGS}}

SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "year", "type": "ordinal", "axis": {"labelAngle": -45}},
        "y": {"field": "capacity", "type": "quantitative"},
    },
    "transform": [{"filter": "datum.year >= 2000"}],
}


def check(config, inputs):
    return problems(config["spec"], SETTINGS)


@register(page=False, component=True, title="Chart", config=["spec"], check_config=check)
class Chart(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    spec = param.Dict(default=SPEC, metadata=SPEC_META)

    def __panel__(self):
        return pn.pane.JSON(self.param.spec)


@register(page=False, component=True, title="Plain chart", config=["spec"])
class PlainChart(Viewer):
    spec = param.Dict(default=SPEC, metadata=SPEC_META)

    def __panel__(self):
        return pn.pane.JSON(self.param.spec)


@register(page=False, component=True, title="JSON", config=["spec"])
class JSONOnly(Viewer):
    spec = param.Dict(default={"a": 1}, metadata={"flowdash": {"widget": "spec"}})

    def __panel__(self):
        return pn.pane.JSON(self.param.spec)


@register(
    page=False, component=True, title="Frame", provides=[{"key": "data", "type": "DataFrame"}]
)
class Frame(Viewer):
    data = param.DataFrame(default=pd.DataFrame({"year": [2000], "capacity": [1.5]}))

    def __panel__(self):
        return pn.pane.Str("frame")


@register(page=False, component=True, title="Query chart", config=["sql", "spec", "limit"])
class QueryChart(Viewer):
    sql = param.String(
        default="SELECT 1", metadata={"flowdash": {"widget": "code", "language": "sql"}}
    )
    spec = param.Dict(default=SPEC, metadata=SPEC_META)
    limit = param.Integer(default=10)

    def __panel__(self):
        return pn.pane.JSON(self.param.spec)


COMPONENTS = {
    "D/chart": Chart,
    "D/plain": PlainChart,
    "D/json": JSONOnly,
    "D/frame": Frame,
    "D/both": QueryChart,
}


def _setting(path):
    return next(s for s in SETTINGS if s.key == path)


class TestPaths:
    def test_set_path_keeps_the_order_and_adds_objects(self):
        spec = {"a": 1, "b": {"c": 2}, "d": 3}
        set_path(spec, "b.c", 5)
        set_path(spec, "e.f", 6)
        assert list(spec) == ["a", "b", "d", "e"] and spec["b"] == {"c": 5}
        assert spec["e"] == {"f": 6}

    def test_set_path_refuses_to_replace_a_value_on_the_way(self):
        spec = {"mark": "line"}
        with pytest.raises(SpecPathError, match='mark is "line"'):
            set_path(spec, "mark.point", True)
        assert spec == {"mark": "line"}

    def test_delete_path_prunes_only_the_objects_it_empties(self):
        spec = {"encoding": {"x": {"title": "Year"}, "y": {"field": "v"}}, "keep": {}}
        delete_path(spec, "encoding.x.title")
        assert spec == {"encoding": {"y": {"field": "v"}}, "keep": {}}
        delete_path(spec, "missing.path")
        assert spec == {"encoding": {"y": {"field": "v"}}, "keep": {}}


class TestSettings:
    def test_round_trip_through_every_setting(self):
        spec = SPEC
        for setting, value in [
            ("mark.type", "line"),
            ("title", "Capacity"),
            ("encoding.x.title", "Year"),
            ("width", 400),
            ("encoding.y.field", "year"),
        ]:
            spec = _setting(setting).write(spec, value)
            assert _setting(setting).read(spec) == value
        assert spec["mark"] == "line"
        assert spec["encoding"]["x"] == {
            "field": "year",
            "type": "ordinal",
            "axis": {"labelAngle": -45},
            "title": "Year",
        }
        assert spec["transform"] == SPEC["transform"]
        assert SPEC["mark"] == "bar", "the spec given is not changed"

    def test_writing_the_default_removes_the_path(self):
        spec = _setting("title").write(SPEC, "Capacity")
        assert spec["title"] == "Capacity"
        assert _setting("title").write(spec, "") == SPEC
        assert list(_setting("title").write(spec, None)) == list(SPEC)

    def test_a_custom_setter_writes_into_an_object_form(self):
        spec = {"mark": {"type": "line", "point": True}}
        assert _setting("mark.type").write(spec, "bar") == {"mark": {"type": "bar", "point": True}}

    def test_unknown_keys_are_reported_outermost_and_in_order(self):
        assert [".".join(p) for p in unknown_paths(SPEC, SETTINGS)] == [
            "encoding.x.axis",
            "transform",
        ]
        assert unknown_paths({"mark": {"type": "bar", "point": True}}, SETTINGS) == []
        shorthand = [SpecSetting("mark.type"), SpecSetting("encoding.x.title")]
        assert unknown_paths({"mark": "bar", "encoding": {"x": {"field": "a"}}}, shorthand) == [
            ("encoding", "x", "field")
        ]

    def test_values_a_control_cannot_show_are_left_to_the_spec(self):
        spec = {"title": {"text": "Capacity", "fontSize": 20}, "width": "container"}
        shown = {s.key for s in shown_in_spec(spec, SETTINGS)}
        assert shown == {"title", "width"}

    def test_problems_name_values_outside_restricted_options(self):
        spec = {"encoding": {"x": {"type": "time"}}}
        assert problems(spec, SETTINGS) == [
            'encoding.x.type is "time"; it must be one of "nominal", "ordinal", '
            '"quantitative", "temporal".'
        ]
        assert problems(SPEC, SETTINGS) == []

    def test_an_unknown_widget_is_refused(self):
        with pytest.raises(ValueError, match="widget 'slider'"):
            SpecSetting("title", widget="slider")

    def test_settings_may_be_declared_as_dicts(self):
        parameter = param.Dict(
            metadata={"flowdash": {"widget": "spec", "settings": [{"path": "title"}]}}
        )
        assert declared_settings(parameter) == [SpecSetting("title")]
        assert declared_settings(JSONOnly.param.spec) == []
        assert declared_settings(param.String(metadata=SPEC_META)) is None
        code = {"flowdash": {"widget": "code", "language": "json"}}
        assert declared_settings(param.Dict(metadata=code)) is None


class TestText:
    @pytest.mark.parametrize(
        "spec", [SPEC, {}, {"a": [], "b": {}, "c": [1, {"d": None}], "é": "ü"}]
    )
    def test_format_matches_json_dumps(self, spec):
        text, _ = format_spec(spec)
        assert text == json.dumps(spec, indent=2, ensure_ascii=False)
        assert parse_spec(text) == spec

    def test_format_maps_paths_to_lines(self):
        text, spans = format_spec(SPEC)
        lines = text.splitlines()
        start, end = spans[("encoding", "x", "axis")]
        assert lines[start].strip().startswith('"axis"') and lines[end].strip() == "}"
        start, end = spans[("transform",)]
        assert lines[start].strip().startswith('"transform"') and lines[end].strip() == "]"

    def test_changed_paths_are_the_innermost_changes(self):
        new = _setting("encoding.x.title").write(SPEC, "Year")
        assert changed_paths(SPEC, new) == [("encoding", "x", "title")]
        assert changed_paths(SPEC, {**SPEC, "title": "T"}) == [("title",)]

    def test_invalid_text_says_where(self):
        with pytest.raises(SpecTextError) as info:
            parse_spec('{\n  "a": 1\n  "b": 2\n}')
        assert (info.value.line, info.value.column) == (3, 3)
        with pytest.raises(SpecTextError, match="JSON object"):
            parse_spec("[1]")
        assert parse_spec("  ") == {}


class TestField:
    def test_the_form_writes_the_spec_and_marks_the_changed_line(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        title = field._controls["title"][1]
        title.value = "Capacity by year"
        assert field.value["title"] == "Capacity by year"
        assert list(field.value)[:3] == ["mark", "encoding", "transform"]
        assert json.loads(field.editor.value) == field.value
        row = field._spans[("title",)][0]
        assert field._marks.changed == [row]

    def test_editing_the_text_updates_the_form(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        field.editor.value = json.dumps({**SPEC, "mark": "point", "title": "T"})
        assert field.value["mark"] == "point"
        assert field._controls["mark.type"][1].value == "Point"
        assert field._controls["title"][1].value == "T"

    def test_unknown_keys_are_flagged_and_kept(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        assert field.unknown == ["encoding.x.axis", "transform"]
        listed = [row[0].object for row in field.unshown[1:]]
        assert "<code>encoding.x.axis</code>" in listed[0] and "1 key" in listed[0]
        assert "<code>transform</code>" in listed[1] and "1 item" in listed[1]
        rows = {a["row"] for a in field.editor.annotations}
        assert rows == {
            field._spans[("encoding", "x", "axis")][0],
            field._spans[("transform",)][0],
        }
        assert set(field._marks.only) >= rows
        field._controls["encoding.x.title"][1].value = "Year"
        assert field.value["transform"] == SPEC["transform"]
        assert field.value["encoding"]["x"]["axis"] == {"labelAngle": -45}

    def test_invalid_text_disables_the_form_and_keeps_the_spec(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        field.editor.value = '{"mark": "line",'
        assert field.error and field.value == SPEC
        assert all(control.disabled for _, control in field._controls.values())
        assert field.editor.annotations[0]["type"] == "error"
        field.editor.value = '{"mark": "line"}'
        assert field.error is None and field.value == {"mark": "line"}
        assert not field._controls["title"][1].disabled

    def test_a_control_whose_value_is_beyond_it_is_disabled(self):
        field = SpecEditor(value={"title": {"text": "T", "fontSize": 20}}, settings=SETTINGS)
        control = field._controls["title"][1]
        assert control.disabled and control.placeholder == "Set in the spec"
        assert field.value == {"title": {"text": "T", "fontSize": 20}}

    def test_a_value_outside_the_options_stays_selectable(self):
        field = SpecEditor(value={"mark": "area"}, settings=SETTINGS)
        control = field._controls["mark.type"][1]
        assert control.value == "area" and "area" in control.options
        control.value = "Bar"
        assert field.value == {"mark": "bar"}

    def test_an_unset_select_says_so_and_can_be_unset_again(self):
        field = SpecEditor(value={"mark": "bar"}, settings=SETTINGS)
        x_type = field._controls["encoding.x.type"][1]
        assert x_type.value == "Not set" and x_type.options[0] == "Not set"
        x_type.value = "temporal"
        assert field.value["encoding"] == {"x": {"type": "temporal"}}
        x_type.value = "Not set"
        assert field.value == {"mark": "bar"}

    def test_an_unset_radio_selects_nothing_it_would_not_write(self):
        field = SpecEditor(value={"layer": []}, settings=SETTINGS)
        mark = field._controls["mark.type"][1]
        assert mark.value == "Not set"
        assert field.value == {"layer": []}

    def test_a_none_option_has_a_label_and_can_be_picked(self):
        settings = [
            SpecSetting(
                "scheme", widget="select", options={"Follow dashboard": None, "Set2": "set2"}
            )
        ]
        field = SpecEditor(value={"scheme": "set2"}, settings=settings)
        control = field._controls["scheme"][1]
        assert control.options == ["Follow dashboard", "Set2"]
        control.value = "Follow dashboard"
        assert field.value == {}

    def test_a_select_whose_value_it_cannot_show_says_so(self):
        settings = [SpecSetting("scheme", widget="select", options={"Follow dashboard": None})]
        field = SpecEditor(value={"scheme": ["#f00", "#0f0"]}, settings=settings)
        control = field._controls["scheme"][1]
        assert control.disabled and control.value == "Set in the spec"
        assert field.value == {"scheme": ["#f00", "#0f0"]}

    def test_a_value_the_control_cannot_show_is_flagged_and_kept(self):
        field = SpecEditor(value={"title": {"text": "T", "fontSize": 20}}, settings=SETTINGS)
        assert field.unknown == ["title"]
        assert "<code>title</code>" in field.unshown[1][0].object

    def test_a_disabled_control_says_why(self):
        settings = [
            SpecSetting(
                "width",
                widget="integer",
                enabled=lambda s: "layer" not in s or "Set in the layers.",
            ),
        ]
        field = SpecEditor(value={"layer": []}, settings=settings)
        assert field._controls["width"][1].disabled
        assert field._notes.visible and field._notes.object == "Set in the layers."
        field.value = {}
        assert not field._controls["width"][1].disabled and not field._notes.visible

    def test_controls_carry_their_label_for_assistive_technology(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        assert field._controls["title"][1].label == "Title"
        assert field._controls["encoding.x.field"][1].label == "X"

    def test_modes_show_the_form_the_text_or_both(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS, mode="form")
        assert field.form.visible and not field.spec.visible
        field.mode_toggle.value = "spec"
        assert field.mode == "spec" and not field.form.visible and field.spec.visible
        assert SpecEditor(value={}, settings=[]).spec.visible

    def test_typed_text_is_unsent_until_applied(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS)
        field.editor.value_input = "{}"
        assert field.unsent
        field.editor.value = "{}"
        assert not field.unsent and field.value == {}

    def test_a_dict_with_settings_gets_a_spec_field(self):
        assert plain_widget(Chart.param.spec, SPEC) == {"type": SpecEditor, "settings": SETTINGS}
        assert plain_widget(JSONOnly.param.spec, {}) == {"type": SpecEditor, "settings": []}


@pytest.fixture
async def editor():
    editor = FlowDash(
        COMPONENTS,
        notifications=False,
        include_builtin_components=False,
        store=MemoryDashboardStore(),
        history_coalesce=0,
    )
    editor.new_dashboard("Charts")
    yield editor
    editor.dispose()


def _field(editor, node_id, run_on_change=False):
    """Select the node, which shows its spec in the code panel, and return the panel's spec editor.

    The spec's text is JSON, and "Run on change" is off unless asked for, so a draft waits for Run.
    """
    panel = editor.code_panel
    panel._run_on_change["spec"] = run_on_change
    if panel.spec_view != "form":
        panel.spec_view = "json"
    panel.spec_text = "json"
    select_on_canvas(editor._flow, node_id=node_id)
    if not editor.code_panel_open:
        assert editor.open_code_panel(node_id)
    assert editor.code_panel.instance_id == node_id
    return editor.code_panel.spec_editor


async def _run(editor):
    editor.code_panel.run()
    await _settle()


class TestChoices:
    def test_settings_offer_the_columns_of_an_input(self):
        spec = build_component_spec(RegistryEntry.from_app(Chart, app_id="D/chart"))
        assert has_choices(spec)
        frame = pd.DataFrame({"year": [2000], "capacity": [1.0]})
        choices = declared_choices(spec, {"data": frame})
        assert choices["spec.encoding.x.field"].values() == ["year", "capacity"]
        assert set(choices) == {"spec.encoding.x.field", "spec.encoding.y.field"}

    async def test_a_hook_offers_choices_for_a_setting(self):
        def hook(config, inputs):
            return {"spec.encoding.x.type": ["ordinal"], "spec.nope": ["x"], "missing": ["y"]}

        entry = RegistryEntry.from_app(PlainChart, app_id="D/plain")
        spec = build_component_spec(entry)
        spec = type(spec)(**{**spec.__dict__, "config_choices": hook})
        choices = await compute_choices(spec, {"spec": SPEC}, {})
        assert choices["spec.encoding.x.type"].values() == ["ordinal"]
        assert "missing" not in choices

    async def test_the_code_panel_passes_the_columns_to_the_form(self, editor):
        frame = editor.add_component("D/frame")
        node = editor.add_component("D/chart")
        assert editor.connect(frame, "data", node, "data") is True
        field = _field(editor, node)
        await _settle()
        field = _field(editor, node)
        x = field._controls["encoding.x.field"][1]
        assert field._labels["encoding.x.field"] == {
            "Not set": None,
            "year (integer)": "year",
            "capacity (number)": "capacity",
        }
        assert x.value == "year (integer)"


class TestCodePanel:
    async def test_the_inspector_offers_the_spec_in_the_panel(self, editor):
        node = editor.add_component("D/plain")
        select_on_canvas(editor._flow, node_id=node)
        body = pn.Column(*editor._inspector._body)
        assert not list(body.select(SpecEditor))
        button = next(b for b in body.select(pmui.Button) if "fd-edit-spec" in b.css_classes)
        button.clicks += 1
        panel = editor.code_panel
        assert (panel.instance_id, panel.field) == (node, "spec")
        assert panel.spec_editor.value == SPEC

    async def test_a_narrow_window_edits_the_spec_in_the_spec_tab(self, editor):
        node = editor.add_component("D/plain")
        editor._split_view.storage.window_width = 800
        select_on_canvas(editor._flow, node_id=node)
        # The Settings tab has no editor of its own: the one editor is the code panel.
        assert not list(pn.Column(*editor._inspector._body).select(SpecEditor))
        assert editor.edit_spec(node)
        panel = editor.code_panel
        assert panel.dock == "sidecar" and (panel.instance_id, panel.field) == (node, "spec")
        assert editor._inspector._spec_column.objects == [panel._layout]

    async def test_form_and_json_are_views_of_one_spec(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        assert panel.view_toggle.visible and panel.view_toggle.value == "form"
        assert panel._spec_form.visible and not panel._spec_text.visible
        field._controls["title"][1].value = "Capacity"
        assert '"title": "Capacity"' in field.editor.value
        panel.view_toggle.value = "json"
        assert not panel._spec_form.visible and panel._spec_text.visible
        field.editor.value = json.dumps({**SPEC, "title": "Typed"})
        assert field._controls["title"][1].value == "Typed"
        assert panel.dirty
        assert editor._component_instances[node].spec == SPEC

    async def test_format_lays_out_the_json(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        assert not panel.format_button.visible
        panel.view_toggle.value = "json"
        assert panel.format_button.visible and panel.spec_tools.can_format
        assert "Shift+Alt+F" in panel.format_button.description
        compact = json.dumps({"title": "T", **SPEC}, separators=(",", ":"))
        field.editor.value_input = compact
        assert panel.dirty
        panel.format()
        expected = format_spec({"title": "T", **SPEC})[0]
        assert field.editor.value == field.editor.value_input == expected
        assert expected.startswith('{\n  "title": "T",\n  "mark": "bar"')
        assert list(field.value) == ["title", *SPEC]
        assert panel.dirty
        assert editor._component_instances[node].spec == SPEC
        await _run(editor)
        assert editor._component_instances[node].spec == {"title": "T", **SPEC}

    async def test_format_of_json_that_does_not_parse(self, editor):
        node = editor.add_component("D/json")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.format('{"a": 1,')
        assert field.error is not None
        assert field.editor.value == '{"a": 1,'
        assert editor._component_instances[node].spec == {"a": 1}

    async def test_format_does_not_run_on_change(self, editor):
        node = editor.add_component("D/json")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.run_on_change = True
        panel.format('{"a":2}')
        assert field.value == {"a": 2}
        await asyncio.sleep(1.0)
        assert editor._component_instances[node].spec == {"a": 1}
        assert panel.dirty

    @pytest.mark.parametrize("move", ["toggle", "narrow"])
    async def test_a_json_draft_survives_a_move(self, editor, move):
        editor._split_view.storage.window_width = 1900
        node = editor.add_component("D/json")
        field = _field(editor, node)
        panel = editor.code_panel
        field.editor.value_input = '{"title": "Draft"}'
        if move == "toggle":
            panel.toggle_dock()
        else:
            editor._split_view.storage.window_width = 900
        assert panel.dock == "sidecar"
        assert field.editor.value == field.editor.value_input
        assert field.value == {"title": "Draft"}
        assert panel.dirty
        assert editor._component_instances[node].spec == {"a": 1}
        await _run(editor)
        assert editor._component_instances[node].spec == {"title": "Draft"}

    async def test_invalid_json_survives_a_move(self, editor):
        node = editor.add_component("D/json")
        field = _field(editor, node)
        panel = editor.code_panel
        field.editor.value_input = '{"title": '
        panel.toggle_dock()
        assert field.editor.value == '{"title": '
        assert field.error is not None
        assert panel.dirty
        assert editor._component_instances[node].spec == {"a": 1}

    async def test_a_form_draft_survives_a_move(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        field._controls["title"][1].value = "Moved"
        panel.toggle_dock()
        assert field.value["title"] == "Moved"
        assert field._controls["title"][1].value == "Moved"
        assert '"title": "Moved"' in field.editor.value
        assert panel.dirty

    async def test_a_spec_without_settings_is_text_only(self, editor):
        node = editor.add_component("D/json")
        _field(editor, node)
        panel = editor.code_panel
        assert panel.view_toggle.options == {"YAML": "yaml", "JSON": "json"}
        assert panel._spec_text.visible and not panel._spec_form.visible

    async def test_form_drafts_are_kept_per_node(self, editor):
        first = editor.add_component("D/plain")
        second = editor.add_component("D/plain")
        field = _field(editor, first)
        field._controls["title"][1].value = "First"
        _field(editor, second)
        assert "title" not in field.value
        field._controls["title"][1].value = "Second"
        _field(editor, first)
        assert field.value["title"] == "First"
        assert field._controls["title"][1].value == "First"
        _field(editor, second)
        assert field.value["title"] == "Second"
        assert all("title" not in editor._component_instances[n].spec for n in (first, second))

    async def test_invalid_text_is_kept_per_node_and_not_run(self, editor):
        first = editor.add_component("D/plain")
        second = editor.add_component("D/plain")
        field = _field(editor, first)
        editor.code_panel.view_toggle.value = "json"
        field.editor.value = '{"mark": '
        assert field.error is not None and editor.code_panel.dirty
        await _run(editor)
        assert editor._component_instances[first].spec == SPEC
        _field(editor, second)
        assert field.error is None and field.value == SPEC
        _field(editor, first)
        assert field.error is not None and field.editor.value == '{"mark": '

    async def test_a_form_edit_is_one_version_and_undo_restores_the_spec_exactly(self, editor):
        node = editor.add_component("D/plain")
        before = json.dumps(editor._component_instances[node].spec)
        field = _field(editor, node)
        editor.edit_history.flush()
        versions = len(editor.edit_history.versions)
        field._controls["title"][1].value = "Capacity"
        await _settle()
        assert "title" not in editor._component_instances[node].spec, "a draft until run"
        await _run(editor)
        editor.edit_history.flush()
        assert editor._component_instances[node].spec["title"] == "Capacity"
        assert editor._tile_items[0]["config"]["spec"]["title"] == "Capacity"
        assert len(editor.edit_history.versions) == versions + 1
        assert editor.undo()
        assert json.dumps(editor._component_instances[node].spec) == before
        field = _field(editor, node)
        assert "title" not in field.value and field._controls["title"][1].value == ""

    async def test_a_checked_edit_applies_once_the_check_passes(self, editor):
        node = editor.add_component("D/chart")
        field = _field(editor, node)
        await _settle()
        field._controls["title"][1].value = "Capacity"
        await _run(editor)
        assert editor._component_instances[node].spec["title"] == "Capacity"
        assert not editor.code_panel.dirty

    async def test_an_invalid_value_is_refused_by_the_check(self, editor):
        node = editor.add_component("D/chart")
        field = _field(editor, node)
        await _settle()
        bad = {**SPEC, "encoding": {**SPEC["encoding"], "x": {"field": "year", "type": "time"}}}
        field.editor.value = json.dumps(bad)
        await _run(editor)
        assert editor._component_instances[node].spec == SPEC
        assert "problem" in editor.code_panel._code_status.object
        assert field.value == bad, "the draft keeps the text for fixing"
        assert editor.code_panel.dirty

    async def test_unknown_keys_survive_form_edits_and_saving(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        field._controls["encoding.y.title"][1].value = "Capacity (MW)"
        await _run(editor)
        editor.save()
        stored = editor.store.load_dashboard(editor.user, editor.dashboard.dashboard_id)
        spec = stored.items[0].config["spec"]
        assert spec["transform"] == SPEC["transform"]
        assert spec["encoding"]["x"]["axis"] == {"labelAngle": -45}
        assert spec["encoding"]["y"]["title"] == "Capacity (MW)"

    async def test_a_change_made_elsewhere_reaches_the_open_form(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        editor._apply_config_patch(node, {"spec": {**SPEC, "title": "From the agent"}})
        assert field.value["title"] == "From the agent"
        assert field._controls["title"][1].value == "From the agent"
        assert editor.undo()
        assert "title" not in field.value and field._controls["title"][1].value == ""

    async def test_a_change_made_elsewhere_keeps_a_form_draft(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        field._controls["title"][1].value = "Mine"
        editor._apply_config_patch(node, {"spec": {**SPEC, "width": 300}})
        assert field.value["title"] == "Mine" and "width" not in field.value
        assert editor.code_panel.dirty

    async def test_spec_text_typed_offers_save(self, editor):
        node = editor.add_component("D/plain")
        editor.save()
        field = _field(editor, node)
        editor.code_panel.view_toggle.value = "json"
        field.editor.value_input = "{}"
        assert editor.code_panel.dirty
        assert editor._has_unsent_edits()


class TestDraftsAcrossChanges:
    async def test_invalid_json_says_why_in_the_form_view(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "json"
        field.editor.value = '{"mark": '
        panel.view_toggle.value = "form"
        assert panel._json_error.visible
        assert "not a valid spec" in panel._json_error[0].object
        assert all(control.disabled for _setting, control in field._controls.values())
        await _run(editor)
        assert "the JSON is not valid" in panel._code_status.object
        assert "the JSON is not valid" in panel._result_footer.object
        panel.fix_json_button.clicks += 1
        assert panel.view_toggle.value == "json" and not panel._json_error.visible
        field.editor.value = json.dumps(SPEC)
        panel.view_toggle.value = "form"
        assert not panel._json_error.visible

    @pytest.mark.parametrize("change", ["agent", "undo"])
    async def test_a_held_form_draft_is_rebased_on_a_change_made_since(self, editor, change):
        first = editor.add_component("D/plain")
        second = editor.add_component("D/plain")
        if change == "undo":
            editor._apply_config_patch(first, {"spec": {**SPEC, "width": 300}})
            editor.edit_history.flush()
        field = _field(editor, first)
        field._controls["title"][1].value = "Mine"
        _field(editor, second)
        if change == "agent":
            editor._apply_config_patch(first, {"spec": {**SPEC, "width": 300}})
            expected_width = 300
        else:
            assert editor.undo()
            expected_width = None
        _field(editor, first)
        assert field.value["title"] == "Mine"
        assert field.value.get("width") == expected_width
        assert editor.code_panel.dirty and not editor.code_panel._stale_notice.visible

    async def test_a_held_text_draft_on_a_changed_field_asks_first(self, editor):
        node = editor.add_component("D/both")
        select_on_canvas(editor._flow, node_id=node)
        assert editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.code.value_input = "SELECT 2"
        editor.close_code_panel()
        assert not editor.code_panel_open
        editor._apply_config_patch(node, {"sql": "SELECT 3"})
        assert editor.open_code_panel(node, "sql")
        assert panel.code.value_input == "SELECT 3"
        assert panel._stale_notice.visible and panel.dirty
        assert editor._component_instances[node].sql == "SELECT 3"
        panel.keep_stale_draft()
        assert panel.code.value_input == "SELECT 2" and not panel._stale_notice.visible
        panel.discard()
        assert panel.code.value_input == "SELECT 3" and not panel.dirty

    async def test_a_stale_draft_can_be_discarded_and_stays_held_until_decided(self, editor):
        node = editor.add_component("D/both")
        other = editor.add_component("D/plain")
        select_on_canvas(editor._flow, node_id=node)
        assert editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.code.value_input = "SELECT 2"
        editor.close_code_panel()
        editor._apply_config_patch(node, {"sql": "SELECT 3"})
        assert editor.open_code_panel(node, "sql")
        assert panel._stale_notice.visible
        select_on_canvas(editor._flow, node_id=other)
        select_on_canvas(editor._flow, node_id=node)
        assert panel.field == "sql" and panel._stale_notice.visible
        panel.discard_stale_draft()
        assert not panel._stale_notice.visible and not panel.has_drafts

    async def test_reselecting_opens_the_field_with_a_draft(self, editor):
        node = editor.add_component("D/both")
        other = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        assert panel.field == "sql"
        assert panel.field_toggle.visible
        assert list(panel.field_toggle.options.values()) == ["sql", "spec"]
        panel.field_toggle.value = "spec"
        assert panel.field == "spec"
        field._controls["title"][1].value = "Mine"
        select_on_canvas(editor._flow, node_id=other)
        select_on_canvas(editor._flow, node_id=node)
        assert panel.field == "spec" and field.value["title"] == "Mine"
        body = pn.Column(*editor._inspector._body)
        labels = [b.label for b in body.select(pmui.Button) if "fd-edit-spec" in b.css_classes]
        assert labels == ["Edit SQL", "Edit Spec · draft not run"]
        panel.field_toggle.value = "sql"
        assert "Spec (draft not run)" in panel.field_toggle.options

    async def test_a_node_without_a_spec_names_nothing_to_edit(self, editor):
        frame = editor.add_component("D/frame")
        assert editor.open_code_panel(frame)
        assert "Nothing to edit" in editor.code_panel._code_header.object

    async def _hold_then_change(self, editor, start, drafted, outside):
        """Hold a spec draft on one node, change its spec from outside, and show it again."""
        node = editor.add_component("D/plain")
        other = editor.add_component("D/plain")
        editor._apply_config_patch(node, {"spec": start})
        field = _field(editor, node)
        field.value = drafted
        assert editor.code_panel.dirty
        _field(editor, other)
        editor._apply_config_patch(node, {"spec": outside})
        _field(editor, node)
        return editor.code_panel, field

    @pytest.mark.parametrize(
        ("start", "drafted", "outside"),
        [
            ({**SPEC, "title": "Old"}, {**SPEC, "title": "Mine"}, {**SPEC, "title": "Agent"}),
            ({**SPEC, "title": "Old"}, SPEC, {**SPEC, "title": "Agent"}),
            (
                SPEC,
                {
                    **SPEC,
                    "encoding": {**SPEC["encoding"], "x": {**SPEC["encoding"]["x"], "title": "X"}},
                },
                {**SPEC, "encoding": {"y": SPEC["encoding"]["y"]}},
            ),
            (
                {**SPEC, "layer": [{"mark": "bar"}, {"mark": "line"}]},
                {**SPEC, "layer": [{"mark": "point"}, {"mark": "line"}]},
                {**SPEC, "layer": [{"mark": "bar"}, {"mark": "area"}]},
            ),
            (
                SPEC,
                {**SPEC, "transform": [{"filter": "datum.year >= 2010"}]},
                {**SPEC, "transform": [*SPEC["transform"], {"filter": "datum.capacity > 1"}]},
            ),
        ],
        ids=["same-path", "deleted-and-edited", "parent-deleted", "list-item", "transform"],
    )
    async def test_a_conflicting_change_asks_instead_of_rebasing(
        self, editor, start, drafted, outside
    ):
        panel, field = await self._hold_then_change(editor, start, drafted, outside)
        assert panel._stale_notice.visible and panel.dirty
        assert field.value == outside, "the current spec is shown until the person decides"
        panel.keep_stale_draft()
        assert field.value == drafted and not panel._stale_notice.visible

    async def test_a_draft_held_against_no_spec_asks(self, editor):
        node = editor.add_component("D/plain")
        other = editor.add_component("D/plain")
        editor._apply_config_patch(node, {"spec": None})
        field = _field(editor, node)
        field.value = {"mark": "point"}
        _field(editor, other)
        editor._apply_config_patch(node, {"spec": {**SPEC, "mark": "line"}})
        _field(editor, node)
        panel = editor.code_panel
        assert panel._stale_notice.visible
        assert field.value["mark"] == "line"

    async def test_running_while_the_notice_is_open_clears_it(self, editor):
        node = editor.add_component("D/both")
        select_on_canvas(editor._flow, node_id=node)
        assert editor.open_code_panel(node, "sql")
        panel = editor.code_panel
        panel.code.value_input = "SELECT 2"
        editor.close_code_panel()
        editor._apply_config_patch(node, {"sql": "SELECT 3"})
        assert editor.open_code_panel(node, "sql")
        assert panel._stale_notice.visible
        panel.run("SELECT 4")
        await _settle()
        assert editor._component_instances[node].sql == "SELECT 4"
        assert not panel._stale_notice.visible and not panel.dirty
        panel.keep_stale_draft()
        assert panel.code.value_input == "SELECT 4"


SELECTION = {
    "params": [{"name": "pick", "select": {"type": "point", "on": "click", "clear": "dblclick"}}],
    "mark": "bar",
    "encoding": {"x": {"field": "year", "type": "ordinal"}},
}

COMMENTED = """# Capacity per year
mark: bar  # bars read best here
params:
  - name: pick
    select: {type: point, on: click}
yes: no
"""


class TestYaml:
    def test_keys_like_on_stay_strings_both_ways(self):
        text, _spans, _doc = format_spec_yaml(SELECTION)
        assert "on: click" in text
        spec, _doc = parse_spec_yaml(text)
        assert spec == SELECTION and json.dumps(spec) == json.dumps(SELECTION)
        spec, _doc = parse_spec_yaml("on: click\nyes: no\noff: true\n")
        assert spec == {"on": "click", "yes": "no", "off": True}

    def test_key_order_is_kept(self):
        spec = {
            "width": 300,
            "mark": "bar",
            "encoding": {"y": {"field": "a"}, "x": {"field": "b"}},
        }
        text, _spans, _doc = format_spec_yaml(spec)
        assert list(parse_spec_yaml(text)[0]["encoding"]) == ["y", "x"]
        assert text.splitlines()[0] == "width: 300"

    def test_dates_and_numbers_read_as_json_values(self):
        spec, _doc = parse_spec_yaml("a: 2024-01-31\nb: 1.5\nc: 7\nd: ~\ne: '1'\n")
        assert spec == {"a": "2024-01-31", "b": 1.5, "c": 7, "d": None, "e": "1"}
        text = format_spec_yaml({"a": "2024-01-31", "b": "true"})[0]
        assert parse_spec_yaml(text)[0] == {"a": "2024-01-31", "b": "true"}

    def test_values_json_writes_are_written_as_yaml(self):
        text = format_spec_yaml({"a": np.float64(1.5), "t": (1, 2), "n": None})[0]
        assert "n: null" in text
        assert parse_spec_yaml(text)[0] == {"a": 1.5, "t": [1, 2], "n": None}

    def test_json_is_read_as_yaml_too(self):
        assert parse_spec_yaml(json.dumps(SPEC, indent=2))[0] == SPEC
        assert parse_spec_yaml("  ") == ({}, None)
        assert format_spec_yaml({})[0] == ""

    def test_comments_are_kept_when_the_spec_changes(self):
        spec, doc = parse_spec_yaml(COMMENTED)
        spec["title"] = "T"
        spec["params"][0]["select"]["on"] = "mouseover"
        del spec["yes"]
        text = format_spec_yaml(spec, doc)[0]
        assert text.startswith("# Capacity per year\nmark: bar  # bars read best here\n")
        assert "select: {type: point, on: mouseover}" in text
        assert "yes" not in text and text.endswith("title: T")
        assert parse_spec_yaml(text)[0] == spec

    def test_invalid_yaml_says_where(self):
        with pytest.raises(SpecTextError) as info:
            parse_spec_yaml("mark: bar\n  width: 2\n")
        assert (info.value.line, info.value.column) == (2, 8)
        with pytest.raises(SpecTextError, match="mapping"):
            parse_spec_yaml("- 1\n- 2\n")
        with pytest.raises(SpecTextError, match="duplicate"):
            parse_spec_yaml("a: 1\na: 2\n")

    @pytest.mark.parametrize("syntax", ["yaml", "json"])
    def test_spans_of_text_as_written(self, syntax):
        text = format_spec_yaml(SPEC)[0] if syntax == "yaml" else json.dumps(SPEC, indent=2)
        spans = text_spans(text)
        lines = text.splitlines()
        start, end = spans[("encoding", "x", "axis")]
        assert "axis" in lines[start] and "labelAngle" in lines[end]
        start, _end = spans[("transform",)]
        assert "transform" in lines[start]
        assert text_spans("a: [") == {}


class TestFieldYaml:
    def test_the_text_is_yaml_and_the_form_follows_it(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml")
        assert field.editor.language == "yaml"
        assert field.editor.value.startswith("mark: bar\nencoding:")
        field.editor.value = COMMENTED
        assert field.value == {
            "mark": "bar",
            "params": [{"name": "pick", "select": {"type": "point", "on": "click"}}],
            "yes": "no",
        }
        assert field._controls["mark.type"][1].value == "Bar"
        assert field.editor.value == COMMENTED, "the text stays as typed"

    def test_a_form_edit_keeps_the_comments(self):
        field = SpecEditor(value={}, settings=SETTINGS, syntax="yaml")
        field.editor.value = COMMENTED
        field._controls["title"][1].value = "Capacity"
        text = field.editor.value
        assert "# Capacity per year" in text and "# bars read best here" in text
        assert "on: click" in text and text.endswith("title: Capacity")
        row = field._spans[("title",)][0]
        assert field._marks.changed == [row]

    def test_switching_the_syntax_shows_the_same_spec(self):
        field = SpecEditor(value={}, settings=SETTINGS, syntax="yaml")
        field.editor.value = COMMENTED
        spec = field.value
        field.syntax = "json"
        assert json.loads(field.editor.value) == spec and field.editor.language == "json"
        field.editor.value = json.dumps({**spec, "width": 200})
        field.syntax = "yaml"
        assert "# bars read best here" in field.editor.value
        assert parse_spec_yaml(field.editor.value)[0] == {**spec, "width": 200}

    def test_invalid_yaml_keeps_the_spec(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml")
        field.editor.value = "mark: [bar\n"
        assert field.error and field.value == SPEC and field.unsent
        assert field.editor.annotations[0]["type"] == "error"
        assert all(control.disabled for _, control in field._controls.values())

    async def test_live_text_applies_once_it_parses_and_typing_pauses(self, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.spec_field.APPLY_DELAY", 0.05)
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml", live=True)
        typed = field.editor.value + "\ntitle: Typed  # live"
        field.editor.value_input = typed
        assert field.unsent and "title" not in field.value
        await _settle(0.2)
        assert field.value["title"] == "Typed" and not field.unsent
        assert field.editor.value_input == typed, "the text is not written back while typing"
        field.editor.value_input = typed + "\nwidth: ["
        await _settle(0.2)
        assert field.error is not None and "width" not in field.value and field.unsent
        field.editor.value_input = typed + "\nwidth: 300"
        await _settle(0.2)
        assert field.error is None and field.value["width"] == 300

    async def test_without_live_text_waits_for_blur(self, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.spec_field.APPLY_DELAY", 0.05)
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml")
        field.editor.value_input = "mark: line"
        await _settle(0.2)
        assert field.value == SPEC and field.unsent
        assert field.flush() and field.value == {"mark": "line"} and not field.unsent

    def test_typing_back_to_the_applied_text_clears_the_error(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml")
        applied = field.editor.value
        field.editor.value = "mark: ["
        assert field.error is not None
        field.editor.value_input = applied
        assert field.error is None and not field.unsent

    def test_unshown_paths_link_to_their_lines(self):
        field = SpecEditor(value=SPEC, settings=SETTINGS, syntax="yaml", mode="form")
        link = field.unshown[2][1]
        assert link.label == "Edit in YAML"
        link.clicks += 1
        assert field.mode == "spec"
        row = field._spans[("transform",)][0]
        assert field.editor.value.splitlines()[row].startswith("transform:")
        assert field._marks.goto == {"row": row, "count": 1}
        field.syntax = "json"
        assert field.unshown[2][1].label == "Edit in JSON"


class TestSpecViews:
    async def test_yaml_is_the_default_text_view_and_the_views_stay_in_sync(self, editor):
        node = editor.add_component("D/plain")
        panel = editor.code_panel
        select_on_canvas(editor._flow, node_id=node)
        assert editor.open_code_panel(node)
        field = panel.spec_editor
        assert panel.view_toggle.options == {"Form": "form", "YAML": "yaml", "JSON": "json"}
        assert panel.view_toggle.value == "form" and panel.spec_text == "yaml"
        panel.run_on_change = False
        field._controls["title"][1].value = "Capacity"
        panel.view_toggle.value = "yaml"
        assert panel._spec_text.visible and field.syntax == "yaml"
        assert "title: Capacity" in field.editor.value
        field.editor.value = field.editor.value.replace("Capacity", "Typed") + "\n# note"
        panel.view_toggle.value = "json"
        assert json.loads(field.editor.value)["title"] == "Typed"
        panel.view_toggle.value = "yaml"
        assert "# note" in field.editor.value
        panel.view_toggle.value = "form"
        assert field._controls["title"][1].value == "Typed"
        assert panel.dirty and "title" not in editor._component_instances[node].spec

    async def test_text_that_does_not_parse_stays_in_its_view(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "yaml"
        field.editor.value_input = "mark: ["
        panel.view_toggle.value = "json"
        assert panel.view_toggle.value == "yaml" and field.syntax == "yaml"
        assert field.editor.value_input == "mark: [" and field.error is not None
        assert "Not switched to JSON" in field.status.object
        panel.view_toggle.value = "form"
        assert panel._json_error.visible and panel.fix_json_button.label == "Fix in YAML"

    async def test_typed_text_is_applied_when_switching_views(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "yaml"
        field.editor.value_input = "mark: line\non: click"
        panel.view_toggle.value = "json"
        assert json.loads(field.editor.value) == {"mark": "line", "on": "click"}
        assert panel.dirty

    async def test_the_view_is_remembered_for_the_browser(self, editor):
        node = editor.add_component("D/plain")
        _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "yaml"
        stored = json.loads(editor._split_view.preferences.to_json())
        assert (stored["spec_view"], stored["spec_text"]) == ("yaml", "yaml")
        editor._split_view.preferences.load({"spec_view": "json", "spec_text": "json"})
        editor._on_split_preferences_loaded()
        assert panel.view_toggle.value == "json" and panel.spec_editor.syntax == "json"

    async def test_comments_survive_a_run_an_undo_and_another_node(self, editor):
        node = editor.add_component("D/plain")
        other = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "yaml"
        field.editor.value = "# mine\n" + field.editor.value + "\ntitle: T  # why"
        editor.edit_history.flush()
        await _run(editor)
        editor.edit_history.flush()
        assert editor._component_instances[node].spec["title"] == "T"
        assert editor.undo()
        assert "title" not in field.value
        assert field.editor.value.startswith("# mine\n")
        _field(editor, other)
        assert "# mine" not in field.editor.value
        _field(editor, node)
        panel.view_toggle.value = "yaml"
        assert field.editor.value.startswith("# mine\n")

    async def test_a_link_from_the_form_opens_the_text_at_the_line(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.spec_text = "yaml"
        assert panel.view_toggle.value == "form"
        assert [row[1].label for row in field.unshown[1:]] == ["Edit in YAML"] * 2
        field.unshown[2][1].clicks += 1
        assert panel.view_toggle.value == "yaml" and panel._spec_text.visible
        row = field._marks.goto["row"]
        assert field.editor.value.splitlines()[row].startswith("transform:")


class TestRunOnChange:
    async def test_on_by_default_for_a_spec_and_off_for_code(self, editor):
        node = editor.add_component("D/both")
        select_on_canvas(editor._flow, node_id=node)
        panel = editor.code_panel
        assert editor.open_code_panel(node, "sql")
        assert not panel.run_on_change and not panel.run_switch.value
        assert editor.open_code_panel(node, "spec")
        assert panel.run_on_change and panel.spec_editor.live
        panel.run_on_change = False
        assert editor.open_code_panel(node, "sql")
        panel.run_on_change = True
        assert editor.open_code_panel(node, "spec")
        assert not panel.run_on_change and not panel.spec_editor.live
        assert editor.open_code_panel(node, "sql")
        assert panel.run_on_change

    async def test_a_form_edit_runs_at_once(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node, run_on_change=True)
        shown = []
        editor.code_panel.param.watch(lambda event: shown.append(event.new), "stale")
        field._controls["title"][1].value = "Now"
        await _settle(0.5)
        assert editor._component_instances[node].spec["title"] == "Now"
        assert not editor.code_panel.dirty
        assert shown == [], "a quick run is not marked stale"

    async def test_text_runs_once_it_parses(self, editor, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.spec_field.APPLY_DELAY", 0.05)
        node = editor.add_component("D/plain")
        field = _field(editor, node, run_on_change=True)
        panel = editor.code_panel
        panel.view_toggle.value = "yaml"
        start = field.editor.value
        field.editor.value_input = start + "\ntitle: ["
        await _settle(0.5)
        assert editor._component_instances[node].spec == SPEC
        assert panel.dirty and field.error is not None
        assert panel._stale_badge.visible and editor._find_node_card(node).stale == "draft"
        field.editor.value_input = (
            start + "\ntitle: Live\nparams: [{name: p, select: {type: point, on: click}}]"
        )
        await _settle(0.5)
        spec = editor._component_instances[node].spec
        assert spec["title"] == "Live" and spec["params"][0]["select"]["on"] == "click"
        assert not panel.dirty

    async def test_code_waits_for_run(self, editor, monkeypatch):
        monkeypatch.setattr("panel_flowdash.ui.code_panel.RUN_DELAY", 0.05)
        node = editor.add_component("D/both")
        select_on_canvas(editor._flow, node_id=node)
        assert editor.open_code_panel(node, "sql")
        editor.code_panel.code.value_input = "SELECT 2"
        await _settle(0.3)
        assert editor._component_instances[node].sql == "SELECT 1"
        assert editor.code_panel.dirty


class TestStale:
    async def test_a_draft_dims_the_result_and_marks_the_card_and_tile(self, editor):
        node = editor.add_component("D/plain")
        other = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        item = next(i for i in editor._tile_items if i["instance_id"] == node)
        tile = editor._grid_view(item, pn.pane.Str("chart"))
        assert not tile.stale_mark.visible
        assert not panel._stale_badge.visible and editor._find_node_card(node).stale is None
        field._controls["title"][1].value = "Draft"
        assert panel._stale_badge.visible and panel.stale_run_button.visible
        assert "Not run" in panel._stale_text.object
        assert "fd-code-result-dimmed" in panel._result_body.css_classes
        assert editor._find_node_card(node).stale == "draft"
        assert tile.stale_mark.visible and tile.stale_mark.object == "Not run"
        assert "flowdash-tile-stale" in tile.css_classes
        _field(editor, other)
        assert editor._find_node_card(node).stale == "draft", "a held draft is not run either"
        assert not panel._stale_badge.visible
        _field(editor, node)
        panel.stale_run_button.clicks += 1
        await _settle()
        assert editor._component_instances[node].spec["title"] == "Draft"
        assert not panel._stale_badge.visible
        assert "fd-code-result-dimmed" not in panel._result_body.css_classes
        assert editor._find_node_card(node).stale is None
        assert not tile.stale_mark.visible and "flowdash-tile-stale" not in tile.css_classes

    async def test_text_that_does_not_parse_is_stale(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        editor.code_panel.view_toggle.value = "yaml"
        field.editor.value_input = "mark: ["
        assert editor.code_panel._stale_badge.visible
        assert editor._find_node_card(node).stale == "draft"
        editor.code_panel.discard()
        assert not editor.code_panel._stale_badge.visible
        assert editor._find_node_card(node).stale is None

    async def test_a_run_in_flight_says_running(self, editor):
        node = editor.add_component("D/chart")
        _field(editor, node)
        panel = editor.code_panel
        panel.running = True
        assert not panel._stale_badge.visible, "a quick run does not flash"
        await _settle(0.5)
        assert "Running" in panel._stale_text.object and not panel.stale_run_button.visible
        assert panel._stale_badge.visible
        assert editor._find_node_card(node).stale == "running"
        panel.running = False
        assert editor._find_node_card(node).stale is None
        assert not panel._stale_badge.visible


class TestRunOnChangeHistory:
    async def test_undo_is_not_run_again(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node, run_on_change=True)
        editor.edit_history.flush()
        versions = len(editor.edit_history.versions)
        field._controls["title"][1].value = "Once"
        await _settle(0.5)
        editor.edit_history.flush()
        assert editor._component_instances[node].spec["title"] == "Once"
        assert len(editor.edit_history.versions) == versions + 1, "one run, one version"
        assert editor.undo()
        await _settle(0.5)
        assert "title" not in editor._component_instances[node].spec
        assert "title" not in field.value and not editor.code_panel.dirty
        assert editor.code_panel.stale is None


class TestYamlAliases:
    def test_merge_keys_have_spans_and_apply(self):
        text = "base: &b {a: 1}\nx:\n  <<: *b\n  c: 2\n"
        spans = text_spans(text)
        assert spans[("x",)][0] == 1 and spans[("x", "c")] == (3, 3)
        field = SpecEditor(value={}, settings=SETTINGS, syntax="yaml")
        field.editor.value = text
        assert field.error is None
        assert field.value == {"base": {"a": 1}, "x": {"c": 2, "a": 1}}

    def test_an_edit_under_an_alias_changes_that_path_only(self):
        spec, doc = parse_spec_yaml("base: &b {a: 1}\nx: *b\n")
        spec["x"]["a"] = 2
        text = format_spec_yaml(spec, doc)[0]
        assert parse_spec_yaml(text)[0] == {"base": {"a": 1}, "x": {"a": 2}}
        spec, doc = parse_spec_yaml("base: &b {a: 1}\nx:\n  <<: *b\n  c: 2\n")
        spec["x"]["a"] = 5
        assert parse_spec_yaml(format_spec_yaml(spec, doc)[0])[0] == spec

    def test_an_unchanged_alias_is_kept(self):
        spec, doc = parse_spec_yaml("base: &b {a: 1}\nx: *b\n")
        spec["title"] = "T"
        assert format_spec_yaml(spec, doc)[0] == "base: &b {a: 1}\nx: *b\ntitle: T"

    def test_a_form_edit_under_an_alias_keeps_text_and_spec_in_step(self):
        field = SpecEditor(value={}, settings=SETTINGS, syntax="yaml")
        field.editor.value = "encoding: &e {x: {title: A}}\nlayer: [{encoding: *e}]\n"
        field._controls["encoding.x.title"][1].value = "B"
        assert field.value["layer"][0]["encoding"] == {"x": {"title": "A"}}
        assert parse_spec_yaml(field.editor.value)[0] == field.value

    def test_anchored_scalars_keep_their_type(self):
        spec, _doc = parse_spec_yaml("a: &t true\nb: *t\nf: &f 1.5\ni: &i 3\n")
        assert spec == {"a": True, "b": True, "f": 1.5, "i": 3}
        assert [type(v) for v in spec.values()] == [bool, bool, float, int]


class TestRunFailure:
    async def test_a_failing_apply_finishes_the_run_and_says_why(self, editor, monkeypatch):
        node = editor.add_component("D/plain")
        field = _field(editor, node, run_on_change=True)
        panel = editor.code_panel

        def fail(*_args):
            raise RuntimeError("boom")

        monkeypatch.setattr(editor, "_apply_config_patch", fail)
        field._controls["title"][1].value = "X"
        await _settle(0.5)
        assert not panel.running and panel.stale == "draft"
        assert "boom" in panel._problems[0].object
        monkeypatch.undo()
        field._controls["title"][1].value = "Y"
        await _settle(0.5)
        assert editor._component_instances[node].spec["title"] == "Y"
        assert not panel.running and panel.stale is None
