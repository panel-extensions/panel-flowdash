"""Global filters: column matching, opt-outs, local filters, the protocol, the YAML and the stores."""

import datetime as dt
import json
import sqlite3
from contextlib import closing

import pandas as pd
import panel as pn
import param
import pytest
import yaml
from panel.viewable import Viewer

from panel_flowdash import FlowDash, MemoryDashboardStore, Predicate, register, request_filters
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
)
from panel_flowdash.dashboard_store import DashboardModel, DashboardStore
from panel_flowdash.global_filters import (
    MAX_OPTIONS,
    FilterError,
    FilterTarget,
    check_filters,
    column_span,
    filter_frame,
    predicate,
    resolve_view,
    skipped_filters,
)
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.turns import change_labels

FLEET = pd.DataFrame(
    {
        "state": ["TX", "OK", "TX", "CA", "IA"],
        "year": [2001, 2005, 2010, 2020, 2021],
        "mw": [1.0, 2.0, 3.0, 4.0, 5.0],
    }
)
BY_REGION = pd.DataFrame({"region": ["South", "West"], "mw": [6.0, 4.0]})


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
class Fleet(Viewer):
    data = param.DataFrame(default=FLEET)

    def __panel__(self):
        return pn.pane.Str("fleet")


@register(component=True, page=False, provides=[{"key": "data", "type": "DataFrame"}])
class Regions(Viewer):
    data = param.DataFrame(default=BY_REGION)

    def __panel__(self):
        return pn.pane.Str("regions")


