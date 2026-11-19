"""Config check problems that say where they are, and the words code fields complete."""

import copy
import pickle

import pandas as pd
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import CheckDiagnostic, CheckWarning, Choices, FlowDash, register
from panel_flowdash.component_spec import build_component_spec
from panel_flowdash.config_check import CheckResult, check_node, diagnostic, run_check
from panel_flowdash.config_choices import code_fields, compute_completions, node_completions
from panel_flowdash.registry import RegistryEntry


class ForeignDiagnostic(str):
    """A diagnostic type of a component library, read by its attributes."""

    def __new__(cls, message, **attrs):
        obj = super().__new__(cls, message)
        for name, value in attrs.items():
            setattr(obj, name, value)
        return obj


def check_query(config, inputs):
    sql = config["sql"]
    if "BAD" in sql:
        return CheckDiagnostic(
            "No column BAD.", field="sql", line=2, column=7, end_column=10, suggestion="good"
        )
    if "RAISE" in sql:
        raise ValueError(CheckDiagnostic("Does not parse.", field="sql", line=1, column=1))
    if "FOREIGN" in sql:
        return [ForeignDiagnostic("Foreign problem.", line=3, column=2, field="sql"), "plain"]
    if "WARN" in sql:
        return [CheckWarning("Not verified."), CheckDiagnostic("Positioned.", line=1)]
    return None


@register(component=True, config=["sql", "limit"], check_config=check_query)
class Query(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)
    other = param.DataFrame(default=None, allow_None=True)
    sql = param.String(default="", metadata={"flowdash": {"widget": "code", "language": "sql"}})
    limit = param.Integer(default=10)

    def __panel__(self):
        return self.sql


@register(
    page=False, component=True, title="Frame", provides=[{"key": "rows", "type": "DataFrame"}]
)
class Frame(Viewer):
    @param.output(param.DataFrame)
    def rows(self):
        return pd.DataFrame({"year": [2020], "capacity": [1.5]})

    def __panel__(self):
        return "frame"


def _spec(app, app_id="Demo/x"):
    return build_component_spec(RegistryEntry.from_app(app, app_id=app_id))


class TestCheckDiagnostic:
    def test_is_its_message(self):
        found = CheckDiagnostic("Bad.", field="sql", line=2, column=3, end_column=5)
        assert found == "Bad."
        assert isinstance(found, str)
        assert (found.field, found.line, found.column, found.end_column) == ("sql", 2, 3, 5)
        assert found.suggestion is None

    def test_repr_shows_the_position(self):
        assert repr(CheckDiagnostic("Bad.", line=2)) == "CheckDiagnostic('Bad.', line=2)"

    def test_copies_keep_the_position(self):
        found = CheckDiagnostic("Bad.", field="sql", line=2, column=3, suggestion="x")
        for copied in (copy.deepcopy(found), pickle.loads(pickle.dumps(found))):
            assert repr(copied) == repr(found)

    def test_a_plain_string_is_not_a_diagnostic(self):
        assert diagnostic("plain") is None

    def test_a_foreign_type_is_read_by_its_attributes(self):
        found = diagnostic(ForeignDiagnostic("Odd.", line=4, column=0, end_column="x", field=1))
        assert isinstance(found, CheckDiagnostic)
        assert (found.field, found.line, found.column, found.end_column) == (None, 4, None, None)

    def test_a_column_without_a_line_is_dropped(self):
        found = diagnostic(ForeignDiagnostic("Odd.", field="sql", column=3))
        assert (found.field, found.line, found.column) == ("sql", None, None)

    def test_a_result_keeps_diagnostics(self):
        result = CheckResult([ForeignDiagnostic("A.", field="sql", line=1), "B."])
        assert result == ["A.", "B."]
        assert [type(p) for p in result] == [CheckDiagnostic, str]

    def test_diagnostics_of_a_field(self):
        result = CheckResult(
            [
                CheckDiagnostic("In sql.", field="sql", line=1),
                CheckDiagnostic("Somewhere.", line=2),
                CheckDiagnostic("In limit.", field="limit"),
                "Plain.",
            ]
        )
        assert result.diagnostics() == ["In sql.", "Somewhere.", "In limit."]
        assert result.diagnostics("sql") == ["In sql.", "Somewhere."]
        assert result.diagnostics("limit") == ["Somewhere.", "In limit."]


