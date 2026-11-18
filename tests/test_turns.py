"""Turns and node origins: format version 2, the stores and the turns the editor records."""

import json
import logging
import sqlite3
from contextlib import closing

import pytest
import yaml

from panel_flowdash import MemoryDashboardStore
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.yaml_format import (
    DashboardYAMLError,
    dashboard_from_yaml,
    dashboard_to_dict,
    dashboard_to_yaml,
)
from panel_flowdash.dashboard_store import (
    MODEL_VERSION,
    DashboardItem,
    DashboardModel,
    DashboardStore,
)
from panel_flowdash.editor import export_model
from panel_flowdash.json_store import JSONFolderDashboardStore
from panel_flowdash.turns import (
    MAX_QUESTION_LENGTH,
    MAX_TURNS,
    DashboardTurn,
    TurnError,
    agent_edits,
    decode_turns,
)

from .test_agent_ops import _editor

V1 = """\
flowdash: 1
title: Old
nodes:
  base:
    component: T/number
  double:
    component: T/doubler
edges:
  - from: base.value
    to: double.value
"""

V2 = """\
flowdash: 2
title: New
turns:
  - id: t1
    actor: user
    at: '2026-11-17T20:10:12Z'
    summary: Added base.
  - id: t2
    actor: agent
    at: '2026-11-17T20:14:03Z'
    run: run-81
    question: Double it
    summary: Added double.
  - id: t3
    question: Then triple it
    status: pending
nodes:
  base:
    component: T/number
    origin: t1
    position: [0, 0]
  double:
    component: T/doubler
    origin: t2
    position: [360, 0]
edges:
  - from: base.value
    to: double.value
"""


def _model(**fields):
    return DashboardModel(
        dashboard_id="d1",
        user_id="alice",
        title="Turns",
        items=[DashboardItem("base", "T/number", origin="t1"), DashboardItem("plain", "T/number")],
        turns=[
            DashboardTurn("t1", "agent", "2026-11-17T20:14:03Z", run="run-1", question="Why?"),
            DashboardTurn("t2", "user", "2026-11-17T20:15:00Z", summary="Moved nodes."),
        ],
        **fields,
    )


@pytest.fixture
def editor():
    editor = _editor(MemoryDashboardStore())
    editor.new_dashboard("Mine")
    yield editor
    editor.dispose()


# --------------------------------------------------------------------------
# The format
# --------------------------------------------------------------------------


def test_a_version_1_file_reads_without_turns_or_origins():
    data = dashboard_from_yaml(V1)
    assert data["version"] == MODEL_VERSION
    assert data["turns"] == []
    assert all("origin" not in item for item in data["items"])
    assert yaml.safe_load(dashboard_to_yaml(data))["flowdash"] == 2


@pytest.mark.parametrize(
    "extra, path",
    [
        ("turns: []\n", "document"),
        ("", "nodes.base"),
    ],
)
def test_version_1_refuses_the_keys_of_version_2(extra, path):
    text = V1 + extra
    if not extra:
        text = text.replace("component: T/number\n", "component: T/number\n    origin: t1\n")
    with pytest.raises(DashboardYAMLError) as info:
        dashboard_from_yaml(text)
    assert info.value.path == path


def test_version_2_round_trips_turns_and_origins():
    data = dashboard_from_yaml(V2)
    assert [t["id"] for t in data["turns"]] == ["t1", "t2", "t3"]
    assert data["turns"][2] == {"id": "t3", "question": "Then triple it", "status": "pending"}
    assert {i["instance_id"]: i["origin"] for i in data["items"]} == {"base": "t1", "double": "t2"}
    assert dashboard_to_dict(data) == yaml.safe_load(V2)
    assert dashboard_to_yaml(data) == V2


@pytest.mark.parametrize("version", [0, 3, "2", True])
def test_unknown_versions_are_refused(version):
    with pytest.raises(DashboardYAMLError, match="this release reads 1 and 2") as info:
        dashboard_from_yaml(V2.replace("flowdash: 2", f"flowdash: {json.dumps(version)}"))
    assert info.value.path == "flowdash"