@register(
    component=True, page=False, tags=["view"], requires=[{"key": "table", "type": "DataFrame"}]
)
class Table(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Str("table")


@register(
    component=True,
    page=False,
    tags=["transform", "filter"],
    requires=[{"key": "data", "type": "DataFrame"}],
    provides=[{"key": "filtered", "type": "DataFrame"}],
)
class Recent(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("data")
    def filtered(self):
        return None if self.data is None else self.data[self.data["year"] >= 2010]

    def __panel__(self):
        return pn.pane.Str("recent")


@register(component=True, page=False, tags=["view"])
class Query(Viewer):
    """A view filtering in its own query, as a SQL-backed component would."""

    received = param.List(default=[])

    def __flowdash_columns__(self):
        return ["state", "year"]

    def __flowdash_apply_filters__(self, filters):
        self.received = [*self.received, dict(filters)]

    def __flowdash_column_values__(self, column):
        return ["NM"] if column == "state" else None

    def __panel__(self):
        return pn.pane.Str("query")


@register(component=True, page=False, provides=[{"key": "spec", "type": "dict"}])
class Spec(Viewer):
    """A source handing on a query, not a DataFrame, as a Lumen pipeline would."""

    spec = param.Dict(default={"columns": []})

    def __panel__(self):
        return pn.pane.Str("spec")


@register(component=True, page=False, tags=["view"], requires=[{"key": "spec", "type": "dict"}])
class LateQuery(Viewer):
    """A view filtering itself whose columns are known only once its query arrives."""

    spec = param.Dict(default=None, allow_None=True)
    extra = param.List(default=[])
    received = param.List(default=[])
    unsupported = param.Dict(default={})

    def __flowdash_columns__(self):
        if self.spec is None and not self.extra:
            return None
        return [*(self.spec or {}).get("columns", []), *self.extra]

    def __flowdash_apply_filters__(self, filters):
        self.received = [*self.received, dict(filters)]
        if self.unsupported == {"raise": True}:
            raise RuntimeError("the query is broken")
        return {c: r for c, r in self.unsupported.items() if c in filters}

    def __panel__(self):
        return pn.pane.Str("late")


COMPONENTS = {
    "T/Spec": Spec,
    "T/LateQuery": LateQuery,
    "T/Fleet": Fleet,
    "T/Regions": Regions,
    "T/Table": Table,
    "T/Recent": Recent,
    "T/Query": Query,
}


@pytest.fixture
def ops():
    editor = FlowDash(COMPONENTS, notifications=False)
    ops = DashboardOps(editor)
    ops.add_node("T/Fleet", {}, name="fleet")
    ops.add_node("T/Regions", {}, name="regions")
    ops.add_node("T/Recent", {}, name="recent")
    for name in ("by_state", "reference", "regional", "latest"):
        ops.add_node("T/Table", {}, name=name)
    ops.connect("fleet.data", "by_state.table")
    ops.connect("fleet.data", "reference.table")
    ops.connect("regions.data", "regional.table")
    ops.connect("fleet.data", "recent.data")
    ops.connect("recent.filtered", "latest.table")
    yield ops
    editor.dispose()


def rows(ops, name):
    table = ops.editor._component_instances[ops._instance(name)].table
    return None if table is None else table["mw"].tolist()


def notes(ops, name):
    return ops.editor._global_filters.notes(ops._instance(name))


class TestMatching:
    def test_a_filter_reaches_every_view_with_the_column(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "widget": "select"}])
        assert rows(ops, "by_state") == [1.0, 2.0, 3.0, 4.0, 5.0]
        editor.set_filter_value("state", ["TX"])
        assert rows(ops, "by_state") == rows(ops, "reference") == [1.0, 3.0]
        assert rows(ops, "regional") == [6.0, 4.0]
        editor.set_filter_value("state", None)
        assert rows(ops, "by_state") == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_sources_and_transforms_are_not_filtered(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "value": ["TX"]}])
        fleet = editor.graph.get_state(ops._instance("fleet")).data
        assert len(fleet) == 5
        assert len(editor._component_instances[ops._instance("recent")].data) == 5

    def test_a_range_filter(self, ops):
        ops.editor.set_filters([{"column": "year", "widget": "range", "value": [2005, 2020]}])
        assert rows(ops, "by_state") == [2.0, 3.0, 4.0]
        ops.editor.set_filter_value("year", [None, 2005])
        assert rows(ops, "by_state") == [1.0, 2.0]

    def test_the_widget_follows_the_column_type(self, ops):
        filters = ops.editor._global_filters
        assert filters.widget({"column": "year"}) == "range"
        assert filters.widget({"column": "state"}) == "select"
        assert filters.options("state") == ["CA", "IA", "OK", "TX"]
        assert filters.span("year") == (2001, 2021)

    def test_new_data_is_filtered_as_it_arrives(self, ops):
        ops.editor.set_filters([{"column": "state", "value": ["CA"]}])
        fleet = ops.editor._component_instances[ops._instance("fleet")]
        fleet.data = pd.concat([FLEET, FLEET])
        assert rows(ops, "by_state") == [4.0, 4.0]

    def test_columns_are_added_to_the_menu(self, ops):
        assert ops.editor._global_filters.available_columns() == ["mw", "region", "state", "year"]
        ops.editor.set_filters([{"column": "state"}])
        assert "state" not in ops.editor._global_filters.available_columns()


