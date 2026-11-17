"""Subscriptions of async Viewer views and of awaitables sync components return end with them."""

import asyncio
import gc
import weakref

import panel as pn
import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import current_dashboard, register
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash

FIRED: list[str] = []
MARKERS: list[weakref.ref] = []


class _Marker:
    pass


def _subscribe(label):
    marker = _Marker()
    MARKERS.append(weakref.ref(marker))
    current_dashboard().param.watch(lambda _event, m=marker: FIRED.append(label), "refresh")


@register(page=False, component=True, title="Async viewer")
class AsyncViewer(pn.viewable.Viewer):
    async def __panel__(self):
        await asyncio.sleep(0)
        _subscribe("viewer")
        return "viewer"


@register(page=False, component=True, title="Async viewer with inputs")
class AsyncViewerWithInputs(pn.viewable.Viewer):
    count = param.Integer(default=0)

    @param.depends("count")
    async def __panel__(self):
        await asyncio.sleep(0)
        _subscribe(f"viewer {self.count}")
        return f"count {self.count}"


async def _later():
    await asyncio.sleep(0)
    _subscribe("awaitable")
    return "awaitable"


async def _stream():
    yield "loading"
    _subscribe("stream")
    yield "streamed"


@register(page=False, component=True, title="Returns an awaitable")
def returns_awaitable(config):
    return _later()


@register(page=False, component=True, title="Returns an async generator")
def returns_stream(config):
    return _stream()


COMPONENTS = {
    "T/viewer": AsyncViewer,
    "T/viewer-inputs": AsyncViewerWithInputs,
    "T/awaitable": returns_awaitable,
    "T/stream": returns_stream,
}


@pytest.fixture(autouse=True)
def _reset():
    FIRED.clear()
    MARKERS.clear()
    yield


@pytest.fixture
def editor():
    with set_curdoc(Document()):
        yield FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)


async def _settle():
    """Let the bodies Panel scheduled when the tiles' panes were created run to the end."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("component", "label"),
    [("T/viewer", "viewer"), ("T/awaitable", "awaitable"), ("T/stream", "stream")],
)
async def test_removing_the_component_ends_its_subscriptions_and_frees_them(
    editor, component, label
):
    node = editor.add_component(component)
    await _settle()
    editor.param.trigger("refresh")
    assert FIRED == [label]

    FIRED.clear()
    editor.remove_component(node)
    editor.param.trigger("refresh")

    assert FIRED == []
    assert node not in editor._component_wiring
    gc.collect()
    assert [ref() for ref in MARKERS] == [None]


@pytest.mark.parametrize("component", ["T/viewer", "T/awaitable", "T/stream"])
async def test_loading_another_dashboard_ends_the_subscriptions(editor, component):
    editor.add_component(component)
    await _settle()

    editor.load_model(DashboardModel(dashboard_id="next", user_id="alice", title="Next"))
    editor.param.trigger("refresh")

    assert FIRED == []
    gc.collect()
    assert [ref() for ref in MARKERS] == [None]


async def test_every_rerender_of_an_async_viewer_is_tracked(editor):
    node = editor.add_component("T/viewer-inputs")
    await _settle()
    editor._component_instances[node].count = 1
    await _settle()
    editor.param.trigger("refresh")
    assert sorted(FIRED) == ["viewer 0", "viewer 1"]

    FIRED.clear()
    editor.remove_component(node)
    editor.param.trigger("refresh")

    assert FIRED == []
