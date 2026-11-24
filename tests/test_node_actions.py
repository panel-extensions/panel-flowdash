"""Node actions component libraries add to a node's menus, and the provenance nodes report."""

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash
from panel_flowdash.agent import DashboardOps
from panel_flowdash.node_actions import (
    NodeAction,
    Provenance,
    call_provenance_hook,
    register_node_action,
    unregister_node_action,
)
from panel_flowdash.turns import agent_edits


class Measured(Viewer):
    """Reports where its number comes from."""

    label = param.String(default="Metric: capacity")

    @param.output(param.Number)
    def value(self):
        return 1.0

    def __flowdash_provenance__(self):
        if not self.label:
            return None
        return {"label": self.label, "detail": "Installed capacity", "kind": "metric"}

    def __panel__(self):
        return "measured"


class Card(Viewer):
    """Shows a number from upstream."""

    value = param.Number(default=0)

    def __panel__(self):
        return "card"


@pytest.fixture
def admin():
    return {"value": True}


@pytest.fixture
def editor(admin):
    editor = FlowDash(
        {"T/measured": Measured, "T/card": Card},
        include_builtin_components=False,
        notifications=False,
        authorize_admin=lambda fresh=False: (
            admin["value"] and (not fresh or admin.get("fresh", True))
        ),
    )
    editor.new_dashboard("Actions")
    editor.add_component("T/measured", instance_id="source")
    editor.add_component("T/card", instance_id="card")
    editor.connect("source", "value", "card", "value")
    yield editor
    editor.dispose()


@pytest.fixture
def ran():
    calls = []
    register_node_action(
        NodeAction(
            "test.save",
            "Save it…",
            run=lambda ctx: calls.append(ctx.node),
            applies=lambda ctx: ctx.node == "source",
            icon="save",
            admin_only=True,
        )
    )
    yield calls
    unregister_node_action("test.save")


def _menu_actions(items):
    return [item["action"] for item in items]


def test_an_admin_only_action_is_offered_only_to_admins_and_where_it_applies(editor, admin, ran):
    assert [a.key for a in editor.node_actions("source")] == ["test.save"]
    assert editor.node_actions("card") == []
    admin["value"] = False
    assert editor.node_actions("source") == []


def test_running_an_action_checks_again_that_it_is_offered(editor, admin, ran):
    editor.run_node_action("source", "test.save")
    assert ran == ["source"]
    admin["value"] = False
    with pytest.raises(PermissionError):
        editor.run_node_action("source", "test.save")
    with pytest.raises(PermissionError):
        editor.run_node_action("card", "test.save")
    assert ran == ["source"]


def test_without_authorize_admin_no_one_is_an_admin(ran):
    editor = FlowDash(
        {"T/measured": Measured}, include_builtin_components=False, notifications=False
    )
    editor.new_dashboard("No admin")
    editor.add_component("T/measured", instance_id="source")
    assert not editor.is_admin()
    assert editor.node_actions("source") == []
    editor.dispose()


def test_a_failing_authorize_admin_counts_as_no_admin(editor, admin, ran):
    editor.authorize_admin = lambda fresh=False: 1 / 0
    assert editor.node_actions("source") == []


def test_the_sidecar_menu_offers_the_action_before_remove(editor, ran):
    header = editor._inspector._node_view("source")[0]
    menu = next(o for o in header.select() if "fd-inspector-menu" in getattr(o, "css_classes", []))
    assert _menu_actions(menu.items) == ["duplicate", "node:test.save", "remove"]
    editor._inspector._on_node_menu("source", {"action": "node:test.save"})
    assert ran == ["source"]


def test_a_tiles_more_menu_offers_the_action(editor, ran):
    editor._rebuild_tile_grid()
    tile = editor._inspect_tiles["source"]
    assert "node:test.save" in _menu_actions(tile.more_menu.items)
    tile._on_menu({"action": "node:test.save"})
    assert ran == ["source"]


def test_a_node_reports_its_own_or_the_nearest_upstream_provenance(editor):
    own, by = editor.node_provenance("source")
    assert own == Provenance("Metric: capacity", "Installed capacity", "metric") and by == "source"
    downstream, by = editor.node_provenance("card")
    assert downstream.label == "Metric: capacity" and by == "source"
    editor._component_instances["source"].label = ""
    assert editor.node_provenance("card") is None


