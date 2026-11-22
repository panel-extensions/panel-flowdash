"""The agent's report tools: reading and editing the report, origins, questions and running them."""

import asyncio

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent import REPORT_TOOLS, DashboardOps, OperationError
from panel_flowdash.agent.assistant import INSTRUCTIONS
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.report import turn_outputs
from panel_flowdash.turns import agent_edits

from .test_agent_tools import _app, _runner, scripted
from .test_report import DOC, REPORT, _editor


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Revenue")
    editor.import_dashboard(DOC + REPORT)
    editor.end_turn()
    yield editor
    editor.dispose()


@pytest.fixture
def plain_editor():
    editor = _editor()
    editor.new_dashboard("Revenue")
    editor.import_dashboard(DOC)
    editor.end_turn()
    yield editor
    editor.dispose()


def _kinds(editor):
    return [next(k for k in b if k != "origin") for b in editor.report["blocks"]]


class TestReading:
    def test_get_report_numbers_the_blocks_and_names_the_questions(self, editor):
        text = DashboardOps(editor).get_report()
        assert "# 0\n- heading: Summary" in text
        assert "# 4, question not run yet: Which products drove the drop\n- turn: t2" in text
        doc = yaml.safe_load(text)
        assert doc["title"] == "Q3 review"
        assert doc["blocks"][2]["node"] == "solo"

    def test_a_dashboard_without_a_report_gives_the_derived_one(self, plain_editor):
        text = DashboardOps(plain_editor).get_report()
        assert text.startswith("# derived:")
        assert yaml.safe_load(text)["blocks"] == [
            {"node": "intro"},
            {"node": "solo"},
            {"group": "regional"},
        ]

    def test_report_text_fills_placeholders_and_lists_nodes(self, editor):
        text = DashboardOps(editor).report_text()
        assert text.startswith("# Q3 review\n\nFilters: pick\n\n## Summary")
        assert "Revenue is 1,234,568." in text
        assert "[node solo: Chart, as a table]\n*Solo, as a table*" in text
        assert "## By region\n\n[node west: Chart]\n[node east: Chart]" in text
        assert "> Not run yet (t2): Which products drove the drop" in text

    def test_a_viewer_cannot_read_the_definition_through_the_report(self, editor):
        editor.read_only = True
        editor.inspect_outputs = True
        with pytest.raises(PermissionError):
            DashboardOps(editor).get_report()


class TestEditing:
    def test_operations_apply_in_order_as_one_edit(self, editor):
        ops = DashboardOps(editor)
        versions = len(editor._history.versions)
        warnings = ops.edit_report(
            [
                {"op": "insert", "block": {"text": "East leads."}, "index": 1},
                {"op": "update", "index": 3, "block": {"caption": None, "show": "view"}},
                {"op": "move", "index": 5, "to": 0},
                {"op": "remove", "index": 1},
                {"op": "title", "title": "Q4 review"},
            ]
        )
        assert warnings == []
        assert len(editor._history.versions) == versions + 1
        report = editor.report
        assert report["title"] == "Q4 review"
        assert _kinds(editor) == ["turn", "text", "text", "node", "group"]
        assert report["blocks"][3]["show"] == "view" and "caption" not in report["blocks"][3]
        assert editor.turns[-1].summary == (
            "Wrote a paragraph in the report. Changed how the report shows solo. "
            "Reordered the report. Removed a heading from the report. Renamed the report."
        )

    @pytest.mark.parametrize(
        ("operations", "message"),
        [
            ([], "at least one operation"),
            ([{"op": "swap"}], r"operations\[0\]\.op: must be one of"),
            ([{"op": "remove", "index": 9}], r"operations\[0\]\.index: must be the index"),
            ([{"op": "move", "index": 0, "to": 1, "x": 1}], "'x'|x: is not a key"),
            ([{"op": "insert", "block": {"text": "a", "heading": "b"}}], "exactly one"),
            ([{"op": "insert", "block": {"node": "ghost"}}], "names no node: 'ghost'"),
            ([{"op": "update", "index": 0, "block": {"level": 7}}], "must be 1, 2 or 3"),
        ],
    )
    def test_a_malformed_operation_changes_nothing(self, editor, operations, message):
        before = editor.report
        versions = len(editor._history.versions)
        with pytest.raises(OperationError, match=message):
            DashboardOps(editor).edit_report(operations)
        assert editor.report == before
        assert len(editor._history.versions) == versions

    def test_a_placeholder_naming_nothing_is_a_warning(self, editor):
        warnings = DashboardOps(editor).edit_report(
            [{"op": "insert", "block": {"text": "{{ m.totl }} and {{ nobody.value }}"}}]
        )
        new = [w for w in warnings if w.path == "report.blocks[5].text"]
        assert len(new) == 2 and "has no output 'totl'" in new[0].message
        assert editor.report["blocks"][5]["text"].startswith("{{ m.totl }}")

    def test_set_report_takes_blocks_or_a_whole_report(self, plain_editor):
        ops = DashboardOps(plain_editor)
        ops.set_report("- heading: Summary\n- node: solo\n")
        assert plain_editor.report == {"blocks": [{"heading": "Summary"}, {"node": "solo"}]}
        assert plain_editor.turns[-1].summary == "Wrote the report."
        ops.set_report("title: Short\nblocks:\n- node: solo\n")
        assert plain_editor.report == {"title": "Short", "blocks": [{"node": "solo"}]}
        ops.set_report("- node: east\n")
        assert plain_editor.report["title"] == "Short"
        with pytest.raises(OperationError, match="list of blocks"):
            ops.set_report("just text")
        with pytest.raises(OperationError, match=r"report\.blocks\[0\]\.group: names no group"):
            ops.set_report("- group: nowhere\n")

    def test_asking_adds_a_question_after_a_block(self, editor):
        ops = DashboardOps(editor)
        turn = ops.ask_in_report("Why did March drop?", after=0)
        assert editor.report["blocks"][1] == {"turn": turn}
        assert next(t for t in editor.turns if t.id == turn).status == "pending"
        with pytest.raises(OperationError, match="after: must be the index"):
            ops.ask_in_report("Why?", after=99)
        with pytest.raises(OperationError, match="question"):
            ops.ask_in_report("   ")

    def test_a_viewer_cannot_edit_the_report(self, editor):
        editor.read_only = True
        ops = DashboardOps(editor)
        for call in (
            lambda: ops.edit_report([{"op": "remove", "index": 0}]),
            lambda: ops.set_report("- node: solo\n"),
            lambda: ops.ask_in_report("Why?"),
            lambda: ops.run_pending("t2"),
        ):
            with pytest.raises(PermissionError):
                call()


