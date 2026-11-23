"""The assistant's tools for the dashboard as a whole: global filters, the title and the step labels."""

import panel as pn
import param
import pytest
import yaml
from panel.viewable import Viewer

pytest.importorskip("pydantic_ai")

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps, OperationError, register_local_filter
from panel_flowdash.agent.chrome import TOOL_LABELS
from panel_flowdash.agent.live import LiveDashboardOps
from panel_flowdash.dashboard_store import Permission

from .test_agent_ops import _editor
from .test_agent_tools import _app, _runner
from .test_global_filters import COMPONENTS as FILTER_COMPONENTS, rows


@register(component=True, page=False, provides=[{"key": "count", "type": "int"}])
class Count(Viewer):
    count = param.Integer(default=5)

    def __panel__(self):
        return pn.pane.Str("count")


@register(component=True, page=False, tags=["view"], requires=[{"key": "value", "type": "int"}])
class Kpi(Viewer):
    value = param.Integer(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Str("kpi")


COMPONENTS = {**FILTER_COMPONENTS, "T/Count": Count, "T/Kpi": Kpi}


@pytest.fixture
def store():
    return MemoryDashboardStore()


@pytest.fixture
def ops(store):
    editor = FlowDash(COMPONENTS, notifications=False, store=store, user="alice")
    editor.new_dashboard("Untitled")
    ops = DashboardOps(editor)
    ops.add_node("T/Fleet", {}, name="fleet")
    ops.add_node("T/Regions", {}, name="regions")
    ops.add_node("T/Count", {}, name="count")
    for name in ("by_state", "regional"):
        ops.add_node("T/Table", {}, name=name)
    ops.add_node("T/Kpi", {}, name="total")
    ops.connect("fleet.data", "by_state.table")
    ops.connect("regions.data", "regional.table")
    ops.connect("count.count", "total.value")
    yield ops
    editor.dispose()


class TestAddFilter:
    def test_adds_the_column_to_the_filter_bar_and_says_what_it_reaches(self, ops):
        result = ops.add_filter("state", label="State")
        assert result["new"]
        assert result["filter"] == {"column": "state", "label": "State", "widget": "select"}
        assert result["filters"] == ["by_state"]
        assert result["cannot"]["regional"] == "its data has no column 'state'"
        assert "receives no table" in result["cannot"]["total"]
        assert yaml.safe_load(ops.get_dashboard())["filters"] == [
            {"column": "state", "label": "State", "widget": "select"}
        ]
        ops.editor.set_filter_value("state", ["TX"])
        assert rows(ops, "by_state") == [1.0, 3.0]
        assert rows(ops, "regional") == [6.0, 4.0]

    def test_a_starting_value_and_the_widget_of_the_column_type(self, ops):
        result = ops.add_filter("year", value=[2005, 2020])
        assert result["filter"]["widget"] == "range"
        assert rows(ops, "by_state") == [2.0, 3.0, 4.0]

    def test_a_column_no_view_has_is_refused_with_the_columns_there_are(self, ops):
        with pytest.raises(OperationError, match=r"a column 'State'.*Did you mean 'state'"):
            ops.add_filter("State")
        with pytest.raises(OperationError, match="The views' columns: mw, region, state, year"):
            ops.add_filter("p_cap")
        assert ops.editor.filters == []

    def test_long_and_control_character_names_are_refused_while_a_view_waits(self, ops):
        ops.add_node("T/Table", {}, name="waiting")
        with pytest.raises(OperationError) as info:
            ops.add_filter("x" * 1_000_000)
        assert "at most 128 characters" in str(info.value)
        assert len(str(info.value)) < 300
        with pytest.raises(OperationError, match="control characters"):
            ops.add_filter("state\x00")
        with pytest.raises(OperationError) as info:
            ops.remove_filter("y" * 10_000)
        assert len(str(info.value)) < 200
        assert ops.editor.filters == []
        assert ops.add_filter("x" * 128)["filters"] == []

    def test_a_malformed_value_is_refused(self, ops):
        with pytest.raises(OperationError, match="a range takes"):
            ops.add_filter("year", widget="range", value=[2005])
        assert ops.editor.filters == []

    def test_adding_it_again_changes_it_and_remove_takes_it_out(self, ops):
        ops.add_filter("state")
        result = ops.add_filter("state", label="US state", value=["TX"])
        assert not result["new"]
        assert ops.editor.filters == [
            {"column": "state", "label": "US state", "widget": "select", "value": ["TX"]}
        ]
        ops.remove_filter("state")
        assert ops.editor.filters == []
        with pytest.raises(OperationError, match=r"no global filter on 'state'. It has none"):
            ops.remove_filter("state")

    def test_it_is_an_undoable_edit(self, ops):
        ops.add_filter("state")
        assert ops.editor.dirty
        ops.editor.undo(whole_runs=False)
        assert ops.editor.filters == []

    def test_views_that_opt_out_are_named(self, ops):
        ops.add_filter("state")
        ops.editor.exclude_from_filter("state", ops._instance("by_state"))
        assert ops.filters()[0]["exclude"] == ["by_state"]
        applies, cannot = ops.filter_reach("state")
        assert applies == [] and cannot["by_state"] == "it opts out of this filter"


class TestFilterTools:
    async def test_add_filter_reports_the_views_it_cannot_filter(self, ops):
        runner = _runner(ops, ("add_filter", {"column": "state", "label": "State"}))
        output = await runner.run("Add a state dropdown")
        assert output.startswith(
            "Done: Added the global filter on 'state' (State, select). It filters: by_state."
        )
        assert "It cannot filter: regional: its data has no column 'state'; total:" in output

    async def test_remove_filter(self, ops):
        ops.add_filter("state")
        output = await _runner(ops, ("remove_filter", {"column": "state"})).run("Drop it")
        assert output == "Done: Removed the global filter on 'state'."
        assert ops.editor.filters == []

    async def test_a_local_filter_is_refused_by_default(self, ops):
        runner = _runner(ops, ("add_filter", {"column": "state", "scope": "local"}))
        output = await runner.run("A state filter for this chart only")
        assert output.startswith("Done: Refused: add_filter only adds filters to the dashboard's")
        assert "No tool here builds one" in output
        assert ops.editor.filters == []

    async def test_a_local_filter_route_can_be_registered(self, ops):
        calls = []
        try:
            register_local_filter("add_filter_control")
            output = await _runner(ops, ("add_filter", {"column": "state", "scope": "local"})).run(
                "Local one"
            )
            assert "Build a local filter with add_filter_control instead." in output

            def route(ctx, **kwargs):
                calls.append(kwargs)
                return "Built a local filter."

            register_local_filter(route)
            output = await _runner(
                ops, ("add_filter", {"column": "state", "scope": "local", "label": "State"})
            ).run("Local one")
            assert output == "Done: Built a local filter."
            assert calls == [{"column": "state", "label": "State", "widget": None, "value": None}]
        finally:
            register_local_filter(None)
        assert ops.editor.filters == []

    async def test_a_mistake_is_sent_back(self, ops):
        output = await _runner(ops, ("add_filter", {"column": "nope"})).run("Filter by nope")
        assert "No view on the dashboard has a column 'nope'" in output


class TestSetTitle:
    def test_renames_the_dashboard_as_an_unsaved_edit(self, ops, store):
        ops.editor.save()
        current = ops.editor.dashboard
        assert ops.set_title("  US wind   fleet ") == "US wind fleet"
        assert current.title == "US wind fleet"
        assert yaml.safe_load(ops.get_dashboard())["title"] == "US wind fleet"
        assert ops.editor.dirty
        assert store._load_any(current.dashboard_id).title == "Untitled"
        ops.editor.save()
        assert store._load_any(current.dashboard_id).title == "US wind fleet"

    def test_undo_and_revert_take_the_title_back(self, ops):
        ops.editor.save()
        ops.set_title("Fleet")
        ops.editor.undo(whole_runs=False)
        assert ops.editor.dashboard.title == "Untitled"
        assert not ops.editor.dirty
        ops.editor.redo()
        assert ops.editor.dashboard.title == "Fleet"
        history = ops.editor.edit_history
        history.restore(history.artifact, history.versions[0])
        assert ops.editor.dashboard.title == "Untitled"

    def test_a_description_is_an_unsaved_setting(self, ops, store):
        ops.editor.save()
        ops.set_title("Fleet", description="Turbines by state.")
        assert ops.editor.settings.description == "Turbines by state."
        assert ops.editor.dirty

    def test_empty_and_long_titles_are_refused(self, ops):
        with pytest.raises(OperationError, match="Give the dashboard a title"):
            ops.set_title("  ")
        with pytest.raises(OperationError, match="at most 200 characters"):
            ops.set_title("x" * 201)

    def test_a_viewer_cannot_rename(self, store):
        editor = _editor(store)
        editor.new_dashboard("Mine")
        editor.read_only = True
        with pytest.raises(PermissionError):
            DashboardOps(editor).set_title("Theirs")
        assert editor.dashboard.title == "Mine"
        editor.dispose()

    def test_an_editor_who_does_not_own_it_cannot_rename(self, store):
        bob = store.create_dashboard("bob", "Bob's")
        store.set_permission(bob.dashboard_id, Permission(allow_edit_users={"alice"}))
        editor = _editor(store)
        editor.load(bob.dashboard_id)
        ops = DashboardOps(editor)
        with pytest.raises(PermissionError, match="owner or an administrator"):
            ops.set_title("Hijacked")
        with pytest.raises(PermissionError):
            editor.set_title("Hijacked")
        assert editor.dashboard.title == "Bob's"
        ops.add_node("T/number")
        editor.save()
        assert store._load_any(bob.dashboard_id).title == "Bob's"
        editor.dispose()

    def test_authorize_rename_decides(self, store):
        bob = store.create_dashboard("bob", "Bob's")
        editor = _editor(store, authorize_rename=lambda model: model.owner == "bob")
        editor.load(bob.dashboard_id)
        DashboardOps(editor).set_title("Fleet")
        editor.save()
        assert store._load_any(bob.dashboard_id).title == "Fleet"
        editor.dispose()

    def test_a_rename_in_the_store_survives_undo(self, ops, store):
        editor = ops.editor
        editor.save()
        ops.add_node("T/Count", {}, name="other")
        store.rename_dashboard("alice", editor.dashboard.dashboard_id, "Renamed")
        editor.title_renamed("Renamed")
        editor.undo(whole_runs=False)
        assert editor.dashboard.title == "Renamed"
        editor.save()
        assert store._load_any(editor.dashboard.dashboard_id).title == "Renamed"

    def test_saving_keeps_a_rename_made_elsewhere(self, ops, store):
        editor = ops.editor
        editor.save()
        store.rename_dashboard("alice", editor.dashboard.dashboard_id, "Elsewhere")
        ops.add_node("T/Count", {}, name="other")
        editor.save()
        assert store._load_any(editor.dashboard.dashboard_id).title == "Elsewhere"

    async def test_the_tool(self, ops):
        output = await _runner(ops, ("set_title", {"title": "Fleet"})).run("Name it")
        assert output == "Done: Titled the dashboard 'Fleet'."
        assert ops.editor.dashboard.title == "Fleet"

    async def test_live_rename_needs_administrator_rights(self, store, monkeypatch):
        theirs = store.create_dashboard("bob", "Bob's")
        mine = store.create_dashboard("alice", "Mine")
        app = _app(store, monkeypatch)
        ops = LiveDashboardOps(app)
        app._load_dashboard_sync(mine.dashboard_id)
        ops.set_title("Alice's fleet")
        assert app._current_dashboard.title == "Alice's fleet"
        assert store._load_any(mine.dashboard_id).title == "Mine"
        ops.save()
        assert store._load_any(mine.dashboard_id).title == "Alice's fleet"
        store.set_permission(theirs.dashboard_id, Permission(allow_edit_users={"alice"}))
        app._load_dashboard_sync(theirs.dashboard_id, edit=True)
        with pytest.raises(PermissionError, match="owner or an administrator"):
            ops.set_title("Mine now")
        assert app._current_dashboard.title == "Bob's"
        assert store._load_any(theirs.dashboard_id).title == "Bob's"


def test_set_config_says_an_input_it_cannot_find_may_be_an_output():
    editor = _editor()
    ops = DashboardOps(editor)
    node = ops.add_node("T/number")
    doubler = ops.add_node("T/doubler")
    with pytest.raises(OperationError) as info:
        ops.set_config(node, {}, {"value": 3})
    message = str(info.value)
    assert f"'{node}' (Number) has no input 'value'. 'value' is an output of '{node}'" in message
    assert "Its inputs: none; its outputs: value. Change its config instead." in message
    with pytest.raises(
        OperationError, match=r"Did you mean 'value'.*Its inputs: value; its outputs: doubled"
    ):
        ops.set_inputs(doubler, {"values": 3})
    editor.dispose()


@pytest.mark.parametrize(
    "name, args, label",
    [
        ("set_title", {"title": "Fleet"}, "Title the dashboard Fleet"),
        ("add_filter", {"column": "t_state", "label": "State"}, "Add a filter on State"),
        ("add_filter", {"column": "t_state"}, "Add a filter on t_state"),
        ("remove_filter", {"column": "t_state"}, "Remove the filter on t_state"),
    ],
)
def test_step_labels(name, args, label):
    assert TOOL_LABELS[name](args) == label
