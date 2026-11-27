"""The data catalogue get_report gives the agent, and placeholders pointed at a node's data."""

import pandas as pd
import pytest

pytest.importorskip("pydantic_ai")

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps, OperationError, report_data
from panel_flowdash.agent.assistant import INSTRUCTIONS
from panel_flowdash.agent.report_data import data_outputs, data_upstream, report_catalogue
from panel_flowdash.report import PlaceholderHint

from .test_agent_tools import _runner


class Pipeline:
    """An object a query hands on, not data a placeholder can show."""


@register(
    page=False,
    component=True,
    title="Query",
    provides=[
        {"key": "pipeline", "type": "any"},
        {"key": "data", "type": "any"},
        {"key": "count", "type": "int"},
    ],
)
def query(config):
    config.pipeline = Pipeline()
    config.data = pd.DataFrame(
        {"region": ["EMEA", "APAC", "AMER"], "amount": [3.5, 2.5, 1.5], "unit price": [1, 2, 3]}
    )
    config.count = 3
    return "query"


@register(
    page=False,
    component=True,
    title="Filter",
    provides=[{"key": "pipeline", "type": "any"}],
    requires=[{"key": "source", "type": "any", "required": False}],
)
def filtered(config):
    config.pipeline = Pipeline()
    return "filter"


@register(
    page=False,
    component=True,
    title="Chart",
    requires=[
        {"key": "pipeline", "type": "any", "required": False},
        {"key": "labels", "type": "any", "required": False},
    ],
)
def chart(config):
    return "chart"


@register(
    page=False,
    component=True,
    title="Number",
    provides=[{"key": "value", "type": "int"}],
)
def number(config):
    config.value = 7
    return "number"


COMPONENTS = {"D/query": query, "D/filter": filtered, "D/chart": chart, "D/number": number}

DOC = """\
flowdash: 1
title: Sales
groups:
  charts: {label: Charts}
nodes:
  q: {component: D/query, position: [0, 0]}
  f: {component: D/filter, position: [200, 0]}
  chart: {component: D/chart, position: [400, 0]}
  n: {component: D/number, position: [0, 200]}
  vault: {component: D/number, position: [0, 400]}
  locked: {component: D/chart, position: [400, 400]}
  trend: {component: D/chart, group: charts, position: [400, 200]}
edges:
- {from: q.pipeline, to: f.source}
- {from: n.value, to: chart.labels}
- {from: f.pipeline, to: chart.pipeline}
- {from: vault.value, to: locked.labels}
report:
  blocks:
  - heading: Sales
  - node: chart
  - node: locked
  - group: charts
"""


@pytest.fixture
def ops():
    editor = FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        user="alice",
        history_coalesce=0,
    )
    editor.new_dashboard("Sales")
    editor.import_dashboard(DOC)
    editor.end_turn()
    restricted = editor._node_restricted
    vault = next(i["instance_id"] for i in editor._tile_items if i["instance_id"] == "vault")
    editor._node_restricted = lambda iid: iid == vault or restricted(iid)
    yield DashboardOps(editor)
    editor.dispose()


def _refusal(ops, text):
    with pytest.raises(OperationError) as info:
        ops.edit_report([{"op": "insert", "block": {"text": text}}])
    return str(info.value)


class TestCatalogue:
    def test_data_outputs_are_judged_by_value(self, ops):
        assert [port for port, _ in data_outputs(ops, "q")] == ["data", "count"]
        assert data_outputs(ops, "f") == [] and data_outputs(ops, "chart") == []
        assert data_outputs(ops, "vault") == []

    def test_it_maps_views_to_their_data_and_lists_what_placeholders_read(self, ops):
        text = report_catalogue(ops)
        assert "- chart (Chart) reads its data from n" in text
        assert "- f (Filter) reads its data from q" in text
        assert "{{ q.data.region }}, {{ q.data.amount }}" in text
        assert "{{#each q.data limit=5}}" in text
        assert "{{ q.count }} is now 3" in text
        assert "cannot name (rename them in the query): 'unit price'" in text
        assert "region | amount" in text and "EMEA | 3.5" in text
        assert "pipeline" not in text
        assert "vault" not in text and "locked" not in text

    def test_a_hint_picks_the_input_and_adds_its_note(self, ops):
        ops.placeholder_hint = lambda component, _instance: (
            PlaceholderHint(upstream="pipeline", note="Cite amounts.")
            if component == "D/chart"
            else None
        )
        assert "- chart (Chart) reads its data from q; Cite amounts." in report_catalogue(ops)

    def test_the_walk_follows_the_given_input_first_and_skips_restricted_nodes(self, ops):
        assert data_upstream(ops, "chart", "pipeline") == "q"
        assert data_upstream(ops, "chart", "labels") == "n"
        assert data_upstream(ops, "locked") is None

    def test_the_catalogue_is_bounded(self, ops, monkeypatch):
        monkeypatch.setattr(report_data, "MAX_CATALOGUE", 10)
        text = report_catalogue(ops)
        assert text.count("- ... and") == 2

    async def test_get_report_adds_the_catalogue_and_host_context(self, ops):
        ops.report_context = lambda _ops: "Amounts are in EUR."
        runner = _runner(ops, ("get_report", {}))
        await runner.run("Read it")
        result = next(
            str(part.content)
            for message in runner.messages
            for part in getattr(message, "parts", [])
            if part.part_kind == "tool-return"
        )
        catalogue = result.split("What the report's placeholders can read")[1]
        assert "{{ q.count }} is now 3" in catalogue
        assert catalogue.rstrip().split("\n")[-2] == "Amounts are in EUR."

    def test_a_viewer_gets_no_catalogue(self, ops):
        ops.editor.read_only = True
        ops.editor.inspect_outputs = True
        with pytest.raises(PermissionError):
            ops.report_catalogue()