class TestOptOutAndHints:
    def test_a_view_opts_out_and_back_in_as_undoable_edits(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "value": ["TX"]}])
        editor.exclude_from_filter("state", ops._instance("reference"))
        assert rows(ops, "reference") == [1.0, 2.0, 3.0, 4.0, 5.0]
        assert rows(ops, "by_state") == [1.0, 3.0]
        assert editor.filters[0]["exclude"] == [ops._instance("reference")]
        editor.undo()
        assert rows(ops, "reference") == [1.0, 3.0]
        editor.exclude_from_filter("state", ops._instance("reference"))
        editor.exclude_from_filter("state", ops._instance("reference"), excluded=False)
        assert "exclude" not in editor.filters[0]
        assert rows(ops, "reference") == [1.0, 3.0]

    def test_a_view_without_the_column_says_so(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "label": "State"}])
        assert notes(ops, "regional")[1] == ""
        editor.set_filter_value("state", ["TX"])
        _, hint, detail = notes(ops, "regional")
        assert hint == "Not filtered by State"
        assert "no column 'state'" in detail
        assert notes(ops, "by_state")[1] == ""

    def test_an_opted_out_view_says_so(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "value": ["TX"]}])
        editor.exclude_from_filter("state", ops._instance("reference"))
        _, hint, detail = notes(ops, "reference")
        assert hint == "Not filtered by state"
        assert "opts out" in detail

    def test_the_hint_is_on_the_tile(self, ops):
        editor = ops.editor
        editor.mode = "dashboard"
        editor.set_filters([{"column": "state", "value": ["TX"]}])
        tile = editor._inspect_tiles[ops._instance("regional")]
        assert tile.filter_notes.visible
        assert [o.label for o in tile.filter_notes] == ["Not filtered by state"]
        editor.set_filter_value("state", None)
        assert not tile.filter_notes.visible

    def test_removing_a_view_drops_its_opt_out(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state"}])
        editor.exclude_from_filter("state", ops._instance("reference"))
        ops.remove_node("reference")
        assert editor.filters == [{"column": "state"}]


class TestLocalFilters:
    def test_global_and_local_filters_combine(self, ops):
        ops.editor.set_filters([{"column": "state", "value": ["TX"]}])
        assert rows(ops, "latest") == [3.0]

    def test_filter_nodes_are_chips_on_the_views_they_feed(self, ops):
        chips, _, _ = notes(ops, "latest")
        assert chips == [
            (ops._instance("recent"), ops.editor._instance_title(ops._instance("recent")))
        ]
        assert notes(ops, "by_state")[0] == []
        ops.editor.mode = "dashboard"
        tile = ops.editor._inspect_tiles[ops._instance("latest")]
        assert [o.icon for o in tile.filter_notes] == ["filter_alt"]


class TestProtocol:
    def test_a_component_filtering_itself_gets_the_predicates_of_its_columns(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        ops = DashboardOps(editor)
        ops.add_node("T/Query", {}, name="query")
        query = editor._component_instances[ops._instance("query")]
        assert isinstance(query, FilterTarget)
        editor.set_filters([{"column": "state"}, {"column": "mw", "widget": "range"}])
        editor.set_filter_value("state", ["TX"])
        editor.set_filter_value("mw", [1, 2])
        assert query.received[-1] == {"state": Predicate("state", "in", ("TX",))}
        assert "NM" in editor._global_filters.options("state")
        editor.exclude_from_filter("state", ops._instance("query"))
        assert query.received[-1] == {}
        _, hint, _ = editor._global_filters.notes(ops._instance("query"))
        assert hint == "Not filtered by state, mw"
        editor.dispose()


@pytest.fixture
def late():
    editor = FlowDash(COMPONENTS, notifications=False)
    ops = DashboardOps(editor)
    ops.add_node("T/Spec", {}, name="spec")
    ops.add_node("T/LateQuery", {}, name="late")
    ops.connect("spec.spec", "late.spec")
    editor.set_filters([{"column": "state", "label": "State", "value": ["TX"]}])
    yield ops
    editor.dispose()


def instance(ops, name):
    return ops.editor._component_instances[ops._instance(name)]


class TestLateColumns:
    def test_a_new_query_at_an_input_reads_the_columns_again(self, late):
        query = instance(late, "late")
        assert query.received[-1:] in ([], [{}])
        assert notes(late, "late")[1] == "Not filtered by State"
        instance(late, "spec").spec = {"columns": ["state", "year"]}
        assert query.received[-1] == {"state": Predicate("state", "in", ("TX",))}
        assert notes(late, "late")[1] == ""
        assert "year" in late.editor._global_filters.available_columns()

    def test_request_filters_hands_the_filters_over_again(self, late):
        query = instance(late, "late")
        query.extra = ["state"]
        assert query.received[-1:] in ([], [{}])
        assert request_filters(query) is True
        assert query.received[-1] == {"state": Predicate("state", "in", ("TX",))}
        count = len(query.received)
        assert request_filters(query) is True
        assert len(query.received) == count + 1
        assert request_filters(object()) is False

    def test_requesting_filters_while_taking_them_hands_them_over_once_more(self, late):
        query = instance(late, "late")
        calls = []

        def apply(filters):
            calls.append(dict(filters))
            assert request_filters(query) is True

        query.__flowdash_apply_filters__ = apply
        query.extra = ["state"]
        request_filters(query)
        assert calls == [{"state": Predicate("state", "in", ("TX",))}] * 2
        calls.clear()
        late.editor.set_filter_value("state", ["OK"])
        assert calls == [{"state": Predicate("state", "in", ("OK",))}] * 2


class TestReportedSkips:
    def test_a_skipped_filter_shows_as_a_hint_with_its_reason(self, late):
        late.editor.mode = "dashboard"
        query = instance(late, "late")
        query.unsupported = {"state": "is a number in this table"}
        instance(late, "spec").spec = {"columns": ["state"]}
        assert query.received[-1] == {"state": Predicate("state", "in", ("TX",))}
        _, hint, detail = notes(late, "late")
        assert hint == "Not filtered by State"
        assert detail == "State: is a number in this table"
        tile = late.editor._inspect_tiles[late._instance("late")]
        assert [o.label for o in tile.filter_notes] == ["Not filtered by State"]
        query.unsupported = {}
        request_filters(query)
        assert notes(late, "late")[1] == ""
        assert not tile.filter_notes.visible

    def test_a_failure_skips_every_filter_handed_over(self, late):
        query = instance(late, "late")
        query.unsupported = {"raise": True}
        instance(late, "spec").spec = {"columns": ["state"]}
        _, hint, detail = notes(late, "late")
        assert hint == "Not filtered by State"
        assert "the query is broken" in detail

    def test_skipped_filters_takes_a_mapping_or_pairs(self):
        assert skipped_filters(None, {"a"}) == {}
        assert skipped_filters({"a": "no", "z": "x"}, {"a"}) == {"a": "no"}
        assert skipped_filters([("a", "no")], {"a"}) == {"a": "no"}
        assert skipped_filters(["a"], {"a"}) == {"a": "could not be applied"}
        with pytest.raises(TypeError):
            skipped_filters(3, {"a"})


class TestSelectionCap:
    def test_a_selection_larger_than_the_options_limit_is_refused(self, ops):
        editor = ops.editor
        editor.set_filters([{"column": "state", "widget": "select"}])
        editor.set_filter_value("state", [f"S{i}" for i in range(MAX_OPTIONS)])
        with pytest.raises(FilterError, match=f"at most {MAX_OPTIONS} values"):
            editor.set_filter_value("state", [f"S{i}" for i in range(MAX_OPTIONS + 1)])
        with pytest.raises(FilterError, match="at most"):
            check_filters([{"column": "state", "value": list(range(MAX_OPTIONS + 1))}])

    def test_options_stop_at_the_limit(self, ops):
        feed(ops, "fleet", pd.DataFrame({"state": [f"S{i:05}" for i in range(3 * MAX_OPTIONS)]}))
        feed(ops, "regions", pd.DataFrame({"state": [f"R{i:05}" for i in range(MAX_OPTIONS)]}))
        assert len(ops.editor._global_filters.options("state")) == MAX_OPTIONS


class TestPure:
    def test_filter_frame_skips_missing_columns(self):
        keep = {"state": Predicate("state", "in", ("TX",)), "x": Predicate("x", "in", (1,))}
        assert filter_frame(FLEET, keep)["mw"].tolist() == [1.0, 3.0]
        assert filter_frame(BY_REGION, keep) is BY_REGION
        assert filter_frame("text", keep) == "text"

    def test_predicates(self):
        assert predicate({"column": "s"}, None) is None
        assert predicate({"column": "s", "widget": "select"}, []) is None
        assert predicate({"column": "y", "widget": "range"}, [None, None]) is None
        dated = predicate({"column": "d", "widget": "range"}, ["2020-01-01", None])
        assert str(dated) == "d >= 2020-01-01"

    def test_resolve_view(self):
        filters = [{"column": "a"}, {"column": "b", "exclude": ["v"]}, {"column": "c"}]
        active = {c: Predicate(c, "in", (1,)) for c in ("a", "b")}
        applied, skipped = resolve_view(filters, active, "v", {"b"})
        assert applied == {}
        assert skipped == {"a": "missing", "b": "excluded"}

    @pytest.mark.parametrize(
        ("filters", "message"),
        [
            ({}, "must be a list"),
            ([{"label": "x"}], r"filters\[0\].column: must name a column"),
            ([{"column": "a"}, {"column": "a"}], "a second time"),
            ([{"column": "a", "widget": "slider"}], "must be select or range"),
            ([{"column": "a", "widget": "range", "value": [1]}], r"\[start, end\]"),
            ([{"column": "a", "value": "TX"}], "must be a list"),
            ([{"column": "a", "exclude": "v"}], "list of node names"),
            ([{"column": "a", "pinned": True}], "unknown key 'pinned'"),
        ],
    )
    def test_refuses_malformed_filters(self, filters, message):
        with pytest.raises(FilterError, match=message):
            check_filters(filters)


FILTERS = """\
filters:
- column: state
  label: State
  widget: select
  value: [TX]
  exclude: [reference]
- column: year
  widget: range
"""


class TestYAML:
    def test_round_trip(self, ops):
        editor = ops.editor
        editor.set_filters(
            [
                {
                    "column": "state",
                    "label": "State",
                    "widget": "select",
                    "value": ["TX"],
                    "exclude": [ops._instance("reference")],
                },
                {"column": "year", "widget": "range"},
            ]
        )
        doc = dashboard_to_dict(editor)
        assert doc["filters"] == yaml.safe_load(FILTERS)["filters"]
        text = ops.get_dashboard()
        assert [i for i in ops.validate(text) if i.severity == "error"] == []
        editor.set_filters([])
        ops.replace_yaml(text, revision=ops.revision())
        assert editor.filters == doc["filters"]
        assert rows(ops, "by_state") == [1.0, 3.0]
        assert rows(ops, "reference") == [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_no_filters_key_is_no_filters(self, ops):
        assert "filters" not in dashboard_to_dict(ops.editor)
        assert "filters" not in dashboard_from_yaml(ops.get_dashboard())

    def test_the_schema_accepts_filters(self, ops):
        jsonschema = pytest.importorskip("jsonschema")
        from panel_flowdash.agent import dashboard_schema

        ops.editor.set_filters([{"column": "state", "value": ["TX"]}])
        schema = dashboard_schema(ops.editor.component_specs)
        doc = dashboard_to_dict(ops.editor)
        jsonschema.validate(doc, schema)
        doc["filters"][0]["widget"] = "slider"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(doc, schema)

    @pytest.mark.parametrize(
        ("filters", "message"),
        [
            ([{"column": "state", "exclude": ["nope"]}], r"filters\[0\].exclude: names no node"),
            ([{"column": "state", "pinned": True}], r"filters\[0\]: unknown key 'pinned'"),
            ([{"column": ""}], r"filters\[0\].column"),
        ],
    )
    def test_refuses_malformed_filters(self, ops, filters, message):
        doc = yaml.safe_load(ops.get_dashboard())
        doc["filters"] = filters
        with pytest.raises(DashboardYAMLError, match=message):
            dashboard_from_yaml(yaml.safe_dump(doc))

    def test_changes_are_labelled(self, ops):
        before = dashboard_to_dict(ops.editor)
        ops.editor.set_filters([{"column": "state"}])
        assert change_labels(before, dashboard_to_dict(ops.editor)) == ["Changed the filters"]


class TestStores:
    def _model(self):
        model = DashboardModel(dashboard_id="d1", user_id="alice", title="T")
        model.filters = [{"column": "state", "value": ["TX"], "exclude": ["n1"]}]
        return model

    def test_an_older_model_loads_without_filters(self):
        data = {"dashboard_id": "d", "user_id": "u", "title": "T", "version": 3, "items": []}
        assert DashboardModel.from_dict(data).filters == []
        assert "filters" not in DashboardModel.from_dict(data).to_dict()

    def test_memory_store(self):
        store = MemoryDashboardStore()
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").filters == self._model().filters

    def test_sqlite_store_drops_malformed_filters(self, tmp_path):
        store = DashboardStore(tmp_path / "db.sqlite")
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").filters == self._model().filters
        with closing(sqlite3.connect(tmp_path / "db.sqlite")) as conn, conn:
            conn.execute("UPDATE dashboards SET filters_json = '[{\"column\": 1}]'")
        assert store.load_dashboard("alice", "d1").filters == []

    def test_json_store_drops_malformed_filters(self, tmp_path):
        store = JSONFolderDashboardStore(tmp_path)
        store.save_dashboard(self._model())
        assert store.load_dashboard("alice", "d1").filters == self._model().filters
        path = next(tmp_path.glob("*.json"))
        data = json.loads(path.read_text())
        data["filters"] = {"bad": True}
        path.write_text(json.dumps(data))
        assert store.load_dashboard("alice", "d1").filters == []

    def test_saved_and_loaded_by_the_editor(self, ops):
        editor = ops.editor
        editor.store = MemoryDashboardStore()
        editor.set_filters([{"column": "state", "value": ["TX"]}])
        model = editor.save()
        other = FlowDash(COMPONENTS, notifications=False, store=editor.store)
        other.load(model.dashboard_id)
        assert other.filters == editor.filters
        table = other._component_instances[ops._instance("by_state")].table
        assert table["mw"].tolist() == [1.0, 3.0]
        other.dispose()


@pytest.fixture
def mixed():
    """Two sources whose columns share names but not types, each feeding a view."""
    editor = FlowDash(COMPONENTS, notifications=False)
    ops = DashboardOps(editor)
    ops.add_node("T/Fleet", {}, name="left")
    ops.add_node("T/Fleet", {}, name="right")
    ops.add_node("T/Table", {}, name="a")
    ops.add_node("T/Table", {}, name="b")
    ops.connect("left.data", "a.table")
    ops.connect("right.data", "b.table")
    yield ops
    editor.dispose()


def feed(ops, name, frame):
    ops.editor._component_instances[ops._instance(name)].data = frame


def mws(ops, name):
    return rows(ops, name)


class TestTypeMismatches:
    def test_int_and_text_columns_of_one_name(self, mixed, caplog):
        feed(mixed, "right", FLEET.assign(year=FLEET["year"].astype(str)))
        editor = mixed.editor
        editor.set_filters([{"column": "year", "widget": "select", "value": [2005, "2010"]}])
        assert mws(mixed, "a") == [2.0, 3.0]
        assert mws(mixed, "b") == [2.0, 3.0]
        editor.set_filters([{"column": "year", "widget": "range", "value": [2005, 2020]}])
        assert mws(mixed, "a") == [2.0, 3.0, 4.0]
        assert mws(mixed, "b") == FLEET["mw"].tolist()
        _, hint, detail = notes(mixed, "b")
        assert hint == "Not filtered by year"
        assert "holds text" in detail
        assert notes(mixed, "a")[1] == ""
        assert "Runtime wiring error" not in caplog.text
        assert editor._global_filters.span("year") == (2001, 2021)

    def test_numeric_strings_filter_a_column_of_numbers(self, mixed):
        mixed.editor.set_filters(
            [{"column": "year", "widget": "range", "value": ["2005", "2010"]}]
        )
        assert mws(mixed, "a") == [2.0, 3.0]

    def test_a_date_range_on_a_column_of_dates(self, mixed):
        days = FLEET.assign(day=[dt.date(y, 1, 1) for y in FLEET["year"]])
        feed(mixed, "left", days)
        feed(mixed, "right", days.assign(day=pd.to_datetime(days["day"])))
        mixed.editor.set_filters(
            [{"column": "day", "widget": "range", "value": ["2005-01-01", "2010-06-01"]}]
        )
        assert mws(mixed, "a") == mws(mixed, "b") == [2.0, 3.0]
        assert notes(mixed, "a")[1] == ""

    def test_a_range_on_unordered_categories_is_not_applied(self, mixed):
        feed(mixed, "left", FLEET.assign(state=FLEET["state"].astype("category")))
        mixed.editor.set_filters([{"column": "state", "widget": "range", "value": ["A", "P"]}])
        assert mws(mixed, "a") == FLEET["mw"].tolist()
        assert "unordered categories" in notes(mixed, "a")[2]

    def test_a_select_on_datetimes_takes_iso_strings(self, mixed):
        stamped = FLEET.assign(when=pd.to_datetime(FLEET["year"].astype(str) + "-01-01"))
        feed(mixed, "left", stamped)
        filters = mixed.editor._global_filters
        mixed.editor.set_filters([{"column": "when", "widget": "select"}])
        assert filters.options("when")[0] == "2001-01-01T00:00:00"
        mixed.editor.set_filter_value("when", ["2005-01-01T00:00:00", "2020-01-01"])
        assert mws(mixed, "a") == [2.0, 4.0]

    def test_set_value_checks_the_value(self, mixed):
        mixed.editor.set_filters([{"column": "year", "widget": "range"}])
        with pytest.raises(FilterError, match=r"\[start, end\]"):
            mixed.editor.set_filter_value("year", [2001])
        with pytest.raises(FilterError, match="must be a list"):
            mixed.editor.set_filter_value("year", "2001")

    def test_spans_that_cannot_be_compared_are_dropped(self):
        stamps = pd.DataFrame({"x": pd.to_datetime(["2020-01-01", "2021-01-01"])})
        numbers = pd.DataFrame({"x": [1, 5]})
        text = pd.DataFrame({"x": ["a", "b"]})
        assert column_span([numbers, text, numbers.assign(x=[0, 2])], "x") == (0, 5)
        assert column_span([stamps], "x") == (dt.datetime(2020, 1, 1), dt.datetime(2021, 1, 1))
        assert column_span([text], "x") is None


class TestSharedChips:
    def test_a_filter_node_feeding_every_view_is_shown_once(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        ops = DashboardOps(editor)
        ops.add_node("T/Fleet", {}, name="fleet")
        ops.add_node("T/Recent", {}, name="recent")
        ops.add_node("T/Table", {}, name="a")
        ops.add_node("T/Table", {}, name="b")
        ops.connect("fleet.data", "recent.data")
        ops.connect("recent.filtered", "a.table")
        ops.connect("recent.filtered", "b.table")
        filters = editor._global_filters
        assert [iid for iid, _ in filters.shared] == [ops._instance("recent")]
        assert notes(ops, "a")[0] == notes(ops, "b")[0] == []
        from panel_flowdash.ui.global_filters import FilterBar

        bar = FilterBar(filters=filters)
        assert [o.label for o in bar._row if "fd-filter-shared" in o.css_classes] == [
            "Recent · all views"
        ]
        ops.add_node("T/Table", {}, name="c")
        ops.connect("fleet.data", "c.table")
        assert filters.shared == []
        assert [iid for iid, _ in notes(ops, "a")[0]] == [ops._instance("recent")]
        editor.dispose()

    def test_notes_reuse_one_pass_over_the_views(self, ops, monkeypatch):
        filters = ops.editor._global_filters
        calls = []
        original = type(filters).targets
        monkeypatch.setattr(
            type(filters), "targets", lambda self: calls.append(1) or original(self)
        )
        ops.editor.set_filters([{"column": "state", "value": ["TX"]}])
        calls.clear()
        for name in ("by_state", "reference", "regional", "latest"):
            filters.notes(ops._instance(name))
        assert calls == []
        assert notes(ops, "regional")[1] == "Not filtered by state"
