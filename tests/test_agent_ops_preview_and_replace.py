"""DashboardOps previews and bulk replaces: generated ids, concurrent edits, bounds and restricted bodies."""

import asyncio
import json

import param
import pytest
import yaml
from panel.viewable import Viewer

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.agent import DashboardOps, OperationError
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem

from .test_agent_ops import COMPONENTS, _editor

CALLS = []


@register(
    page=False, component=True, title="Counted secret", provides=[{"key": "value", "type": "int"}]
)
def counted_secret(config):
    CALLS.append("secret")
    config.value = 1
    return "secret"


@register(page=False, component=True, title="Wide", provides=[{"key": "df", "type": "DataFrame"}])
def wide(config):
    import pandas as pd

    config.df = pd.DataFrame({("c" * 5000) + str(i): ["x" * 1000] * 30 for i in range(60)})
    return "wide"


@register(page=False, component=True, title="Boom", provides=[{"key": "v", "type": "int"}])
def boom(config):
    raise RuntimeError("failed " + "y" * 50_000)


EXTRA = {**COMPONENTS, "T/counted": counted_secret, "T/wide": wide, "T/boom": boom}


def _extra_editor(store=None, **params):
    return FlowDash(
        components=EXTRA,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        authorize_component=lambda e: e.app_id not in ("T/secret", "T/counted"),
        **params,
    )


@pytest.fixture
def generated():
    """A dashboard with generated ids: a restricted source feeding a doubler."""
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Fin")
    model.items = [
        DashboardItem("T__counted_a1b2c3", "T/counted"),
        DashboardItem("T__doubler_d4e5f6", "T/doubler"),
    ]
    model.edges = [DashboardEdge("T__counted_a1b2c3", "value", "T__doubler_d4e5f6", "value")]
    store.save_dashboard(model)
    editor = _extra_editor(store)
    ops = DashboardOps(editor)
    ops.open_dashboard(model.dashboard_id)
    return ops


def test_previewing_the_canvas_yaml_matches_previewing_the_canvas(generated):
    CALLS.clear()
    canvas = generated.preview()
    text = generated.preview(generated.get_dashboard())
    assert canvas == text
    assert canvas["nodes"]["counted_a1b2c3"]["status"] == "restricted"
    assert CALLS == []


def test_dropping_the_restricted_connection_is_refused(generated):
    doc = yaml.safe_load(generated.get_dashboard())
    doc["nodes"]["n"] = {"component": "T/number"}
    doc["edges"] = [{"from": "n.value", "to": "doubler_d4e5f6.value"}]
    with pytest.raises(OperationError) as info:
        generated.replace_yaml(yaml.safe_dump(doc), revision=generated.revision())
    message = str(info.value)
    assert "keep {from: counted_a1b2c3, to: doubler_d4e5f6.value}" in message
    assert "already fed by a restricted node" in message
    assert len(generated.editor._tile_items) == 2


def test_hardened_reading_covers_replace_and_preview(generated):
    bomb = "flowdash: 1\nnodes: &a [*a]\n"
    with pytest.raises(OperationError, match="anchors and aliases"):
        generated.replace_yaml(bomb, revision=generated.revision())
    with pytest.raises(OperationError, match="anchors and aliases"):
        generated.preview(bomb)


async def test_async_replace_refuses_edits_made_while_importing(monkeypatch):
    editor = _editor()
    editor.new_dashboard("Mine")
    ops = DashboardOps(editor)
    ops.add_node("T/number")
    text = ops.get_dashboard()
    original = editor.ensure_components_loaded_async

    async def slow(ids):
        await asyncio.sleep(0.1)
        await original(ids)

    monkeypatch.setattr(editor, "ensure_components_loaded_async", slow)

    async def user_edit():
        await asyncio.sleep(0.02)
        editor.add_component("T/doubler")

    task = asyncio.ensure_future(user_edit())
    with pytest.raises(OperationError, match="changed since this YAML was read"):
        await ops.replace_yaml_async(text)
    await task
    assert [i["component_id"] for i in editor._tile_items] == ["T/number", "T/doubler"]


async def test_async_replace_times_out(monkeypatch):
    editor = _editor()
    editor.new_dashboard("Mine")
    ops = DashboardOps(editor)

    async def stuck(ids):
        await asyncio.sleep(1)

    monkeypatch.setattr(editor, "ensure_components_loaded_async", stuck)
    with pytest.raises(OperationError, match="longer than"):
        await ops.replace_yaml_async(ops.get_dashboard(), timeout=0.05)


def test_the_report_and_its_messages_stay_small():
    editor = _extra_editor()
    editor.new_dashboard("x")
    report = DashboardOps(editor).preview(
        "flowdash: 1\nnodes:\n  w: {component: T/wide}\n  b: {component: T/boom}\n"
    )
    assert len(json.dumps(report, default=str)) < 25_000
    assert all(len(n.get("message", "")) <= 201 for n in report["nodes"].values())