def test_the_provenance_hook_takes_a_provenance_or_a_dict_and_ignores_failures():
    class Broken:
        def __flowdash_provenance__(self):
            raise RuntimeError("boom")

    class Odd:
        def __flowdash_provenance__(self):
            return 42

    class Given:
        def __flowdash_provenance__(self):
            return Provenance("Ad hoc", "Sum of x over all rows", "ad hoc")

    assert call_provenance_hook(Broken()) is None
    assert call_provenance_hook(Odd()) is None
    assert call_provenance_hook(object()) is None
    assert call_provenance_hook(Given()).detail == "Sum of x over all rows"


def _chip_labels(tile):
    return [
        o.label for o in tile.filter_notes.objects if "flowdash-provenance-chip" in o.css_classes
    ]


def test_tiles_show_the_provenance_chip_in_edit_mode_and_in_view_mode_only_when_asked(editor):
    editor._rebuild_tile_grid()
    tile = editor._inspect_tiles["card"]
    editor.editable = True
    assert _chip_labels(tile) == ["Metric: capacity"]
    editor.editable = False
    assert _chip_labels(tile) == []
    editor.provenance_labels = True
    assert _chip_labels(tile) == ["Metric: capacity"]


def test_the_sidecar_shows_the_provenance_under_its_header_and_follows_edits(editor):
    inspector = editor._inspector
    objects = inspector._node_view("card")
    inspector._node_tabs = objects[2]
    row = objects[1]
    assert "fd-inspector-provenance" in row.css_classes
    assert row.visible and row[0].label == "Metric: capacity"
    editor._component_instances["source"].label = "Metric: other"
    editor.param.trigger("edited")
    assert row[0].label == "Metric: other"
    editor._component_instances["source"].label = ""
    editor.param.trigger("edited")
    assert not row.visible


def test_how_it_was_made_starts_with_the_provenance(editor):
    with agent_edits(run="run-1", question="Show capacity"):
        editor.add_component("T/card", instance_id="made")
    editor.connect("source", "value", "made", "value")
    _caption, body, _span = editor._how_made_parts("made", None)
    assert "flowdash-provenance-source" in body[0].css_classes
    assert body[0][0].label == "Metric: capacity"


def test_a_kpi_in_the_top_bar_shows_its_provenance_while_editing():
    from panel_flowdash.ui import tile_layout

    editor = FlowDash({"T/measured": Measured}, notifications=False)
    editor.new_dashboard("Top bar")
    editor.add_component("T/measured", instance_id="source")
    editor.add_component("Widgets/KPI", instance_id="kpi")
    editor.connect("source", "value", "kpi", "value")
    tile_layout.move_to_area(editor, "kpi", tile_layout.TOP)
    editor.editable = True
    controls = editor._view_controls
    controls.sync()
    note = controls._provenance["kpi"][1]
    assert note.visible and note[0].label == "Metric: capacity"
    editor.editable = False
    assert not note.visible
    editor.dispose()


def test_an_app_editor_asks_the_app_whether_the_session_is_admin(tmp_path):
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.dashboard_store import MemoryDashboardStore

    app = FlowDashApp(registry={}, project_dir=tmp_path, store=MemoryDashboardStore())
    # The app re-reads the project file each time, so an admin removed meanwhile is refused.
    assert app._editor.authorize_admin == app._authorize_admin
    assert not app._editor.is_admin() and not app._editor.is_admin(fresh=True)
    assert app._editor.project is app._settings_project()


def test_running_an_admin_action_checks_the_admin_afresh(editor, admin, ran):
    admin["fresh"] = False
    assert [a.key for a in editor.node_actions("source")] == ["test.save"]
    with pytest.raises(PermissionError):
        editor.run_node_action("source", "test.save")
    assert ran == []


@pytest.mark.parametrize("state", [{"editable": False}, {"read_only": True}, {"preview": True}])
def test_an_edit_mode_action_is_neither_offered_nor_run_outside_edit_mode(editor, ran, state):
    editor.param.update(**state)
    assert editor.node_actions("source") == []
    with pytest.raises(PermissionError):
        editor.run_node_action("source", "test.save")
    assert ran == []


