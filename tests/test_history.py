"""The edit history: a version per edit, coalescing, undo and redo, storage, retention and rights."""

import itertools
import json
import sqlite3
import time
from contextlib import closing

import pytest
import yaml
from panel_agents.context import _scope
from panel_agents.events import RunFinished, RunStarted

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.ops import read_revision
from panel_flowdash.dashboard_store import (
    DashboardConflictError,
    DashboardEdge,
    DashboardItem,
    DashboardStore,
)
from panel_flowdash.history import (
    ARTIFACT_KIND,
    BURST_SECONDS,
    MAX_EVENTS,
    artifact_id_for,
    compact,
    decode,
)
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.turns import agent_edits

from .test_agent_ops import _editor as _agent_ops_editor


def _editor(store=None, **params):
    """An editor that records each of the person's edits on its own, unless told otherwise."""
    params.setdefault("history_coalesce", 0)
    return _agent_ops_editor(store, **params)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def editor():
    editor = _editor(MemoryDashboardStore())
    editor.new_dashboard("Mine")
    yield editor
    editor.dispose()


def _ops(editor):
    return [v.metadata["op"] for v in editor.edit_history.versions]


def _last(editor):
    return editor.edit_history.versions[-1]


def _names(editor):
    return sorted(item["instance_id"] for item in editor._tile_items)


def _two_nodes(editor):
    base = editor.add_component("T/number", instance_id="base")
    double = editor.add_component("T/doubler", instance_id="double")
    return base, double


# --------------------------------------------------------------------------
# One version per edit
# --------------------------------------------------------------------------


def test_the_first_edit_publishes_the_dashboard_as_opened_first(editor):
    editor.add_component("T/number", instance_id="base")
    opened, added = editor.edit_history.versions
    assert opened.metadata["op"] == "open" and opened.metadata["base"] is None
    assert "nodes" not in yaml.safe_load(opened.value)
    assert added.metadata["base"] == opened.metadata["revision"]
    assert editor.edit_history.artifact_id == artifact_id_for(editor.dashboard.dashboard_id)
    assert editor.edit_history.artifact.kind == ARTIFACT_KIND


def _move_base(editor):
    editor._flow.nodes = [
        {**node, "position": {"x": 40, "y": 80}} if node["id"] == "base" else node
        for node in editor._flow.nodes
    ]
    editor._mark_rearranged("Moved nodes on the canvas", "move_nodes")


@pytest.mark.parametrize(
    "edit, op, args",
    [
        (
            lambda e: e.add_component("T/scaled", instance_id="extra"),
            "add_node",
            {"component": "T/scaled", "name": "extra"},
        ),
        (lambda e: e.remove_component("base"), "remove_node", {"name": "base"}),
        (
            lambda e: e.connect("base", "value", "double", "value"),
            "connect",
            {"source": "base.value", "target": "double.value"},
        ),
        (lambda e: e.clear(), "clear", {}),
        (
            lambda e: e._apply_config_patch("double", {"factor": 3}),
            "set_config",
            {"name": "double", "keys": ["factor"]},
        ),
        (_move_base, "move_nodes", {}),
        (
            lambda e: setattr(e.settings, "refresh_period", 7),
            "settings",
            {"keys": ["refresh_period"]},
        ),
    ],
)
def test_each_edit_is_one_version_with_its_operation(editor, edit, op, args):
    editor.add_component("T/number", instance_id="base")
    editor.add_component("T/scaled", instance_id="double")
    before = len(editor.edit_history.versions)
    previous = _last(editor)
    edit(editor)
    versions = editor.edit_history.versions
    assert len(versions) == before + 1
    version = versions[-1]
    meta = version.metadata
    assert (meta["op"], meta["args"]) == (op, args)
    assert version.actor == "user" and version.user == "alice"
    assert version.run_id is None and version.tool_call_id is None
    assert meta["turn"] == editor.turns[-1].id
    assert meta["base"] == previous.metadata["revision"]
    assert meta["revision"] == read_revision(version.value) == DashboardOps(editor).revision()
    assert meta["label"] and len(meta["version_id"]) == 32
    assert version.version == editor.edit_history.artifact.applied


def test_edge_edits_and_transforms_are_versions(editor):
    editor.add_component("T/number", instance_id="base")
    editor.add_component("T/doubler", instance_id="double")
    editor.connect("base", "value", "double", "value")
    editor.set_edge_transform("base", "value", "double", "value", [{"op": "round", "digits": 0}])
    editor.disconnect("base", "value", "double", "value")
    assert _ops(editor)[-3:] == ["connect", "set_transform", "disconnect"]
    assert all(
        v.metadata["args"] == {"source": "base.value", "target": "double.value"}
        for v in editor.edit_history.versions[-3:]
    )


def test_imports_and_replaces_are_versions(editor):
    ops = DashboardOps(editor)
    editor.add_component("T/number", instance_id="base")
    editor.import_dashboard({"items": [{"instance_id": "n", "component_id": "T/number"}]})
    # The import laid out n's tile, so the layout names the node too.
    text = ops.get_dashboard().replace("  n:\n", "  m:\n").replace("node: n\n", "node: m\n")
    ops.replace_yaml(text)
    assert _ops(editor)[-2:] == ["import", "replace"]
    assert editor.edit_history.versions[-1].metadata["label"] == "Added m. Removed n."


def test_tile_layout_from_ops_is_a_version(editor):
    ops = DashboardOps(editor)
    editor.add_component("T/number", instance_id="base")
    ops.place_tile("base", width=50)
    assert editor.edit_history.versions[-1].metadata["args"] == {"name": "base", "keys": ["width"]}