def test_preview_keeps_the_hosts_unknown_component_rule():
    editor = _editor(import_unknown_components=False)
    editor.new_dashboard("x")
    assert DashboardOps(editor)._scratch_editor().import_unknown_components is False


def test_a_new_draft_previews_on_its_own(generated):
    report = generated.preview("flowdash: 1\nnodes:\n  n: {component: T/number}\n")
    assert report["status"] == "ok" and list(report["nodes"]) == ["n"]


@pytest.mark.parametrize("secret_id", ["secret", "T__counted_a1b2c3"])
def test_connect_refuses_an_input_a_restricted_node_feeds(secret_id):
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Fin")
    model.items = [DashboardItem(secret_id, "T/counted"), DashboardItem("double", "T/doubler")]
    model.edges = [DashboardEdge(secret_id, "value", "double", "value")]
    store.save_dashboard(model)
    ops = DashboardOps(_extra_editor(store))
    ops.open_dashboard(model.dashboard_id)
    number = ops.add_node("T/number")
    with pytest.raises(OperationError, match="already fed by a component you do not have"):
        ops.connect(f"{number}.value", "double.value")
    assert ops.editor.graph.edges == []
    assert not [i for i in ops.validate() if i.severity == "error"]


class Reciprocal(Viewer):
    """A Viewer whose output fails once its input is zero."""

    value = param.Number(default=1)

    @param.output(param.Number)
    @param.depends("value")
    def inverse(self):
        return 1 / self.value

    def __panel__(self):
        return str(self.value)


@register(page=False, component=True, title="Zero", provides=[{"key": "value", "type": "float"}])
def zero(config):
    config.value = 0
    return "zero"


FAILING = (
    "flowdash: 1\nnodes:\n  z: {component: T/zero}\n  r: {component: T/reciprocal}\n"
    "  s: {component: T/reciprocal}\n  ok: {component: T/reciprocal}\nedges:\n"
    "  - {from: z.value, to: r.value}\n  - {from: r.inverse, to: s.value}\n"
)


@pytest.fixture
def failing():
    editor = FlowDash(
        {"T/zero": zero, "T/reciprocal": Reciprocal},
        include_builtin_components=False,
        notifications=False,
        propagate_failures=True,
    )
    editor.new_dashboard("Failing")
    yield DashboardOps(editor)
    editor.dispose()


@pytest.mark.failures
def test_preview_marks_a_node_whose_output_failed(failing):
    report = failing.preview(FAILING)
    nodes = report["nodes"]
    assert report["status"] == "failed"
    assert nodes["r"]["status"] == "failed"
    assert nodes["r"]["message"] == "output 'inverse' failed: ZeroDivisionError: division by zero"
    assert nodes["r"]["outputs"]["inverse"]["type"] == "failure"
    assert nodes["s"]["status"] == "failed"
    assert nodes["s"]["message"].startswith("input 'value' failed upstream")
    assert nodes["z"]["status"] == nodes["ok"]["status"] == "ok"


@pytest.mark.failures
def test_preview_without_samples_still_marks_a_failed_output(failing):
    from panel_flowdash.agent.preview import preview_editor

    failing.replace_yaml(FAILING, revision=failing.revision())
    report = preview_editor(failing.editor, failing._names(), samples=False)
    assert report["nodes"]["r"]["status"] == "failed"
    assert "outputs" not in report["nodes"]["r"]


@pytest.mark.failures
def test_replacing_warns_about_each_failed_node(failing):
    issues = failing.replace_yaml(FAILING, revision=failing.revision())
    failed = {i.path: i for i in issues if i.path in ("nodes.r", "nodes.s")}
    assert set(failed) == {"nodes.r", "nodes.s"}
    assert {i.severity for i in failed.values()} == {"warning"}
    assert str(failed["nodes.r"]) == (
        "nodes.r: The node failed: output 'inverse' failed: ZeroDivisionError: division by zero"
    )
    assert not [i for i in issues if i.path in ("nodes.z", "nodes.ok")]


@pytest.mark.failures
async def test_replacing_async_warns_about_failed_nodes(failing):
    issues = await failing.replace_yaml_async(FAILING, revision=failing.revision())
    assert {i.path for i in issues if i.message.startswith("The node failed")} == {
        "nodes.r",
        "nodes.s",
    }


@pytest.mark.failures
def test_replacing_a_healthy_dashboard_adds_no_failure_warnings(failing):
    text = "flowdash: 1\nnodes:\n  ok: {component: T/reciprocal}\n"
    assert failing.replace_yaml(text, revision=failing.revision()) == []
