"""A re-run of an async ``__panel__`` keeps only its own subscriptions, and clones stay tracked."""

import asyncio

import panel as pn
import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.util import _SteppedParamMethod
from tests.test_async_viewer_subscriptions import COMPONENTS, FIRED, MARKERS, _subscribe

GATE: dict[str, asyncio.Event] = {}


@register(page=False, component=True, title="Gated viewer")
class GatedViewer(pn.viewable.Viewer):
    count = param.Integer(default=0)

    @param.depends("count")
    async def __panel__(self):
        count = self.count
        if count == 1:
            await GATE["open"].wait()
        else:
            await asyncio.sleep(0)
        _subscribe(f"gated {count}")
        return f"count {count}"


@pytest.fixture(autouse=True)
def _reset():
    FIRED.clear()
    MARKERS.clear()
    yield


@pytest.fixture
def editor():
    with set_curdoc(Document()):
        yield FlowDash(
            {**COMPONENTS, "T/gated": GatedViewer},
            include_builtin_components=False,
            notifications=False,
        )


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_a_rerun_releases_the_previous_runs_subscriptions(editor):
    node = editor.add_component("T/viewer-inputs")
    await _settle()
    instance = editor._component_instances[node]
    for count in (1, 2, 3):
        instance.count = count
        await _settle()

    editor.param.trigger("refresh")
    assert FIRED == ["viewer 3"]
    assert len(editor._component_wiring[node]) == len(set(map(id, editor._component_wiring[node])))

    FIRED.clear()
    editor.remove_component(node)
    editor.param.trigger("refresh")
    assert FIRED == []


async def test_only_the_latest_of_overlapping_runs_keeps_its_subscriptions(editor):
    GATE["open"] = asyncio.Event()
    node = editor.add_component("T/gated")
    await _settle()
    instance = editor._component_instances[node]
    instance.count = 1
    await _settle()
    instance.count = 2
    await _settle()
    # The first run subscribes only now, after the second one replaced it.
    GATE["open"].set()
    await _settle()

    editor.param.trigger("refresh")
    assert FIRED == ["gated 2"]


class _Counter(param.Parameterized):
    count = param.Integer(default=0)

    @param.depends("count")
    async def view(self):
        await asyncio.sleep(0)
        return f"count {self.count}"


async def test_a_clone_runs_inside_the_same_steps():
    entered = []

    class _Step:
        def __enter__(self):
            entered.append(True)

        def __exit__(self, *exc):
            return False

    pane = _SteppedParamMethod(_Counter().view, run=lambda: _Step)
    clone = pane.clone()
    await clone.eval(clone.object)
    assert entered
