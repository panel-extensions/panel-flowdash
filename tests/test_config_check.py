"""Components' config checks: the hook itself, the dashboard operations, the agent tools and the inspector."""

import asyncio
import concurrent.futures
import dataclasses
import logging
import threading
import time
import types

import panel as pn
import param
import pytest
import yaml
from panel.viewable import Viewer

from panel_flowdash import FlowDash, config_check, current_dashboard, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.config_check import (
    check_node,
    effective_config,
    node_inputs,
    run_check,
    run_check_sync,
)
from panel_flowdash.registry import PanelAppMetadata
from panel_flowdash.ui.config_draft import ConfigDraft
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.util import ReactiveError

CALLS = []


def _problems_of(config, inputs):
    sql = config["sql"]
    if "BROKEN" in sql:
        raise ValueError("syntax error near 'BROKEN'")
    if "BUG" in sql:
        raise RuntimeError("bug in the check")
    if "MANY" in sql:
        return ["first problem", "second problem"]
    limit = inputs.get("limit")
    if limit is not None and config["rows"] > limit:
        return f"rows must be at most {limit}"
    if "DROP" in sql:
        return "Only SELECT statements are allowed."
    return None


HANG = threading.Event()
THREADS = []


def check_sql(config, inputs):
    CALLS.append((dict(config), dict(inputs), current_dashboard()))
    THREADS.append(threading.current_thread().name)
    sql = config["sql"]
    if "SLOW" in sql:
        time.sleep(0.5)
    if "HANG" in sql:
        HANG.wait(5)
    if "PAUSE" in sql:
        time.sleep(0.3)
    if "OWN_TIMEOUT" in sql:
        raise TimeoutError("the database timed out")
    for word, result in (("INT", 5), ("DICT", {"a": 1}), ("NONES", ["", None]), ("EMPTY", [])):
        if word in sql:
            return result
    if "X" in sql and config["rows"] > 5:
        return "X needs at most 5 rows"
    return _problems_of(config, inputs)


async def check_sql_async(config, inputs):
    await asyncio.sleep(0)
    CALLS.append((dict(config), dict(inputs), current_dashboard()))
    if "SLOW" in config["sql"]:
        await asyncio.sleep(0.5)
    return _problems_of(config, inputs)


@register(page=False, component=True, title="Limit", config=["n"])
class Limit(Viewer):
    n = param.Integer(default=5)

    @param.output(param.Integer)
    @param.depends("n")
    def limit(self):
        return self.n

    def __panel__(self):
        return "limit"


@register(
    page=False, component=True, title="Query", config=["sql", "rows"], check_config=check_sql
)
class Query(Viewer):
    limit = param.Integer(
        default=None, allow_None=True, metadata={"flowdash": {"required": False}}
    )
    sql = param.String(default="SELECT 1")
    rows = param.Integer(default=10)

    @param.output(param.Integer)
    @param.depends("rows")
    def result(self):
        return self.rows

    def __panel__(self):
        return "query"


@register(
    page=False,
    component=True,
    title="Async query",
    config=["sql", "rows"],
    check_config=check_sql_async,
)
class AsyncQuery(Query):
    pass


@register(page=False, component=True, title="Plain", config=["sql"])
class Plain(Viewer):
    sql = param.String(default="DROP")

    def __panel__(self):
        return "plain"


@register(
    page=False, component=True, title="Drop query", config=["sql", "rows"], check_config=check_sql
)
class DropQuery(Query):
    sql = param.String(default="DROP t")


LIMIT, QUERY, ASYNC, PLAIN, DROP = "T/limit", "T/query", "T/async_query", "T/plain", "T/drop"
COMPONENTS = {LIMIT: Limit, QUERY: Query, ASYNC: AsyncQuery, PLAIN: Plain, DROP: DropQuery}


@pytest.fixture(autouse=True)
def _clear_calls():
    CALLS.clear()
    THREADS.clear()
    HANG.clear()
    yield
    HANG.set()
    CALLS.clear()


@pytest.fixture
def editor():
    editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
    editor.new_dashboard("Checks")
    yield editor
    editor.dispose()


@pytest.fixture
def ops(editor):
    return DashboardOps(editor)


def _spec(editor, component_id):
    editor.ensure_components_loaded([component_id])
    return editor.component_specs[component_id]


class TestRegister:
    async def test_metadata_and_spec_carry_the_check(self, editor):
        assert PanelAppMetadata.from_app(Query).check_config is check_sql
        assert PanelAppMetadata.from_app(Plain).check_config is None
        assert _spec(editor, QUERY).check_config is check_sql
        assert _spec(editor, ASYNC).check_config is check_sql_async


