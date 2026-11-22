"""`placement_context` tells a component why it is being created."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
    FlowDash,
    MemoryDashboardStore,
    current_dashboard,
    current_node_id,
    placement_context,
    register,
)
from panel_flowdash.agent import DashboardOps

SEEN: list[tuple[str, str | None]] = []


@register(page=False, component=True, title="Plain", provides=[{"key": "n", "type": "int"}])
def plain(config):
    SEEN.append(("plain", placement_context()))
    config.n = 1
    return "plain"


class Tracked(Viewer):
    n = param.Integer(default=0)

    def __init__(self, **params):
        super().__init__(**params)
        SEEN.append(("tracked", placement_context()))

    def __panel__(self):
        return "tracked"


@register(page=False, component=True, title="Later")
async def later(config):
    await asyncio.sleep(0)
    SEEN.append(("later", placement_context()))
    return "later"


@register(page=False, component=True, title="Spawner")
def spawner(config):
    SEEN.append(("spawner", placement_context()))
    editor = current_dashboard()
    if "child" not in {item["instance_id"] for item in editor._tile_items}:
        editor.add_component("T/plain", instance_id="child")
    SEEN.append(("spawner-after", placement_context()))
    return "spawner"


@register(page=False, component=True, title="Threaded")
def threaded(config):
    seen = []
    thread = threading.Thread(target=lambda: seen.append(placement_context()))
    thread.start()
    thread.join()
    SEEN.append(("thread", seen[0]))
    return "threaded"


BARRIER = threading.Barrier(2)


@register(page=False, component=True, title="Waiting")
def waiting(config):
    # Both placements are inside their component at once, each on its own thread.
    BARRIER.wait(timeout=5)
    SEEN.append(("waiting", placement_context()))
    return "waiting"


COMPONENTS = {
    "T/waiting": waiting,
    "T/plain": plain,
    "T/tracked": Tracked,
    "T/later": later,
    "T/spawner": spawner,
    "T/threaded": threaded,
}


@pytest.fixture
def editor():
    editor = FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=MemoryDashboardStore(),
        history_coalesce=0,
    )
    editor.new_dashboard("Placement")
    SEEN.clear()
    yield editor
    editor.dispose()


def _reasons(name="plain"):
    return [reason for seen, reason in SEEN if seen == name]


def test_outside_creation_there_is_no_reason():
    assert placement_context() is None


def test_adding_a_component_is_new(editor):
    editor.add_component("T/plain")
    editor.add_component("T/tracked")
    assert SEEN == [("plain", "new"), ("tracked", "new")]
    assert placement_context() is None


def test_adding_from_the_palette_is_new(editor):
    editor._add_from_ui("T/plain", position={"x": 10, "y": 10})
    assert _reasons() == ["new"]


def test_the_agents_add_node_is_new(editor):
    DashboardOps(editor).add_node("T/plain", name="added")
    assert _reasons() == ["new"]


def test_loading_a_dashboard_is_load(editor):
    model = editor.store.create_dashboard(None, "Other")
    editor.add_component("T/plain", instance_id="a")
    editor.add_component("T/tracked", instance_id="b")
    model.items = editor.to_model().items
    editor.store.save_dashboard(model)
    SEEN.clear()
    editor.load_model(model)
    editor.load(model.dashboard_id)
    assert SEEN == [("plain", "load"), ("tracked", "load")] * 2


def test_importing_and_replacing_are_load(editor):
    editor.import_dashboard({"items": [{"instance_id": "a", "component_id": "T/plain"}]})
    ops = DashboardOps(editor)
    ops.replace_yaml(
        "flowdash: 1\ntitle: Placement\nnodes:\n  b:\n    component: T/plain\n",
        revision=ops.revision(),
    )
    assert _reasons() == ["load", "load"]


def test_undo_redo_and_restoring_a_version_are_restore(editor):
    editor.add_component("T/plain", instance_id="a")
    editor.remove_component("a")
    SEEN.clear()
    assert editor.undo()
    assert editor.redo()
    assert editor.undo()
    assert _reasons() == ["restore", "restore"]
    history = editor.edit_history
    editor.remove_component("a")
    artifact = history.artifact
    version = next(v for v in artifact.versions if v.metadata.get("op") == "add_node")
    SEEN.clear()
    assert history.restore(artifact, version)
    assert _reasons() == ["restore"]


async def test_an_async_body_sees_the_reason_once_it_runs(editor):
    editor.import_dashboard({"items": [{"instance_id": "b", "component_id": "T/later"}]})
    assert _reasons("later") == []
    index = next(i for i, item in enumerate(editor._tile_items) if item["instance_id"] == "b")
    (function,) = editor._tile_objects[index].select(pn.param.ParamFunction)
    await function.object()
    assert _reasons("later") == ["load"]
    assert placement_context() is None


def test_a_node_a_component_adds_while_loading_is_new(editor):
    editor.import_dashboard({"items": [{"instance_id": "s", "component_id": "T/spawner"}]})
    assert SEEN == [("spawner", "load"), ("plain", "new"), ("spawner-after", "load")]


def test_a_thread_the_component_starts_has_no_reason(editor):
    editor.add_component("T/threaded")
    assert SEEN == [("thread", None)]


def test_placements_in_other_threads_keep_their_own_reason(editor):
    other = FlowDash(components=COMPONENTS, include_builtin_components=False, notifications=False)
    errors = []

    def place(target, load):
        try:
            if load:
                target.import_dashboard(
                    {"items": [{"instance_id": "w", "component_id": "T/waiting"}]}
                )
            else:
                target.add_component("T/waiting", instance_id="w")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=place, args=(editor, False)),
        threading.Thread(target=place, args=(other, True)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    other.dispose()
    assert not errors
    assert sorted(_reasons("waiting")) == ["load", "new"]


@register(page=False, component=True, title="Query", config=["sql"])
class Query(Viewer):
    # The example of docs/how-to/register-components.md.
    sql = param.String(default="")

    def __init__(self, **params):
        super().__init__(**params)
        if placement_context() == "new":
            current_dashboard().set_default(
                current_node_id(), {"sql": "SELECT * FROM sales LIMIT 100"}
            )

    def __panel__(self):
        return pn.pane.Markdown(self.param.sql)


@register(page=False, component=True, title="Charted", config=["spec"])
class Charted(Viewer):
    """Takes a default from its input once it arrives."""

    n = param.Integer(default=None, allow_None=True)
    spec = param.Dict(default={})

    def __init__(self, **params):
        super().__init__(**params)
        self._editor = current_dashboard()

    @param.depends("n", watch=True)
    def _default(self):
        editor = self._editor
        if editor is not None and self.n is not None:
            SEEN.append(
                ("default", editor.set_default(editor.node_id(self), {"spec": {"n": self.n}}))
            )

    def __panel__(self):
        return "charted"


DEFAULT_SQL = "SELECT * FROM sales LIMIT 100"


def _query_editor(store, **params):
    return FlowDash(
        components={"T/query": Query, "T/plain": plain, "T/charted": Charted},
        include_builtin_components=False,
        notifications=False,
        store=store,
        history_coalesce=0,
        **params,
    )


def _config(editor, instance_id):
    return next(i["config"] for i in editor._tile_items if i["instance_id"] == instance_id)


def test_a_default_set_while_placing_survives_save_and_reopen():
    store = MemoryDashboardStore()
    editor = _query_editor(store)
    editor.new_dashboard("Queries")
    fresh = editor.add_component("T/query")
    configured = editor.add_component("T/query", config={"sql": "SELECT 2"})
    assert editor._component_instances[fresh].sql == DEFAULT_SQL
    saved = editor.save()
    editor.dispose()
    assert {item.instance_id: item.config for item in saved.items} == {
        fresh: {"sql": DEFAULT_SQL},
        configured: {"sql": "SELECT 2"},
    }
    reopened = _query_editor(store)
    reopened.load(saved.dashboard_id)
    shown = {iid: reopened._component_instances[iid].sql for iid in (fresh, configured)}
    reopened.dispose()
    assert shown == {fresh: DEFAULT_SQL, configured: "SELECT 2"}


def test_one_undo_takes_back_the_node_and_its_default():
    editor = _query_editor(MemoryDashboardStore())
    editor.new_dashboard("Queries")
    editor.add_component("T/plain", instance_id="first")
    versions = len(editor.edit_history.versions)
    editor.add_component("T/query")
    assert len(editor.edit_history.versions) == versions + 1
    assert editor.undo()
    assert [item["instance_id"] for item in editor._tile_items] == ["first"]
    assert editor.redo()
    assert [item["config"] for item in editor._tile_items] == [{}, {"sql": DEFAULT_SQL}]
    editor.dispose()


def test_a_default_is_not_set_while_loading_or_for_viewers():
    editor = _query_editor(None)
    editor.import_dashboard({"items": [{"instance_id": "q", "component_id": "T/query"}]})
    assert _config(editor, "q") == {}
    editor.read_only = True
    assert not editor.set_default("q", {"sql": DEFAULT_SQL})
    editor.dispose()


def test_set_default_refuses_unknown_nodes_and_fields(editor):
    node = editor.add_component("T/plain")
    assert not editor.set_default("missing", {"sql": "x"})
    assert not editor.set_default(node, {"nope": 1})


def test_a_default_from_a_connection_is_part_of_it(monkeypatch):
    ticks = []
    monkeypatch.setattr("panel_flowdash.editor._next_tick", ticks.append)
    editor = _query_editor(MemoryDashboardStore())
    editor.new_dashboard("Charts")
    source = editor.add_component("T/plain")
    chart = editor.add_component("T/charted")
    editor.edit_history.flush()
    versions = len(editor.edit_history.versions)
    SEEN.clear()
    assert editor.connect(source, "n", chart, "n") is True
    editor.edit_history.flush()
    assert SEEN == [("default", True)]
    assert _config(editor, chart) == {"spec": {"n": 1}}
    assert len(editor.edit_history.versions) == versions + 1
    assert editor.edit_history.versions[-1].metadata["op"] == "connect"
    [record] = ticks
    record()
    editor.edit_history.flush()
    assert len(editor.edit_history.versions) == versions + 1
    assert editor.undo(whole_runs=False)
    assert _config(editor, chart).get("spec", {}) == {}
    editor.dispose()


def test_a_default_from_an_input_arriving_on_its_own_is_recorded_right_away(monkeypatch):
    ticks = []
    monkeypatch.setattr("panel_flowdash.editor._next_tick", ticks.append)
    editor = _query_editor(MemoryDashboardStore())
    editor.new_dashboard("Charts")
    chart = editor.add_component("T/charted")
    editor.edit_history.flush()
    versions = len(editor.edit_history.versions)
    editor._component_instances[chart].n = 5
    editor.edit_history.flush()
    assert _config(editor, chart) == {"spec": {"n": 5}} and editor.dirty
    assert len(editor.edit_history.versions) == versions + 1
    assert editor.edit_history.versions[-1].metadata["op"] == "set_config"
    assert not ticks
    editor.dispose()


def test_an_edit_before_the_next_tick_does_not_take_in_a_default(monkeypatch):
    ticks = []
    monkeypatch.setattr("panel_flowdash.editor._next_tick", ticks.append)
    editor = _query_editor(MemoryDashboardStore())
    editor.new_dashboard("Charts")
    chart = editor.add_component("T/charted")
    editor._component_instances[chart].n = 5
    editor.add_component("T/plain", instance_id="later")
    for tick in ticks:
        tick()
    assert editor.undo(whole_runs=False)
    assert [item["instance_id"] for item in editor._tile_items] == [chart]
    assert _config(editor, chart) == {"spec": {"n": 5}}
    editor.dispose()


@pytest.mark.parametrize("rewired", [False, True], ids=["on-its-own", "rewired"])
def test_saving_before_the_next_tick_leaves_the_dashboard_saved(monkeypatch, rewired):
    ticks = []
    monkeypatch.setattr("panel_flowdash.editor._next_tick", ticks.append)
    editor = _query_editor(MemoryDashboardStore())
    editor.new_dashboard("Charts")
    source = editor.add_component("T/plain")
    chart = editor.add_component("T/charted")
    editor.save()
    if rewired:
        # An input rewired without an edit to take the default in.
        assert editor._dataflow_graph.add_edge(source, "n", chart, "n") is True
    else:
        editor._component_instances[chart].n = 5
    assert ticks == ([editor._record_defaults] if rewired else [])
    saved = editor.save()
    for tick in ticks:
        tick()
    assert not editor.dirty
    assert {item.instance_id: item.config for item in saved.items}[chart] == {
        "spec": {"n": 1 if rewired else 5}
    }
    editor.dispose()


def test_code_panel_node_follows_the_code_panel(editor):
    assert editor.code_panel_node is None
    editor.code_panel.instance_id = "x"
    assert editor.code_panel_node == "x"
    editor.code_panel.instance_id = None
    assert editor.code_panel_node is None


@register(page=False, component=True, title="Offloading")
async def offloading(config):
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(1) as pool:
        in_executor = await loop.run_in_executor(pool, placement_context)
    SEEN.append(("executor", in_executor))
    SEEN.append(("to_thread", await asyncio.to_thread(placement_context)))
    return "offloading"


async def test_to_thread_copies_the_reason_and_an_executor_does_not(editor):
    other = FlowDash(
        components={"T/offloading": offloading},
        include_builtin_components=False,
        notifications=False,
    )
    other.add_component("T/offloading", instance_id="o")
    index = next(i for i, item in enumerate(other._tile_items) if item["instance_id"] == "o")
    (function,) = other._tile_objects[index].select(pn.param.ParamFunction)
    await function.object()
    other.dispose()
    # Panel may also run the body itself once the loop is free, so count only what is seen.
    assert set(_reasons("executor")) == {None} and set(_reasons("to_thread")) == {"new"}