def test_an_edit_that_changes_nothing_is_no_version(editor):
    editor.add_component("T/number", instance_id="base")
    count = len(editor.edit_history.versions)
    editor._record_edit(op="layout")
    assert len(editor.edit_history.versions) == count


def test_marking_the_canvas_dirty_by_hand_is_recorded_too(editor):
    editor.add_component("T/number", instance_id="base")
    editor.dashboard.settings["extra"] = 1
    editor.settings.refresh_period = 9
    assert _ops(editor)[-1] == "settings"


def test_a_read_only_editor_records_nothing():
    store = MemoryDashboardStore()
    editor = _editor(store)
    editor.new_dashboard("Mine")
    editor.read_only = True
    editor.add_component("T/number", instance_id="base")
    assert editor.edit_history.versions == [] and not editor.can_undo
    assert editor.undo() is False


def test_history_can_be_turned_off():
    editor = _editor(MemoryDashboardStore(), history=False)
    editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="base")
    assert editor.edit_history.versions == [] and not editor.can_undo


def test_the_history_state_bounds_its_events(editor):
    assert editor.edit_history.state.max_events == MAX_EVENTS


# --------------------------------------------------------------------------
# Coalescing gestures
# --------------------------------------------------------------------------


@pytest.fixture
def clocked():
    editor = _editor(MemoryDashboardStore(), history_coalesce=BURST_SECONDS)
    clock = Clock()
    editor._history._clock = clock
    editor.new_dashboard("Mine")
    editor.add_component("T/scaled", instance_id="a")
    editor.add_component("T/scaled", instance_id="b")
    clock.now += 10
    editor.edit_history.flush()
    yield editor, clock
    editor.dispose()


def test_a_burst_of_edits_is_one_version(clocked):
    editor, clock = clocked
    for factor in (2, 3, 4):
        editor._apply_config_patch("a", {"factor": factor})
        clock.now += 1
    editor.add_component("T/number", instance_id="c")
    clock.now += 1
    editor.connect("c", "value", "b", "value")
    editor.edit_history.flush()
    assert _ops(editor) == ["open", "add_node", "set_config"]
    version = _last(editor)
    assert version.metadata["edits"] == 5
    assert [op["op"] for op in version.metadata["ops"]] == [
        "set_config",
        "set_config",
        "set_config",
        "add_node",
        "connect",
    ]
    assert version.metadata["label"] == "Changed the config of a. Added c. Connected c to b."
    assert yaml.safe_load(version.value)["nodes"]["a"]["config"] == {"factor": 4}


def test_the_first_burst_holds_the_first_edits(editor):
    editor.history_coalesce = BURST_SECONDS
    clock = Clock()
    editor._history._clock = clock
    editor.add_component("T/number", instance_id="a")
    editor.add_component("T/number", instance_id="b")
    editor.edit_history.flush()
    assert _ops(editor) == ["open", "add_node"] and _last(editor).metadata["edits"] == 2


def test_a_pause_ends_the_burst(clocked):
    editor, clock = clocked
    editor._apply_config_patch("a", {"factor": 2})
    clock.now += BURST_SECONDS + 1
    editor._apply_config_patch("a", {"factor": 3})
    editor.edit_history.flush()
    assert _ops(editor)[-2:] == ["set_config", "set_config"]


def test_a_save_or_undo_ends_the_burst(clocked):
    editor, _ = clocked
    editor._apply_config_patch("a", {"factor": 2})
    editor.save()
    editor._apply_config_patch("a", {"factor": 3})
    editor.edit_history.flush()
    assert _ops(editor)[-2:] == ["set_config", "set_config"]
    assert editor.undo() and editor._tile_items[0]["config"]["factor"] == 2
    editor._apply_config_patch("a", {"factor": 5})
    editor._apply_config_patch("b", {"factor": 5})
    editor.edit_history.flush()
    assert _last(editor).metadata["edits"] == 2


def test_an_agent_edit_ends_the_burst(clocked):
    editor, _ = clocked
    editor._apply_config_patch("a", {"factor": 2})
    with agent_edits("run-x"):
        editor._apply_config_patch("b", {"factor": 3})
    editor.end_agent_turns()
    editor._apply_config_patch("a", {"factor": 4})
    editor.edit_history.flush()
    assert [(v.actor, v.metadata["op"]) for v in editor.edit_history.versions[-3:]] == [
        ("user", "set_config"),
        ("agent", "request"),
        ("user", "set_config"),
    ]


def test_a_burst_is_published_before_undo(clocked):
    editor, _ = clocked
    editor._apply_config_patch("a", {"factor": 2})
    assert editor.can_undo and editor.undo()
    assert editor._tile_items[0]["config"].get("factor", 1) == 1


def test_a_paused_burst_is_published_by_the_timer(clocked):
    from bokeh.document import Document

    editor, clock = clocked
    editor._apply_config_patch("a", {"factor": 2})
    history = editor.edit_history
    count = len(history.state.artifacts[history.artifact_id].versions)
    history._on_timer(Document())
    assert len(history.state.artifacts[history.artifact_id].versions) == count
    clock.now += editor.history_coalesce
    history._on_timer(Document())
    assert len(history.state.artifacts[history.artifact_id].versions) == count + 1
    assert history._pending is None


def test_batching_can_be_turned_off(clocked):
    editor, _ = clocked
    editor.history_coalesce = 0
    editor._apply_config_patch("a", {"factor": 2})
    editor._apply_config_patch("a", {"factor": 3})
    assert _ops(editor)[-2:] == ["set_config", "set_config"]


def test_undo_and_redo_step_through_a_persons_edits(editor):
    base, double = _two_nodes(editor)
    editor.connect(base, "value", double, "value")
    assert editor.undo() and editor.graph.edges == []
    assert editor.undo() and _names(editor) == ["base"]
    assert editor.can_redo
    assert editor.redo() and _names(editor) == ["base", "double"]
    assert editor.redo() and len(editor.graph.edges) == 1
    assert not editor.can_redo and editor.redo() is False


