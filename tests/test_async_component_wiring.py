"""Subscriptions made by async function components are released with the component."""

import asyncio
import gc
import weakref

import panel as pn
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import current_dashboard, register
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash

FIRED: list[str] = []
SEEN: list[object] = []
MARKERS: list[weakref.ref] = []
GATE: dict[str, asyncio.Event] = {}


class _Marker:
    pass


@register(page=False, component=True, title="Live")
async def live(config):
    await asyncio.sleep(0)
    editor = current_dashboard()
    SEEN.append(editor)
    marker = _Marker()
    MARKERS.append(weakref.ref(marker))
    editor.param.watch(lambda _event, m=marker: FIRED.append(config.name), "refresh")
    return "live"


@register(page=False, component=True, title="Streaming")
async def streaming(config):
    yield "loading"
    current_dashboard().param.watch(lambda _event: FIRED.append(config.name), "refresh")
    yield "done"


@register(page=False, component=True, title="Waiting")
async def waiting(config):
    await GATE["go"].wait()
    current_dashboard().param.watch(lambda _event: FIRED.append(config.name), "refresh")
    return "waited"


COMPONENTS = {"T/live": live, "T/streaming": streaming, "T/waiting": waiting}


@pytest.fixture(autouse=True)
def _reset():
    FIRED.clear()
    SEEN.clear()
    MARKERS.clear()
    GATE["go"] = asyncio.Event()
    yield


@pytest.fixture
def editor():
    # In a session, where `current_dashboard` would find the editor even
    # without the body being tracked, so only the release is under test.
    with set_curdoc(Document()):
        yield FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)


def _body(editor, instance_id):
    index = next(
        i for i, item in enumerate(editor._tile_items) if item["instance_id"] == instance_id
    )
    (function,) = editor._tile_objects[index].select(pn.param.ParamFunction)
    return function.object


async def _render(editor, instance_id):
    body = _body(editor, instance_id)()
    if hasattr(body, "__anext__"):
        return [item async for item in body]
    return await body


async def test_async_body_sees_its_editor_across_awaits(editor):
    node = editor.add_component("T/live")
    assert await _render(editor, node) == "live"
    assert SEEN == [editor]
    editor.param.trigger("refresh")
    assert FIRED == [node]


async def test_removing_an_async_component_ends_its_subscriptions_and_frees_them(editor):
    node = editor.add_component("T/live")
    await _render(editor, node)
    editor.remove_component(node)
    editor.param.trigger("refresh")
    assert FIRED == []
    assert node not in editor._component_wiring
    gc.collect()
    assert [ref() for ref in MARKERS] == [None]


async def test_loading_another_dashboard_ends_async_subscriptions(editor):
    node = editor.add_component("T/live")
    await _render(editor, node)
    editor.load_model(DashboardModel(dashboard_id="next", user_id="alice", title="Next"))
    editor.param.trigger("refresh")
    assert FIRED == []


async def test_async_generator_subscriptions_end_with_the_component(editor):
    node = editor.add_component("T/streaming")
    assert await _render(editor, node) == ["loading", "done"]
    editor.param.trigger("refresh")
    assert FIRED == [node]
    editor.remove_component(node)
    editor.param.trigger("refresh")
    assert FIRED == [node]


async def test_a_body_finishing_after_removal_does_not_stay_subscribed(editor):
    node = editor.add_component("T/waiting")
    task = asyncio.ensure_future(_render(editor, node))
    await asyncio.sleep(0)
    editor.remove_component(node)
    GATE["go"].set()
    assert await task == "waited"
    editor.param.trigger("refresh")
    assert FIRED == []
    assert node not in editor._component_wiring


async def test_subscriptions_made_elsewhere_while_a_body_waits_are_kept(editor):
    node = editor.add_component("T/waiting")
    task = asyncio.ensure_future(_render(editor, node))
    await asyncio.sleep(0)
    editor.param.watch(lambda _event: FIRED.append("app"), "refresh")
    GATE["go"].set()
    await task
    editor.remove_component(node)
    editor.param.trigger("refresh")
    assert FIRED == ["app"]


async def test_async_body_finds_its_editor_outside_a_session():
    editor = FlowDash(COMPONENTS, include_builtin_components=False, notifications=False)
    node = editor.add_component("T/live")
    await _render(editor, node)
    assert SEEN == [editor]
    assert current_dashboard() is None
