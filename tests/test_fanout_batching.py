"""A control feeding several tiles sends their updates to the browser together."""

import threading

import panel as pn
import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc, state

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardEdge, DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash


@register(
    page=False, component=True, title="Viewer control", provides=[{"key": "value", "type": "int"}]
)
class ViewerControl(pn.viewable.Viewer):
    value = param.Integer(default=0)

    def __panel__(self):
        return pn.pane.Markdown("control")


@register(
    page=False, component=True, title="Widget control", provides=[{"key": "value", "type": "int"}]
)
def widget_control(config):
    widget = pn.widgets.IntInput(value=0)
    widget.link(config, value="value")
    config.value = widget.value
    return widget


@register(
    page=False, component=True, title="Viewer tile", requires=[{"key": "value", "type": "int"}]
)
class ViewerTile(pn.viewable.Viewer):
    value = param.Integer(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(lambda v: f"viewer {v}", self.param.value))


@register(
    page=False, component=True, title="Function tile", requires=[{"key": "value", "type": "int"}]
)
def function_tile(config):
    return pn.pane.Markdown(pn.bind(lambda v: f"function {v}", config.param.value))


COMPONENTS = {
    "t/viewer-control": ViewerControl,
    "t/widget-control": widget_control,
    "t/viewer": ViewerTile,
    "t/function": function_tile,
}

CONSUMERS = ["viewer", "viewer", "viewer", "function", "function"]


def _dashboard(control):
    items = [DashboardItem("control", control)]
    items += [DashboardItem(f"tile{i}", f"t/{kind}") for i, kind in enumerate(CONSUMERS)]
    edges = [DashboardEdge("control", "value", f"tile{i}", "value") for i in range(len(CONSUMERS))]
    return DashboardModel(dashboard_id="d", user_id="u", title="Fan-out", items=items, edges=edges)


@pytest.fixture
def connected_doc(monkeypatch):
    doc = Document()
    # As in a served session whose client has loaded the page; Panel otherwise
    # drops the changes a hold collected, since the document is sent in full later.
    monkeypatch.setitem(state._thread_id_, doc, threading.get_ident())
    monkeypatch.setitem(state._connected, doc, True)
    return doc


def _render(doc, control):
    with set_curdoc(doc):
        editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
        editor.load_model(_dashboard(control))
        for view in editor._tile_objects:
            doc.add_root(view.get_root(doc))
    events = []
    doc.on_change(events.append)
    received = {}
    for i in range(len(CONSUMERS)):
        editor.graph.get_state(f"tile{i}").param.watch(
            lambda _, n=f"tile{i}": received.setdefault(n, len(events)), ["value"]
        )
    return editor, events, received


def _texts(events):
    return [str(getattr(e, "new", "")) for e in events if getattr(e, "attr", None) == "text"]


@pytest.mark.parametrize("control", ["t/viewer-control", "t/widget-control"])
def test_every_consumer_updates_before_the_document_sends_anything(connected_doc, control):
    editor, events, received = _render(connected_doc, control)

    with set_curdoc(connected_doc):
        if control == "t/viewer-control":
            editor._component_instances["control"].value = 5
        else:
            next(iter(editor._tile_objects[0].select(pn.widgets.IntInput))).value = 5

    # Only the widget's own change, if any, goes out before the consumers update.
    assert sorted(received) == [f"tile{i}" for i in range(len(CONSUMERS))]
    assert set(received.values()) == {0 if control == "t/viewer-control" else 1}
    texts = " ".join(_texts(events))
    assert texts.count("viewer 5") == 3
    assert texts.count("function 5") == 2
    editor.dispose()
