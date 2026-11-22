"""Tests for the inspector's Data tab: port headings, table previews and components' own previews."""

import contextlib
import datetime as dt
import json

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.inspector import _table_preview
from panel_flowdash.ui.node_preview import PREVIEW_KIND_KEY
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.ui.text import escape_markdown
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
    _preview_rev = param.Integer(default=0, precedence=-1)

    @param.output(param.Parameter)
    def pipeline(self):
        return Pipeline()

    @param.output(param.String)
    def note(self):
        return "plain text"

    def __flowdash_preview__(self, output=None):
        self.calls.append(output)
        if output == "pipeline":
            return FRAME if self._preview_rev else "Fetching rows..."
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


@register(
    page=False, component=True, title="Chart", provides=[{"key": "view", "type": "demo.view"}]
)
class Chart(Viewer):
    """Outputs a live view and previews it as a chart, as a Vega-Lite view node does."""

    @param.output(param.Parameter)
    def view(self):
        return object()

    def __flowdash_preview__(self, output=None):
        return {
            PREVIEW_KIND_KEY: "chart",
            "format": "vega-lite",
            "spec": {
                "mark": "bar",
                "encoding": {"x": {"field": "state"}, "y": {"field": "n"}},
            },
            "data": pd.DataFrame({"state": ["AK", "CA", "TX"], "n": [3, 9, 5]}),
        }

    def __panel__(self):
        return "chart"


@register(
    page=False, component=True, title="Summary", provides=[{"key": "summary", "type": "demo.s"}]
)
class Summary(Viewer):
    """Previews its output as the text in ``text``."""

    text = param.String(default="1 table: sales")

    @param.output(param.Parameter)
    def summary(self):
        return object()

    def __flowdash_preview__(self, output=None):
        return self.text

    def __panel__(self):
        return "summary"


LAZY, OPAQUE, BROKEN, EMPTY = "Demo/lazy", "Demo/opaque", "Demo/broken", "Demo/empty"
CHART, SUMMARY = "Demo/chart", "Demo/summary"
COMPONENTS = {
    **TABLE_COMPONENTS,
    LAZY: LazyTable,
    OPAQUE: Opaque,
    BROKEN: Broken,
    EMPTY: Empty,
    CHART: Chart,
    SUMMARY: Summary,
}


@pytest.fixture
def editor():
    return FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)


def _data(editor, component):
    node = editor.add_component(component)
    select_on_canvas(editor._flow, node_id=node)
    editor._inspector._node_tabs.active = 1
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
    instance._preview_rev += 1
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
    editor._component_instances[node]._preview_rev += 1
    column = editor._inspector._data_column
    assert "Fetching rows..." not in _texts(column)
    assert len(_tables(column)) == 1


async def test_live_object_without_preview_gets_a_caption_not_its_view(editor):
    _, column = _data(editor, OPAQUE)
    texts = _texts(column)
    assert any(t.endswith("This component gives no preview of it.") for t in texts)
    assert "live" not in texts


@pytest.mark.failures
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
    moment = dt.datetime(2026, 11, 22, 9, 30)
    with pytest.raises(TypeError):
        json.dumps(moment)
    assert isinstance(editor._value_preview({"a": 1}), pn.pane.JSON)
    # Shown as typed, without the margins and highlighting of a Markdown code block.
    preview = editor._value_preview(moment)
    assert isinstance(preview, pn.pane.HTML)
    assert preview.object == '<pre class="fd-value-repr">2026-11-22 09:30:00</pre>'


async def test_value_preview_names_an_object_by_its_type_not_its_repr(editor):
    preview = editor._value_preview(Pipeline())
    assert isinstance(preview, pn.pane.Markdown)
    assert preview.object == "Pipeline"


async def test_text_preview_is_a_compact_caption(editor):
    _, column = _data(editor, SUMMARY)
    (caption,) = [o for o in column.select(pmui.Typography) if "fd-data-text" in o.css_classes]
    assert caption.object == escape_markdown("1 table: sales")
    assert caption.variant == "caption"
    assert caption.margin == 0
    assert caption.sx["lineHeight"] == 1.45
    assert all(isinstance(o, pmui.Typography) for o in column.select(pn.pane.Markdown))
    assert not [o for o in column.select(pn.pane.HTML) if "fd-data-markdown" in o.css_classes]


async def test_text_preview_with_markdown_structure_is_markdown_without_margins(editor):
    node, _ = _data(editor, SUMMARY)
    instance = editor._component_instances[node]
    instance.text = "**2 tables**\n\n- sales\n- <b>regions</b>"
    instance._preview_rev = 1
    editor._inspector._node_tabs.active = 0
    editor._inspector._node_tabs.active = 1
    column = editor._inspector._data_column
    (pane,) = [o for o in column.select(pn.pane.HTML) if "fd-data-markdown" in o.css_classes]
    assert pane.margin == 0
    assert "<strong>2 tables</strong>" in pane.object
    assert "<li>sales</li>" in pane.object
    # A component's text never becomes markup of its own.
    assert "&lt;b&gt;regions&lt;/b&gt;" in pane.object
    assert "h6 { margin: 0 0 4px; }" in pane.stylesheets[0]
    assert ":last-child { margin-bottom: 0; }" in pane.stylesheets[0]


async def test_chart_preview_is_the_card_thumbnail(editor):
    _, column = _data(editor, CHART)
    assert "bar · 3 bars" in _texts(column)
    (chart,) = [o for o in column.select(pn.pane.HTML) if "fd-data-chart-pane" in o.css_classes]
    assert chart.object.startswith('<div class="fd-data-chart"><svg viewBox="0 0 360 180"')
    assert chart.object.count('class="fd-ch-br"') == 3
    assert ".fd-ch-br" in chart.stylesheets[0]
    assert "__flowdash_preview_kind__" not in " ".join(_texts(column))