def test_a_new_edit_ends_redo(editor):
    _two_nodes(editor)
    editor.undo()
    editor.add_component("T/scaled", instance_id="other")
    assert not editor.can_redo
    assert _names(editor) == ["base", "other"]


def test_undo_reaches_the_dashboard_as_opened_and_stops(editor):
    _two_nodes(editor)
    assert editor.undo() and editor.undo()
    assert editor._tile_items == [] and not editor.can_undo
    assert editor.undo() is False


def test_undo_is_recorded_without_a_new_version(editor):
    _two_nodes(editor)
    count = len(editor.edit_history.versions)
    editor.undo()
    assert len(editor.edit_history.versions) == count
    assert editor.edit_history.artifact.applied == count - 1


def test_undo_restores_turns_and_origins_and_opens_no_turn(editor):
    base = editor.add_component("T/number", instance_id="base")
    turn = editor.node_origin(base)
    editor.end_turn()
    editor.add_component("T/doubler", instance_id="double")
    assert len(editor.turns) == 2
    editor.undo()
    assert [t.id for t in editor.turns] == [turn]
    editor.redo()
    assert len(editor.turns) == 2 and editor.node_origin("double") == editor.turns[1].id
    editor.undo()
    editor.undo()
    assert editor.turns == [] and editor.node_origin("base") is None
    editor.redo()
    assert editor.node_origin("base") == turn


def test_the_next_edit_after_undo_does_not_repeat_the_undone_label(editor):
    _two_nodes(editor)
    editor.undo()
    editor.add_component("T/scaled", instance_id="other")
    assert editor.turns[-1].summary == "Added base. Added other."


def test_undoing_back_to_the_saved_canvas_is_not_dirty(editor):
    editor.add_component("T/number", instance_id="base")
    editor.save()
    editor.add_component("T/doubler", instance_id="double")
    assert editor.dirty
    editor.undo()
    assert not editor.dirty
    editor.undo()
    assert editor.dirty


def test_an_agents_request_is_one_version_undone_as_a_whole(editor):
    editor.add_component("T/number", instance_id="mine")
    state = editor.edit_history.state
    state.apply(RunStarted(run_id="run-1", agent_name="assistant", input="Add two"))
    with _scope(run_id="run-1", tool_call_id="call-1", actor="agent"), agent_edits("run-1"):
        editor.add_component("T/number", instance_id="a1")
    with _scope(run_id="run-1", tool_call_id="call-2", actor="agent"), agent_edits("run-1"):
        editor.add_component("T/doubler", instance_id="a2")
        editor.connect("a1", "value", "a2", "value")
    assert editor.edit_history.versions[-1].metadata["op"] == "add_node"
    state.apply(RunFinished(run_id="run-1"))
    request = editor.edit_history.versions[-1]
    assert (request.actor, request.run_id, request.user) == ("agent", "run-1", None)
    assert request.metadata["op"] == "request" and request.metadata["edits"] == 3
    assert request.metadata["tool_call_ids"] == ["call-1", "call-2"]
    assert [op["op"] for op in request.metadata["ops"]] == ["add_node", "add_node", "connect"]
    agent_turn = next(t for t in editor.turns if t.actor == "agent")
    assert request.metadata["turn"] == agent_turn.id
    assert request.metadata["label"] == "Added a1. Added a2. Connected a1 to a2."
    assert editor.undo() and _names(editor) == ["mine"]
    assert editor.redo() and _names(editor) == ["a1", "a2", "mine"]


def test_a_persons_edit_during_a_request_splits_it_and_loses_nothing():
    store = MemoryDashboardStore()
    editor = _editor(store)
    model = editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="before")
    editor.save()
    state = editor.edit_history.state
    state.apply(RunStarted(run_id="r1", agent_name="assistant", input="q"))
    with _scope(run_id="r1", tool_call_id="c1", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="agent1")
    editor.add_component("T/doubler", instance_id="mine")
    with _scope(run_id="r1", tool_call_id="c2", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="agent2")
    state.apply(RunFinished(run_id="r1"))
    editor.end_agent_turns()
    versions = editor.edit_history.versions
    assert [(v.actor, v.metadata["op"], v.run_id) for v in versions[-3:]] == [
        ("agent", "request", "r1"),
        ("user", "add_node", None),
        ("agent", "request", "r1"),
    ]
    assert versions[-3].metadata["turn"] == versions[-1].metadata["turn"]
    assert all(
        v.metadata["base"] == p.metadata["revision"] for p, v in itertools.pairwise(versions)
    )
    # Undo steps back one version at a time, so the person's edit is never lost.
    assert editor.undo() and _names(editor) == ["agent1", "before", "mine"]
    assert editor.redo()
    editor.save()

    again = _editor(store)
    again.load(model.dashboard_id)
    assert [v.metadata["op"] for v in again.edit_history.versions] == [
        "open",
        "add_node",
        "request",
        "add_node",
        "request",
    ]
    for expected in (["agent1", "before", "mine"], ["agent1", "before"], ["before"], []):
        assert again.undo() and _names(again) == expected


def test_a_save_during_a_request_publishes_its_edits_so_far(editor):
    state = editor.edit_history.state
    state.apply(RunStarted(run_id="r1", agent_name="assistant", input="q"))
    with _scope(run_id="r1", tool_call_id="c1", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="a")
    editor.save()
    assert not editor.dirty
    with _scope(run_id="r1", tool_call_id="c2", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="b")
    state.apply(RunFinished(run_id="r1"))
    assert [v.metadata["op"] for v in editor.edit_history.versions] == [
        "open",
        "request",
        "request",
    ]
    assert editor.undo() and _names(editor) == []