class TestRunCheck:
    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    @pytest.mark.parametrize(
        ("sql", "problems"),
        [
            ("SELECT 1", []),
            ("DROP t", ["Only SELECT statements are allowed."]),
            ("MANY", ["first problem", "second problem"]),
            ("BROKEN", ["syntax error near 'BROKEN'"]),
        ],
    )
    async def test_results_become_a_list_of_problems(self, editor, component, sql, problems):
        spec = _spec(editor, component)
        config = effective_config(spec, {"sql": sql})
        assert await run_check(spec, config, {}) == problems
        assert run_check_sync(spec, config, {}) == problems

    async def test_an_empty_string_or_list_passes(self, editor):
        spec = _spec(editor, QUERY)
        for result in ("", [], [""]):
            check = lambda config, inputs, result=result: result
            spec_with = dataclasses.replace(spec, check_config=check)
            assert await run_check(spec_with, {}, {}) == []

    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    async def test_another_exception_is_a_logged_failed_check(self, editor, component, caplog):
        spec = _spec(editor, component)
        config = effective_config(spec, {"sql": "BUG"})
        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            problems = await run_check(spec, config, {})
        assert problems == ["The config check of 'Query' failed (RuntimeError)."] or problems == [
            "The config check of 'Async query' failed (RuntimeError)."
        ]
        assert "bug in the check" not in problems[0]
        assert any("bug in the check" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)

    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    async def test_a_slow_check_times_out(self, editor, component):
        spec = _spec(editor, component)
        config = effective_config(spec, {"sql": "SLOW"})
        start = time.monotonic()
        (problem,) = await run_check(spec, config, {}, timeout=0.05)
        assert time.monotonic() - start < 0.4
        assert "did not finish within the time allowed" in problem

    async def test_a_sync_check_runs_off_the_event_loop(self, editor):
        spec = _spec(editor, QUERY)
        ticks = []

        async def tick():
            for _ in range(5):
                ticks.append(time.monotonic())
                await asyncio.sleep(0.02)

        await asyncio.gather(run_check(spec, effective_config(spec, {"sql": "SLOW"}), {}), tick())
        assert len(ticks) == 5 and ticks[-1] - ticks[0] < 0.3

    async def test_sync_callers_can_run_an_async_check_inside_a_running_loop(self, editor):
        spec = _spec(editor, ASYNC)
        config = effective_config(spec, {"sql": "DROP"})
        assert run_check_sync(spec, config, {}) == ["Only SELECT statements are allowed."]
        assert run_check_sync(spec, effective_config(spec, {"sql": "SLOW"}), {}, timeout=0.05)

    def test_sync_callers_can_run_an_async_check_without_a_loop(self):
        editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
        try:
            spec = _spec(editor, ASYNC)
            assert run_check_sync(spec, effective_config(spec, {"sql": "DROP"}), {}) == [
                "Only SELECT statements are allowed."
            ]
        finally:
            editor.dispose()

    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    async def test_current_dashboard_is_the_editor_inside_a_check(self, editor, component):
        spec = _spec(editor, component)
        config = effective_config(spec)
        await run_check(spec, config, {}, editor=editor)
        run_check_sync(spec, config, {}, editor=editor)
        assert [call[2] for call in CALLS] == [editor, editor]
        assert current_dashboard() is None

    async def test_config_is_merged_over_defaults_with_former_names_resolved(self, editor):
        spec = _spec(editor, QUERY)
        assert effective_config(spec) == {"sql": "SELECT 1", "rows": 10}
        assert effective_config(spec, {"rows": 3}, {"sql": "x"}) == {"sql": "x", "rows": 3}


class TestNodeInputs:
    async def test_set_values_only(self, editor):
        query = editor.add_component(QUERY)
        spec = _spec(editor, QUERY)
        assert node_inputs(editor, query, spec) == {}
        limit = editor.add_component(LIMIT)
        editor.connect(limit, "limit", query, "limit")
        assert node_inputs(editor, query, spec) == {"limit": 5}

    async def test_failures_are_left_out(self, editor):
        if ReactiveError is None:
            pytest.skip("needs param.ReactiveError")
        query = editor.add_component(QUERY)
        editor.graph.get_state(query).limit = ReactiveError(ValueError("upstream"))
        assert node_inputs(editor, query, _spec(editor, QUERY)) == {}

    async def test_the_check_sees_the_live_inputs(self, editor):
        query, limit = editor.add_component(QUERY), editor.add_component(LIMIT)
        editor.connect(limit, "limit", query, "limit")
        CALLS.clear()
        assert await check_node(editor, query, {"rows": 6}) == ["rows must be at most 5"]
        config, inputs, _ = CALLS[-1]
        assert config == {"sql": "SELECT 1", "rows": 6} and inputs == {"limit": 5}


