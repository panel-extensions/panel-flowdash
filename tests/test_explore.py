import asyncio

import panel as pn
import pytest
from panel_agents import RunState, events as ev
from panel_agents.components.approval import ApprovalCard

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardItem, MemoryDashboardStore
from panel_flowdash.editor import FlowDash
from panel_flowdash.explore import EARLIER_KIND, ExploreFeed
from panel_flowdash.turns import agent_edits

from .test_agent_ops import COMPONENTS, _editor


@register(page=False, component=True, title="Note", tags=["view"])
def note(config):
    return pn.pane.Markdown("A note", name="note-view")


@pytest.fixture
def editor():
    editor = FlowDash(
        components={**COMPONENTS, "T/note": note},
        include_builtin_components=False,
        notifications=False,
        store=MemoryDashboardStore(),
        user="alice",
    )
    editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="mine")
    with agent_edits(run="run-1", question="Double it"):
        editor.add_component("T/doubler", instance_id="double")
    yield editor
    editor.dispose()


class _Runner:
    """Stands in for an AgentRunner: the feed only reads its state."""

    def __init__(self):
        self.state = RunState(flush_interval=0)


def _feed(editor, runner=None) -> ExploreFeed:
    feed = editor.explore_feed
    if runner is not None:
        feed.attach(runner)
    feed.shown = True
    return feed


def _messages(feed, run_id):
    return [
        (m.role, m.content)
        for m in feed.state.items
        if getattr(m, "run_id", None) == run_id and hasattr(m, "role")
    ]


def _cells(feed):
    return {a.artifact_id: a for a in feed.state.artifacts}


def _turn(editor, instance_id):
    return editor.node_origin(instance_id)


def test_each_turn_is_a_run_with_its_question_summary_and_cards(editor):
    feed = _feed(editor)
    mine, double = _turn(editor, "mine"), _turn(editor, "double")
    assert list(feed.state.runs) == [f"turn-{mine}", f"turn-{double}"]
    assert feed.state.runs[f"turn-{mine}"].agent_name == "Edited by hand"
    assert _messages(feed, f"turn-{mine}") == [("assistant", "Added mine.")]
    assert _messages(feed, f"turn-{double}") == [
        ("user", "Double it"),
        ("assistant", "Added double."),
    ]
    cells = _cells(feed)
    assert cells["node:mine"].value == {"node": "mine"}
    assert cells["node:mine"].versions[0].run_id == f"turn-{mine}"
    assert cells["node:double"].versions[0].run_id == f"turn-{double}"
    assert feed.state.running is False


def test_nodes_without_a_recorded_turn_are_listed_in_a_leading_cell():
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Old")
    model.items = [DashboardItem("old", "T/number"), DashboardItem("older", "T/number")]
    store.save_dashboard(model)
    editor = _editor(store)
    editor.load_model(store.find_by_id_or_title(model.dashboard_id))
    feed = _feed(editor)
    (run_id,) = feed.state.runs
    (earlier,) = feed.state.artifacts
    assert earlier.kind == EARLIER_KIND and earlier.value == {"nodes": ["old", "older"]}
    assert "2 outputs" in _messages(feed, run_id)[0][1]
    body = feed._render(earlier)
    toggle, output = body.objects[0].objects
    assert not output.visible and output.objects == []
    toggle.clicks += 1
    assert output.visible and output.objects
    editor.dispose()


def test_a_recorded_run_replaces_its_projection_and_streams(editor):
    runner = _Runner()
    feed = _feed(editor, runner)
    source = runner.state
    source.add_user_message("Scale it", run_id="run-2")
    source.apply_many(
        [
            ev.RunStarted(run_id="run-2", agent_name="Assistant"),
            ev.ToolCallStart(tool_call_id="c1", tool_name="add_node", run_id="run-2"),
            ev.ToolCallEnd(tool_call_id="c1", run_id="run-2"),
        ]
    )
    assert "run-2" in feed.state.runs and feed.state.running
    assert "c1" in feed.state.tool_calls
    with agent_edits(run="run-2", question="Scale it"):
        editor.add_component("T/scaled", instance_id="scaled")
    source.publish_artifact(
        "dashboard:x", "yaml", kind="flowdash-dashboard", run_id="run-2", applied=True
    )
    source.apply_many(
        [
            ev.ToolCallResult(tool_call_id="c1", content="ok", run_id="run-2"),
            ev.RunFinished(run_id="run-2"),
        ]
    )
    assert "dashboard:x" not in feed.state.artifacts
    assert _cells(feed)["node:scaled"].versions[0].run_id == "run-2"
    assert f"turn-{_turn(editor, 'scaled')}" not in feed.state.runs
    assert list(feed.state.runs)[-1] == "run-2"
    assert feed.state.runs["run-2"].status == "finished"


def test_runs_recorded_while_hidden_show_once_the_feed_is(editor):
    runner = _Runner()
    feed = editor.explore_feed
    feed.attach(runner)
    runner.state.add_user_message("Hello", run_id="run-9")
    runner.state.apply_many([ev.RunStarted(run_id="run-9"), ev.RunFinished(run_id="run-9")])
    assert "run-9" not in feed.state.runs
    feed.shown = True
    assert "run-9" in feed.state.runs