@pytest.mark.parametrize(
    "old, new, path",
    [
        ("    origin: t2\n", "    origin: t9\n", "nodes.double.origin"),
        ("  - id: t2\n", "  - id: t1\n", "turns[1].id"),
        ("  - id: t2\n", "  - id: 'bad id'\n", "turns[1].id"),
        ("    actor: agent\n", "    actor: robot\n", "turns[1].actor"),
        ("    actor: agent\n", "", "turns[1].actor"),
        ("    at: '2026-11-17T20:14:03Z'\n", "    at: yesterday\n", "turns[1].at"),
        ("    run: run-81\n", "    run: 81\n", "turns[1].run"),
        ("    status: pending\n", "    status: done\n", "turns[2].status"),
        ("    question: Then triple it\n", "", "turns[2].question"),
        ("    summary: Added base.\n", "    colour: red\n", "turns[0]"),
    ],
)
def test_malformed_turns_name_the_offending_key(old, new, path):
    assert old in V2
    with pytest.raises(DashboardYAMLError) as info:
        dashboard_from_yaml(V2.replace(old, new, 1))
    assert info.value.path == path


def test_extension_keys_are_ignored_in_turns():
    data = dashboard_from_yaml(V2.replace("    summary: Added base.\n", "    x-note: hi\n"))
    assert data["turns"][0] == {"id": "t1", "actor": "user", "at": "2026-11-17T20:10:12Z"}


def test_questions_can_be_left_out_of_an_export():
    model = _model()
    data = export_model(model, questions=False)
    assert all("question" not in turn for turn in data["turns"])
    assert "Why?" not in dashboard_to_yaml(model, questions=False)
    assert "Why?" in dashboard_to_yaml(model)
    assert model.turns[0].question == "Why?"


def test_export_model_passes_turns_and_origins_through():
    data = export_model(_model())
    assert [t["id"] for t in data["turns"]] == ["t1", "t2"]
    assert [i.get("origin") for i in data["items"]] == ["t1", None]


def test_origins_naming_no_turn_are_not_written():
    model = _model()
    model.items[1].origin = "gone"
    doc = dashboard_to_dict(model)
    assert doc["nodes"]["base"]["origin"] == "t1"
    assert "origin" not in doc["nodes"]["plain"]


# --------------------------------------------------------------------------
# The model and the stores
# --------------------------------------------------------------------------


def test_the_model_migrates_older_versions():
    data = _model().to_dict()
    del data["turns"]
    data["version"] = 3
    loaded = DashboardModel.from_dict(data)
    assert loaded.version == MODEL_VERSION == 4
    assert loaded.turns == []


def test_turns_round_trip_through_to_dict():
    model = _model()
    assert DashboardModel.from_dict(model.to_dict()) == model


@pytest.mark.parametrize(
    "value, error",
    [
        ("oops", TypeError),
        ([{"id": "t1"}], TurnError),
        ([{"id": "t1", "actor": "user", "at": "2026-01-01T00:00:00Z"}] * 2, TurnError),
    ],
)
def test_malformed_turns_are_rejected_by_from_dict(value, error):
    with pytest.raises(error):
        decode_turns(value)
    data = _model().to_dict()
    data["turns"] = value
    with pytest.raises(error):
        DashboardModel.from_dict(data)