class TestOps:
    async def test_add_node_refuses_a_failing_config_and_places_nothing(self, ops):
        with pytest.raises(OperationError, match="'Query' was not placed: Only SELECT"):
            ops.add_node(QUERY, {"sql": "DROP t"})
        assert ops.editor._tile_items == []
        assert CALLS[-1][1] == {}

    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    async def test_add_node_async_checks_off_the_loop(self, ops, component):
        with pytest.raises(OperationError, match="syntax error near 'BROKEN'"):
            await ops.add_node_async(component, {"sql": "BROKEN"})
        assert ops.editor._tile_items == []
        assert await ops.add_node_async(component, {"sql": "SELECT 2"}, name="q") == "q"
        assert CALLS[-1][:2] == ({"sql": "SELECT 2", "rows": 10}, {})

    async def test_set_config_refuses_and_keeps_the_config(self, ops):
        ops.add_node(QUERY, {"sql": "SELECT 2"}, name="q")
        with pytest.raises(OperationError, match="The config of 'q' was not changed:\n- first"):
            ops.set_config("q", {"sql": "MANY"})
        assert ops.node_config("q") == {"sql": "SELECT 2"}
        assert ops.set_config("q", {"rows": 3}) == {"sql": "SELECT 2", "rows": 3}

    @pytest.mark.parametrize("component", [QUERY, ASYNC])
    async def test_set_config_async_checks_with_the_live_inputs(self, ops, component):
        ops.add_node(component, name="q")
        ops.add_node(LIMIT, name="cap")
        ops.connect("cap.limit", "q.limit")
        with pytest.raises(OperationError, match="rows must be at most 5"):
            await ops.set_config_async("q", {"rows": 8})
        assert ops.node_config("q") == {}
        assert await ops.set_config_async("q", {"rows": 4}) == {"rows": 4}
        assert ops.editor.graph.get_config_state(ops._instance("q")).rows == 4

    async def test_a_component_without_a_check_is_not_checked(self, ops):
        ops.add_node(PLAIN, {"sql": "DROP"}, name="p")
        await ops.set_config_async("p", {"sql": "DROP t"})
        assert ops.node_config("p") == {"sql": "DROP t"}

    async def test_the_timeout_is_set_on_the_ops(self, ops):
        ops.check_timeout = 0.05
        ops.add_node(QUERY, name="q")
        with pytest.raises(OperationError, match="did not finish"):
            await ops.set_config_async("q", {"sql": "SLOW"})

    async def test_replace_yaml_loads_the_node_and_reports_its_problems(self, ops):
        ops.add_node(QUERY, name="q")
        text = ops.get_dashboard().replace(
            "component: T/query", "component: T/query\n    config:\n      sql: DROP t"
        )
        issues = ops.replace_yaml(text)
        errors = [i for i in issues if i.severity == "error"]
        assert [(i.path, i.message) for i in errors] == [
            ("nodes.q.config", "Only SELECT statements are allowed.")
        ]
        assert ops.node_config("q") == {"sql": "DROP t"}

    async def test_replace_yaml_async_reports_problems_too(self, ops):
        ops.add_node(ASYNC, name="q")
        text = ops.get_dashboard().replace(
            "component: T/async_query", "component: T/async_query\n    config:\n      sql: BROKEN"
        )
        issues = await ops.replace_yaml_async(text)
        assert [(i.path, i.message, i.severity) for i in issues] == [
            ("nodes.q.config", "syntax error near 'BROKEN'", "error")
        ]

    async def test_preview_keeps_running_and_marks_the_node_invalid(self, ops):
        ops.add_node(QUERY, name="q")
        text = ops.get_dashboard().replace(
            "component: T/query", "component: T/query\n    config:\n      sql: DROP t"
        )
        report = ops.preview(text)
        assert report["status"] == "invalid"
        node = report["nodes"]["q"]
        assert node["status"] == "invalid"
        assert node["message"] == "Only SELECT statements are allowed."
        assert node["outputs"]["result"]["value"] == 10
        assert ops.node_config("q") == {}

    async def test_preview_async_of_the_open_dashboard(self, ops):
        ops.add_node(ASYNC, name="q")
        ops.add_node(LIMIT, name="cap")
        ops.connect("cap.limit", "q.limit")
        ops.editor._apply_config_patch(ops._instance("q"), {"rows": 9})
        report = await ops.preview_async()
        assert report["nodes"]["q"]["status"] == "invalid"
        assert report["nodes"]["q"]["message"] == "rows must be at most 5"
        assert report["nodes"]["cap"]["status"] == "ok"

    async def test_validate_runs_the_checks_and_warnings_do_not(self, ops):
        ops.add_node(QUERY, name="q")
        ops.editor._apply_config_patch(ops._instance("q"), {"sql": "DROP t"})
        CALLS.clear()
        issues = ops.validate()
        assert [(i.path, i.severity) for i in issues if i.severity == "error"] == [
            ("nodes.q.config", "error")
        ]
        assert len(CALLS) == 1
        CALLS.clear()
        assert all(i.severity != "error" for i in ops._warnings())
        assert CALLS == []
        assert not [i for i in ops.validate(checks=False) if i.severity == "error"]

    async def test_validate_of_yaml_uses_the_inputs_of_the_node_of_the_same_name(self, ops):
        ops.add_node(QUERY, name="q")
        ops.add_node(LIMIT, name="cap")
        ops.connect("cap.limit", "q.limit")
        text = ops.get_dashboard().replace(
            "component: T/query", "component: T/query\n    config:\n      rows: 7"
        )
        (issue,) = [i for i in ops.validate(text) if i.severity == "error"]
        assert issue.message == "rows must be at most 5"
        assert not [i for i in ops.validate(text, standalone=True) if i.severity == "error"]

    async def test_a_node_with_other_errors_is_not_checked(self, ops):
        ops.add_node(QUERY, name="q")
        text = ops.get_dashboard().replace(
            "component: T/query", "component: T/query\n    config:\n      nope: 1"
        )
        CALLS.clear()
        issues = [i for i in ops.validate(text) if i.severity == "error"]
        assert [i.path for i in issues] == ["nodes.q.config.nope"]
        assert CALLS == []