def _columns(frame):
    (table,) = [o for o in _table_preview(frame) if isinstance(o, pn.pane.DataFrame)]
    assert table.index is False
    return list(table.object.columns), table


async def test_index_carrying_data_is_shown_as_columns():
    sums = FRAME.groupby("state").sum()
    columns, table = _columns(sums)
    assert columns == ["state", "case_id"]
    assert list(table.object["state"]) == ["AK", "CA"]

    pivot = FRAME.assign(kind=["a", "b", "c", "d"] * 10).groupby(["state", "kind"]).size()
    assert _columns(pivot.to_frame("n"))[0] == ["state", "kind", "n"]

    dated = pd.DataFrame({"v": [1, 2]}, index=pd.date_range("2026-01-01", periods=2))
    assert _columns(dated)[0] == ["index", "v"]

    clash = FRAME.set_index(FRAME["state"])
    assert _columns(clash)[0] == ["state (index)", "case_id", "state"]


@pytest.mark.parametrize(
    "frame",
    [
        FRAME,
        FRAME[FRAME.case_id > 5],
        FRAME.sort_values("case_id", ascending=False),
        FRAME.iloc[[0, 13, 26, 39]],
    ],
    ids=["default", "filtered", "sorted", "sampled"],
)
async def test_unnamed_integer_index_is_hidden(frame):
    assert _columns(frame)[0] == ["case_id", "state"]


async def test_wide_table_is_capped_and_says_so():
    wide = pd.DataFrame({f"c{i}": [i] for i in range(2000)})
    objects = _table_preview(wide)
    assert "1 row \u00d7 2,000 columns, first 50 columns shown" in _texts(objects)
    columns, table = _columns(wide)
    assert len(columns) == 50
    assert len(table.stylesheets[0]) < 10_000


async def test_numbers_are_aligned_on_the_rendered_columns():
    nested = pd.DataFrame(
        [[1, "x", 2.5]],
        columns=pd.MultiIndex.from_tuples([("a", "n"), ("a", "s"), ("b", "f")]),
        index=pd.Index(["k"], name="key"),
    )
    columns, table = _columns(nested)
    assert columns == ["key", "a / n", "a / s", "b / f"]
    css = table.stylesheets[0]
    assert "td:nth-child(2)" in css and "td:nth-child(4)" in css
    assert "td:nth-child(1)" not in css and "td:nth-child(3)" not in css


async def test_data_tab_runs_hooks_only_while_shown(editor):
    node = editor.add_component(LAZY)
    instance = editor._component_instances[node]
    select_on_canvas(editor._flow, node_id=node)
    assert instance.calls == []
    assert not len(editor._inspector._data_column)
    editor._inspector._node_tabs.active = 1
    assert sorted(instance.calls) == ["note", "pipeline"]
    editor._inspector._node_tabs.active = 0
    instance._preview_rev += 1
    assert len(instance.calls) == 2


class _Doc:
    """Records next-tick callbacks instead of running them."""

    session_context = object()

    def __init__(self):
        self.callbacks = []

    def add_next_tick_callback(self, callback):
        self.callbacks.append(callback)


async def test_off_loop_preview_coalesces_and_drops_stale_results(editor, monkeypatch):
    from panel_flowdash.ui import inspector as module

    jobs = []
    monkeypatch.setattr(module, "run_off_loop", lambda work, then, doc: jobs.append((work, then)))
    node = editor.add_component(LAZY)
    select_on_canvas(editor._flow, node_id=node)
    inspector = editor._inspector
    doc = _Doc()
    inspector._doc = doc
    monkeypatch.setattr(module, "set_curdoc", lambda doc: contextlib.nullcontext())
    instance = editor._component_instances[node]

    inspector._node_tabs.active = 1
    instance._preview_rev += 1
    instance._preview_rev += 1
    # A burst of changes is one rebuild on the next tick.
    assert len(doc.callbacks) == 1
    assert instance.calls == []
    doc.callbacks.pop()()
    assert len(jobs) == 2
    assert "Loading the preview\u2026" in _texts(inspector._data_column)

    stale = jobs[:]
    instance._preview_rev += 1
    doc.callbacks.pop()()
    for work, then in stale:
        then(work())
    # The superseded results are dropped; the current rebuild still loads.
    assert not _tables(inspector._data_column)
    for work, then in jobs[2:]:
        then(work())
    assert len(_tables(inspector._data_column)) == 1


async def test_wide_polars_frame_is_capped():
    pl = pytest.importorskip("polars")
    wide = pl.DataFrame({f"c{i}": [i, i + 1] for i in range(80)})
    objects = _table_preview(wide)
    assert "2 rows \u00d7 80 columns, first 50 columns shown" in _texts(objects)
    (table,) = [o for o in objects if isinstance(o, pn.pane.DataFrame)]
    assert list(table.object.columns) == [f"c{i}" for i in range(50)]


async def test_pending_refresh_skips_hooks_once_the_tab_is_left(editor, monkeypatch):
    from panel_flowdash.ui import inspector as module

    monkeypatch.setattr(module, "set_curdoc", lambda doc: contextlib.nullcontext())
    node = editor.add_component(LAZY)
    select_on_canvas(editor._flow, node_id=node)
    inspector = editor._inspector
    doc = _Doc()
    inspector._doc = doc
    instance = editor._component_instances[node]

    inspector._node_tabs.active = 1
    (refresh,) = doc.callbacks
    inspector._node_tabs.active = 0
    refresh()
    assert instance.calls == []
    assert not len(inspector._data_column)
