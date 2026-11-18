"""Resources a host hands to components through the editor's context."""

import weakref

import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc
from panel.viewable import Viewer

from panel_flowdash import FlowDash, dashboard_context, register
from panel_flowdash.agent import DashboardOps
from panel_flowdash.editor import _SESSION_EDITORS

SEEN = []


@register(page=False, component=True, title="Reader", provides=[{"key": "value", "type": "str"}])
def reader(config):
    context = dashboard_context()
    SEEN.append(context)
    config.value = context.get("demo.source", "none")
    return "reader"


@register(page=False, component=True, title="Number", provides=[{"key": "n", "type": "int"}])
def number(config):
    config.n = 21
    return "number"


class Lookup(Viewer):
    """Reads the context each time its output runs, not only when it is created."""

    n = param.Integer(default=0)

    @param.output(param.String)
    @param.depends("n")
    def found(self):
        return f"{dashboard_context().get('demo.source', 'none')}:{self.n}"

    def __panel__(self):
        return "lookup"


def _editor(**params):
    editor = FlowDash(
        {"T/reader": reader, "T/number": number, "T/lookup": Lookup},
        include_builtin_components=False,
        notifications=False,
        **params,
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


def test_an_output_rerun_outside_a_session_sees_the_context():
    editor = _editor(context={"demo.source": "db"})
    instance_id = editor.add_component("T/lookup")
    assert editor.graph.get_state(instance_id).found == "db:0"
    editor._component_instances[instance_id].n = 1
    assert editor.graph.get_state(instance_id).found == "db:1"


def test_outputs_see_their_own_editor_in_a_session_with_another():
    shown = _editor(context={"demo.source": "shown"})
    editor = _editor(context={"demo.source": "db"})
    doc = Document()
    with set_curdoc(doc):
        _SESSION_EDITORS[doc] = weakref.ref(shown)
        instance_id = editor.add_component("T/lookup")
        editor._component_instances[instance_id].n = 2
        assert editor.graph.get_state(instance_id).found == "db:2"
        report = DashboardOps(editor).preview(
            "flowdash: 1\nnodes:\n  n: {component: T/number}\n  l: {component: T/lookup}\n"
            "edges:\n  - {from: n.n, to: l.n}\n"
        )
    assert report["nodes"]["l"]["outputs"]["found"]["value"] == "db:21"


def test_editors_built_from_one_dict_do_not_share_its_containers():
    shared = {"demo.sources": {"s0": "db"}, "demo.names": ["a"], "demo.client": object()}
    first, second = _editor(context=shared), _editor(context=shared)
    first.context["demo.sources"]["s1"] = "other"
    first.context["demo.names"].append("b")
    assert second.context["demo.sources"] == shared["demo.sources"] == {"s0": "db"}
    assert second.context["demo.names"] == shared["demo.names"] == ["a"]
    assert first.context["demo.client"] is second.context["demo.client"]
    scratch = DashboardOps(first)._scratch_editor()
    try:
        scratch.context["demo.sources"]["s2"] = "preview"
        assert "s2" not in first.context["demo.sources"]
    finally:
        scratch.dispose()


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