class TestAgentTools:
    async def test_a_refused_config_is_sent_back_for_the_model_to_fix(self, editor):
        pytest.importorskip("pydantic_ai")
        from .test_agent_tools import _runner

        ops = DashboardOps(editor)
        ops.add_node(QUERY, name="q")
        runner = _runner(ops, ("set_config", {"name": "q", "config": {"sql": "DROP t"}}))
        output = await runner.run("Change the query")
        assert "The config of 'q' was not changed: Only SELECT statements are allowed." in output
        assert not [v for a in runner.state.artifacts for v in a.versions if v.actor == "agent"]
        assert ops.node_config("q") == {}

    async def test_the_model_retries_with_a_fixed_config(self, editor):
        pytest.importorskip("pydantic_ai")
        from .test_agent_tools import _runner

        ops = DashboardOps(editor)
        runner = _runner(
            ops,
            ("add_node", {"component": ASYNC, "name": "q", "config": {"sql": "BROKEN"}}),
            ("add_node", {"component": ASYNC, "name": "q", "config": {"sql": "SELECT 3"}}),
        )
        output = await runner.run("Add a query")
        assert output == "Done: Added node 'q'."
        assert ops.node_config("q") == {"sql": "SELECT 3"}
        assert [c[0]["sql"] for c in CALLS] == ["BROKEN", "SELECT 3"]

    async def test_replace_dashboard_lists_the_failed_checks(self, editor):
        pytest.importorskip("pydantic_ai")
        from .test_agent_tools import _runner

        ops = DashboardOps(editor)
        ops.add_node(QUERY, name="q")
        text = ops.get_dashboard().replace(
            "component: T/query", "component: T/query\n    config:\n      sql: DROP t"
        )
        runner = _runner(ops, ("replace_dashboard", {"dashboard_yaml": text}))
        output = await runner.run("Replace it")
        assert "Replaced the dashboard." in output
        assert "The config checks of these nodes failed; fix their config:" in output
        assert "nodes.q.config: Only SELECT statements are allowed." in output