def test_runs_of_another_dashboard_are_left_out(editor):
    runner = _Runner()
    feed = _feed(editor, runner)
    runner.state.apply_many([ev.RunStarted(run_id="run-3"), ev.RunFinished(run_id="run-3")])
    assert "run-3" in feed.state.runs
    editor.new_dashboard("Other")
    assert "run-3" not in feed.state.runs


def test_read_only_viewers_do_not_see_questions_or_summaries(editor):
    editor.read_only = True
    feed = _feed(editor)
    double = _turn(editor, "double")
    assert _messages(feed, f"turn-{double}") == [("assistant", "Changed the dashboard.")]
    assert feed.chat._feed.__class__.__name__ == "ChatFeed"
    assert feed._placement(_cells(feed)["node:double"]) is None


def test_a_removed_node_rebuilds_the_feed_without_its_card(editor):
    feed = _feed(editor)
    assert "node:double" in _cells(feed)
    editor.remove_component("double")
    feed.sync()
    assert "node:double" not in _cells(feed)
    assert "node:mine" in _cells(feed)


def test_a_data_cards_value_is_live(editor):
    from panel_flowdash.ui import tile_layout

    tile_layout.update_tile(editor, "mine", visible=False)
    feed = _feed(editor)
    assert _cells(feed)["node:mine"].kind == "data"
    body = feed._render(_cells(feed)["node:mine"])
    note, value = body.objects
    assert not note.visible
    assert "21" in str(value.objects[0].object)
    feed.editor._dataflow_graph.get_state("mine").value = 5
    assert "5" in str(value.objects[0].object) and "21" not in str(value.objects[0].object)


def test_a_number_on_the_dashboard_shows_its_tile_and_a_hidden_one_its_value(editor):
    from panel_flowdash.ui import tile_layout

    feed = _feed(editor)
    assert _cells(feed)["node:mine"].kind == "view"
    tile_layout.update_tile(editor, "mine", visible=False)
    feed.sync()
    assert _cells(feed)["node:mine"].kind == "data"


def test_a_view_card_shows_the_tiles_view(editor):
    with agent_edits(run="run-4", question="Note it"):
        editor.add_component("T/note", instance_id="memo")
    feed = _feed(editor)
    index = [i["instance_id"] for i in editor._tile_items].index("memo")
    body = feed._render(_cells(feed)["node:memo"])
    assert _cells(feed)["node:memo"].kind == "view"
    assert body.objects[1].objects[0] is editor._tile_objects[index]


def test_a_later_change_marks_the_card(editor):
    feed = _feed(editor)
    body = feed._render(_cells(feed)["node:mine"])
    note = body.objects[0]
    assert not note.visible
    editor.end_turn()
    with agent_edits(run="run-5", question="Connect"):
        editor.connect("mine", "value", "double", "value")
    feed.sync()
    assert note.visible and note.object == "Changed later by 1 request."


def test_add_to_dashboard_shows_a_hidden_tile(editor):
    feed = _feed(editor)
    cell = _cells(feed)["node:mine"]
    from panel_flowdash.ui import tile_layout

    tile_layout.update_tile(editor, "mine", visible=False)
    editor._rebuild_tile_grid()
    assert feed._placed(cell) is None
    assert feed._placement(cell) == {"name": "mine"}
    asyncio.run(feed.actions.invoke("add_to_dashboard", {"name": "mine"}))
    assert feed._placed(cell) == "On dashboard"


def test_approvals_are_decided_on_the_agent_sessions_request(editor):
    runner = _Runner()
    feed = _feed(editor, runner)
    runner.state.apply_many(
        [
            ev.RunStarted(run_id="run-6"),
            ev.ToolCallStart(tool_call_id="c6", tool_name="save_dashboard", run_id="run-6"),
            ev.ToolCallEnd(tool_call_id="c6", run_id="run-6"),
            ev.ApprovalRequested(
                tool_call_id="c6", tool_name="save_dashboard", args={}, run_id="run-6"
            ),
        ]
    )
    request = feed.state.approvals["c6"]
    card = feed._approval(request, feed.state)
    assert isinstance(card, ApprovalCard)
    assert card.request is runner.state.approvals["c6"] and card.state is runner.state
    runner.state.resolve_approval("c6", True)
    assert feed.state.approvals["c6"].status == "approved"


def test_the_composer_needs_an_agent_and_edit_rights(editor):
    feed = _feed(editor)
    assert feed.chat.readonly
    feed.attach(_Runner())
    assert not feed.chat.readonly
    editor.read_only = True
    feed.sync()
    assert feed.chat.readonly


def test_explore_takes_the_place_of_the_canvas_and_grid(editor):
    view = editor._view
    assert view[1] is editor._editor_row
    editor.explore = True
    assert view[1] is editor.explore_feed.__panel__() and editor.explore_feed.shown
    assert not editor._status_bar.visible
    assert not editor._grid_on_screen
    editor.explore = False
    assert view[1] is editor._editor_row and not editor.explore_feed.shown


def test_the_switchers_show_explore(editor):
    bar = editor._dashboard_bar
    editor.editable = False
    editor.explore = True
    bar.sync()
    assert bar.mode_toggle.value == "explore" and bar.mode_tabs.value == "explore"
    bar.mode_toggle.value = "view"
    assert not editor.explore
    bar.mode_tabs.value = "explore"
    assert editor.explore
    bar.mode_tabs.value = "dashboard"
    assert not editor.explore and editor.mode == "dashboard"
    editor.read_only = True
    assert list(bar.mode_toggle.options.values()) == ["explore", "view"]
