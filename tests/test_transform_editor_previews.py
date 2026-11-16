"""A transform editor only re-runs its edge's transform while it is shown."""

import pandas as pd
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register, transform_editor as transform_editor_module
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.status_bar import select_on_canvas

FRAME = pd.DataFrame({"state": ["TX", "CA", "IA"], "t_cap": [1500, 3000, 2500]})


@register(
    page=False,
    component=True,
    title="Rows",
    provides=[{"key": "rows", "type": "DataFrame"}],
)
class Rows(Viewer):
    frame = param.DataFrame(default=FRAME)

    @param.output(param.DataFrame)
    @param.depends("frame")
    def rows(self):
        return self.frame

    def __panel__(self):
        return "rows"


@register(page=False, component=True, title="Table")
class Table(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return "table"


@pytest.fixture
def checks(monkeypatch):
    calls = []
    original = transform_editor_module.check_steps

    def counting(steps, value):
        calls.append(value)
        return original(steps, value)

    monkeypatch.setattr(transform_editor_module, "check_steps", counting)
    return calls


def _edges(count):
    editor = FlowDash({"T/rows": Rows, "T/table": Table}, notifications=False)
    src = editor.add_component("T/rows")
    for _ in range(count):
        dst = editor.add_component("T/table")
        assert editor.connect(src, "rows", dst, "rows", [{"op": "head", "n": 2}]) is True
    return editor, editor._component_instances[src], list(editor._edge_id_map)


def _texts(view):
    return [str(t.object) for t in view._body.select(pmui.Typography)]


async def test_hidden_transform_editors_do_not_rerun_their_transform(checks):
    editor, source, edge_ids = _edges(3)
    for edge_id in edge_ids:
        select_on_canvas(editor._flow, edge_id=edge_id)
    select_on_canvas(editor._flow)
    assert len(editor._transform_editors) == 3

    checks.clear()
    for n in range(10):
        source.frame = FRAME.head(n % 3 + 1)
    assert checks == []


async def test_only_the_shown_transform_editor_follows_the_source(checks):
    editor, source, (first, second) = _edges(2)
    select_on_canvas(editor._flow, edge_id=first)
    select_on_canvas(editor._flow, edge_id=second)

    checks.clear()
    source.frame = FRAME.head(1)
    assert len(checks) == 1
    assert "In: 1 row, 2 columns" in _texts(editor._transform_editors[second])


async def test_a_hidden_editor_catches_up_once_shown_again(checks):
    editor, source, (edge_id,) = _edges(1)
    select_on_canvas(editor._flow, edge_id=edge_id)
    view = editor._transform_editors[edge_id]
    select_on_canvas(editor._flow)
    for n in (1, 2, 1):
        source.frame = FRAME.head(n)

    checks.clear()
    select_on_canvas(editor._flow, edge_id=edge_id)
    assert len(checks) == 1
    assert "In: 1 row, 2 columns" in _texts(view)


async def test_hiding_the_inspector_hides_the_transform_editor(checks):
    editor, source, (edge_id,) = _edges(1)
    select_on_canvas(editor._flow, edge_id=edge_id)
    editor._inspector.visible = False

    checks.clear()
    source.frame = FRAME.head(1)
    assert checks == []
