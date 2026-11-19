"""Tests for the choices config fields offer, from declared columns and the config_choices hook."""

import asyncio
import enum
import json
import time

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import Choices, columns_of, port_types, register, register_port_type
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.config_choices import compute_choices, node_choices
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.choice_widgets import (
    EMPTY,
    ChoiceMultiSelect,
    ChoiceSelect,
    SuggestInput,
    choice_widget,
    plain_widget,
)
from panel_flowdash.ui.status_bar import select_on_canvas


class LazyTable:
    """A table that lists its columns without holding rows, like a query."""

    def __init__(self, columns):
        self.schema = columns


@pytest.fixture
def port_types_restored(monkeypatch):
    """Let a test register port types without leaving them registered for other tests."""
    for name in ("_ALIASES", "_SUPERTYPES", "_COLUMN_READERS"):
        registered = getattr(port_types, name)
        copied = {k: set(v) if isinstance(v, set) else v for k, v in registered.items()}
        monkeypatch.setattr(port_types, name, copied)


@pytest.fixture
def lazy_types(port_types_restored):
    lazy = register_port_type(
        "tests.LazyTable", columns=lambda value: dict(value.schema) if value is not None else None
    )
    return lazy, register_port_type("tests.SubTable", subtype_of=[lazy])


@register(
    page=False, component=True, title="Frame", provides=[{"key": "rows", "type": "DataFrame"}]
)
class FrameSource(Viewer):
    wide = param.Boolean(default=False)

    @param.output(param.DataFrame)
    @param.depends("wide")
    def rows(self):
        frame = pd.DataFrame({"state": ["TX"], "year": [2020], "capacity": [1.5]})
        if self.wide:
            frame["when"] = pd.to_datetime(["2020-01-01"])
        return frame

    def __panel__(self):
        return "frame"


@register(
    page=False,
    component=True,
    title="Pick",
    config=["column", "columns", "label"],
)
class Pick(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)
    column = param.String(default="", metadata={"flowdash": {"choices": "columns", "of": "rows"}})
    columns = param.List(
        default=[], item_type=str, metadata={"flowdash": {"choices": "columns", "of": "rows"}}
    )
    label = param.String(default="")

    def __panel__(self):
        return self.column


def table_choices(config, inputs):
    source = inputs.get("source")
    if source is None:
        return {}
    return {"table": sorted(source), "label": Choices(["Totals", "Counts"], restrict=False)}


@register(
    page=False,
    component=True,
    title="Table pick",
    config=["table", "label"],
    config_choices=table_choices,
    requires=[{"key": "source", "type": "Dict", "required": False}],
)
class TablePick(Viewer):
    source = param.Dict(default=None, allow_None=True)
    table = param.String(default="")
    label = param.String(default="")

    def __panel__(self):
        return self.table


@register(page=False, component=True, title="Tables", provides=[{"key": "source", "type": "Dict"}])
class Tables(Viewer):
    @param.output(param.Dict)
    def source(self):
        return {"turbines": 1, "population": 2}

    def __panel__(self):
        return "tables"


CALLS = []


def counted_choices(config, inputs):
    CALLS.append(inputs.get("rows"))
    time.sleep(0.1)
    return {"column": ["a"]}


@register(
    page=False, component=True, title="Counted", config=["column"], config_choices=counted_choices
)
class Counted(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)
    column = param.String(default="")

    def __panel__(self):
        return self.column


def slow_choices(config, inputs):
    time.sleep(0.6)
    return {"label": ["Slow"]}


@register(
    page=False,
    component=True,
    title="Slow pick",
    config=["column", "label"],
    config_choices=slow_choices,
)
class SlowPick(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)
    column = param.String(default="", metadata={"flowdash": {"choices": "columns", "of": "rows"}})
    label = param.String(default="")

    def __panel__(self):
        return self.column


COMPONENTS = {
    "Demo/slow_pick": SlowPick,
    "Demo/counted": Counted,
    "Demo/frame": FrameSource,
    "Demo/pick": Pick,
    "Demo/table_pick": TablePick,
    "Demo/tables": Tables,
}