def test_an_action_declares_the_modes_it_applies_in(editor):
    calls = []
    register_node_action(
        NodeAction("test.look", "Look", run=lambda ctx: calls.append(ctx.mode), modes={"view"})
    )
    try:
        assert "test.look" not in [a.key for a in editor.node_actions("source")]
        editor.editable = False
        editor.run_node_action("source", "test.look")
        assert calls == ["view"]
    finally:
        unregister_node_action("test.look")
    with pytest.raises(ValueError, match="modes"):
        NodeAction("bad", "Bad", run=print, modes={"sometimes"})


def test_the_context_reads_the_node_and_its_edges(editor):
    from panel_flowdash.node_actions import NodeActionContext

    ctx = NodeActionContext(editor, "card")
    assert ctx.node == "card" and ctx.component() == "T/card"
    assert ctx.inputs() == [{"from": "source.value", "to": "card.value", "transform": []}]
    assert ctx.outputs("source") == ctx.inputs()
    assert ctx.output("value", "source") == 1.0
    assert ctx.input_value("value") == 1.0
    assert ctx.mode == "edit" and ctx.project is None
    with pytest.raises(TypeError):
        ctx.context["x"] = 1


def test_replace_node_keeps_the_wiring_and_tile_slot_and_one_undo_reverts_it(editor):
    from panel_flowdash.node_actions import NodeActionContext
    from panel_flowdash.ui import tile_layout

    ctx = NodeActionContext(editor, "source")
    ctx.ops.place_tile("source", index=0, width=50)
    before = [i["instance_id"] for i in tile_layout.area_items(editor, tile_layout.GRID)]
    editor.save()
    new = ctx.replace_node("source", "T/measured", new_name="other")
    grid = [i["instance_id"] for i in tile_layout.area_items(editor, tile_layout.GRID)]
    assert grid.index("other") == before.index("source")
    assert tile_layout.tile_entry(editor, "other")["width"] == 50
    assert ctx.inputs("card") == [{"from": "other.value", "to": "card.value", "transform": []}]
    assert new == "other" and "source" not in ctx.nodes()
    assert editor.undo()
    assert "source" in ctx.nodes() and "other" not in ctx.nodes()
    assert ctx.inputs("card")[0]["from"] == "source.value"


def test_a_failing_edit_is_undone(editor):
    from panel_flowdash.node_actions import NodeActionContext

    ctx = NodeActionContext(editor, "source")
    editor.save()

    def refuse(edge):
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        ctx.replace_node("source", "T/measured", new_name="other", rewire=refuse)
    with pytest.raises(RuntimeError):
        with ctx.edit():
            ctx.ops.add_node("T/card", name="extra")
            raise RuntimeError("boom")
    assert sorted(ctx.nodes()) == ["card", "source"]


def test_an_edit_block_is_one_undo_step_even_without_coalescing(editor):
    from panel_flowdash.node_actions import NodeActionContext

    editor.history_coalesce = 0
    editor.save()
    ctx = NodeActionContext(editor, "source")
    ctx.ops.add_node("T/card", name="before")
    with ctx.edit():
        ctx.ops.add_node("T/card", name="one")
        ctx.ops.add_node("T/card", name="two")
    assert editor.undo()
    assert sorted(ctx.nodes()) == ["before", "card", "source"]


def test_a_failing_edit_leaves_nothing_to_redo(editor):
    from panel_flowdash.node_actions import NodeActionContext

    editor.history_coalesce = 0
    editor.save()
    ctx = NodeActionContext(editor, "source")
    ctx.ops.add_node("T/card", name="kept")
    with pytest.raises(RuntimeError):
        with ctx.edit():
            ctx.ops.add_node("T/card", name="extra")
            ctx.ops.remove_node("card")
            raise RuntimeError("boom")
    assert sorted(ctx.nodes()) == ["card", "kept", "source"]
    assert not editor._history.can_redo
    assert editor.undo()
    assert sorted(ctx.nodes()) == ["card", "source"]
    assert editor.redo()
    assert sorted(ctx.nodes()) == ["card", "kept", "source"]


