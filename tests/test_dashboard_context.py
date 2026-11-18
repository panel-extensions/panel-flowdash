"""Resources a host hands to components through the editor's context."""

import pytest

from panel_flowdash import FlowDash, dashboard_context, register
from panel_flowdash.agent import DashboardOps

SEEN = []


@register(page=False, component=True, title="Reader", provides=[{"key": "value", "type": "str"}])
def reader(config):
    context = dashboard_context()
    SEEN.append(context)
    config.value = context.get("demo.source", "none")
    return "reader"


def _editor(**params):
    editor = FlowDash(
        {"T/reader": reader}, include_builtin_components=False, notifications=False, **params
    )
    editor.new_dashboard("Context")
    return editor


@pytest.fixture(autouse=True)
def _clear():
    SEEN.clear()


def test_the_editor_keeps_its_own_copy_of_the_context():
    given = {"demo.source": "db"}
    editor = _editor(context=given)
    given["demo.source"] = "other"
    assert editor.context == {"demo.source": "db"}
    assert _editor().context == {} and _editor().context is not _editor().context


def test_components_read_the_context_but_cannot_change_it():
    editor = _editor(context={"demo.source": "db"})
    instance_id = editor.add_component("T/reader")
    assert editor.graph.get_state(instance_id).value == "db"
    (seen,) = SEEN
    with pytest.raises(TypeError):
        seen["demo.source"] = "x"
    editor.context["demo.later"] = 1
    assert seen["demo.later"] == 1


def test_without_an_editor_the_context_is_empty():
    assert dict(dashboard_context()) == {}


def test_previews_see_the_hosts_resources():
    editor = _editor(context={"demo.source": "db"})
    ops = DashboardOps(editor)
    report = ops.preview("flowdash: 1\nnodes:\n  r: {component: T/reader}\n")
    assert report["nodes"]["r"]["outputs"]["value"] == {"type": "str", "value": "db"}
    assert editor._tile_items == []


def test_a_preview_gets_a_copy_of_the_context():
    editor = _editor(context={"demo.source": "db"})
    ops = DashboardOps(editor)
    scratch = ops._scratch_editor()
    try:
        assert scratch.context == editor.context
        scratch.context["demo.source"] = "changed"
        assert editor.context["demo.source"] == "db"
    finally:
        scratch.dispose()