class TestRunCheck:
    @pytest.fixture
    def spec(self):
        return _spec(Query)

    async def test_a_returned_diagnostic_is_kept(self, spec):
        result = await run_check(spec, {"sql": "SELECT BAD"}, {})
        (found,) = result.diagnostics("sql")
        assert (found.line, found.column, found.end_column, found.suggestion) == (2, 7, 10, "good")

    async def test_a_raised_diagnostic_is_kept(self, spec):
        result = await run_check(spec, {"sql": "RAISE"}, {})
        assert result == ["Does not parse."]
        assert result.diagnostics("sql")[0].line == 1

    async def test_a_foreign_diagnostic_is_kept(self, spec):
        result = await run_check(spec, {"sql": "FOREIGN"}, {})
        assert result == ["Foreign problem.", "plain"]
        assert [(d.line, d.column) for d in result.diagnostics("sql")] == [(3, 2)]

    async def test_warnings_stay_apart(self, spec):
        result = await run_check(spec, {"sql": "WARN"}, {})
        assert result == ["Positioned."]
        assert result.warnings == ["Not verified."]

    async def test_check_node_keeps_diagnostics(self):
        editor = FlowDash(
            {"Demo/query": Query}, notifications=False, include_builtin_components=False
        )
        node = editor.add_component("Demo/query")
        result = await check_node(editor, node, {"sql": "BAD"})
        assert result.diagnostics("sql")[0].column == 7


class TestCompletions:
    def test_code_fields(self):
        assert code_fields(_spec(Query)) == ["sql"]

    async def test_input_columns_by_default(self):
        rows = pd.DataFrame({"year": [2020], "state": ["TX"]})
        other = pd.DataFrame({"state": ["TX"], "capacity": [1.5]})
        words = await compute_completions(_spec(Query), {}, {"rows": rows, "other": other})
        assert list(words) == ["sql"]
        assert words["sql"].values() == ["year", "state", "capacity"]
        assert words["sql"].types == {"year": "integer", "state": "string", "capacity": "number"}
        assert words["sql"].restrict is False

    async def test_nothing_without_tabular_inputs(self):
        assert await compute_completions(_spec(Query), {}, {}) == {}

    async def test_nothing_without_code_fields(self):
        @register(component=True, config=["label"])
        class Plain(Viewer):
            rows = param.DataFrame(default=None, allow_None=True)
            label = param.String(default="")

        assert (
            await compute_completions(_spec(Plain), {}, {"rows": pd.DataFrame({"a": [1]})}) == {}
        )

    async def test_the_hook_replaces_the_default(self):
        async def hook(config, inputs):
            return {"sql": Choices(["p_year"], types={"p_year": "int64"}), "limit": ["x"]}

        @register(component=True, config=["sql", "limit"], config_completions=hook)
        class Hooked(Viewer):
            rows = param.DataFrame(default=None, allow_None=True)
            sql = param.String(default="", metadata={"flowdash": {"widget": "code"}})
            limit = param.Integer(default=1)

        words = await compute_completions(_spec(Hooked), {}, {"rows": pd.DataFrame({"a": [1]})})
        assert list(words) == ["sql"]
        assert words["sql"].values() == ["p_year"]
        assert words["sql"].types == {"p_year": "int64"}

    @pytest.mark.parametrize("returned", [None, ["a"]])
    async def test_a_hook_without_words_keeps_the_default(self, returned, caplog):
        @register(component=True, config=["sql"], config_completions=lambda c, i: returned)
        class Empty(Viewer):
            rows = param.DataFrame(default=None, allow_None=True)
            sql = param.String(default="", metadata={"flowdash": {"widget": "code"}})

        words = await compute_completions(_spec(Empty), {}, {"rows": pd.DataFrame({"a": [1]})})
        assert words["sql"].values() == ["a"]
        if returned is not None:
            assert "must return a dict" in caplog.text

    async def test_a_failing_hook_keeps_the_default(self, caplog):
        def hook(config, inputs):
            raise RuntimeError("boom")

        @register(component=True, config=["sql"], config_completions=hook)
        class Failing(Viewer):
            sql = param.String(default="", metadata={"flowdash": {"widget": "code"}})

        assert await compute_completions(_spec(Failing), {}, {}) == {}
        assert "config completions" in caplog.text

    async def test_node_completions_read_live_inputs(self):
        editor = FlowDash(
            {"Demo/query": Query, "Demo/frame": Frame},
            notifications=False,
            include_builtin_components=False,
        )
        frame, query = editor.add_component("Demo/frame"), editor.add_component("Demo/query")
        assert await node_completions(editor, query) == {}
        editor.connect(frame, "rows", query, "rows")
        words = await node_completions(editor, query)
        assert words["sql"].values() == ["year", "capacity"]
