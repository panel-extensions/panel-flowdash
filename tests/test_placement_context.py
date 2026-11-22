"""`placement_context` tells a component why it is being created."""

import asyncio
import threading

import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash, MemoryDashboardStore, placement_context, register
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
    from panel_flowdash import current_dashboard

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
