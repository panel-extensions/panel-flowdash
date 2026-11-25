"""References: a node of one dashboard shown, read only, in another."""

import weakref

import pandas as pd
import panel as pn
import param
import pytest
from bokeh.document import Document
from panel.viewable import Viewer

import panel_flowdash.references as references
from panel_flowdash import DashboardStore, FlowDash, MemoryDashboardStore, Permission, register
from panel_flowdash.references import _SOURCES, REFERENCE_COMPONENT, Reference, source_graph


@register(page=False, component=True, title="Numbers", config=["scale"])
class Numbers(Viewer):
    scale = param.Integer(default=1)

    @param.output(param.DataFrame)
    @param.depends("scale")
    def table(self):
        return pd.DataFrame({"x": [1, 2, 3], "y": [v * self.scale for v in (1, 2, 3)]})

    def __panel__(self):
        return pn.pane.Markdown("numbers")


@register(page=False, component=True, title="Bars", config=["color"])
class Bars(Viewer):
    table = param.DataFrame(default=None, allow_None=True)

    color = param.String(default="red")

    @param.output(param.Number)
    @param.depends("table")
    def total(self):
        return None if self.table is None else float(self.table["y"].sum())

    def _text(self, table, color):
        return "no data" if table is None else f"bars {color} {int(table['y'].sum())}"

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(self._text, self.param.table, self.param.color))


@register(page=False, component=True, title="Shown")
class Shown(Viewer):
    value = param.Parameter(default=None)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(lambda v: f"value {v}", self.param.value))


COMPONENTS = {"Test/numbers": Numbers, "Test/bars": Bars, "Test/shown": Shown}


def text(view):
    return " ".join(str(p.object) for p in view.select(pn.pane.Markdown))


def tile(editor, instance_id):
    index = [item["instance_id"] for item in editor._tile_items].index(instance_id)
    return editor._tile_objects[index]


@pytest.fixture
def store():
    return MemoryDashboardStore()


def make_editor(store, **params):
    return FlowDash(COMPONENTS, store=store, notifications=False, **params)


@pytest.fixture
def source(store):
    """Dashboard A: numbers feeding a bar chart, saved."""
    editor = make_editor(store)
    editor.new_dashboard("Exec dashboard")
    editor.add_component("Test/numbers", {"scale": 2}, instance_id="numbers")
    editor.add_component("Test/bars", {"color": "blue"}, instance_id="chart")
    assert editor.connect("numbers", "table", "chart", "table") is True
    editor.save()
    yield editor
    editor.dispose()


@pytest.fixture
def importer(store, source):
    """Dashboard B, referencing A's chart."""
    editor = make_editor(store)
    editor.new_dashboard("Ops dashboard")
    yield editor
    editor.dispose()


def add_reference(editor, dashboard, node, instance_id="ref"):
    return editor.add_component(
        REFERENCE_COMPONENT, {"dashboard": dashboard, "node": node}, instance_id=instance_id
    )