def _spec(app, app_id="Demo/x"):
    return build_component_spec(RegistryEntry.from_app(app, app_id=app_id))


async def _settle(seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def _form(editor):
    return next(iter(pn.Column(*editor._inspector._body).select(pn.Param)))


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)


class TestColumns:
    def test_columns_of_a_dataframe_have_type_names(self):
        frame = pd.DataFrame(
            {
                "i": [1],
                "f": [1.5],
                "s": ["a"],
                "b": [True],
                "t": pd.to_datetime(["2020-01-01"]),
            }
        )
        assert columns_of(frame) == [
            ("i", "integer"),
            ("f", "number"),
            ("s", "string"),
            ("b", "boolean"),
            ("t", "datetime"),
        ]

    def test_columns_of_a_value_that_is_not_tabular(self):
        assert columns_of(None) is None
        assert columns_of([1, 2]) is None

    def test_a_registered_reader_lists_columns(self, lazy_types):
        table = LazyTable({"a": "integer", "b": None})
        assert columns_of(table, lazy_types[0]) == [("a", "integer"), ("b", None)]

    def test_subtypes_inherit_the_reader(self, lazy_types):
        assert columns_of(LazyTable({"a": "string"}), lazy_types[1]) == [("a", "string")]

    def test_readers_may_return_names_or_pairs(self, port_types_restored):
        names = register_port_type("tests.Names", columns=lambda value: ["x", "y"])
        pairs = register_port_type("tests.Pairs", columns=lambda value: [("x", "number")])
        assert columns_of(object(), names) == [("x", None), ("y", None)]
        assert columns_of(object(), pairs) == [("x", "number")]


class TestChoicesValue:
    def test_labels_show_types(self):
        choices = Choices(["a", "b"], types={"a": "integer"})
        assert choices.labelled() == {"a (integer)": "a", "b": "b"}
        assert choices.values() == ["a", "b"]

    def test_a_dict_keeps_its_labels(self):
        assert Choices({"First": 1}).labelled() == {"First": 1}


class TestComputeChoices:
    async def test_declared_columns_of_an_input(self):
        spec = _spec(Pick)
        rows = pd.DataFrame({"state": ["TX"], "year": [2020]})
        choices = await compute_choices(spec, {}, {"rows": rows})
        assert set(choices) == {"column", "columns"}
        assert choices["column"].values() == ["state", "year"]
        assert choices["column"].types == {"state": "string", "year": "integer"}

    async def test_no_choices_without_the_input(self):
        assert await compute_choices(_spec(Pick), {}, {}) == {}

    async def test_the_hook_computes_choices_from_inputs(self):
        spec = _spec(TablePick)
        choices = await compute_choices(spec, {}, {"source": {"b": 1, "a": 2}})
        assert choices["table"] == Choices(["a", "b"])
        assert choices["label"].restrict is False

    async def test_an_async_hook(self):
        async def hook(config, inputs):
            await asyncio.sleep(0)
            return {"table": {"Turbines": "turbines"}}

        @register(component=True, config=["table"], config_choices=hook)
        class Async(Viewer):
            table = param.String(default="")

        choices = await compute_choices(_spec(Async), {"table": ""}, {})
        assert choices["table"].labelled() == {"Turbines": "turbines"}

    async def test_the_hook_overrides_declared_choices(self):
        @register(component=True, config=["column"], config_choices=lambda c, i: {"column": ["z"]})
        class Both(Viewer):
            rows = param.DataFrame(default=None, allow_None=True)
            column = param.String(
                default="", metadata={"flowdash": {"choices": "columns", "of": "rows"}}
            )

        choices = await compute_choices(_spec(Both), {}, {"rows": pd.DataFrame({"a": [1]})})
        assert choices["column"].values() == ["z"]

    async def test_unknown_fields_and_bad_values_are_ignored(self):
        @register(
            component=True,
            config=["table"],
            config_choices=lambda c, i: {"nope": ["x"], "table": 3},
        )
        class Odd(Viewer):
            table = param.String(default="")

        assert await compute_choices(_spec(Odd), {}, {}) == {}

    async def test_a_failing_hook_offers_nothing(self, caplog):
        def hook(config, inputs):
            raise RuntimeError("boom")

        @register(component=True, config=["table"], config_choices=hook)
        class Failing(Viewer):
            table = param.String(default="")

        assert await compute_choices(_spec(Failing), {}, {}) == {}
        assert "config choices" in caplog.text

    async def test_a_hook_returning_a_list_is_logged(self, caplog):
        @register(component=True, config=["table"], config_choices=lambda c, i: ["a"])
        class Listy(Viewer):
            table = param.String(default="")

        assert await compute_choices(_spec(Listy), {}, {}) == {}
        assert "must return a dict" in caplog.text

    async def test_a_slow_hook_times_out(self, caplog):
        def hook(config, inputs):
            time.sleep(0.5)
            return {"table": ["a"]}

        @register(component=True, config=["table"], config_choices=hook)
        class Slow(Viewer):
            table = param.String(default="")

        assert await compute_choices(_spec(Slow), {}, {}, timeout=0.05) == {}

    async def test_node_choices_reads_live_inputs(self, editor):
        tables, pick = editor.add_component("Demo/tables"), editor.add_component("Demo/table_pick")
        assert await node_choices(editor, pick) == {}
        editor.connect(tables, "source", pick, "source")
        choices = await node_choices(editor, pick)
        assert choices["table"].values() == ["population", "turbines"]