def test_a_failed_request_is_one_version_too(editor):
    from panel_agents.events import RunError

    state = editor.edit_history.state
    state.apply(RunStarted(run_id="run-1", agent_name="assistant", input="Add"))
    with _scope(run_id="run-1", tool_call_id="call-1", actor="agent"), agent_edits("run-1"):
        editor.add_component("T/number", instance_id="a1")
    state.apply(RunError(run_id="run-1", message="cancelled", code="cancelled"))
    assert [(v.actor, v.metadata["op"]) for v in editor.edit_history.versions] == [
        ("user", "open"),
        ("agent", "request"),
    ]


def test_a_request_outside_a_run_ends_with_its_turn(editor):
    with agent_edits("run-x"):
        editor.add_component("T/number", instance_id="a1")
        editor.add_component("T/number", instance_id="a2")
    assert editor.edit_history.requests_pending
    editor.end_agent_turns()
    assert [v.metadata["op"] for v in editor.edit_history.versions] == ["open", "request"]


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite", "json"])
def any_store(request, tmp_path):
    if request.param == "memory":
        yield MemoryDashboardStore()
    elif request.param == "sqlite":
        store = DashboardStore(tmp_path / "d.db")
        yield store
        store.close()
    else:
        yield JSONFolderDashboardStore(tmp_path / "dashboards")


def test_the_history_is_kept_across_sessions(any_store):
    first = _editor(any_store)
    model = first.new_dashboard("Mine")
    _two_nodes(first)
    first.save()
    first.add_component("T/scaled", instance_id="unsaved")
    first.dispose()

    second = _editor(any_store)
    second.load(model.dashboard_id)
    assert _names(second) == ["base", "double"]
    # Only the edits that made the saved dashboard are loaded, not the unsaved one.
    assert [v.metadata["op"] for v in second.edit_history.versions] == [
        "open",
        "add_node",
        "add_node",
    ]
    assert second.undo() and _names(second) == ["base"]
    assert second.undo() and second._tile_items == []
    assert not second.can_undo
    assert second.redo() and _names(second) == ["base"]
    assert second.redo() and _names(second) == ["base", "double"]
    second.dispose()


def test_reopening_the_saved_last_version_adds_no_version(any_store):
    first = _editor(any_store)
    model = first.new_dashboard("Mine")
    _two_nodes(first)
    first.save()
    second = _editor(any_store)
    second.load(model.dashboard_id)
    assert len(second.edit_history.versions) == 3
    assert second.undo() and _names(second) == ["base"]


def test_applied_records_name_their_version_by_id(any_store):
    editor = _editor(any_store)
    model = editor.new_dashboard("Mine")
    _two_nodes(editor)
    editor.undo()
    records = any_store.load_history(model.dashboard_id)
    applied = [r for r in records if r["type"] == "ARTIFACT_APPLIED"]
    ids = [r["metadata"]["version_id"] for r in records if r["type"] == "ARTIFACT_PUBLISHED"]
    assert [r["version_id"] for r in applied] == [ids[1]] and applied[0]["action"] == "undo"


def test_undo_never_steps_into_another_sessions_unsaved_edits(any_store):
    a = _editor(any_store, history_coalesce=0)
    model = a.new_dashboard("Shared")
    a.add_component("T/number", instance_id="n")
    a.save()
    b = _editor(any_store, history_coalesce=0)
    b.load(model.dashboard_id)
    a.add_component("T/doubler", instance_id="a1")
    b.add_component("T/scaled", instance_id="b1")
    a.add_component("T/doubler", instance_id="a2")
    b.undo()
    a.save()
    with pytest.raises(DashboardConflictError):
        b.add_component("T/scaled", instance_id="b2")
        b.save()
    c = _editor(any_store, history_coalesce=0)
    c.load(model.dashboard_id)
    assert _names(c) == ["a1", "a2", "n"]
    assert c.undo() and _names(c) == ["a1", "n"]
    assert c.undo() and _names(c) == ["n"]
    assert c.undo() and c._tile_items == []
    assert not c.can_undo


def test_decode_follows_the_line_to_the_opened_revision():
    def published(revision, base, vid):
        value = f"# revision: {revision}\nflowdash: 2\n"
        return {
            "type": "ARTIFACT_PUBLISHED",
            "artifact_id": "a",
            "value": value,
            "metadata": {"revision": revision, "base": base, "version_id": vid},
        }

    records = [
        published("a1", None, "v1"),
        published("a2", "a1", "v2"),
        published("b1", "a1", "v3"),
        {"type": "ARTIFACT_APPLIED", "artifact_id": "a", "version_id": "v1"},
        published("a3", "a2", "v4"),
    ]
    assert [r["metadata"]["version_id"] for r in decode(records, "a", "a3")] == ["v1", "v2", "v4"]
    assert [r["metadata"]["version_id"] for r in decode(records, "a", "b1")] == ["v1", "v3"]
    ids = lambda out: [r["metadata"]["version_id"] for r in out]
    # Nothing matches: the latest version anchors the line.
    assert ids(decode(records, "a", "ff")) == ["v1", "v2", "v4"]
    # A save of the store revision the dashboard was opened at wins over the YAML.
    saved = [*records, {"type": "FLOWDASH_SAVED", "version_id": "v3", "revision": 7}]
    assert ids(decode(saved, "a", "a3", saved=7)) == ["v1", "v3"]
    # A save naming another revision only anchors when the YAML matches nothing.
    assert ids(decode(saved, "a", "a3", saved=8)) == ["v1", "v2", "v4"]
    assert ids(decode(saved, "a", "ff", saved=8)) == ["v1", "v3"]


