"""Tests for the inspector's Data tab: port headings, table previews and components' own previews."""

import json

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.status_bar import select_on_canvas
from tests.test_ui_inspector import ROWS, TABLE, TABLE_COMPONENTS, _texts

FRAME = pd.DataFrame({"case_id": range(40), "state": ["AK", "CA"] * 20})


class Pipeline:
    """Stands in for a lazy pipeline: a table without ``columns`` or ``head``."""


@register(
    page=False,
    component=True,
    title="Lazy Table",
    provides=[
        {"key": "pipeline", "type": "demo.pipeline"},
        {"key": "note", "type": "str"},
    ],
)
class LazyTable(Viewer):
    """Outputs an opaque pipeline and previews it as rows through the preview hook."""

    calls = param.List(default=[])
    fetched = param.Integer(default=0, precedence=-1)

    @param.output(param.Parameter)
    def pipeline(self):
        return Pipeline()

    @param.output(param.String)
    def note(self):
        return "plain text"

    def __flowdash_preview__(self, output=None):
        self.calls.append(output)
        if output == "pipeline":
            return FRAME if self.fetched else "Fetching rows..."
        return None

    def __panel__(self):
        return "lazy"


@register(
    page=False, component=True, title="Opaque", provides=[{"key": "thing", "type": "demo.thing"}]
)
class Opaque(Viewer):
    """Outputs a live object and gives no preview of it."""

    @param.output(param.Parameter)
    def thing(self):
        return pn.pane.Markdown("live")

    def __panel__(self):
        return "opaque"


@register(
    page=False, component=True, title="Broken", provides=[{"key": "value", "type": "number"}]
)
class Broken(Viewer):
    @param.output(param.Parameter)
    def value(self):
        return param.ReactiveError(ValueError("no column 'cap'\nmore detail"))

    def __panel__(self):
        return "broken"


@register(
    page=False, component=True, title="Empty", provides=[{"key": "rows", "type": "DataFrame"}]
)
class Empty(Viewer):
    @param.output(param.DataFrame)
    def rows(self):
        return FRAME.head(0)

    def __panel__(self):
        return "empty"


LAZY, OPAQUE, BROKEN, EMPTY = "Demo/lazy", "Demo/opaque", "Demo/broken", "Demo/empty"
COMPONENTS = {
    **TABLE_COMPONENTS,
    LAZY: LazyTable,
    OPAQUE: Opaque,
    BROKEN: Broken,
    EMPTY: Empty,
}


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)


def _data(editor, component):
    node = editor.add_component(component)
    select_on_canvas(editor._flow, node_id=node)
    return node, editor._inspector._data_column


def _tables(column):
    return list(column.select(pn.pane.DataFrame))


async def test_heading_is_the_port_label_without_prefix_or_type_id(editor):
    _, column = _data(editor, ROWS)
    texts = _texts(column)
    assert "Rows" in texts
    assert not any(t.startswith("Output") or "DataFrame" in t for t in texts)
    (tooltip,) = column.select(pmui.Tooltip)
    assert tooltip.title == "Type: DataFrame"


async def test_table_shows_size_and_first_rows_without_index(editor):
    editor.add_component(ROWS)
    node, column = _data(editor, LAZY)
    instance = editor._component_instances[node]
    instance.fetched += 1
    column = editor._inspector._data_column
    assert "40 rows \u00d7 2 columns, first 15 shown" in _texts(column)
    (table,) = _tables(column)
    assert table.index is False
    assert len(table.object) == 15
    assert list(table.object.columns) == ["case_id", "state"]


async def test_small_table_shows_every_row(editor):
    _, column = _data(editor, ROWS)
    assert "3 rows \u00d7 2 columns" in _texts(column)
    (table,) = _tables(column)
    assert len(table.object) == 3


async def test_preview_hook_is_asked_per_output(editor):
    node, column = _data(editor, LAZY)
    instance = editor._component_instances[node]
    assert "pipeline" in instance.calls and "note" in instance.calls
    texts = _texts(column)
    assert "Pipeline" in texts and "Note" in texts
    assert "Fetching rows..." in texts
    assert "plain text" in texts
    assert not any("demo.pipeline" in t for t in texts)


async def test_preview_follows_the_component_without_new_outputs(editor):
    node, column = _data(editor, LAZY)
    assert not _tables(column)
    editor._component_instances[node].fetched += 1
    column = editor._inspector._data_column
    assert "Fetching rows..." not in _texts(column)
    assert len(_tables(column)) == 1


async def test_live_object_without_preview_gets_a_caption_not_its_view(editor):
    _, column = _data(editor, OPAQUE)
    texts = _texts(column)
    assert any(t.endswith("This component gives no preview of it.") for t in texts)
    assert "live" not in texts


async def test_failure_is_a_short_caption(editor):
    _, column = _data(editor, BROKEN)
    texts = _texts(column)
    assert any(t.startswith("ValueError") and "no column 'cap'" in t for t in texts)
    assert not any("more detail" in t for t in texts)


async def test_empty_table_says_so(editor):
    _, column = _data(editor, EMPTY)
    texts = _texts(column)
    assert "0 rows \u00d7 2 columns" in texts
    assert "No rows." in texts


async def test_view_without_input_says_no_value_yet(editor):
    _, column = _data(editor, TABLE)
    assert "No value yet. It appears once the component has run." in _texts(column)


async def test_value_preview_falls_back_to_text_for_values_json_cannot_hold(editor):
    preview = editor._value_preview(Pipeline())
    assert not isinstance(preview, pn.pane.JSON)
    with pytest.raises(TypeError):
        json.dumps(Pipeline())
    assert isinstance(editor._value_preview({"a": 1}), pn.pane.JSON)


async def test_index_carrying_data_is_shown_as_columns(editor):
    from panel_flowdash.ui.inspector import _table_preview

    sums = FRAME.groupby("state").sum()
    (table,) = [o for o in _table_preview(sums) if isinstance(o, pn.pane.DataFrame)]
    assert list(table.object.columns) == ["state", "case_id"]
    assert list(table.object["state"]) == ["AK", "CA"]

    pivot = FRAME.assign(kind=["a", "b", "c", "d"] * 10).groupby(["state", "kind"]).size()
    (table,) = [o for o in _table_preview(pivot.to_frame("n")) if isinstance(o, pn.pane.DataFrame)]
    assert list(table.object.columns) == ["state", "kind", "n"]

    dated = pd.DataFrame({"v": [1, 2]}, index=pd.date_range("2026-01-01", periods=2))
    (table,) = [o for o in _table_preview(dated) if isinstance(o, pn.pane.DataFrame)]
    assert list(table.object.columns) == ["index", "v"]

    (table,) = [o for o in _table_preview(FRAME) if isinstance(o, pn.pane.DataFrame)]
    assert list(table.object.columns) == ["case_id", "state"]
    assert table.index is False