class TestChoiceWidget:
    def test_a_restricted_field_is_a_dropdown(self):
        spec = choice_widget(param.String(), Choices(["a", "b"], types={"a": "string"}), "a")
        assert issubclass(spec["type"], pmui.Select)
        assert spec["options"] == {EMPTY: "", "a (string)": "a", "b": "b"}

    def test_a_text_field_can_always_be_cleared(self):
        assert choice_widget(param.String(), Choices(["a"]), "")["options"] == {
            EMPTY: "",
            "a": "a",
        }
        nullable = param.String(default=None, allow_None=True)
        assert choice_widget(nullable, Choices(["a"]), None)["options"] == {EMPTY: None, "a": "a"}
        assert choice_widget(nullable, Choices(["a"]), "a")["options"] == {EMPTY: None, "a": "a"}
        text = param.String(default="x", allow_None=True)
        assert choice_widget(text, Choices(["a"]), "a")["options"] == {EMPTY: "", "a": "a"}

    def test_a_field_that_allows_none_can_be_cleared(self):
        field = param.Integer(default=None, allow_None=True)
        assert choice_widget(field, Choices([1, 2]), 1)["options"] == {EMPTY: None, "1": 1, "2": 2}
        assert EMPTY not in choice_widget(param.Integer(default=1), Choices([1]), 1)["options"]

    def test_a_value_not_among_the_choices_stays(self):
        spec = choice_widget(param.String(), Choices(["a"]), "gone")
        assert spec["options"] == {EMPTY: "", "gone (not available)": "gone", "a": "a"}

    def test_an_open_field_suggests(self):
        spec = choice_widget(param.String(), Choices(["a"], restrict=False), "x")
        assert issubclass(spec["type"], pmui.AutocompleteInput)
        assert spec["options"] == ["a"] and spec["restrict"] is False

    def test_a_list_field_is_a_multi_choice(self):
        spec = choice_widget(param.List(), Choices(["a", "b"]), ["b", "gone"])
        assert issubclass(spec["type"], pmui.MultiChoice)
        assert spec["options"] == {"a": "a", "b": "b", "gone (not available)": "gone"}

    @pytest.mark.parametrize(
        "spec",
        [
            {"type": ChoiceSelect, "options": {EMPTY: "", "a": "a"}},
            {"type": SuggestInput, "options": ["a"], "restrict": False},
        ],
    )
    def test_a_rendered_widget_keeps_a_value_outside_its_options(self, spec):
        class State(param.Parameterized):
            column = param.String(default="")

        state = State()
        form = pn.Param(state, parameters=["column"], widgets={"column": spec})
        form.get_root()
        state.column = "elsewhere"
        assert form._widgets["column"].value == "elsewhere"
        assert state.column == "elsewhere"

    def test_many_choices_are_searchable(self):
        assert choice_widget(param.String(), Choices(list("abcdefghij")), "a")["searchable"]
        assert not choice_widget(param.String(), Choices(["a"]), "a")["searchable"]