class TestOrigins:
    def test_blocks_the_agent_writes_carry_its_turn(self, editor):
        ops = DashboardOps(editor)
        with agent_edits("run-1", "Write a summary"):
            ops.edit_report(
                [
                    {"op": "insert", "block": {"text": "Revenue fell.", "origin": "t1"}},
                    {"op": "update", "index": 0, "block": {"heading": "Overview"}},
                    {"op": "move", "index": 2, "to": 3},
                ]
            )
        agent = editor.turns[-1]
        assert agent.actor == "agent" and agent.question == "Write a summary"
        blocks = editor.report["blocks"]
        assert blocks[0] == {"heading": "Overview", "level": 1, "origin": agent.id}
        assert blocks[-1] == {"text": "Revenue fell.", "origin": agent.id}
        # Blocks the agent left as they were keep who wrote them, the moved one included.
        assert blocks[1] == {"text": "Revenue is {{ m.total:,.0f }}.", "origin": "t1"}
        assert blocks[3]["node"] == "solo" and "origin" not in blocks[3]

    def test_the_agent_cannot_forge_an_origin(self, editor):
        with agent_edits("run-1"):
            DashboardOps(editor).edit_report(
                [{"op": "update", "index": 1, "block": {"origin": "t2"}}]
            )
        assert editor.report["blocks"][1]["origin"] == "t1"

    def test_a_persons_edits_keep_the_blocks_as_given(self, editor):
        DashboardOps(editor).edit_report([{"op": "insert", "block": {"text": "Mine."}}])
        assert editor.report["blocks"][-1] == {"text": "Mine."}


class TestRunPending:
    def test_the_next_edit_answers_the_question(self, editor):
        ops = DashboardOps(editor)
        with agent_edits("run-1", "Answer the question"):
            ops.edit_report([{"op": "insert", "block": {"heading": "Before"}}])
            assert ops.run_pending("t2") == "Which products drove the drop"
            editor.add_component("R/chart", instance_id="answer")
            editor.connect("m", "total", "answer", "value")
        editor.end_agent_turns()
        before, answer = editor.turns[-1], next(t for t in editor.turns if t.id == "t2")
        assert (answer.status, answer.actor, answer.run) == (None, "agent", "run-1")
        assert answer.question == "Which products drove the drop"
        assert editor.node_origin("answer") == "t2"
        assert turn_outputs(editor, "t2") == ["answer"]
        assert before.id != "t2" and before.summary == "Added a heading to the report."
        assert "### Which products drove the drop\n\n[node answer: Chart]" in ops.report_text()

    def test_a_request_without_edits_leaves_the_question_pending(self, editor):
        with agent_edits("run-1"):
            DashboardOps(editor).run_pending("t2")
        editor.end_agent_turns("run-1")
        editor.add_component("R/chart", instance_id="later")
        assert next(t for t in editor.turns if t.id == "t2").status == "pending"
        assert editor.node_origin("later") != "t2"

    def test_only_a_pending_question_can_be_run(self, editor):
        with pytest.raises(OperationError, match=r"t1.*Those waiting are: t2"):
            DashboardOps(editor).run_pending("t1")