async def _settle(seconds=0.3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(0.01)


class TestInspectorDraft:
    def _form(self, editor, node_id):
        select_on_canvas(editor._flow, node_id=node_id)
        body = pn.Column(*editor._inspector._body)
        return next(iter(body.select(pn.Param)))

    async def test_a_failing_edit_is_held_back_and_shown(self, editor):
        query = editor.add_component(QUERY)
        form = self._form(editor, query)
        config_state = editor.graph.get_config_state(query)
        assert form.object is not config_state
        form._widgets["sql"].value = "DROP t"
        await _settle()
        assert config_state.sql == "SELECT 1"
        assert editor._component_instances[query].sql == "SELECT 1"
        alerts = [
            a
            for a in pn.Column(*editor._inspector._body).select()
            if "fd-config-check-alert" in getattr(a, "css_classes", [])
        ]
        assert len(alerts) == 1 and "Only SELECT statements are allowed." in alerts[0].object
        form._widgets["sql"].value = "SELECT 2"
        await _settle()
        assert config_state.sql == "SELECT 2"
        assert editor._component_instances[query].sql == "SELECT 2"
        assert editor._tile_items[0]["config"] == {"sql": "SELECT 2"}
        alerts = [
            a
            for a in pn.Column(*editor._inspector._body).select()
            if "fd-config-check-alert" in getattr(a, "css_classes", [])
        ]
        assert alerts == []

    async def test_a_change_from_elsewhere_reaches_the_draft(self, editor):
        query = editor.add_component(QUERY)
        form = self._form(editor, query)
        ops = DashboardOps(editor)
        await ops.set_config_async(ops.nodes()[0]["name"], {"rows": 4})
        assert form.object.rows == 4

    async def test_the_latest_edit_wins(self, editor):
        query = editor.add_component(ASYNC)
        runs = {}
        draft = ConfigDraft(editor, query, runs)
        slow = asyncio.ensure_future(draft.check(1, {"sql": "SLOW"}))
        runs[query] = 2
        await draft.check(2, {"sql": "SELECT 5"})
        assert editor.graph.get_config_state(query).sql == "SELECT 5"
        await slow
        assert editor.graph.get_config_state(query).sql == "SELECT 5"

    async def test_a_stale_failure_does_not_show(self, editor):
        query = editor.add_component(QUERY)
        runs = {query: 2}
        draft = ConfigDraft(editor, query, runs)
        assert await draft.check(1, {"sql": "DROP"}) == ["Only SELECT statements are allowed."]
        assert list(draft.alert) == []
        assert editor.graph.get_config_state(query).sql == "SELECT 1"

    async def test_a_custom_editor_patches_through_the_check(self):
        patches = []

        def custom_editor(data, schema, *, id, type, on_patch):
            patches.append(on_patch)
            return pn.pane.Markdown("custom")

        @register(
            page=False,
            component=True,
            title="Custom query",
            config=["sql", "rows"],
            config_editor=custom_editor,
            check_config=check_sql,
        )
        class CustomQuery(Query):
            pass

        editor = FlowDash(
            {"T/custom": CustomQuery}, include_builtin_components=False, notifications=False
        )
        try:
            node = editor.add_component("T/custom")
            select_on_canvas(editor._flow, node_id=node)
            (on_patch,) = patches
            on_patch({"sql": "DROP t"})
            await _settle()
            assert editor.graph.get_config_state(node).sql == "SELECT 1"
            on_patch({"rows": 2})
            await _settle()
            # The draft holds both changes, so the refused one is still refused.
            assert editor.graph.get_config_state(node).rows == 10
            on_patch({"sql": "SELECT 9"})
            await _settle()
            state = editor.graph.get_config_state(node)
            assert (state.sql, state.rows) == ("SELECT 9", 2)
        finally:
            editor.dispose()


class TestResults:
    @pytest.mark.parametrize(
        ("sql", "kind"), [("INT", "int"), ("DICT", "dict"), ("NONES", "list holding NoneType")]
    )
    async def test_other_results_are_a_logged_failed_check(self, editor, sql, kind, caplog):
        spec = _spec(editor, QUERY)
        with caplog.at_level(logging.ERROR, logger="panel_flowdash"):
            problems = await run_check(spec, effective_config(spec, {"sql": sql}), {})
        assert problems == [f"The config check of 'Query' failed (it returned a {kind})."]
        assert "must return None, a string or a list of strings" in caplog.text

    async def test_an_empty_list_passes(self, editor):
        spec = _spec(editor, QUERY)
        assert await run_check(spec, effective_config(spec, {"sql": "EMPTY"}), {}) == []

    async def test_a_checks_own_timeout_error_is_not_the_check_timing_out(self, editor, caplog):
        spec = _spec(editor, QUERY)
        config = effective_config(spec, {"sql": "OWN_TIMEOUT"})
        for problems in (await run_check(spec, config, {}), run_check_sync(spec, config, {})):
            assert problems == ["The config check of 'Query' failed (TimeoutError)."]
        assert "the database timed out" in caplog.text


class TestThreads:
    async def test_sync_checks_run_on_their_own_threads(self, editor):
        spec = _spec(editor, QUERY)
        await run_check(spec, effective_config(spec), {})
        run_check_sync(spec, effective_config(spec), {})
        assert THREADS and all(n.startswith("flowdash-config-check") for n in THREADS)

    def test_a_sync_caller_waits_for_a_hanging_check_at_most_the_timeout(self):
        editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
        try:
            spec = _spec(editor, QUERY)
            start = time.monotonic()
            (problem,) = run_check_sync(
                spec, effective_config(spec, {"sql": "HANG"}), {}, timeout=0.1
            )
            assert time.monotonic() - start < 1
            assert "did not finish within the time allowed" in problem
        finally:
            HANG.set()
            editor.dispose()


class TestRaces:
    async def test_a_check_is_repeated_when_the_config_changed_meanwhile(self, ops):
        ops.add_node(QUERY, name="q")
        task = asyncio.ensure_future(ops.set_config_async("q", {"sql": "X SLOW"}))
        await asyncio.sleep(0.1)
        ops.set_config("q", {"rows": 10})
        with pytest.raises(OperationError, match="X needs at most 5 rows"):
            await task
        state = ops.editor.graph.get_config_state(ops._instance("q"))
        assert (state.sql, state.rows) == ("SELECT 1", 10)

    async def test_parallel_tool_calls_cannot_combine_into_a_refused_config(self, editor):
        pytest.importorskip("pydantic_ai")
        from pydantic_ai import Agent

        from panel_flowdash.agent.tools import FlowDashDeps, approval_policy, dashboard_toolset

        from .test_agent_tools import AgentRunner, RunState, responses

        ops = DashboardOps(editor)
        ops.add_node(QUERY, name="q")
        agent = Agent(
            responses(
                [
                    ("set_config", {"name": "q", "config": {"sql": "X SLOW"}}),
                    ("set_config", {"name": "q", "config": {"rows": 10}}),
                ]
            ),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset()],
        )
        runner = AgentRunner(
            agent,
            RunState(flush_interval=0),
            deps=FlowDashDeps(ops),
            approval_policy=approval_policy(),
        )
        output = await runner.run("Change both")
        assert "X needs at most 5 rows" in output
        state = editor.graph.get_config_state(ops._instance("q"))
        assert (state.sql, state.rows) == ("SELECT 1", 10)

    @pytest.mark.parametrize("operation", ["add", "set"])
    async def test_opening_another_dashboard_during_a_check_refuses_the_edit(self, ops, operation):
        if operation == "set":
            ops.add_node(QUERY, name="q")
            task = ops.set_config_async("q", {"sql": "SELECT SLOW"})
        else:
            task = ops.add_node_async(QUERY, {"sql": "SELECT SLOW"}, name="n")
        task = asyncio.ensure_future(task)
        await asyncio.sleep(0.1)
        ops.editor.new_dashboard("Second")
        with pytest.raises(PermissionError, match="opened another dashboard"):
            await task
        assert ops.editor._tile_items == []

    async def test_the_agent_is_told_to_stop_when_the_dashboard_changed(self, editor):
        pytest.importorskip("pydantic_ai")
        from .test_agent_tools import _runner

        ops = DashboardOps(editor)
        runner = _runner(ops, ("add_node", {"component": QUERY, "config": {"sql": "SELECT SLOW"}}))
        task = asyncio.ensure_future(runner.run("Add"))
        await asyncio.sleep(0.25)
        editor.new_dashboard("Second")
        assert "Refused: the user opened another dashboard" in await task
        assert editor._tile_items == []

    async def test_an_agent_change_during_a_draft_check_wins(self, editor):
        query = editor.add_component(QUERY)
        state = editor.graph.get_config_state(query)
        draft = ConfigDraft(editor, query, {})
        releases = draft.bind(state, ["sql", "rows"])
        try:
            draft.state.sql = "SELECT SLOW"
            await asyncio.sleep(0.1)
            ops = DashboardOps(editor)
            await ops.set_config_async(ops.nodes()[0]["name"], {"sql": "SELECT agent"})
            assert draft.state.sql == "SELECT agent"
            await _settle(0.8)
            assert state.sql == draft.state.sql == "SELECT agent"
        finally:
            for release in releases:
                release()

    async def test_a_draft_is_checked_against_the_config_it_is_applied_to(self, editor):
        query = editor.add_component(QUERY)
        state = editor.graph.get_config_state(query)
        draft = ConfigDraft(editor, query, {})
        releases = draft.bind(state, ["sql", "rows"])
        try:
            draft.state.sql = "X SLOW"
            await asyncio.sleep(0.1)
            ops = DashboardOps(editor)
            await ops.set_config_async(ops.nodes()[0]["name"], {"rows": 10})
            await _settle(1.5)
            assert (state.sql, state.rows) == ("SELECT 1", 10)
            assert "X needs at most 5 rows" in draft.alert[0].object
        finally:
            for release in releases:
                release()


