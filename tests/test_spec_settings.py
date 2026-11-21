"""A spec edited through a form of curated settings and as JSON, kept in sync both ways."""

import asyncio
import json

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
    parse_spec,
    problems,
    set_path,
    shown_in_spec,
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
        assert "transform" in field.banner[0].object
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
        assert "title" in field.banner[0].object

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


def _field(editor, node_id):
    """Select the node, which shows its spec in the code panel, and return the panel's spec editor."""
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

    async def test_a_narrow_window_keeps_the_spec_in_the_inspector(self, editor):
        node = editor.add_component("D/plain")
        editor._split_view.storage.window_width = 800
        select_on_canvas(editor._flow, node_id=node)
        assert len(list(pn.Column(*editor._inspector._body).select(SpecEditor))) == 1

    async def test_form_and_json_are_views_of_one_spec(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        assert panel.view_toggle.visible and panel.view_toggle.value == "form"
        assert panel._spec_form.visible and not panel._spec_text.visible
        field._controls["title"][1].value = "Capacity"
        assert '"title": "Capacity"' in field.editor.value
        panel.view_toggle.value = "spec"
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
        panel.view_toggle.value = "spec"
        assert panel.format_button.visible and panel.spec_tools.can_format
        assert "Shift+Alt+F" in panel.format_button.description
        compact = json.dumps({"title": "T", **SPEC}, separators=(",", ":"))
        field.editor.value_input = compact
        assert panel.dirty
        panel.format()
        expected = format_spec({"title": "T", **SPEC})[0]
        assert field.editor.value == field.editor.value_input == expected
        assert expected.startswith('{\n  "title": "T",\n  "mark": "bar"')
        assert panel.spec_tools.replacement["text"] == expected
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

    async def test_a_spec_without_settings_is_json_only(self, editor):
        node = editor.add_component("D/json")
        _field(editor, node)
        panel = editor.code_panel
        assert not panel.view_toggle.visible
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
        editor.code_panel.view_toggle.value = "spec"
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
        editor.code_panel.view_toggle.value = "spec"
        field.editor.value_input = "{}"
        assert editor.code_panel.dirty
        assert editor._has_unsent_edits()


class TestDraftsAcrossChanges:
    async def test_invalid_json_says_why_in_the_form_view(self, editor):
        node = editor.add_component("D/plain")
        field = _field(editor, node)
        panel = editor.code_panel
        panel.view_toggle.value = "spec"
        field.editor.value = '{"mark": '
        panel.view_toggle.value = "form"
        assert panel._json_error.visible
        assert "not a valid spec" in panel._json_error[0].object
        assert all(control.disabled for _setting, control in field._controls.values())
        await _run(editor)
        assert "the JSON is not valid" in panel._code_status.object
        assert "the JSON is not valid" in panel._result_footer.object
        panel.fix_json_button.clicks += 1
        assert panel.view_toggle.value == "spec" and not panel._json_error.visible
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
        editor._split_view.storage.window_width = 800
        assert not editor.code_panel_open
        editor._apply_config_patch(node, {"sql": "SELECT 3"})
        editor._split_view.storage.window_width = 1400
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