class TestPlainWidget:
    def test_a_list_selector_is_a_multi_choice(self):
        spec = plain_widget(param.ListSelector(default=[], objects=["a", "b"]), ["b"])
        assert spec["type"] is ChoiceMultiSelect
        assert spec["options"] == {"a": "a", "b": "b"}

    def test_a_selector_that_allows_none_has_an_empty_option(self):
        spec = plain_widget(
            param.Selector(default=None, objects=["x", "y"], allow_None=True), None
        )
        assert spec["type"] is ChoiceSelect
        assert spec["options"] == {EMPTY: None, "x": "x", "y": "y"}

    def test_a_selector_without_none_has_no_empty_option(self):
        spec = plain_widget(param.Selector(default="x", objects=["x", "y"]), "x")
        assert spec["type"] is ChoiceSelect and spec["options"] == {"x": "x", "y": "y"}

    def test_a_selector_keeps_a_value_its_objects_lack(self):
        spec = plain_widget(param.Selector(default="x", objects=["x", "y"]), "gone")
        assert spec["options"] == {"gone (not available)": "gone", "x": "x", "y": "y"}

    @pytest.mark.parametrize("cls", [param.Integer, param.Number])
    def test_an_optional_number_shows_none_as_empty(self, cls):
        class State(param.Parameterized):
            n = cls(default=None, allow_None=True, bounds=(1, None))

        state = State()
        form = pn.Param(state, parameters=["n"], widgets={"n": plain_widget(State.param.n, None)})
        assert form._widgets["n"].value is None
        # The browser shows the model's value; a plain number input sends 0 for None.
        assert form._widgets["n"].get_root().data.value is None
        form._widgets["n"].value = 3
        assert state.n == 3

    def test_other_fields_keep_panels_widget(self):
        assert plain_widget(param.String(), "") is None
        assert plain_widget(param.Integer(default=1), 1) is None