class TestPlacement:
    async def test_validate_of_rewired_yaml_does_not_use_the_live_inputs(self, ops):
        ops.add_node(QUERY, name="q")
        ops.add_node(LIMIT, name="cap")
        ops.connect("cap.limit", "q.limit")
        ops.editor._apply_config_patch(ops._instance("q"), {"rows": 7})
        assert [i.message for i in ops.validate() if i.severity == "error"] == [
            "rows must be at most 5"
        ]
        doc = yaml.safe_load(ops.get_dashboard())
        doc["edges"] = []
        assert not [i for i in ops.validate(doc) if i.severity == "error"]

    async def test_the_inspector_shows_why_a_placed_nodes_defaults_are_refused(self, editor):
        node = editor.add_component(DROP)
        select_on_canvas(editor._flow, node_id=node)
        await _settle()
        alerts = [
            a
            for a in pn.Column(*editor._inspector._body).select()
            if "fd-config-check-alert" in getattr(a, "css_classes", [])
        ]
        assert len(alerts) == 1 and "Only SELECT statements are allowed." in alerts[0].object
        assert alerts[0].title == "Config refused"

    async def test_run_command_checks_the_defaults_of_a_component(self, monkeypatch):
        pytest.importorskip("pydantic_ai")
        from panel_flowdash import MemoryDashboardStore
        from panel_flowdash.agent.actions import palette_actions

        from .test_agent_tools import _app

        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "Mine")
        app = _app(store, monkeypatch, components=COMPONENTS)
        app._load_dashboard_sync(model.dashboard_id)
        app._show_edit_mode()
        registry = palette_actions(app)
        with pytest.raises(ValueError, match="'Drop query' was not placed"):
            await registry.invoke("run_command", {"command": f"component:{DROP}"})
        assert app._editor._tile_items == []
        await registry.invoke("run_command", {"command": f"component:{QUERY}"})
        assert [i["component_id"] for i in app._editor._tile_items] == [QUERY]

    async def test_applying_a_version_checks_off_the_event_loop(self, monkeypatch):
        pytest.importorskip("pydantic_ai")
        from pydantic_ai import Agent

        from panel_flowdash import MemoryDashboardStore
        from panel_flowdash.agent.session import AgentSession
        from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

        from .test_agent_tools import _app, scripted

        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "Live")
        app = _app(store, monkeypatch, components=COMPONENTS)
        app._load_dashboard_sync(model.dashboard_id)
        agent = Agent(
            scripted(
                ("add_node", {"component": QUERY, "name": "q"}),
                ("set_config", {"name": "q", "config": {"rows": 3}}),
            ),
            deps_type=FlowDashDeps,
            toolsets=[dashboard_toolset()],
        )
        session = AgentSession(app, agent)
        await session.runner.run("Build")
        (artifact,) = session.runner.state.artifacts

        def blocking(*args, **kwargs):
            raise AssertionError("replace_yaml blocks the event loop")

        monkeypatch.setattr(type(session.ops), "replace_yaml", blocking)
        first = next(v for v in artifact.versions if v.actor == "agent")
        assert await session.apply_version(artifact, first)
        assert DashboardOps(app._editor).node_config("q") == {}