@pytest.fixture
def store():
    return MemoryDashboardStore()


@pytest.fixture
def ops(store):
    from .test_agent_ops import _editor as ops_editor

    editor = ops_editor(store)
    editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="base")
    yield DashboardOps(editor)
    editor.dispose()


class TestTools:
    def test_the_report_tools_can_be_offered_by_name(self):
        toolset = dashboard_toolset(REPORT_TOOLS)
        assert list(toolset.tools) == list(REPORT_TOOLS)

    async def test_an_agent_with_its_own_instructions_learns_where_prose_goes(self):
        from .test_agent_tool_subset import _request

        seen = await _request(
            default_instructions=False, tools=["get_report", "edit_report"], catalogue="none"
        )
        assert "Prose about a dashboard" in seen["instructions"]
        narrow = await _request(default_instructions=False, tools=["get_report"], catalogue="none")
        assert "Prose about a dashboard" not in narrow["instructions"]
        assert "Prose about a dashboard" in INSTRUCTIONS

    async def test_an_agents_report_edits_are_its_turn_and_one_undo(self, ops):
        editor = ops.editor
        editor.set_report({"blocks": [{"heading": "Numbers"}]})
        runner = _runner(
            ops,
            ("get_report", {}),
            (
                "edit_report",
                {
                    "operations": [
                        {"op": "insert", "block": {"text": "Base is {{ base.value }}."}},
                        {"op": "insert", "block": {"node": "base"}},
                    ]
                },
            ),
            ("report_text", {}),
        )
        runner.deps.start_request("Describe the base")
        output = await runner.run("Describe the base")
        runner.deps.end_request()
        assert "Base is 21." in output
        agent = editor.turns[-1]
        assert agent.summary == "Wrote a paragraph in the report. Added base to the report."
        assert editor.report["blocks"] == [
            {"heading": "Numbers"},
            {"text": "Base is {{ base.value }}.", "origin": agent.id},
            {"node": "base", "origin": agent.id},
        ]
        assert editor.undo()
        assert editor.report["blocks"] == [{"heading": "Numbers"}]

    async def test_a_mistake_is_sent_back_and_changes_nothing(self, ops):
        runner = _runner(ops, ("edit_report", {"operations": [{"op": "remove", "index": 3}]}))
        output = await runner.run("Remove it")
        assert "operations[0].index" in output
        assert ops.editor.report is None

    async def test_run_pending_answers_the_question_in_the_request(self, ops):
        editor = ops.editor
        turn = editor.ask_in_report("What is twice the base?")
        runner = _runner(
            ops,
            ("run_pending", {"turn": turn}),
            ("add_node", {"component": "T/doubler", "name": "twice"}),
            ("connect", {"source": "base.value", "target": "twice.value"}),
        )
        runner.deps.start_request("Answer the open question")
        output = await runner.run("Answer the open question")
        runner.deps.end_request()
        assert "Connected base.value to twice.value." in output
        answered = next(t for t in editor.turns if t.id == turn)
        assert (answered.status, answered.actor) == (None, "agent")
        assert answered.question == "What is twice the base?"
        assert answered.summary == "Added twice. Connected base to twice."
        assert editor.node_origin("twice") == turn
        assert editor.report["blocks"][-1] == {"turn": turn}
        assert editor.undo()
        assert next(t for t in editor.turns if t.id == turn).status == "pending"
        assert "twice" not in {i["instance_id"] for i in editor._tile_items}


async def test_the_run_button_asks_the_assistant_as_the_questions_request(monkeypatch):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    agent = Agent(
        scripted(
            ("add_node", {"component": "T/doubler", "name": "twice"}),
            ("connect", {"source": "base.value", "target": "twice.value"}),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    app = _app(store, monkeypatch, agent=agent)
    app._load_dashboard_sync(model.dashboard_id)
    session = app._agent_session
    editor = app._editor
    assert editor.run_pending == session.run_pending
    editor.add_component("T/number", instance_id="base")
    turn = editor.ask_in_report("What is twice the base?")
    editor.run_pending(turn)
    assert session.drawer.open
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not session.runner.state.running and editor.node_origin("twice"):
            break
    answered = next(t for t in editor.turns if t.id == turn)
    assert (answered.status, answered.actor) == (None, "agent")
    assert answered.run == session.runner.run_id
    assert editor.node_origin("twice") == turn
    with pytest.raises(ValueError, match="not a question waiting"):
        session.run_pending(turn)
    editor.read_only = True
    with pytest.raises(PermissionError):
        session.run_pending(turn)
