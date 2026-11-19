"""Tests for the choices config fields offer, from declared columns and the config_choices hook."""

import asyncio
import time

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import Choices, columns_of, register, register_port_type
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.config_choices import compute_choices, node_choices
from panel_flowdash.editor import FlowDash
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.choice_widgets import CHOOSE, ChoiceSelect, SuggestInput, choice_widget
from panel_flowdash.ui.status_bar import select_on_canvas


class LazyTable:
    """A table that lists its columns without holding rows, like a query."""

    def __init__(self, columns):
        self.schema = columns


LAZY_TYPE = register_port_type(
    "tests.LazyTable", columns=lambda value: dict(value.schema) if value is not None else None
)
SUB_TYPE = register_port_type("tests.SubTable", subtype_of=[LAZY_TYPE])


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


COMPONENTS = {
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

    def test_a_registered_reader_lists_columns(self):
        table = LazyTable({"a": "integer", "b": None})
        assert columns_of(table, LAZY_TYPE) == [("a", "integer"), ("b", None)]

    def test_subtypes_inherit_the_reader(self):
        assert columns_of(LazyTable({"a": "string"}), SUB_TYPE) == [("a", "string")]

    def test_readers_may_return_names_or_pairs(self):
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
        assert spec["options"] == {"a (string)": "a", "b": "b"}

    def test_an_empty_value_shows_a_prompt(self):
        spec = choice_widget(param.String(), Choices(["a"]), "")
        assert spec["options"] == {CHOOSE: "", "a": "a"}

    def test_a_value_not_among_the_choices_stays(self):
        spec = choice_widget(param.String(), Choices(["a"]), "gone")
        assert spec["options"] == {"gone (not available)": "gone", "a": "a"}

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
            {"type": ChoiceSelect, "options": {CHOOSE: "", "a": "a"}},
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
            CHOOSE: "",
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
        assert CHOOSE not in _form(editor)._widgets["column"].options

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
            CHOOSE: "",
            "population": "population",
            "turbines": "turbines",
        }
        label = form._widgets["label"]
        assert isinstance(label, pmui.AutocompleteInput)
        label.value = "Anything"
        assert editor._component_instances[pick].label == "Anything"

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
            tags: list[str] = []
            window: tuple[int, int] = (1, 2)
            column: str = Field("", json_schema_extra={"x-flowdash": {"choices": "columns"}})

        spec, cls = self._state(Model)
        p = cls.param
        assert isinstance(p.title, param.String)
        assert isinstance(p.rows, param.Integer) and p.rows.bounds == (1, 50)
        assert isinstance(p.scale, param.Number) and p.scale.allow_None
        assert isinstance(p.show, param.Boolean)
        assert isinstance(p.mode, param.Selector) and p.mode.objects == ["a", "b"]
        assert isinstance(p.tags, param.List) and p.tags.item_type is str
        assert isinstance(p.window, param.Tuple) and cls().window == (1, 2)
        assert next(f for f in spec.config if f.name == "column").choices == {"choices": "columns"}


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