def test_a_reference_shows_the_source_node_live(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    reference = importer._component_instances["ref"]
    assert reference.status == "ok"
    assert text(tile(importer, "ref")) == "bars blue 12"
    # Its own view, not the source editor's.
    assert tile(importer, "ref") is not tile(source, "chart")


def test_a_reference_passes_on_the_source_outputs(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    assert [port.name for port in importer._node_spec("ref").outputs] == ["total"]
    importer.add_component("Test/shown", instance_id="shown")
    assert importer.connect("ref", "total", "shown", "value") is True
    assert text(tile(importer, "shown")) == "value 12.0"


def test_an_edit_saved_at_the_source_updates_the_reference(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    importer.add_component("Test/shown", instance_id="shown")
    importer.connect("ref", "total", "shown", "value")

    view = importer._component_instances["ref"]._holder.objects[0]
    source._apply_config_patch("numbers", {"scale": 10})
    source._apply_config_patch("chart", {"color": "green"})
    # Unsaved edits stay in the source.
    assert text(tile(importer, "ref")) == "bars blue 12"
    source.save()
    assert text(tile(importer, "ref")) == "bars green 60"
    assert text(tile(importer, "shown")) == "value 60.0"
    # Only config changed, so the source graph patched its nodes and the view stayed.
    assert importer._component_instances["ref"]._holder.objects[0] is view


def test_references_in_one_session_share_one_source_graph(store, source, importer, monkeypatch):
    doc = Document()
    monkeypatch.setattr(references, "_session_doc", lambda: doc)
    other = make_editor(store)
    other.new_dashboard("Board dashboard")
    dashboard_id = source.dashboard.dashboard_id
    add_reference(importer, dashboard_id, "chart")
    add_reference(other, dashboard_id, "chart")
    graph = source_graph(importer, dashboard_id)
    assert len(graph._references) == 2
    # Each shows its own view of the same node.
    assert tile(importer, "ref") is not tile(other, "ref")
    assert text(tile(other, "ref")) == text(tile(importer, "ref")) == "bars blue 12"
    # Only what the chart needs is computed.
    assert set(graph._loaded) == {"numbers", "chart"}
    # The graph does not keep the session alive, and goes the moment the session ends.
    assert isinstance(graph._doc, weakref.ref)
    for callback in doc.session_destroyed_callbacks:
        callback(None)
    assert graph.disposed and graph.editor is None
    assert doc not in _SOURCES


def test_editors_outside_a_session_never_share_a_source_graph(store, source, importer):
    other = make_editor(store, user="bob")
    other.new_dashboard("Board dashboard")
    dashboard_id = source.dashboard.dashboard_id
    add_reference(importer, dashboard_id, "chart")
    add_reference(other, dashboard_id, "chart")
    assert source_graph(importer, dashboard_id) is not source_graph(other, dashboard_id)
    importer.remove_component("ref")
    other.remove_component("ref")
    assert not _SOURCES.get(importer) and not _SOURCES.get(other)


def test_a_deleted_node_shows_missing_node(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    source.remove_component("chart")
    source.save()
    reference = importer._component_instances["ref"]
    assert reference.status == "missing"
    assert "Missing node: chart" in [p.label for p in [reference.__flowdash_provenance__()]]
    assert "Missing node: chart" in str(tile(importer, "ref"))


def test_a_deleted_dashboard_shows_missing_node(store, source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    store.delete_dashboard(source.user, source.dashboard.dashboard_id)
    reference = importer._component_instances["ref"]
    assert reference.status == "missing"
    found = importer.node_provenance("ref")
    assert found[0].label == "Missing node: chart"


def test_a_viewer_without_access_gets_no_access(store, source):
    dashboard_id = source.dashboard.dashboard_id
    importer = make_editor(
        store, authorize_dashboard=lambda model, access: model.dashboard_id != dashboard_id
    )
    importer.new_dashboard("Ops dashboard")
    add_reference(importer, dashboard_id, "chart")
    reference = importer._component_instances["ref"]
    assert reference.status == "denied"
    assert importer.node_provenance("ref")[0].label == "No access"
    # Nothing of the source is evaluated for this viewer.
    assert not _SOURCES.get(store)
    importer.dispose()


def test_the_provenance_says_where_the_node_comes_from(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    provenance, node = importer.node_provenance("ref")
    assert (provenance.label, provenance.kind, node) == ("From Exec dashboard", "reference", "ref")
    importer.add_component("Test/shown", instance_id="shown")
    importer.connect("ref", "total", "shown", "value")
    assert importer.node_provenance("shown")[0].label == "From Exec dashboard"


def test_a_reference_is_saved_as_an_ordinary_node(store, source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    saved = importer.save()
    (item,) = saved.items
    assert item.component_id == REFERENCE_COMPONENT
    assert item.config == {"dashboard": source.dashboard.dashboard_id, "node": "chart"}
    reopened = make_editor(store)
    reopened.load(saved.dashboard_id)
    assert text(tile(reopened, "ref")) == "bars blue 12"
    reopened.dispose()


def test_a_reference_follows_a_change_of_permission(store, source, importer):
    dashboard_id = source.dashboard.dashboard_id
    importer.authorize_dashboard = lambda model, access: model.permission.deny_users == frozenset()
    add_reference(importer, dashboard_id, "chart")
    store.set_permission(dashboard_id, Permission.from_spec(deny_users=["someone"]))
    assert importer._component_instances["ref"].status == "denied"


def test_a_reference_is_a_regular_reference_component():
    assert isinstance(Reference, type)


def test_a_node_brought_back_at_the_source_shows_again(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    source.remove_component("chart")
    source.save()
    assert importer._component_instances["ref"].status == "missing"
    source.undo()
    source.save()
    assert importer._component_instances["ref"].status == "ok"
    assert text(tile(importer, "ref")) == "bars blue 12"


def test_the_dashboard_title_follows_a_rename(store, source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    store.rename_dashboard(source.user, source.dashboard.dashboard_id, "Board dashboard")
    assert importer.node_provenance("ref")[0].label == "From Board dashboard"


def test_new_ports_at_the_source_place_the_reference_again(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    importer.add_component("Test/shown", instance_id="shown")
    importer.connect("ref", "total", "shown", "value")
    # The chart is replaced by the numbers node's kind, whose output is a table.
    numbers = source.to_model()
    chart = next(i for i in numbers.items if i.instance_id == "chart")
    chart.component_id, chart.config = "Test/numbers", {"scale": 3}
    numbers.edges = []
    source.load_model(numbers)
    source.save()
    assert [p.name for p in importer._node_spec("ref").outputs] == ["table"]
    assert text(tile(importer, "ref")) == "numbers"
    # The edge from the old port is gone, the node keeps its place and config.
    assert importer._dataflow_graph.edges == []
    assert importer.to_model().items[0].config["node"] == "chart"


def test_a_missing_source_keeps_the_ports_and_passes_on_the_failure(source, importer):
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    importer.add_component("Test/shown", instance_id="shown")
    importer.connect("ref", "total", "shown", "value")
    source.remove_component("chart")
    source.save()
    assert [p.name for p in importer._node_spec("ref").outputs] == ["total"]
    assert importer.connect("ref", "total", "shown", "value") is not True  # still wired
    assert len(importer._dataflow_graph.edges) == 1


async def test_the_inspector_offers_the_readable_dashboards_and_their_nodes(
    store, source, importer
):
    from panel_flowdash.config_choices import node_choices

    hidden = make_editor(store)
    hidden.new_dashboard("Private dashboard")
    importer.authorize_dashboard = lambda model, access: model.title != "Private dashboard"
    importer.add_component(REFERENCE_COMPONENT, instance_id="ref")
    choices = await node_choices(importer, "ref")
    assert choices["dashboard"].labelled() == {"Exec dashboard": source.dashboard.dashboard_id}
    importer._apply_config_patch("ref", {"dashboard": source.dashboard.dashboard_id})
    choices = await node_choices(importer, "ref")
    assert choices["node"].values() == ["chart", "numbers"]
    hidden.dispose()


def test_a_reference_picked_in_the_inspector_gets_the_source_ports(source, importer):
    importer.add_component(REFERENCE_COMPONENT, instance_id="ref")
    assert importer._component_instances["ref"].status == "unset"
    assert importer._node_spec("ref").outputs == []
    importer._apply_config_patch(
        "ref", {"dashboard": source.dashboard.dashboard_id, "node": "chart"}
    )
    assert [p.name for p in importer._node_spec("ref").outputs] == ["total"]
    assert text(tile(importer, "ref")) == "bars blue 12"
    assert importer.to_model().items[0].config == {
        "dashboard": source.dashboard.dashboard_id,
        "node": "chart",
    }


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_stores_say_which_dashboard_changed(kind, tmp_path):
    store = MemoryDashboardStore() if kind == "memory" else DashboardStore(tmp_path / "d.db")
    seen = []

    class Watcher:
        def changed(self, dashboard_id):
            seen.append(dashboard_id)

    watcher = Watcher()
    store.watch_changes(watcher.changed)
    model = store.create_dashboard("alice", "Exec dashboard")
    store.rename_dashboard("alice", model.dashboard_id, "Board dashboard")
    store.set_permission(model.dashboard_id, Permission.from_spec(allow_users=["bob"]))
    store.set_owner(model.dashboard_id, "bob")
    store.delete_dashboard("bob", model.dashboard_id)
    assert seen == [model.dashboard_id] * 5
    # Held weakly, and gone once unwatched.
    store.unwatch_changes(watcher.changed)
    store.create_dashboard("alice", "Ops dashboard")
    assert len(seen) == 5
    store.watch_changes(watcher.changed)
    del watcher
    store.create_dashboard("alice", "Board deck")
    assert len(seen) == 5
    if kind == "sqlite":
        store.close()


def hide(editor, instance_id):
    layout = [e for e in editor.layout if e.get("instance_id") != instance_id]
    editor._set_grid_layout([*layout, {"instance_id": instance_id, "visible": False}], True)


def viewer_of(dashboard_id):
    """An authorize_dashboard letting its user view, but not edit, *dashboard_id*."""
    return lambda model, access: access == "view" or model.dashboard_id != dashboard_id


def test_a_viewer_may_reference_only_visible_tiles_unless_they_may_inspect(store, source):
    dashboard_id = source.dashboard.dashboard_id
    source._pending_tile_layout = [
        {"instance_id": "numbers", "visible": False},
        {"instance_id": "chart"},
    ]
    source.save()
    assert {e["instance_id"]: e.get("visible") for e in store._load_any(dashboard_id).tile_layout}[
        "numbers"
    ] is False
    viewer = make_editor(store, authorize_dashboard=viewer_of(dashboard_id))
    viewer.new_dashboard("Ops dashboard")
    add_reference(viewer, dashboard_id, "numbers", "hidden")
    add_reference(viewer, dashboard_id, "chart", "shown")
    assert viewer._component_instances["hidden"].status == "denied"
    assert viewer._node_spec("hidden").outputs == []
    assert viewer._component_instances["shown"].status == "ok"
    model = store._load_any(dashboard_id)
    model.settings["viewers_can_inspect"] = True
    store.save_dashboard(model)
    assert viewer._component_instances["hidden"].status == "ok"
    viewer.dispose()


async def test_the_inspector_offers_a_viewer_only_the_visible_tiles(store, source):
    from panel_flowdash.config_choices import node_choices

    dashboard_id = source.dashboard.dashboard_id
    source._pending_tile_layout = [{"instance_id": "numbers", "visible": False}]
    source.save()
    viewer = make_editor(store, authorize_dashboard=viewer_of(dashboard_id))
    viewer.new_dashboard("Ops dashboard")
    viewer.add_component(REFERENCE_COMPONENT, {"dashboard": dashboard_id}, instance_id="ref")
    assert (await node_choices(viewer, "ref"))["node"].values() == ["chart"]
    viewer.dispose()


def test_a_revoked_dashboard_is_not_computed_again(store, source, importer):
    dashboard_id = source.dashboard.dashboard_id
    importer.authorize_dashboard = lambda model, access: "bob" not in model.permission.deny_users
    add_reference(importer, dashboard_id, "chart")
    graph = source_graph(importer, dashboard_id)
    store.set_permission(dashboard_id, Permission.from_spec(deny_users=["bob"]))
    source._apply_config_patch("numbers", {"scale": 100})
    source.save()
    assert importer._component_instances["ref"].status == "denied"
    # The graph read the new version but loaded none of it.
    assert graph.disposed or not graph._loaded


@register(page=False, component=True, title="Banner")
def banner():
    return pn.pane.Markdown("banner ok")


def test_a_function_component_without_config_is_shown(store):
    components = {**COMPONENTS, "Test/banner": banner}
    source = FlowDash(components, store=store, notifications=False)
    source.new_dashboard("Exec dashboard")
    source.add_component("Test/banner", instance_id="banner")
    source.save()
    importer = FlowDash(components, store=store, notifications=False)
    importer.new_dashboard("Ops dashboard")
    add_reference(importer, source.dashboard.dashboard_id, "banner")
    assert importer._component_instances["ref"].status == "ok"
    assert text(tile(importer, "ref")) == "banner ok"
    importer.dispose()
    source.dispose()


def test_a_node_of_a_restricted_component_is_no_access(store, source):
    importer = make_editor(store, authorize_component=lambda entry: entry.app_id != "Test/bars")
    importer.new_dashboard("Ops dashboard")
    add_reference(importer, source.dashboard.dashboard_id, "chart")
    assert importer._component_instances["ref"].status == "denied"
    assert importer._node_spec("ref").outputs == []
    importer.dispose()


def test_a_forbidden_dashboard_with_a_guessable_id_looks_missing(store):
    source = make_editor(store)
    source.new_dashboard("Exec dashboard")
    source.add_component("Test/bars", instance_id="chart")
    model = source.to_model()
    model.dashboard_id = "exec"
    store.save_dashboard(model)
    importer = make_editor(
        store, authorize_dashboard=lambda model, access: model.dashboard_id != "exec"
    )
    importer.new_dashboard("Ops dashboard")
    add_reference(importer, "exec", "chart", "guessed")
    add_reference(importer, "nothing", "chart", "absent")
    assert importer._component_instances["guessed"].status == "missing"
    assert importer._component_instances["absent"].status == "missing"
    importer.dispose()
    source.dispose()


def test_nodes_of_one_component_share_their_state_class(source):
    source.add_component("Test/numbers", instance_id="more")
    graph = source._dataflow_graph
    assert type(graph.get_state("numbers")) is type(graph.get_state("more"))
    assert type(graph.get_state("more")) is graph._state_classes["Test/numbers"]