class TestPool:
    @pytest.fixture
    def small_pool(self, monkeypatch):
        def make(workers):
            pool = concurrent.futures.ThreadPoolExecutor(workers)
            monkeypatch.setattr(config_check, "_POOL", pool)
            return pool

        yield make
        HANG.set()

    async def test_the_timeout_starts_when_a_queued_check_runs(self, editor, small_pool):
        small_pool(2)
        spec = _spec(editor, QUERY)
        config = effective_config(spec, {"sql": "SELECT PAUSE"})
        results = await asyncio.gather(
            *(run_check(spec, config, {}, timeout=0.5) for _ in range(4))
        )
        assert results == [[]] * 4

    def test_a_queued_sync_check_gets_its_whole_timeout(self, small_pool):
        small_pool(1)
        editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
        try:
            spec = _spec(editor, QUERY)
            config = effective_config(spec, {"sql": "SELECT PAUSE"})
            with concurrent.futures.ThreadPoolExecutor(2) as callers:
                futures = [
                    callers.submit(run_check_sync, spec, config, {}, timeout=0.5) for _ in range(2)
                ]
                assert [f.result() for f in futures] == [[], []]
        finally:
            editor.dispose()

    async def test_a_check_that_finds_no_free_worker_is_refused(self, editor, small_pool):
        small_pool(1)
        spec = _spec(editor, QUERY)
        hung = asyncio.ensure_future(run_check(spec, effective_config(spec, {"sql": "HANG"}), {}))
        await asyncio.sleep(0.05)
        (problem,) = await run_check(spec, effective_config(spec), {}, timeout=0.1)
        assert "could not start within the time allowed" in problem
        (problem,) = run_check_sync(spec, effective_config(spec), {}, timeout=0.1)
        assert "could not start within the time allowed" in problem
        HANG.set()
        assert await hung == []

    def test_the_pool_size_is_a_module_setting(self, monkeypatch):
        monkeypatch.setattr(config_check, "_POOL", None)
        monkeypatch.setattr(config_check, "CHECK_WORKERS", 3)
        assert config_check._executor()._max_workers == 3