class TestSuggestions:
    def test_a_view_is_pointed_at_the_data_upstream(self, ops):
        ops.placeholder_hint = lambda component, _instance: (
            PlaceholderHint(upstream="pipeline") if component == "D/chart" else None
        )
        message = _refusal(ops, "Sales were {{ chart.pipeline }}.")
        assert "'chart' has no data of its own; it reads 'q', so read that instead, e.g. " in (
            message
        )
        assert "'{{ q.data.region }}'" in message
        assert "{{ f." not in message

    def test_an_object_port_is_pointed_at_the_nodes_data(self, ops):
        message = _refusal(ops, "{{ q.pipeline }}")
        assert "reads a Pipeline, not data" in message
        assert "'q' has data placeholders can read, e.g. '{{ q.data.region }}'." in message

    def test_a_restricted_node_is_never_suggested(self, ops):
        message = _refusal(ops, "{{ locked.labels }}")
        assert "vault" not in message

    def test_bare_braces_outside_code_are_refused(self, ops):
        message = _refusal(ops, "Total {{ value }} and `{{ code }}`.")
        assert "'{{ value }}' names no node and output" in message
        assert "code" not in message


class TestAnchors:
    def test_insert_after_a_node(self, ops):
        ops.edit_report(
            [
                {"op": "insert", "block": {"text": "Count {{ q.count }}"}, "after": "chart"},
                {"op": "insert", "block": {"heading": "Lead"}, "before": "chart"},
            ]
        )
        kinds = [next(iter(b)) for b in ops.report()["blocks"]]
        assert kinds == ["heading", "heading", "node", "text", "node", "group"]
        assert ops.report()["blocks"][3]["text"] == "Count {{ q.count }}"

    def test_insert_next_to_a_group_member(self, ops):
        ops.edit_report([{"op": "insert", "block": {"text": "Trend"}, "before": "trend"}])
        assert ops.report()["blocks"][3:] == [{"text": "Trend"}, {"group": "charts"}]

    @pytest.mark.parametrize(
        "operation, message",
        [
            ({"after": "q"}, r"no block of the report shows 'q'; those shown are chart, locked"),
            ({"after": "chart", "index": 0}, "give one of index, before and after"),
            ({"after": "chart", "before": "chart"}, "give one of index, before and after"),
        ],
    )
    def test_a_bad_anchor_changes_nothing(self, ops, operation, message):
        before = ops.report()
        with pytest.raises(OperationError, match=message):
            ops.edit_report([{"op": "insert", "block": {"text": "x"}, **operation}])
        assert ops.report() == before

    async def test_the_reply_reads_new_text_back(self, ops):
        runner = _runner(
            ops,
            (
                "edit_report",
                {
                    "operations": [
                        {"op": "insert", "block": {"text": "We had {{ q.count }} regions."}}
                    ]
                },
            ),
        )
        await runner.run("Write it")
        reply = next(
            str(part.content)
            for message in runner.messages
            for part in getattr(message, "parts", [])
            if part.part_kind == "tool-return"
        )
        assert "Your new text as readers see it now" in reply
        assert "We had 3 regions." in reply


def test_the_instructions_tell_the_story_rules():
    assert "Write a report as a story" in INSTRUCTIONS
    assert '"after": "<node>"' in INSTRUCTIONS