def test_deleting_a_dashboard_deletes_its_history(any_store):
    editor = _editor(any_store)
    model = editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="base")
    assert any_store.load_history(model.dashboard_id)
    assert any_store.delete_dashboard("alice", model.dashboard_id)
    assert any_store.load_history(model.dashboard_id) == []
    # An editor still showing it writes nothing more.
    editor.add_component("T/number", instance_id="late")
    assert any_store.load_history(model.dashboard_id) == []


def test_a_store_without_history_still_undoes_within_the_session():
    from panel_flowdash.dashboard_store import BaseDashboardStore

    class Minimal(MemoryDashboardStore):
        load_history = BaseDashboardStore.load_history
        append_history = BaseDashboardStore.append_history
        replace_history = BaseDashboardStore.replace_history

    editor = _editor(Minimal())
    editor.new_dashboard("Mine")
    _two_nodes(editor)
    assert editor.undo() and _names(editor) == ["base"]


_V4_SCHEMA = """
    CREATE TABLE dashboards (
        dashboard_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        items_json TEXT NOT NULL DEFAULT '[]',
        edges_json TEXT NOT NULL DEFAULT '[]',
        tile_layout_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        breakpoints_json TEXT NOT NULL DEFAULT '[]',
        responsive_layouts_json TEXT NOT NULL DEFAULT '{}',
        permission_json TEXT NOT NULL DEFAULT '{}',
        reference_width_json TEXT NOT NULL DEFAULT 'null',
        settings_json TEXT NOT NULL DEFAULT '{}',
        turns_json TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 0
    )
"""


def test_a_model_v4_database_gains_a_history(tmp_path):
    path = tmp_path / "v4.db"
    items = [{"instance_id": "base", "component_id": "T/number", "x": 0, "y": 0, "config": {}}]
    turns = [{"id": "t1", "actor": "user", "at": "2026-11-17T20:15:00Z"}]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(_V4_SCHEMA)
        conn.execute(
            "INSERT INTO dashboards (dashboard_id, user_id, title, version, items_json, "
            "turns_json, revision) VALUES ('old', 'alice', 'Old', 4, ?, ?, 2)",
            (json.dumps(items), json.dumps(turns)),
        )
    store = DashboardStore(path)
    with closing(sqlite3.connect(path)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "dashboard_history" in tables
    assert store.load_history("old") == []
    editor = _editor(store)
    editor.load("old")
    assert editor.can_undo is False
    editor.add_component("T/doubler", instance_id="double")
    editor.save()
    store.close()

    again = _editor(DashboardStore(path))
    again.load("old")
    assert [v.metadata["op"] for v in again.edit_history.versions] == ["open", "add_node"]
    assert again.undo() and _names(again) == ["base"]
    assert [t.id for t in again.turns] == ["t1"]


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def _published(store, dashboard_id):
    return [r for r in store.load_history(dashboard_id) if r["type"] == "ARTIFACT_PUBLISHED"]


def test_opening_a_dashboard_keeps_only_the_newest_versions(any_store):
    editor = _editor(any_store, history_coalesce=0)
    model = editor.new_dashboard("Mine")
    for i in range(8):
        editor.add_component("T/number", instance_id=f"n{i}")
    editor.save()
    reopened = _editor(any_store, history_limit=5)
    reopened.load(model.dashboard_id)
    # Loaded at the first look, not when the dashboard opens.
    assert len(_published(any_store, model.dashboard_id)) == 9
    assert len(reopened.edit_history.versions) == 5
    stored = _published(any_store, model.dashboard_id)
    assert [r["metadata"]["args"].get("name") for r in stored] == [f"n{i}" for i in range(3, 8)]
    assert len(reopened.edit_history.versions) == 5
    assert reopened.undo() and "n7" not in _names(reopened)


def test_old_versions_are_dropped_after_max_days(any_store):
    editor = _editor(any_store, history_coalesce=0)
    model = editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="old")
    editor.add_component("T/number", instance_id="new")
    editor.save()
    records = any_store.load_history(model.dashboard_id)
    for record in records[:2]:
        record["timestamp"] = time.time() - 100 * 86400
    any_store.replace_history(model.dashboard_id, records)
    reopened = _editor(any_store, history_max_days=30)
    reopened.load(model.dashboard_id)
    assert len(reopened.edit_history.versions) == 1
    assert [r["metadata"]["op"] for r in _published(any_store, model.dashboard_id)] == ["add_node"]


def test_a_long_session_is_trimmed_as_it_goes():
    store = MemoryDashboardStore()
    editor = _editor(store, history_coalesce=0, history_limit=20)
    model = editor.new_dashboard("Mine")
    for i in range(40):
        editor.add_component("T/number", instance_id=f"n{i}")
    assert len(_published(store, model.dashboard_id)) <= 20 + 10


def test_compact_keeps_the_runs_and_applied_records_of_kept_versions():
    def published(vid, run=None, at=0.0):
        return {
            "type": "ARTIFACT_PUBLISHED",
            "artifact_id": "a",
            "run_id": run,
            "timestamp": at,
            "metadata": {"version_id": vid},
        }

    records = [
        {"type": "RUN_STARTED", "run_id": "r1"},
        published("v1", "r1"),
        {"type": "RUN_FINISHED", "run_id": "r1"},
        {"type": "RUN_STARTED", "run_id": "r2"},
        {"type": "RUN_STARTED", "run_id": "r3", "parent_run_id": "r2"},
        published("v2", "r3"),
        {"type": "RUN_FINISHED", "run_id": "r3"},
        {"type": "RUN_FINISHED", "run_id": "r2"},
        {"type": "ARTIFACT_APPLIED", "artifact_id": "a", "version_id": "v1"},
        {"type": "ARTIFACT_APPLIED", "artifact_id": "a", "version_id": "v2"},
    ]
    assert compact(records, 5, None) is records
    kept = compact(records, 1, None)
    assert [(r["type"], r.get("run_id"), r.get("version_id")) for r in kept] == [
        ("RUN_STARTED", "r2", None),
        ("RUN_STARTED", "r3", None),
        ("ARTIFACT_PUBLISHED", "r3", None),
        ("RUN_FINISHED", "r3", None),
        ("RUN_FINISHED", "r2", None),
        ("ARTIFACT_APPLIED", None, "v2"),
    ]
    assert compact(records, None, 10, now=100.0) == []