class TestDraftState:
    def _alerts(self, editor):
        return [
            a
            for a in pn.Column(*editor._inspector._body).select()
            if "fd-config-check-alert" in getattr(a, "css_classes", [])
        ]

    async def test_the_alert_stays_while_the_config_still_fails(self, editor):
        node = editor.add_component(DROP)
        select_on_canvas(editor._flow, node_id=node)
        await _settle()
        assert len(self._alerts(editor)) == 1
        editor._apply_config_patch(node, {"rows": 3})
        await _settle()
        assert len(self._alerts(editor)) == 1
        editor._apply_config_patch(node, {"sql": "SELECT 1"})
        await _settle()
        assert self._alerts(editor) == []

    async def test_selecting_a_node_again_reuses_the_last_check(self, editor):
        node, other = editor.add_component(DROP), editor.add_component(LIMIT)
        select_on_canvas(editor._flow, node_id=node)
        await _settle()
        assert len(CALLS) == 1
        select_on_canvas(editor._flow, node_id=other)
        select_on_canvas(editor._flow, node_id=node)
        await _settle()
        assert len(CALLS) == 1
        assert "Only SELECT statements are allowed." in self._alerts(editor)[0].object
        select_on_canvas(editor._flow, node_id=other)
        editor._apply_config_patch(node, {"rows": 3})
        select_on_canvas(editor._flow, node_id=node)
        await _settle()
        assert len(CALLS) == 2


class TestRestore:
    async def _session(self, monkeypatch, *calls, history=True):
        pytest.importorskip("pydantic_ai")
        from pydantic_ai import Agent

        from panel_flowdash import MemoryDashboardStore
        from panel_flowdash.agent.session import AgentSession
        from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

        from .test_agent_tools import _app, scripted

        store = MemoryDashboardStore()
        model = store.create_dashboard("alice", "Live")
        app = _app(store, monkeypatch, components=COMPONENTS, editor_options={"history": history})
        app._load_dashboard_sync(model.dashboard_id)
        agent = Agent(scripted(*calls), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])
        session = AgentSession(app, agent)
        await session.runner.run("Build")
        (artifact,) = session.runner.state.artifacts
        return app, session, artifact

    async def test_a_restore_tells_the_user_which_nodes_fail_their_checks(self, monkeypatch):
        app, session, artifact = await self._session(
            monkeypatch,
            ("add_node", {"component": QUERY, "name": "q", "config": {"sql": "SELECT 1"}}),
            ("set_config", {"name": "q", "config": {"sql": "SELECT 2"}}),
        )
        first = next(v for v in artifact.versions if v.actor == "agent")
        bad = first.value.replace("sql: SELECT 1", "sql: DROP t")
        version = types.SimpleNamespace(value=bad, version=1)
        notes = []
        monkeypatch.setattr(app, "_notify", lambda *args, **kwargs: notes.append(args))
        assert await session.apply_version(artifact, version)
        ((severity, message),) = notes
        assert severity == "warning"
        assert message == (
            "Restored v1, but 1 node fails its config checks: "
            "q: Only SELECT statements are allowed."
        )

    async def test_a_restore_over_a_changing_dashboard_says_so_to_the_user(self, monkeypatch):
        # Without the edit history a restore replaces the YAML, which can race with edits.
        _app, session, artifact = await self._session(
            monkeypatch, ("add_node", {"component": QUERY, "name": "q"}), history=False
        )
        ops = session.ops
        original = type(ops).replace_yaml_async

        async def changed_meanwhile(self, text, *, revision=None, timeout=30):
            self.editor._apply_config_patch(self._instance("q"), {"rows": 4})
            return await original(self, text, revision=revision, timeout=timeout)

        monkeypatch.setattr(type(ops), "replace_yaml_async", changed_meanwhile)
        with pytest.raises(ValueError, match="changed while this version was being restored"):
            await session.apply_version(artifact, artifact.versions[0])
