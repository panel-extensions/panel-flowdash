"""Node actions component libraries add to a node's menus, and the provenance nodes report."""

import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash
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