def test_a_failing_edit_is_rolled_back_without_an_edit_history():
    from panel_flowdash.node_actions import NodeActionContext

    editor = FlowDash(
        {"T/measured": Measured, "T/card": Card},
        include_builtin_components=False,
        notifications=False,
        history=False,
    )
    editor.new_dashboard("No history")
    editor.add_component("T/measured", instance_id="source")
    editor.add_component("T/card", instance_id="card")
    editor.connect("source", "value", "card", "value")
    editor.save()
    assert not editor.dirty
    ctx = NodeActionContext(editor, "source")
    with pytest.raises(ValueError, match="no"):
        with ctx.edit():
            ctx.ops.add_node("T/card", name="extra")
            ctx.ops.disconnect("source.value", "card.value")
            raise ValueError("no")
    assert sorted(ctx.nodes()) == ["card", "source"]
    assert ctx.inputs("card") == [{"from": "source.value", "to": "card.value", "transform": []}]
    assert not editor.dirty
    editor.dispose()


def test_the_context_offers_only_the_ops_of_the_open_dashboard(editor):
    from panel_flowdash.node_actions import NodeActionContext, NodeActionOps

    ctx = NodeActionContext(editor, "source")
    assert isinstance(ctx.ops, NodeActionOps)
    for name in ("save", "open_dashboard", "list_dashboards", "set_title", "replace_yaml"):
        assert not hasattr(ctx.ops, name)
    assert "add_node" in dir(ctx.ops) and "save" not in dir(ctx.ops)
    assert ctx.ops.get_dashboard().startswith("# revision:")
    with pytest.raises(TypeError):
        ctx.ops.get_dashboard("other")


def test_a_failing_action_is_reported(editor, monkeypatch):
    notes = []
    monkeypatch.setattr(
        editor, "_notify", lambda kind, message, **kw: notes.append((kind, message))
    )
    register_node_action(NodeAction("test.fail", "Fail", run=lambda ctx: 1 / 0))
    try:
        editor._on_node_action("source", "test.fail")
    finally:
        unregister_node_action("test.fail")
    assert notes and notes[0][0] == "error" and "division by zero" in notes[0][1]


def test_preview_shows_no_provenance_chip_unless_asked(editor):
    editor._rebuild_tile_grid()
    tile = editor._inspect_tiles["card"]
    editor.editable = True
    assert _chip_labels(tile) == ["Metric: capacity"]
    editor.preview = True
    assert _chip_labels(tile) == []
    editor.provenance_labels = True
    assert _chip_labels(tile) == ["Metric: capacity"]


class Rows(Viewer):
    """Reports a metric and says when a reader sees its rows aggregated."""

    seen = param.List(default=[])

    @param.output(param.DataFrame)
    def data(self):
        import pandas as pd

        return pd.DataFrame({"mw": [1.0, 3.0]})

    def __flowdash_provenance__(self, via=()):
        self.seen.append(via)
        steps = [step for edge in via for step in edge["transform"]]
        if any(step["op"] == "aggregate" for step in steps):
            return Provenance("Derived", "The mean of capacity's rows", "derived")
        return Provenance("Metric: capacity", "Installed capacity", "metric")

    def __panel__(self):
        return "rows"


def test_the_provenance_hook_is_told_the_edges_to_the_view():
    editor = FlowDash(
        {"T/rows": Rows, "T/card": Card}, include_builtin_components=False, notifications=False
    )
    editor.new_dashboard("Via")
    editor.add_component("T/rows", instance_id="rows")
    editor.add_component("T/card", instance_id="card")
    editor.add_component("T/card", instance_id="other")
    transform = [{"op": "column", "column": "mw"}, {"op": "aggregate", "how": "mean"}]
    editor.connect("rows", "data", "card", "value", transform)
    rows = editor._component_instances["rows"]
    assert editor.node_provenance("rows")[0].label == "Metric: capacity"
    assert rows.seen[-1] == ()
    found, by = editor.node_provenance("card")
    assert found.label == "Derived" and by == "rows"
    assert rows.seen[-1] == ({"from": "rows.data", "to": "card.value", "transform": transform},)
    rows.seen[-1][0]["transform"].clear()
    assert editor._dataflow_graph.edges[0]["transform"] == transform
    editor.dispose()