# --------------------------------------------------------------------------
# Rights
# --------------------------------------------------------------------------


@pytest.fixture
def finance_store():
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Finance")
    model.items = [DashboardItem("double", "T/doubler")]
    store.save_dashboard(model)
    return store, model.dashboard_id


def test_versions_hide_what_the_loading_user_may_not_see(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT salary"}, instance_id="secret")
    banker.connect("secret", "value", "double", "value")
    banker.save()
    assert "SELECT salary" in banker.edit_history.versions[-1].value

    alice = _editor(store)
    alice.load(dashboard_id)
    for version in alice.edit_history.versions:
        assert "SELECT salary" not in version.value
        assert "secret.value" not in json.dumps(version.metadata)
        assert read_revision(version.value) == version.metadata["revision"]
    connect = alice.edit_history.versions[-1]
    assert connect.metadata["args"] == {"source": "secret", "target": "double.value"}


def test_undo_never_brings_back_a_node_the_user_may_not_use(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True, history_coalesce=0)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT salary"}, instance_id="secret")
    banker.remove_component("secret")
    banker.save()

    alice = _editor(store)
    alice.load(dashboard_id)
    before = DashboardOps(alice).revision()
    with pytest.raises(PermissionError, match="'secret'"):
        alice.undo()
    assert _names(alice) == ["double"] and DashboardOps(alice).revision() == before
    assert alice.edit_history.artifact.applied == len(alice.edit_history.versions)


def test_undo_keeps_a_restricted_node_as_it_is(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT salary"}, instance_id="secret")
    banker.connect("secret", "value", "double", "value")
    banker.save()

    alice = _editor(store)
    alice.load(dashboard_id)
    alice.add_component("T/number", instance_id="mine")
    assert alice.undo()
    model = alice.to_model()
    secret = next(item for item in model.items if item.instance_id == "secret")
    assert secret.config == {"query": "SELECT salary"}
    assert DashboardEdge("secret", "value", "double", "value") in model.edges


def test_the_agent_cannot_undo_through_the_palette():
    from panel_flowdash.agent.actions import _PERSON_ONLY

    assert _PERSON_ONLY == {"action:undo", "action:redo"}


# --------------------------------------------------------------------------
# Review findings
# --------------------------------------------------------------------------


def test_saving_a_canvas_without_a_dashboard_keeps_its_versions():
    store = MemoryDashboardStore()
    editor = _editor(store, history_coalesce=0)
    assert editor.dashboard is None
    editor.add_component("T/number", instance_id="a")
    model = editor.save("Fresh")
    assert editor.edit_history.artifact_id == artifact_id_for(model.dashboard_id)
    editor.add_component("T/number", instance_id="b")
    ops = [r["metadata"]["op"] for r in _published(store, model.dashboard_id)]
    assert ops == ["open", "add_node", "save", "add_node"]
    assert editor.undo() and editor.undo() and editor.undo() and editor._tile_items == []
    again = _editor(store)
    again.load(model.dashboard_id)
    assert again.undo() and _names(again) == ["a"]
    assert again.undo() and again._tile_items == []


def _agent_edit(editor, run_id, call_id, **kwargs):
    with _scope(run_id=run_id, tool_call_id=call_id, actor="agent"), agent_edits(run_id):
        editor.add_component("T/number", **kwargs)


def test_undo_waits_while_an_agent_request_runs(editor):
    from panel_flowdash.history import HistoryBusy

    editor.add_component("T/number", instance_id="mine")
    state = editor.edit_history.state
    state.apply(RunStarted(run_id="run-1", agent_name="assistant", input="q"))
    _agent_edit(editor, "run-1", "c1", instance_id="a1")
    assert not editor.can_undo and not editor.can_redo
    with pytest.raises(HistoryBusy):
        editor.undo()
    artifact = editor.edit_history.artifact
    with pytest.raises(HistoryBusy):
        editor.edit_history.restore(artifact, artifact.versions[0])
    _agent_edit(editor, "run-1", "c2", instance_id="a2")
    state.apply(RunFinished(run_id="run-1"))
    assert editor.can_undo and editor.undo() and _names(editor) == ["mine"]


def test_a_person_can_undo_removing_a_node_they_may_not_use(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT salary"}, instance_id="secret")
    banker.connect("secret", "value", "double", "value")
    banker.save()

    alice = _editor(store)
    alice.load(dashboard_id)
    alice.remove_component("secret")
    assert alice.can_undo and alice.undo()
    model = alice.to_model()
    secret = next(item for item in model.items if item.instance_id == "secret")
    assert (secret.component_id, secret.config) == ("T/secret", {"query": "SELECT salary"})
    assert DashboardEdge("secret", "value", "double", "value") in model.edges
    assert alice.redo() and "secret" not in _names(alice)


def test_can_undo_says_no_when_undo_would_be_refused(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True, history_coalesce=0)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT salary"}, instance_id="secret")
    banker.remove_component("secret")
    banker.save()
    alice = _editor(store)
    alice.load(dashboard_id)
    assert not alice.can_undo


def test_undo_keeps_the_origin_a_version_gives_a_restricted_node(finance_store):
    from panel_agents.events import RunFinished as Finished

    store, dashboard_id = finance_store
    banker = _editor(store, finance=True, history_coalesce=0)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT 1"}, instance_id="secret")
    first = banker.node_origin("secret")
    banker.end_turn()
    state = banker.edit_history.state
    state.apply(RunStarted(run_id="run-1", agent_name="assistant", input="redo it"))
    with _scope(run_id="run-1", tool_call_id="c1", actor="agent"), agent_edits("run-1"):
        banker.remove_component("secret")
    with _scope(run_id="run-1", tool_call_id="c2", actor="agent"), agent_edits("run-1"):
        banker.add_component("T/secret", {"query": "SELECT 2"}, instance_id="secret")
    state.apply(Finished(run_id="run-1"))
    banker.end_agent_turns()
    banker.save()

    alice = _editor(store)
    alice.load(dashboard_id)
    assert alice.undo()
    assert alice.node_origin("secret") == first
    assert {t.id for t in alice.turns} >= {first}


def test_config_keys_of_a_node_the_user_may_not_use_are_hidden(finance_store):
    store, dashboard_id = finance_store
    banker = _editor(store, finance=True, history_coalesce=0)
    banker.load(dashboard_id)
    banker.add_component("T/secret", {"query": "SELECT 1"}, instance_id="secret")
    banker._apply_config_patch("secret", {"query": "SELECT 2"})
    banker.save()
    alice = _editor(store)
    alice.load(dashboard_id)
    config = [v for v in alice.edit_history.versions if v.metadata["op"] == "set_config"]
    assert config and config[0].metadata["args"] == {"name": "secret"}


def test_previews_and_checks_record_no_history(editor):
    editor.add_component("T/number", instance_id="base")
    scratch = DashboardOps(editor)._scratch_editor()
    assert scratch.history is False


# panel-agents' artifact view passes old and new button options, which older Panel warns about.
@pytest.mark.filterwarnings("ignore:Both '.*' and '.*' were provided:PendingDeprecationWarning")
async def test_an_artifact_view_stays_in_step_with_a_long_session():
    from panel_agents.components.artifacts import ArtifactView

    store = MemoryDashboardStore()
    editor = _editor(store, history_coalesce=0, history_limit=10)
    editor.new_dashboard("Long")
    editor.add_component("T/scaled", instance_id="n")
    history = editor.edit_history
    artifact = history.artifact
    view = ArtifactView(artifact=artifact, state=history.state, on_apply=history.restore)
    for factor in range(2, 10):
        editor._apply_config_patch("n", {"factor": factor})
    assert history.artifact is artifact
    editor.history_limit = 3
    editor.history_coalesce = 0
    for k in range(20):
        editor._apply_config_patch("n", {"factor": k % 9 + 2})
    assert len(artifact.versions) == 30
    assert sum(not v.dropped for v in artifact.versions) <= 3 + 10
    target = len(artifact.versions) - 3
    assert await view.apply_version(target, action="undo")
    assert artifact.applied == target
    expected = yaml.safe_load(artifact.versions[target - 1].value)["nodes"]["n"]["config"]
    assert editor._tile_items[0]["config"]["factor"] == expected["factor"]
    applied = [
        r
        for r in store.load_history(editor.dashboard.dashboard_id)
        if r["type"] == "ARTIFACT_APPLIED"
    ]
    assert (
        applied
        and applied[-1]["version_id"] == artifact.versions[target - 1].metadata["version_id"]
    )


def test_the_history_survives_a_change_in_how_the_yaml_reads():
    import param

    store = MemoryDashboardStore()
    first = _editor(store, history_coalesce=0)
    model = first.new_dashboard("Mine")
    _two_nodes(first)
    first.save()

    class Settings(type(first.settings)):
        accent = param.String(default="blue")

    second = _editor(store, history_coalesce=0, settings_class=Settings)
    second.load(model.dashboard_id)
    assert [v.metadata["op"] for v in second.edit_history.versions] == [
        "open",
        "add_node",
        "add_node",
    ]
    # The saved version stands for the canvas, so the first undo changes something.
    assert second.can_undo
    assert second.undo() and _names(second) == ["base"]


def test_without_a_matching_save_the_latest_saved_version_anchors_the_history():
    store = MemoryDashboardStore()
    first = _editor(store, history_coalesce=0)
    model = first.new_dashboard("Mine")
    _two_nodes(first)
    first.save()
    first.add_component("T/scaled", instance_id="unsaved")
    stored = store._load_any(model.dashboard_id)
    stored.title = "Renamed elsewhere"
    stored.revision = None
    store._dashboards[model.dashboard_id] = stored
    second = _editor(store, history_coalesce=0)
    second.load(model.dashboard_id)
    assert [v.metadata["op"] for v in second.edit_history.versions] == [
        "open",
        "add_node",
        "add_node",
    ]


@pytest.mark.parametrize("kind", ["sqlite", "json"])
def test_retention_loses_no_record_appended_meanwhile(kind, tmp_path):
    import threading

    store = (
        DashboardStore(tmp_path / "d.db")
        if kind == "sqlite"
        else JSONFolderDashboardStore(tmp_path / "d")
    )
    model = store.create_dashboard("alice", "Mine")
    store.append_history(model.dashboard_id, {"type": "ARTIFACT_PUBLISHED", "n": 1})
    writer = None

    def rewrite(records):
        nonlocal writer
        writer = threading.Thread(
            target=store.append_history,
            args=(model.dashboard_id, {"type": "ARTIFACT_PUBLISHED", "n": 2}),
        )
        writer.start()
        writer.join(0.2)
        return []

    store.rewrite_history(model.dashboard_id, rewrite)
    writer.join()
    assert store.load_history(model.dashboard_id) == [{"type": "ARTIFACT_PUBLISHED", "n": 2}]


def test_restoring_changes_only_what_differs(editor):
    base, double = _two_nodes(editor)
    editor.add_component("T/scaled", {"factor": 3}, instance_id="scaled")
    editor.connect(base, "value", double, "value")
    views = {iid: editor._component_instances.get(iid) for iid in (base, double, "scaled")}
    editor._apply_config_patch("scaled", {"factor": 5})
    editor.remove_component(double)
    config = lambda iid: next(i for i in editor._tile_items if i["instance_id"] == iid)["config"]
    assert editor.undo() and editor.undo()
    assert config("scaled") == {"factor": 3}
    assert editor._component_instances.get(base) is views[base]
    assert editor._component_instances.get("scaled") is views["scaled"]
    assert len(editor.graph.edges) == 1
    assert editor.undo() and editor.graph.edges == []
    assert editor.undo() and _names(editor) == ["base", "double"]
    assert editor.redo() and config("scaled") == {"factor": 3}


def test_a_restore_it_cannot_patch_loads_the_whole_version(editor, monkeypatch):
    calls = []
    monkeypatch.setattr(
        type(editor), "_patch_canvas", lambda self, model: calls.append(model) or False
    )
    _two_nodes(editor)
    assert editor.undo() and _names(editor) == ["base"] and calls


@pytest.mark.parametrize("seed", range(20))
def test_undo_and_redo_land_exactly_on_each_version(seed):
    import random

    from panel_flowdash.history import _value_revision

    rnd = random.Random(seed)
    editor = _editor(MemoryDashboardStore())
    editor.new_dashboard("Fuzz")
    ids = []
    for step in range(25):
        r = rnd.random()
        try:
            if r < 0.3 or len(ids) < 2:
                iid = f"n{step}"
                editor.add_component(
                    rnd.choice(["T/number", "T/doubler", "T/scaled"]),
                    instance_id=iid,
                    position={"x": rnd.randint(0, 500), "y": rnd.randint(0, 500)},
                )
                ids.append(iid)
            elif r < 0.45:
                iid = rnd.choice(ids)
                editor.remove_component(iid)
                ids.remove(iid)
            elif r < 0.65:
                editor.connect(*_pair(rnd, ids, "value"))
            elif r < 0.75:
                editor.disconnect(*_pair(rnd, ids, "value"))
            elif r < 0.9:
                editor._apply_config_patch(rnd.choice(ids), {"value": rnd.randint(0, 9)})
            else:
                editor.settings.refresh_period = rnd.randint(1, 60)
        except Exception:
            pass
    history = editor.edit_history
    for step in (editor.undo, editor.redo):
        while step(**({"whole_runs": False} if step == editor.undo else {})):
            applied = history.artifact.applied_version
            assert history.snapshot()[1] == _value_revision(applied.value)
    editor.dispose()


def _pair(rnd, ids, port):
    a, b = rnd.sample(ids, 2)
    return a, port, b, port


def _interleaved_request(editor):
    editor.add_component("T/number", instance_id="before")
    state = editor.edit_history.state
    state.apply(RunStarted(run_id="r1", agent_name="assistant", input="q"))
    with _scope(run_id="r1", tool_call_id="c1", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="agent1")
    editor.add_component("T/doubler", instance_id="mine")
    with _scope(run_id="r1", tool_call_id="c2", actor="agent"), agent_edits("r1"):
        editor.add_component("T/number", instance_id="agent2")
    state.apply(RunFinished(run_id="r1"))
    editor.end_agent_turns()


# panel-agents' artifact view passes old and new button options, which older Panel warns about.
@pytest.mark.filterwarnings("ignore:Both '.*' and '.*' were provided:PendingDeprecationWarning")
async def test_the_drawer_cannot_undo_a_request_over_someone_elses_edit(editor):
    from panel_agents.components.artifacts import ArtifactView

    _interleaved_request(editor)
    history = editor.edit_history
    view = ArtifactView(artifact=history.artifact, state=history.state, on_apply=history.restore)
    applied = history.artifact.applied
    assert await view.undo_run("r1") is False
    assert _names(editor) == ["agent1", "agent2", "before", "mine"]
    assert history.artifact.applied == applied
    assert await view.undo() and _names(editor) == ["agent1", "before", "mine"]
    assert await view.apply_version(applied) and _names(editor) == [
        "agent1",
        "agent2",
        "before",
        "mine",
    ]


# panel-agents' artifact view passes old and new button options, which older Panel warns about.
@pytest.mark.filterwarnings("ignore:Both '.*' and '.*' were provided:PendingDeprecationWarning")
async def test_reverting_to_before_a_split_request_works_and_inside_it_is_refused(editor):
    from panel_agents.components.artifacts import ArtifactView

    # v1 open, v2 before, v3 the request's first part, v4 mine, v5 its second part.
    _interleaved_request(editor)
    history = editor.edit_history
    artifact = history.artifact
    assert [(v.actor, v.metadata["op"]) for v in artifact.versions] == [
        ("user", "open"),
        ("user", "add_node"),
        ("agent", "request"),
        ("user", "add_node"),
        ("agent", "request"),
    ]
    with pytest.raises(PermissionError, match="Someone else edited"):
        history.restore(artifact, artifact.versions[2])
    assert artifact.applied == 5
    view = ArtifactView(artifact=artifact, state=history.state, on_apply=history.restore)
    # Before the whole request is a deliberate revert of everything after it.
    assert await view.apply_version(2) and _names(editor) == ["before"]
    assert await view.apply_version(5)
    assert await view.apply_version(1) and editor._tile_items == []
