"""An input fed by a component the user may not use takes no other connection."""

import pytest

from panel_flowdash import FlowDash, MemoryDashboardStore, register
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem


@register(page=False, component=True, title="Secret", provides=[{"key": "value", "type": "str"}])
def secret(config):
    return "secret"


@register(page=False, component=True, title="Source", provides=[{"key": "value", "type": "str"}])
def source(config):
    return "source"


@register(
    page=False,
    component=True,
    title="Sink",
    requires=[{"key": "value", "type": "str"}, {"key": "many", "type": "list"}],
)
def sink(config):
    return "sink"


COMPONENTS = {"T/secret": secret, "T/source": source, "T/sink": sink}


@pytest.fixture(params=["readable", "generated"])
def editor(request):
    """Alice's editor on a dashboard where a secret source she may not use feeds a sink."""
    secret_id = "secret" if request.param == "readable" else "T__secret_a1b2c3"
    store = MemoryDashboardStore()
    model = store.create_dashboard("alice", "Plant")
    model.items = [
        DashboardItem(secret_id, "T/secret"),
        DashboardItem("src", "T/source"),
        DashboardItem("sink", "T/sink"),
    ]
    model.edges = [
        DashboardEdge(secret_id, "value", "sink", "value"),
        DashboardEdge(secret_id, "value", "sink", "many"),
    ]
    store.save_dashboard(model)
    editor = FlowDash(
        components=COMPONENTS,
        include_builtin_components=False,
        notifications=False,
        store=store,
        user="alice",
        authorize_component=lambda entry: entry.app_id != "T/secret",
    )
    editor.load(model.dashboard_id)
    yield editor
    editor.dispose()


def test_connect_refuses_an_input_a_restricted_component_feeds(editor):
    result = editor.connect("src", "value", "sink", "value")
    assert result == (
        "Input 'value' is already fed by a component you do not have access to, so it "
        "takes no other connection."
    )
    assert editor.graph.edges == []
    assert sorted(e.target_port for e in editor.to_model().edges) == ["many", "value"]


def test_a_list_input_still_takes_more_edges(editor):
    assert editor.connect("src", "value", "sink", "many") is True


def test_the_canvas_refuses_the_edge_before_it_is_drawn(editor):
    payload = {"source": "src", "sourceHandle": "value", "target": "sink", "targetHandle": "value"}
    assert "already fed by a component you do not have access to" in editor._validate_connection(
        payload["source"], payload["sourceHandle"], payload["target"], payload["targetHandle"]
    )
