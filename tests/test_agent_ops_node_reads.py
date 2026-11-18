"""Reading the nodes of a dashboard, their config and their live outputs through the operations API."""

import pandas as pd
import param
import pytest

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import DashboardOps, OperationError, OutputUnavailable

from .test_agent_ops import (
    COMPONENTS,
    _editor,
    store,  # noqa: F401
)
from .test_editor import FAILING_COMPONENTS


class Holder(param.Parameterized):
    rows = param.Integer(default=2)


@register(
    page=False,
    component=True,
    title="Frame",
    config_schema=Holder,
    provides=[{"key": "df", "type": "DataFrame"}, {"key": "nothing", "type": "str"}],
)
def frame(config, instance_config):
    config.df = pd.DataFrame({"a": list(range(instance_config.rows))})
    config.nothing = None
    return "frame"


@register(
    page=False,
    component=True,
    title="Fetch",
    aliases=["Old/fetch"],
    provides=[{"key": "out", "type": "str"}],
)
def fetch(config):
    return "fetch"


@register(
    page=False,
    component=True,
    title="Needs",
    provides=[{"key": "out", "type": "int"}],
    requires=[{"key": "x", "type": "int"}],
)
def needs(config):
    return "needs"


@pytest.fixture
def ops():
    editor = FlowDash(
        components={**COMPONENTS, "T/frame": frame, "T/needs": needs},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Mine")
    ops = DashboardOps(editor)
    ops.add_node("T/number", name="base")
    ops.add_node("T/doubler", name="twice")
    ops.connect("base.value", "twice.value")
    ops.add_node("T/frame", {"rows": 3}, name="frame")
    ops.add_node("T/needs", name="waits")
    yield ops
    editor.dispose()


def _opened(store, **params):  # noqa: F811
    editor = _editor(store, **params)
    (model,) = store._all_dashboards()
    ops = DashboardOps(editor)
    ops.open_dashboard(model.dashboard_id)
    return ops


def test_nodes_in_placement_order(ops):
    assert ops.nodes() == [
        {"name": "base", "component": "T/number"},
        {"name": "twice", "component": "T/doubler"},
        {"name": "frame", "component": "T/frame"},
        {"name": "waits", "component": "T/needs"},
    ]
    assert ops.nodes("T/doubler") == [{"name": "twice", "component": "T/doubler"}]


def test_nodes_match_former_component_ids():
    editor = FlowDash(
        components={**COMPONENTS, "T/fetch": fetch},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Fetch")
    ops = DashboardOps(editor)
    ops.add_node("Old/fetch", name="fetched")
    assert [n["name"] for n in ops.nodes("Old/fetch")] == ["fetched"]
    assert [n["name"] for n in ops.nodes("T/fetch")] == ["fetched"]
    editor.dispose()


def test_node_config_is_a_copy(ops):
    config = ops.node_config("frame")
    assert config == {"rows": 3}
    config["rows"] = 9
    assert ops.node_config("frame") == {"rows": 3}
    with pytest.raises(OperationError, match="No node 'nope'"):
        ops.node_config("nope")


def test_node_output_values(ops):
    assert ops.node_output("base", "value") == 21
    assert ops.node_output("twice.doubled") == 42
    frame = ops.node_output("frame.df")
    assert isinstance(frame, pd.DataFrame) and len(frame) == 3
    assert ops.node_output("frame", "df") is frame
    assert ops.node_output("frame.nothing") is None
    with pytest.raises(OperationError, match="has no output 'value'"):
        ops.node_output("twice.value")
    with pytest.raises(OperationError, match="not both"):
        ops.node_output("frame.df", "df")
    with pytest.raises(OperationError, match=r"Name the output as 'frame.<port>'"):
        ops.node_output("frame")


def test_waiting_node_has_no_output(ops):
    with pytest.raises(OutputUnavailable, match="'waits' is waiting") as info:
        ops.node_output("waits.out")
    assert info.value.status == "waiting"
    assert not isinstance(info.value, OperationError)


@pytest.mark.failures
async def test_failed_output_and_failed_upstream_are_reported():
    editor = FlowDash(
        FAILING_COMPONENTS,
        notifications=False,
        propagate_failures=True,
        include_builtin_components=False,
    )
    ops = DashboardOps(editor)
    ops.add_node("Demo/inverse", name="src")
    ops.add_node("Demo/inverse", name="dst")
    ops.connect("src.inverse", "dst.value")
    source_state = editor.graph.get_state(editor.export_dashboard()["items"][0]["instance_id"])
    source_state.value = 4
    assert ops.node_output("dst.inverse") == 4
    source_state.value = 0
    with pytest.raises(OutputUnavailable) as info:
        ops.node_output("src.inverse")
    assert info.value.status == "failed"
    with pytest.raises(
        OutputUnavailable, match="'dst' has failed: input 'value' failed upstream"
    ) as info:
        ops.node_output("dst.inverse")
    assert info.value.status == ops.preview()["nodes"]["dst"]["status"] == "failed"
    editor.dispose()


def test_restricted_node_is_masked(store):  # noqa: F811
    ops = _opened(store)
    assert ops.nodes() == [
        {"name": "secret", "component": "flowdash:restricted"},
        {"name": "double", "component": "T/doubler"},
    ]
    assert ops.nodes("T/secret") == []
    with pytest.raises(PermissionError, match=r"^'secret' is restricted\.$"):
        ops.node_config("secret")
    for ref in ("secret.value", "secret.nope"):
        with pytest.raises(PermissionError, match=r"^'secret' is restricted\.$") as info:
            ops.node_output(ref)
        assert "Secret source" not in str(info.value)
    with pytest.raises(OutputUnavailable, match="waiting"):
        ops.node_output("double.doubled")


@pytest.mark.parametrize("viewers_can_inspect", [False, True])
def test_viewer_reads_only_what_inspect_allows(store, viewers_can_inspect):  # noqa: F811
    (model,) = store._all_dashboards()
    ops = _opened(store, access={model.dashboard_id: "view"}, inspect_outputs=True)
    ops.editor.settings.viewers_can_inspect = viewers_can_inspect
    if viewers_can_inspect:
        assert [n["name"] for n in ops.nodes()] == ["secret", "double"]
        with pytest.raises(OutputUnavailable):
            ops.node_output("double.doubled")
    else:
        for read in (
            ops.nodes,
            lambda: ops.node_config("double"),
            lambda: ops.node_output("double.doubled"),
        ):
            with pytest.raises(PermissionError, match="definition"):
                read()
        with pytest.raises(PermissionError, match="definition"):
            ops.node_output("nope.x")


def test_definition_visible_without_sampling(store):  # noqa: F811
    (model,) = store._all_dashboards()
    ops = _opened(store, access={model.dashboard_id: "view"}, inspect_outputs=False)
    assert ops.node_config("double") == {}
    with pytest.raises(PermissionError, match="inspect"):
        ops.node_output("double.doubled")


def test_reads_refused_once_edit_access_is_revoked(store):  # noqa: F811
    (model,) = store._all_dashboards()
    access = {}
    ops = _opened(store, access=access, inspect_outputs=True)
    ops.add_node("T/number", name="n")
    access[model.dashboard_id] = "view"
    ops.editor.read_only = True
    for read in (ops.nodes, lambda: ops.node_output("n.value")):
        with pytest.raises(PermissionError):
            read()