class TestInspectorChoices:
    async def test_declared_columns_become_a_dropdown_once_wired(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/pick")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        form = _form(editor)
        assert not isinstance(form._widgets["column"], pmui.Select)
        editor.connect(frame, "rows", pick, "rows")
        await _settle()
        form = _form(editor)
        column = form._widgets["column"]
        assert isinstance(column, pmui.Select)
        assert column.options == {
            EMPTY: "",
            "state (string)": "state",
            "year (integer)": "year",
            "capacity (number)": "capacity",
        }
        assert isinstance(form._widgets["columns"], pmui.MultiChoice)
        assert not isinstance(form._widgets["label"], (pmui.Select, pmui.AutocompleteInput))

    async def test_picking_a_choice_sets_the_config(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/pick")
        editor.connect(frame, "rows", pick, "rows")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        _form(editor)._widgets["column"].value = "year"
        assert editor._component_instances[pick].column == "year"
        await _settle()
        assert _form(editor)._widgets["column"].options[EMPTY] == ""

    async def test_choices_follow_the_input(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/pick")
        editor.connect(frame, "rows", pick, "rows")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        editor._component_instances[frame].wide = True
        await _settle()
        assert "when (datetime)" in _form(editor)._widgets["column"].options

    async def test_disconnecting_drops_the_choices(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/pick")
        editor.connect(frame, "rows", pick, "rows")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        assert isinstance(_form(editor)._widgets["column"], pmui.Select)
        editor.disconnect(frame, "rows", pick, "rows")
        await _settle()
        assert not isinstance(_form(editor)._widgets["column"], pmui.Select)

    async def test_hook_choices_and_suggestions(self, editor):
        tables, pick = editor.add_component("Demo/tables"), editor.add_component("Demo/table_pick")
        editor.connect(tables, "source", pick, "source")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        form = _form(editor)
        assert form._widgets["table"].options == {
            EMPTY: "",
            "population": "population",
            "turbines": "turbines",
        }
        label = form._widgets["label"]
        assert isinstance(label, pmui.AutocompleteInput)
        label.value = "Anything"
        assert editor._component_instances[pick].label == "Anything"

    async def test_declared_columns_do_not_wait_for_a_slow_hook(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/slow_pick")
        editor.connect(frame, "rows", pick, "rows")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle(0.2)
        loading = next(
            w
            for w in pn.Column(*editor._inspector._body).select(pmui.LinearProgress)
            if "fd-choices-loading" in w.css_classes
        )
        form = _form(editor)
        assert isinstance(form._widgets["column"], pmui.Select)
        assert not isinstance(form._widgets["label"], pmui.AutocompleteInput)
        assert loading.visible
        await _settle(0.8)
        assert not loading.visible
        assert isinstance(_form(editor)._widgets["label"], pmui.Select)

    async def test_a_config_set_elsewhere_keeps_its_value(self, editor):
        frame, pick = editor.add_component("Demo/frame"), editor.add_component("Demo/pick")
        editor.connect(frame, "rows", pick, "rows")
        select_on_canvas(editor._flow, node_id=pick)
        await _settle()
        editor._apply_config_patch(pick, {"column": "missing"})
        await _settle()
        column = _form(editor)._widgets["column"]
        assert column.value == "missing"
        assert column.options["missing (not available)"] == "missing"
        assert editor._component_instances[pick].column == "missing"


class TestSchemaTypes:
    def _state(self, schema):
        @register(component=True, config_schema=schema)
        def app(config):
            pass

        spec = build_component_spec(RegistryEntry.from_app(app, app_id="Demo/schema"))
        return spec, spec.config_state_class

    def test_json_schema_properties_are_typed(self):
        spec, cls = self._state(
            {
                "properties": {
                    "title": {"type": "string", "default": "Hi", "description": "Heading."},
                    "rows": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "scale": {"type": ["number", "null"], "default": None},
                    "show": {"type": "boolean", "default": True},
                    "tags": {"type": "array", "items": {"type": "string"}, "default": []},
                    "style": {"type": "object", "default": {}},
                    "free": {"default": 3},
                }
            }
        )
        p = cls.param
        assert isinstance(p.title, param.String) and p.title.doc == "Heading."
        assert isinstance(p.rows, param.Integer) and p.rows.bounds == (1, 50)
        assert isinstance(p.scale, param.Number) and p.scale.allow_None
        assert isinstance(p.show, param.Boolean)
        assert isinstance(p.tags, param.List) and p.tags.item_type is str
        assert isinstance(p.style, param.Dict)
        assert type(p.free) is param.Parameter
        with pytest.raises(ValueError):
            cls(rows=100)
        assert [f.type for f in spec.config][:2] == ["string", "integer"]

    def test_a_default_of_another_type_leaves_the_property_untyped(self):
        _, cls = self._state({"properties": {"n": {"type": "integer", "default": "many"}}})
        assert type(cls.param.n) is param.Parameter
        assert cls().n == "many"

    def test_json_schema_declares_choices(self):
        spec, _ = self._state(
            {
                "properties": {
                    "column": {
                        "type": "string",
                        "default": "",
                        "x-flowdash": {"choices": "columns", "of": "rows"},
                    }
                }
            }
        )
        assert spec.config[0].choices == {"choices": "columns", "of": "rows"}

    def test_pydantic_fields_are_typed(self):
        from typing import Literal

        from pydantic import BaseModel, Field

        class Model(BaseModel):
            title: str = "Hi"
            rows: int = Field(10, ge=1, le=50)
            scale: float | None = None
            show: bool = True
            mode: Literal["a", "b"] = "a"
            maybe: Literal["a", "b"] | None = None
            kinds: list[Literal["a", "b"]] = []
            tags: list[str] = []
            column: str = Field("", json_schema_extra={"x-flowdash": {"choices": "columns"}})

        spec, cls = self._state(Model)
        p = cls.param
        assert isinstance(p.title, param.String)
        assert isinstance(p.rows, param.Integer) and p.rows.bounds == (1, 50)
        assert isinstance(p.scale, param.Number) and p.scale.allow_None
        assert isinstance(p.show, param.Boolean)
        assert isinstance(p.mode, param.Selector) and p.mode.objects == ["a", "b"]
        assert isinstance(p.maybe, param.Selector) and p.maybe.allow_None
        assert p.maybe.objects == ["a", "b"]
        assert isinstance(p.kinds, param.ListSelector) and p.kinds.objects == ["a", "b"]
        assert isinstance(p.tags, param.List) and p.tags.item_type is str
        assert next(f for f in spec.config if f.name == "column").choices == {"choices": "columns"}

    def test_json_schema_enums_with_null_and_arrays_of_enums(self):
        _, cls = self._state(
            {
                "properties": {
                    "e": {"type": ["string", "null"], "enum": ["p", "q", None], "default": None},
                    "t": {"type": "array", "items": {"enum": ["a", "b"]}, "default": ["a"]},
                }
            }
        )
        assert isinstance(cls.param.e, param.Selector) and cls.param.e.allow_None
        assert cls.param.e.objects == ["p", "q"]
        assert isinstance(cls.param.t, param.ListSelector) and cls().t == ["a"]


class Color(enum.Enum):
    red = "red"
    blue = "blue"


def _schema_components():
    from typing import Literal

    from pydantic import BaseModel

    class Inner(BaseModel):
        a: int = 1

    class Model(BaseModel):
        window: tuple[int, int] = (1, 2)
        inner: Inner = Inner()
        color: Color = Color.red
        maybe_color: Color | None = None
        colors: list[Color] = []
        anything: object = None
        kinds: list[Literal["a", "b"]] = []
        mode: Literal["x", "y"] | None = None
        rows: int | None = None

    class Params(param.Parameterized):
        span = param.Tuple(default=(1, 2))
        bounds = param.Range(default=(0, 1))
        anything = param.Parameter(default=None)
        day = param.Date(default=None)
        limit = param.Integer(default=None, allow_None=True, bounds=(1, None))

    schema = {
        "properties": {
            "free": {"default": [1, 2]},
            "mismatch": {"type": "integer", "default": "many"},
            "kinds": {"type": "array", "items": {"enum": ["a", "b"]}, "default": ["a"]},
            "maybe": {"type": ["string", "null"], "enum": ["p", "q", None], "default": None},
            "pair": {"type": "array", "default": (1, 2)},
            "nested": {"type": "object", "default": {"a": [1]}},
        }
    }
    return {"pydantic": Model, "param": Params, "json": schema}


class TestPlacingSchemaComponents:
    @pytest.mark.parametrize("kind", ["pydantic", "param", "json"])
    async def test_every_kind_of_field_can_be_placed_and_shown(self, kind):
        @register(
            page=False, component=True, title="Schema", config_schema=_schema_components()[kind]
        )
        def app(config):
            return "schema"

        editor = FlowDash(
            {"Demo/schema": app}, notifications=False, include_builtin_components=False
        )
        node = editor.add_component("Demo/schema")
        state = editor.graph.get_config_state(node)
        select_on_canvas(editor._flow, node_id=node)
        form = _form(editor)
        names = [n for n in state.param if n != "name"]
        assert names and all(n in form._widgets for n in names)

    async def test_ref_fields_are_labelled_by_their_own_name(self):
        @register(
            page=False,
            component=True,
            title="Schema",
            config_schema=_schema_components()["pydantic"],
        )
        def app(config):
            return "schema"

        spec = build_component_spec(RegistryEntry.from_app(app, app_id="Demo/schema"))
        labels = {f.name: f.label for f in spec.config}
        assert labels["inner"] == "Inner" and labels["maybe_color"] == "Maybe Color"
        assert labels["colors"] == "Colors"
        p = spec.config_state_class.param
        assert isinstance(p.colors, param.ListSelector) and p.colors.objects == ["red", "blue"]

    async def test_pydantic_defaults_are_json_data(self):
        @register(
            page=False,
            component=True,
            title="Schema",
            config_schema=_schema_components()["pydantic"],
        )
        def app(config):
            return "schema"

        editor = FlowDash(
            {"Demo/schema": app}, notifications=False, include_builtin_components=False
        )
        state = editor.graph.get_config_state(editor.add_component("Demo/schema"))
        assert (state.window, state.inner, state.color) == ([1, 2], {"a": 1}, "red")
        assert isinstance(state.param.color, param.Selector)
        assert state.param.color.objects == ["red", "blue"]

    async def test_the_form_offers_multi_choice_and_clearable_selects(self):
        @register(
            page=False,
            component=True,
            title="Schema",
            config_schema=_schema_components()["pydantic"],
        )
        def app(config):
            return "schema"

        editor = FlowDash(
            {"Demo/schema": app}, notifications=False, include_builtin_components=False
        )
        node = editor.add_component("Demo/schema")
        select_on_canvas(editor._flow, node_id=node)
        widgets = _form(editor)._widgets
        assert isinstance(widgets["kinds"], pmui.MultiChoice)
        assert widgets["mode"].options == {EMPTY: None, "x": "x", "y": "y"}
        assert widgets["maybe_color"].options == {EMPTY: None, "red": "red", "blue": "blue"}
        assert widgets["rows"].value is None
        widgets["mode"].value = "y"
        widgets["mode"].value = None
        assert editor.graph.get_config_state(node).mode is None


class TestInspectorCoalescing:
    async def test_a_burst_of_input_changes_asks_at_most_twice_more(self, editor):
        frame, counted = editor.add_component("Demo/frame"), editor.add_component("Demo/counted")
        editor.connect(frame, "rows", counted, "rows")
        select_on_canvas(editor._flow, node_id=counted)
        await _settle(0.5)
        CALLS.clear()
        source = editor._component_instances[frame]
        for _ in range(5):
            source.wide = not source.wide
        await _settle(0.8)
        assert 1 <= len(CALLS) <= 2
        assert "when" in CALLS[-1].columns


@register(page=False, component=True, title="Params", config_schema=_schema_components()["param"])
def params_app(config):
    return "params"


@register(page=False, component=True, title="Json", config_schema=_schema_components()["json"])
def json_app(config):
    return "json"


@register(page=False, component=True, title="Modes", config=["mode"])
class Modes(Viewer):
    mode = param.Selector(default="a", objects=["a", "b"])

    def __panel__(self):
        return self.mode


class TestSavedConfig:
    def _reload(self, editor):
        data = json.loads(json.dumps(editor.to_model().to_dict()))
        reloaded = FlowDash(
            editor.components, notifications=False, include_builtin_components=False
        )
        reloaded.load_model(DashboardModel.from_dict(data))
        return reloaded

    @pytest.mark.parametrize(("app", "field"), [(params_app, "span"), (json_app, "pair")])
    async def test_a_tuple_survives_save_and_reload(self, app, field):
        editor = FlowDash({"Demo/app": app}, notifications=False, include_builtin_components=False)
        node = editor.add_component("Demo/app")
        editor._apply_config_patch(node, {field: [5, 6]})
        assert getattr(editor.graph.get_config_state(node), field) == (5, 6)
        reloaded = self._reload(editor)
        assert getattr(reloaded.graph.get_config_state(node), field) == (5, 6)

    async def test_a_saved_selector_value_its_objects_lack_is_kept_and_flagged(self):
        editor = FlowDash(
            {"Demo/modes": Modes}, notifications=False, include_builtin_components=False
        )
        node = editor.add_component("Demo/modes")
        editor._tile_items[0]["config"] = {"mode": "gone"}
        reloaded = self._reload(editor)
        state = reloaded.graph.get_config_state(node)
        assert state.mode == "gone"
        assert reloaded._tile_items[0]["config"] == {"mode": "gone"}
        select_on_canvas(reloaded._flow, node_id=node)
        assert _form(reloaded)._widgets["mode"].options == {
            "gone (not available)": "gone",
            "a": "a",
            "b": "b",
        }