@pytest.fixture(params=["sqlite", "memory", "json"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return DashboardStore(tmp_path / "test.db")
    if request.param == "json":
        return JSONFolderDashboardStore(tmp_path / "dashboards")
    return MemoryDashboardStore()


def test_every_store_keeps_turns_and_origins(store):
    store.save_dashboard(_model())
    loaded = store.load_dashboard("alice", "d1")
    assert loaded.turns == _model().turns
    assert [i.origin for i in loaded.items] == ["t1", None]


def test_malformed_stored_turns_are_dropped_with_a_warning(tmp_path, caplog):
    store = JSONFolderDashboardStore(tmp_path / "dashboards")
    store.save_dashboard(_model())
    path = tmp_path / "dashboards" / "d1.json"
    data = json.loads(path.read_text())
    data["turns"] = [{"id": "no actor"}]
    path.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        loaded = store.load_dashboard("alice", "d1")
    assert loaded.turns == [] and [i.instance_id for i in loaded.items] == ["base", "plain"]
    assert any("malformed turns" in r.getMessage() for r in caplog.records)


# The SQLite schema written by releases with dashboard model version 3.
_V3_SCHEMA = """
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
        revision INTEGER NOT NULL DEFAULT 0
    )
"""


class TestSqliteTurnsMigration:
    @pytest.fixture
    def v3_db(self, tmp_path):
        path = tmp_path / "v3.db"
        items = [{"instance_id": "n1", "component_id": "Comp/a", "x": 1, "y": 2, "config": {}}]
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(_V3_SCHEMA)
            conn.execute(
                "INSERT INTO dashboards (dashboard_id, user_id, title, version, items_json, "
                "settings_json, revision) VALUES ('old', 'user1', 'Old', 3, ?, ?, 4)",
                (json.dumps(items), json.dumps({"refresh_period": 5})),
            )
        return path

    def test_a_v3_dashboard_loads_without_turns(self, v3_db):
        loaded = DashboardStore(v3_db).load_dashboard("user1", "old")
        assert loaded.version == MODEL_VERSION
        assert loaded.turns == [] and loaded.items[0].origin is None
        assert loaded.settings == {"refresh_period": 5} and loaded.revision == 4
        with closing(sqlite3.connect(v3_db)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(dashboards)")]
        assert "turns_json" in columns

    def test_the_migrated_database_saves_turns(self, v3_db):
        store = DashboardStore(v3_db)
        loaded = store.load_dashboard("user1", "old")
        loaded.turns = [DashboardTurn("t1", "user", "2026-11-17T20:15:00Z")]
        loaded.items[0].origin = "t1"
        store.save_dashboard(loaded)
        again = DashboardStore(v3_db).load_dashboard("user1", "old")
        assert again.turns == loaded.turns and again.items[0].origin == "t1"
        assert again.version == MODEL_VERSION

    def test_a_malformed_turns_column_loads_without_turns(self, v3_db, caplog):
        store = DashboardStore(v3_db)
        with closing(sqlite3.connect(v3_db)) as conn, conn:
            conn.execute("UPDATE dashboards SET turns_json = '{broken'")
        with caplog.at_level(logging.WARNING):
            loaded = store.load_dashboard("user1", "old")
        assert loaded is not None and loaded.turns == []
        assert any("malformed turns" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# The turns the editor records
# --------------------------------------------------------------------------


def test_a_persons_edits_form_one_turn(editor):
    base = editor.add_component("T/number", instance_id="base")
    double = editor.add_component("T/doubler", instance_id="double")
    editor.connect(base, "value", double, "value")
    (turn,) = editor.turns
    assert turn.actor == "user" and turn.run is None and turn.question is None
    assert turn.id.startswith("t") and len(turn.id) == 9
    assert turn.summary == "Added base. Added double. Connected base.value to double.value."
    assert editor.node_origin("base") == editor.node_origin("double") == turn.id


def test_ending_the_turn_starts_a_new_one_at_the_next_edit(editor):
    editor.add_component("T/number", instance_id="base")
    editor.end_turn()
    editor.end_turn()
    assert len(editor.turns) == 1
    editor.add_component("T/number", instance_id="other")
    first, second = editor.turns
    assert first.id != second.id
    assert editor.node_origin("other") == second.id


def test_an_agent_request_opens_its_turn_at_its_first_edit(editor):
    editor.add_component("T/number", instance_id="base")
    with agent_edits(run="run-1", question="Double it"):
        assert len(editor.turns) == 1
        editor.add_component("T/doubler", instance_id="double")
    # The person's edit while the agent works is a turn between two of the agent's edits.
    editor.add_component("T/number", instance_id="mine")
    with agent_edits(run="run-1"):
        editor.connect("base", "value", "double", "value")
    user, agent, between = editor.turns
    assert user.actor == "user"
    assert (agent.actor, agent.run, agent.question) == ("agent", "run-1", "Double it")
    assert agent.summary == "Added double. Connected base.value to double.value."
    assert (between.actor, between.summary) == ("user", "Added mine.")
    assert editor.node_origin("double") == agent.id
    assert editor.node_origin("mine") == between.id


def test_every_kind_of_edit_is_noted(editor):
    base = editor.add_component("T/number", instance_id="base")
    scaled = editor.add_component("T/scaled", instance_id="scaled")
    editor.end_turn()
    editor._apply_config_patch(scaled, {"factor": 3})
    editor.connect(base, "value", scaled, "value")
    editor.set_edge_transform(base, "value", scaled, "value", [{"op": "round"}])
    editor.disconnect(base, "value", scaled, "value")
    editor.settings.refresh_period = 30
    editor.remove_component(base)
    editor.dirty = True
    turn = editor.turns[-1]
    assert turn.summary == (
        "Changed the config of scaled. Connected base.value to scaled.value. Changed the "
        "transform from base.value to scaled.value. Disconnected base.value from "
        "scaled.value. Changed the settings. Removed base."
    )
    assert len(editor.turns) == 2


def test_long_summaries_and_questions_are_bounded(editor):
    with agent_edits(run="run-1", question="x" * (MAX_QUESTION_LENGTH + 10)):
        for i in range(10):
            editor.add_component("T/number", instance_id=f"n{i}")
    (turn,) = editor.turns
    assert len(turn.question) == MAX_QUESTION_LENGTH and turn.question.endswith("…")
    assert turn.summary.endswith("Added n7. And 2 more changes.")


def test_turns_without_nodes_are_dropped_past_the_limit(editor):
    editor.add_component("T/number", instance_id="base")
    first = editor.turns[0].id
    for i in range(MAX_TURNS + 5):
        editor.end_turn()
        editor.settings.refresh_period = i + 1
    turns = editor.turns
    assert len(turns) == MAX_TURNS
    assert turns[0].id == first


def test_turns_and_origins_are_saved_and_loaded(editor):
    editor.add_component("T/number", instance_id="base")
    with agent_edits(run="run-1", question="Add one"):
        editor.add_component("T/doubler", instance_id="double")
    model = editor.save()
    assert [t.actor for t in model.turns] == ["user", "agent"]
    assert {i.instance_id: i.origin for i in model.items} == {
        "base": model.turns[0].id,
        "double": model.turns[1].id,
    }
    other = _editor(editor.store)
    other.load(model.dashboard_id)
    assert other.turns == model.turns and other.node_origin("double") == model.turns[1].id
    other.add_component("T/number", instance_id="later")
    assert len(other.turns) == 3, "a loaded dashboard's turns are closed"
    other.dispose()


def test_loading_a_dashboard_records_no_turn(editor):
    store = editor.store
    model = store.create_dashboard("alice", "Other")
    model.items = [DashboardItem("a", "T/number"), DashboardItem("b", "T/doubler")]
    from panel_flowdash.dashboard_store import DashboardEdge

    model.edges = [DashboardEdge("a", "value", "b", "value")]
    store.save_dashboard(model)
    editor.load(model.dashboard_id)
    assert editor.turns == [] and not editor.dirty
    assert editor.to_model().items[0].origin is None


def test_importing_a_file_takes_its_turns_and_origins(editor):
    editor.add_component("T/number", instance_id="old")
    editor.import_dashboard(V2.replace("  base:\n", "  base:\n").replace("    origin: t1\n", ""))
    turns = editor.turns
    assert [t.id for t in turns[:3]] == ["t1", "t2", "t3"]
    (imported,) = turns[3:]
    assert (imported.actor, imported.summary) == ("user", "Loaded a dashboard file.")
    assert editor.node_origin("double") == "t2"
    assert editor.node_origin("base") == imported.id


def test_a_replace_keeps_the_canvas_turns_and_origins(editor):
    ops = DashboardOps(editor)
    editor.add_component("T/number", instance_id="base")
    (mine,) = editor.turns
    text = ops.get_dashboard().replace(f"origin: {mine.id}", "origin: forged")
    text = text.replace(
        "nodes:\n", "nodes:\n  double:\n    component: T/doubler\n    origin: forged\n"
    )
    with agent_edits(run="run-7", question="Add a doubler"):
        warnings = ops.replace_yaml(text)
    assert any(w.path == "turns" for w in warnings)
    user, agent = editor.turns
    assert user == mine
    assert (agent.actor, agent.run, agent.summary) == ("agent", "run-7", "Added double.")
    assert editor.node_origin("base") == mine.id
    assert editor.node_origin("double") == agent.id


def test_importing_a_file_that_changes_nothing_opens_no_turn(editor):
    editor.add_component("T/number", instance_id="base")
    exported = editor.export_dashboard()
    editor.import_dashboard(dashboard_to_yaml(exported))
    assert editor.export_dashboard() == exported and editor.dirty