def test_a_hook_without_via_is_called_without_it():
    class Plain:
        def __flowdash_provenance__(self):
            return Provenance("Ad hoc")

    class Keywords:
        def __flowdash_provenance__(self, **kwargs):
            return Provenance(f"Seen {len(kwargs['via'])}")

    edge = {"from": "a.x", "to": "b.y", "transform": []}
    assert call_provenance_hook(Plain(), [edge]).label == "Ad hoc"
    assert call_provenance_hook(Keywords(), [edge]).label == "Seen 1"


def test_the_provenance_detail_is_shown_as_literal_text():
    from panel_flowdash.ui.provenance import provenance_line

    line = provenance_line(Provenance("Ad hoc", "Sum of **mw** over `rows`", "ad hoc"))
    assert line[1].object == "Sum of \\*\\*mw\\*\\* over \\`rows\\`"


def test_an_edit_resyncs_only_the_tiles_it_may_change(editor, monkeypatch):
    for i in range(30):
        editor.add_component("T/card", instance_id=f"other{i}")
    editor.editable = True
    editor._rebuild_tile_grid()
    synced = []
    sync = editor._sync_tile
    monkeypatch.setattr(
        editor, "_sync_tile", lambda iid, *args, **kw: (synced.append(iid), sync(iid, *args, **kw))
    )
    editor.connect("source", "value", "other3", "value")
    assert synced == ["other3"]
    assert _chip_labels(editor._inspect_tiles["other3"]) == ["Metric: capacity"]
    synced.clear()
    DashboardOps(editor).disconnect("source.value", "other3.value")
    assert synced == ["other3"]
    assert _chip_labels(editor._inspect_tiles["other3"]) == []
    synced.clear()
    editor.add_component("T/card", instance_id="late")
    assert set(synced) <= {"late"}


def test_node_names_are_worked_out_once_until_the_nodes_change(editor, monkeypatch):
    from panel_flowdash.agent import yaml_format

    calls = []
    original = yaml_format.node_names
    monkeypatch.setattr(
        yaml_format, "node_names", lambda items: (calls.append(1), original(items))[1]
    )
    for _ in range(5):
        editor._node_name("source")
        editor._node_name("card")
    assert len(calls) <= 1
    calls.clear()
    editor.add_component("T/card", instance_id="more")
    calls.clear()
    for _ in range(5):
        assert editor._node_name("more") == "more"
    assert calls == []


def test_undo_and_redo_offer_the_restored_provenance_again(editor):
    editor.history_coalesce = 0
    editor.editable = True
    editor._rebuild_tile_grid()
    editor.save()
    assert _chip_labels(editor._inspect_tiles["card"]) == ["Metric: capacity"]
    DashboardOps(editor).disconnect("source.value", "card.value")
    assert _chip_labels(editor._inspect_tiles["card"]) == []
    assert editor.undo()
    assert _chip_labels(editor._inspect_tiles["card"]) == ["Metric: capacity"]
    assert editor.redo()
    assert _chip_labels(editor._inspect_tiles["card"]) == []


def test_refresh_provenance_follows_state_outside_the_dashboard(editor):
    editor.editable = True
    editor._rebuild_tile_grid()
    inspector = editor._inspector
    inspector.visible = True
    editor._flow.selection = {"nodes": ["card"], "edges": []}
    inspector.refresh()

    def sidecar_chip():
        rows = [o for o in inspector._body if "fd-inspector-provenance" in o.css_classes]
        return [c.label for row in rows if row.visible for c in row]

    assert sidecar_chip() == ["Metric: capacity"]
    editor._component_instances["source"].label = "Missing metric: capacity"
    assert _chip_labels(editor._inspect_tiles["card"]) == ["Metric: capacity"]
    editor.refresh_provenance(["source"])
    assert _chip_labels(editor._inspect_tiles["card"]) == ["Missing metric: capacity"]
    assert sidecar_chip() == ["Missing metric: capacity"]
    editor._component_instances["source"].label = "Metric: capacity"
    editor.refresh_provenance()
    assert sidecar_chip() == ["Metric: capacity"]


def test_a_nested_config_changed_in_place_is_seen(editor):
    item = next(i for i in editor._tile_items if i["instance_id"] == "card")
    item["config"] = {"nested": {"a": 1}}
    editor._sync_tile_actions()
    before = editor._tile_signatures["card"]
    item["config"]["nested"]["a"] = 2
    assert editor._node_signatures()["card"] != before
