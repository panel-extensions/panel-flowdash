"""Tests for the previews of each node's value inside its card on the wiring canvas."""

import contextlib
import html
import re
import threading
import time
from functools import partial
from html.parser import HTMLParser

import numpy as np
import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.settings import DashboardSettings
from panel_flowdash.ui import node_card as node_card_module, node_preview
from panel_flowdash.ui.node_card import (
    NodeCard,
    card_height,
    port_tops,
)
from panel_flowdash.ui.node_preview import (
    HEIGHTS,
    MAX_CHIPS,
    MAX_COLUMNS,
    MAX_POINTS,
    MAX_ROWS,
    MAX_TEXT_CHARS,
    Coalescer,
    build_preview,
    describe_object,
    hook_preview,
    sparkline_svg,
)
from panel_flowdash.ui.roles import ROLE_ICONS
from panel_flowdash.util import ReactiveError
from tests.untrusted_payloads import PAYLOADS

# Carrying failures along edges, which marks the failed node, needs param >= 2.5.
FAILURES = ReactiveError is not None

PAYLOAD = 'Q3 <img src=x onerror="document.title=\'pwned\'"> *draft* & "quoted"'


@register(page=False, component=True, title="Turbines", config=["size"])
class Turbines(Viewer):
    """A source publishing a table."""

    size = param.Integer(default=5)

    @param.output(param.DataFrame)
    @param.depends("size")
    def rows(self):
        return pd.DataFrame(
            {
                "state": ["TX", "IA", "OK", "KS", "IL", "CA"][: self.size],
                "capacity": [1.5, 2.3, 2.0, 3.1, 1.8, 2.7][: self.size],
                "year": [2001, 2008, 2012, 2015, 2019, 2021][: self.size],
                "model": ["GE", "Vestas", "Siemens", "GE", "Nordex", "Vestas"][: self.size],
            }
        )

    def __panel__(self):
        return "turbines"


@register(page=False, component=True, title="Filter")
class Filter(Viewer):
    """A transform keeping the rows above a capacity."""

    rows = param.DataFrame(default=None, allow_None=True)
    minimum = param.Number(default=0)

    @param.output(param.DataFrame)
    @param.depends("rows", "minimum")
    def kept(self):
        if self.rows is None:
            return None
        return self.rows[self.rows["capacity"] >= self.minimum]

    def __panel__(self):
        return "filter"


@register(page=False, component=True, title="Trend")
class Trend(Viewer):
    """A transform publishing a list of numbers."""

    rows = param.DataFrame(default=None, allow_None=True)

    @param.output(param.List)
    @param.depends("rows")
    def capacity(self):
        if self.rows is None:
            return None
        return [float(v) for v in np.sin(np.linspace(0, 6, 60)) * 2 + len(self.rows)]

    def __panel__(self):
        return "trend"


@register(page=False, component=True, title="States")
class States(Viewer):
    """A transform publishing a list of strings."""

    rows = param.DataFrame(default=None, allow_None=True)

    @param.output(param.List)
    @param.depends("rows")
    def states(self):
        return None if self.rows is None else list(self.rows["state"])

    def __panel__(self):
        return "states"


@register(page=False, component=True, title="Stats")
class Stats(Viewer):
    """A transform publishing a dict."""

    rows = param.DataFrame(default=None, allow_None=True)

    @param.output(param.Dict)
    @param.depends("rows")
    def stats(self):
        if self.rows is None:
            return None
        return {
            "count": len(self.rows),
            "mean": float(self.rows["capacity"].mean()),
            "states": list(self.rows["state"]),
            "first": int(self.rows["year"].min()),
        }

    def __panel__(self):
        return "stats"


@register(page=False, component=True, title="Table", default_size={"height": 300})
class Table(Viewer):
    """A view of one required table."""

    rows = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    def __panel__(self):
        return "table"


@register(page=False, component=True, title="Inverse")
class Inverse(Viewer):
    """A transform that fails for zero."""

    value = param.Number(default=0)

    @param.output(param.Number)
    @param.depends("value")
    def inverse(self):
        return 1 / self.value

    def __panel__(self):
        return "inverse"


@register(page=False, component=True, title="Note", config=["text"])
class Note(Viewer):
    """A component previewing its own text through the hook."""

    text = param.String(default="Capacity *grew* 12% <b>this</b> year.")

    def __flowdash_preview__(self):
        return self.text

    def __panel__(self):
        return pn.pane.Markdown(self.param.text)


@register(page=False, component=True, title="Raising", config=["text"])
class Raising(Viewer):
    """A component whose preview hook raises."""

    text = param.String(default="")

    @param.output(param.String)
    @param.depends("text")
    def echo(self):
        return self.text or "fallback"

    def __flowdash_preview__(self):
        raise RuntimeError("no preview")

    def __panel__(self):
        return "raising"


DEMO = {
    "Demo/turbines": Turbines,
    "Demo/filter": Filter,
    "Demo/trend": Trend,
    "Demo/states": States,
    "Demo/stats": Stats,
    "Demo/table": Table,
    "Demo/inverse": Inverse,
    "Demo/note": Note,
    "Demo/raising": Raising,
}


def demo_editor(**params) -> tuple[FlowDash, dict[str, str]]:
    """Place one component of each kind of preview, a failed one and a waiting one."""
    params.setdefault("node_previews", True)
    editor = FlowDash(DEMO, notifications=False, propagate_failures=FAILURES, **params)
    ids = {
        "turbines": editor.add_component("Demo/turbines", position=(0, 0)),
        "slider": editor.add_component(
            "Widgets/Slider",
            config={"default_start": 0, "default_end": 3, "step": 0.1, "default_value": 1.5},
            position=(0, 240),
        ),
        "filter": editor.add_component("Demo/filter", position=(320, 0)),
        "trend": editor.add_component("Demo/trend", position=(640, -150)),
        "states": editor.add_component("Demo/states", position=(640, 0)),
        "stats": editor.add_component("Demo/stats", position=(640, 130)),
        "table": editor.add_component("Demo/table", position=(960, 0)),
        "waiting": editor.add_component("Demo/table", position=(960, 200)),
        "zero": editor.add_component(
            "Widgets/Slider",
            config={"default_start": 0, "default_end": 1, "default_value": 1},
            position=(320, 330),
        ),
        "inverse": editor.add_component("Demo/inverse", position=(640, 330)),
        "note": editor.add_component("Demo/note", position=(960, 330)),
    }
    editor.connect(ids["turbines"], "rows", ids["filter"], "rows")
    editor.connect(ids["slider"], "selected", ids["filter"], "minimum")
    for name in ("trend", "states", "stats", "table"):
        editor.connect(ids["filter"], "kept", ids[name], "rows")
    editor.connect(ids["zero"], "selected", ids["inverse"], "value")
    editor._component_instances[ids["zero"]].value = 0
    return editor, ids


def card(editor, instance_id) -> NodeCard:
    return next(n["view"] for n in editor._flow.nodes if n["id"] == instance_id)


def _visible_text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


_TAGS = {
    "button",
    "circle",
    "div",
    "line",
    "path",
    "polyline",
    "rect",
    "span",
    "svg",
    "table",
    "td",
    "text",
    "th",
    "tr",
}
_ATTRIBUTES = {
    "aria-hidden",
    "aria-label",
    "class",
    "cx",
    "cy",
    "d",
    "data-fd-action",
    "fill-opacity",
    "height",
    "points",
    "preserveaspectratio",
    "r",
    "rx",
    "style",
    "text-anchor",
    "title",
    "type",
    "vector-effect",
    "viewbox",
    "width",
    "x",
    "x1",
    "x2",
    "y",
    "y1",
    "y2",
}


class _Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attributes, self.styles = set(), set(), set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attributes |= {name for name, _ in attrs}
        self.styles |= {value for name, value in attrs if name == "style"}


def _assert_escaped(markup: str):
    """Only the preview's own tags and attributes are in *markup*, so any text is shown as text."""
    parsed = _Markup()
    parsed.feed(markup)
    assert parsed.tags <= _TAGS, parsed.tags
    assert parsed.attributes <= _ATTRIBUTES, parsed.attributes
    assert all(re.fullmatch(r"height: \d+px", style) for style in parsed.styles), parsed.styles


class TestBuilders:
    async def test_table_shows_capped_rows_and_columns(self):
        frame = pd.DataFrame({f"c{i}": range(100) for i in range(10)})
        preview = build_preview(frame, "rows")
        assert preview.kind == "table"
        assert preview.html.count("<tr>") == MAX_ROWS + 1
        assert preview.html.count("<th") == MAX_COLUMNS
        assert "100 rows, 10 columns" in preview.html
        assert "c3" not in preview.html

    async def test_table_cells_are_truncated_and_numbers_formatted(self):
        frame = pd.DataFrame({"name": ["x" * 200], "value": [1234567.891], "n": [12345]})
        preview = build_preview(frame, "rows")
        assert "x" * 24 not in preview.html
        assert "x" * 23 + "…" in preview.html
        assert "1.235e+06" in preview.html and "12,345" in preview.html
        assert 'class="fd-num"' in preview.html

    async def test_table_with_fewer_rows_shrinks_to_them(self):
        short = build_preview(pd.DataFrame({"a": [1]}), "rows")
        empty = build_preview(pd.DataFrame({"a": []}), "rows")
        full = build_preview(pd.DataFrame({"a": range(10)}), "rows")
        assert short.height == empty.height < full.height == HEIGHTS["table"]

    @pytest.mark.parametrize(
        "values",
        [list(range(1000)), np.arange(1000.0), pd.Series(range(1000)), (1, 5, 2, 8)],
    )
    async def test_numbers_become_a_sparkline(self, values):
        preview = build_preview(values, "values")
        assert preview.kind == "series"
        points = re.search(r'points="([^"]*)"', preview.html)[1].split()
        assert 2 <= len(points) <= MAX_POINTS

    async def test_sparkline_skips_missing_values_and_handles_constants(self):
        assert sparkline_svg([1.0, float("nan"), 3.0]).count(",") == 2
        assert "12.0" in sparkline_svg([2, 2, 2])
        assert sparkline_svg([float("nan")]) == ""
        assert build_preview([float("nan")] * 5, "v").kind == "chips"

    async def test_strings_become_chips(self):
        preview = build_preview([f"s{i}" for i in range(50)], "states")
        assert preview.kind == "chips"
        assert preview.html.count('class="fd-pv-chip"') == MAX_CHIPS
        assert "50 items" in preview.html

    async def test_dict_shows_a_few_entries(self):
        preview = build_preview({f"k{i}": list(range(i)) for i in range(10)}, "stats")
        assert preview.kind == "entries"
        assert preview.html.count('class="fd-pv-entry"') == 3
        assert "10 entries" in preview.html

    @pytest.mark.parametrize(
        ("value", "text"),
        [(3, "3"), (2.5, "2.5"), ("TX", "TX"), (True, "True"), (pd.Timestamp(2024, 1, 2), "2024")],
    )
    async def test_scalars_show_their_value_on_one_line(self, value, text):
        preview = build_preview(value, "selected")
        assert preview.kind == "value"
        assert preview.height == 22
        assert text in preview.html and "selected" in preview.html

    async def test_charts_are_named_without_rendering(self):
        hv = pytest.importorskip("holoviews")
        curve = hv.Curve([(0, 1), (1, 2)], "x", "y")
        assert describe_object(curve) == "HoloViews Curve of y over x"
        assert build_preview(curve, "plot").kind == "value"
        assert describe_object(pn.pane.Markdown("x")) == "Panel Markdown"
        assert describe_object(1) is None

    async def test_long_values_are_capped(self):
        text = build_preview("x" * 10_000, "text").html
        assert len(text) < MAX_TEXT_CHARS * 3 + 400

    async def test_no_value_has_no_preview(self):
        assert build_preview(None, "x") is None

    @pytest.mark.parametrize("dtype", ["Int64", "Float64", "int64[pyarrow]"])
    async def test_nullable_values_are_gaps(self, dtype):
        if "pyarrow" in dtype:
            pytest.importorskip("pyarrow")
        series = pd.Series([1, None, 3, 4, None], dtype=dtype)
        preview = build_preview(series, "v")
        assert preview.kind == "series"
        assert "1 to 4" in preview.html
        table = build_preview(pd.DataFrame({"a": series}), "rows")
        assert "&lt;NA&gt;" in table.html

    async def test_huge_numbers_do_not_overflow(self):
        preview = build_preview([1, 10**400, 3, 4], "v")
        assert preview.kind == "series"
        assert sparkline_svg([10**400, 1, 2]).count(",") == 2

    async def test_sets_show_a_few_items_without_sorting_them_all(self):
        preview = build_preview(set(range(10_000)), "ids")
        assert preview.kind == "chips"
        assert preview.html.count('class="fd-pv-chip"') == MAX_CHIPS
        assert "10,000 items" in preview.html

    async def test_large_objects_are_not_written_out(self):
        nested = {"rows": list(range(100_000)), "matrix": np.zeros((500, 500))}
        markup = build_preview(nested, "v").html
        assert len(markup) < 2000
        assert "ndarray of shape 500 x 500" in markup
        assert len(build_preview(np.zeros((500, 500)), "v").html) < 1000

    async def test_midnight_timestamps_are_dates(self):
        days = pd.Series(pd.date_range("2024-01-01", periods=3))
        assert ">2024-01-01<" in build_preview(days, "days").html
        assert ">2024-01-01<" in build_preview(pd.Timestamp("2024-01-01"), "day").html
        assert ">2024-01-01 12:30<" in build_preview(pd.Timestamp("2024-01-01 12:30"), "t").html
        frame = pd.DataFrame({"day": pd.date_range("2024-01-01", periods=2)})
        assert ">2024-01-02<" in build_preview(frame, "rows").html

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    async def test_untrusted_text_is_escaped_everywhere(self, name):
        payload = PAYLOADS[name]
        values = [
            payload,
            [payload, payload],
            {payload: payload},
            pd.DataFrame({payload: [payload]}),
            pd.Series([payload]),
            {payload, "other"},
            {"nested": {payload: [payload]}, payload: (payload, 1)},
            pd.DataFrame(
                [[payload, 1]], columns=pd.MultiIndex.from_tuples([(payload, "a"), ("b", payload)])
            ),
        ]
        for value in values:
            preview = build_preview(value, payload)
            _assert_escaped(preview.html)

    async def test_untrusted_chart_dimensions_are_escaped(self):
        hv = pytest.importorskip("holoviews")
        curve = hv.Curve([(0, 1)], hv.Dimension(PAYLOAD), hv.Dimension(PAYLOAD + "y"))
        markup = build_preview(curve, PAYLOAD).html
        _assert_escaped(markup)
        assert PAYLOAD in _visible_text(markup)

    async def test_untrusted_text_is_shown_as_typed(self):
        for value in (PAYLOAD, [PAYLOAD], {"k": PAYLOAD}, pd.DataFrame({"c": [PAYLOAD]})):
            markup = build_preview(value, "label").html
            _assert_escaped(markup)
            assert PAYLOAD[:20] in _visible_text(markup)


class TestHook:
    async def test_string_is_plain_text(self):
        class Component:
            def __flowdash_preview__(self):
                return PAYLOAD

        preview = hook_preview(Component())
        assert preview.kind == "text"
        _assert_escaped(preview.html)
        assert PAYLOAD in _visible_text(preview.html)

    async def test_short_line_gets_a_one_line_box(self):
        class Component:
            def __flowdash_preview__(self):
                return "A short <b>note</b>"

        preview = hook_preview(Component())
        assert (preview.kind, preview.height) == ("value", 22)
        _assert_escaped(preview.html)
        assert "A short <b>note</b>" in _visible_text(preview.html)

    async def test_other_values_are_previewed_like_outputs(self):
        class Component:
            def __flowdash_preview__(self):
                return pd.DataFrame({"a": [1, 2]})

        assert hook_preview(Component()).kind == "table"

    async def test_none_or_no_hook_keeps_the_default(self):
        class Component:
            def __flowdash_preview__(self):
                return None

        assert hook_preview(Component()) is None
        assert hook_preview(object()) is None

    async def test_raising_hook_falls_back_to_the_ports(self, caplog):
        editor = FlowDash(DEMO, notifications=False, node_previews=True)
        raising = editor.add_component("Demo/raising")
        assert card(editor, raising).preview.kind == "value"
        assert "fallback" in card(editor, raising).preview.html
        assert "The preview hook of Raising failed" in caplog.text

    async def test_hook_follows_the_component(self):
        editor = FlowDash(DEMO, notifications=False, node_previews=True)
        note = editor.add_component("Demo/note")
        preview = card(editor, note).preview
        assert preview.kind == "value"
        assert "Capacity *grew* 12% &lt;b&gt;this&lt;/b&gt; year." in preview.html
        editor._component_instances[note].text = "Updated"
        assert "Updated" in card(editor, note).__panel__().object

    async def test_blank_string_keeps_the_default(self):
        class Component:
            def __flowdash_preview__(self):
                return "  \n"

        assert hook_preview(Component()) is None

    async def test_markdown_previews_its_text_without_markup(self):
        editor = FlowDash({}, notifications=False, node_previews=True)
        node = editor.add_component(
            "Text/Markdown",
            config={"text": "# Capacity\n\nGrew **12%** <img src=x onerror=alert(1)>"},
        )
        preview = card(editor, node).preview
        assert preview.kind == "text"
        _assert_escaped(preview.html)
        text = _visible_text(preview.html)
        assert "Capacity" in text and "Grew 12%" in text and "**" not in text
        editor._component_instances[node].text = "Updated *text*"
        assert "Updated text" in _visible_text(card(editor, node).preview.html)

    async def test_notes_preview_their_plain_text(self):
        editor = FlowDash({}, notifications=False, node_previews=True)
        node = editor.add_component(
            "Text/Notes", config={"text": "<p>First <strong>line</strong></p><p>Second</p>"}
        )
        preview = card(editor, node).preview
        assert preview.kind == "text"
        _assert_escaped(preview.html)
        text = _visible_text(preview.html)
        assert "First line" in text and "Second" in text and "strong" not in text
        editor._component_instances[node].text = "<p>Edited</p>"
        assert "Edited" in _visible_text(card(editor, node).preview.html)

    async def test_empty_notes_keep_the_compact_card(self):
        editor = FlowDash({}, notifications=False, node_previews=True)
        node = editor.add_component("Text/Notes")
        assert card(editor, node).preview is None

    async def test_hook_watchers_are_released(self):
        editor = FlowDash(DEMO, notifications=False, node_previews=True)
        note = editor.add_component("Demo/note")
        instance, note_card = editor._component_instances[note], card(editor, note)
        editor.remove_component(note)
        instance.text = "After removal"
        assert "After removal" not in note_card.__panel__().object


class TestCards:
    async def test_each_kind_of_node(self):
        editor, ids = demo_editor()
        kinds = {name: getattr(card(editor, i).preview, "kind", None) for name, i in ids.items()}
        assert kinds == {
            "turbines": "table",
            "slider": "value",
            "filter": "table",
            "trend": "series",
            "states": "chips",
            "stats": "entries",
            "table": "table",
            "waiting": None,
            "zero": "value",
            "inverse": None if FAILURES else "value",
            "note": "value",
        }
        assert card(editor, ids["inverse"]).status == ("failed" if FAILURES else "ok")
        assert card(editor, ids["waiting"]).status == "waiting"
        assert "Showing 5 rows, 4 columns" in card(editor, ids["table"]).preview.html

    async def test_card_grows_by_the_preview_and_keeps_its_height(self):
        editor, ids = demo_editor()
        filtered = card(editor, ids["filter"])
        pane = filtered.__panel__()
        base = card_height(2, 1)
        assert filtered.base_height == base
        assert pane.height == base - 34 + HEIGHTS["table"]
        editor._component_instances[ids["slider"]].value = 2.2
        assert "2 rows, 4 columns" in pane.object
        assert pane.height == base - 34 + HEIGHTS["table"]

    async def test_short_table_card_fits_its_rows_then_only_grows(self):
        editor, ids = demo_editor()
        editor._show_node_previews(False)
        editor._component_instances[ids["slider"]].value = 2.9
        editor._show_node_previews(True)
        filtered = card(editor, ids["filter"])
        pane = filtered.__panel__()
        short = pane.height
        assert short < card_height(2, 1) - 34 + HEIGHTS["table"]
        editor._component_instances[ids["slider"]].value = 2.2
        grown = pane.height
        assert grown > short
        editor._component_instances[ids["slider"]].value = 2.9
        assert "1 row, 4 columns" in pane.object
        assert pane.height == grown

    async def test_previews_are_off_by_default(self):
        editor = FlowDash(DEMO, notifications=False)
        src = editor.add_component("Demo/turbines")
        assert not editor.settings.node_previews
        assert editor._status_bar.preview_toggle.value is False
        assert card(editor, src).preview is None
        assert 'class="fd-summary"' in card(editor, src).__panel__().object

    async def test_setting_turns_previews_off(self):
        editor, ids = demo_editor()
        filtered = card(editor, ids["filter"])
        assert editor.settings.node_previews and filtered.show_preview
        editor.settings.node_previews = False
        assert not filtered.show_preview and filtered.preview is None
        assert filtered.__panel__().height == card_height(2, 1)
        assert 'class="fd-summary"' in filtered.__panel__().object
        assert editor._status_bar.preview_toggle.value is False
        assert editor.dirty

    async def test_canvas_toggle_is_session_state(self):
        editor, ids = demo_editor()
        editor.dirty = False
        toggle = editor._status_bar.preview_toggle
        toggle.value = False
        assert not card(editor, ids["filter"]).show_preview
        assert editor.settings.node_previews and not editor.dirty
        placed = editor.add_component("Demo/turbines")
        assert not card(editor, placed).show_preview
        toggle.value = True
        assert card(editor, ids["filter"]).preview.kind == "table"
        assert card(editor, placed).show_preview

    async def test_canvas_toggle_is_labelled_and_names_its_shortcut(self):
        editor, _ids = demo_editor()
        toggle = editor._status_bar.preview_toggle
        assert toggle.label == "Previews"
        assert toggle.description == "Hide node previews (P)"
        toggle.value = False
        assert toggle.description == "Show node previews (P)"
        editor._status_bar._on_shortcut("p")
        assert toggle.value and toggle.description == "Hide node previews (P)"

    async def test_canvas_toggle_leaves_a_read_only_dashboard_unchanged(self):
        editor, ids = demo_editor(read_only=True)
        editor.dirty = False
        editor._status_bar.preview_toggle.value = False
        assert not card(editor, ids["filter"]).show_preview
        assert not editor.dirty

    async def test_toggling_renders_every_card_at_once(self, monkeypatch):
        editor, ids = demo_editor()
        doc = _FakeDoc()
        monkeypatch.setattr(NodeCard, "_document", lambda self: doc)
        editor._status_bar.preview_toggle.value = False
        assert doc.ticks == [] and card(editor, ids["filter"]).preview is None

    async def test_editor_default_and_saved_setting(self):
        editor, ids = demo_editor(node_previews=False)
        assert not card(editor, ids["filter"]).show_preview
        model = editor.to_model()
        model.settings["node_previews"] = True
        editor.load_model(model)
        assert editor.settings.node_previews
        assert all(
            n["view"].show_preview for n in editor._flow.nodes if isinstance(n["view"], NodeCard)
        )

    async def test_previews_are_not_built_while_off(self, monkeypatch):
        calls = []
        real = node_preview.build_preview
        monkeypatch.setattr(
            "panel_flowdash.ui.node_card.build_preview",
            lambda *a, **k: calls.append(a) or real(*a, **k),
        )
        editor = FlowDash(DEMO, notifications=False, node_previews=False)
        src = editor.add_component("Demo/turbines")
        editor._component_instances[src].size = 2
        assert calls == []
        editor.settings.node_previews = True
        assert len(calls) == 1

    async def test_card_skips_identical_updates(self):
        editor = FlowDash(DEMO, notifications=False, node_previews=True)
        src = editor.add_component("Demo/turbines")
        pane = card(editor, src).__panel__()
        events = []
        pane.param.watch(events.append, ["object", "height"])
        editor.graph.get_state(src).param.trigger("rows")
        assert events == []

    async def test_role_icons_match_the_palette(self):
        editor, ids = demo_editor()
        icons = {name: card(editor, ids[name]).icon for name in ("turbines", "filter", "table")}
        assert icons == {
            "turbines": ROLE_ICONS["source"],
            "filter": ROLE_ICONS["transform"],
            "table": ROLE_ICONS["view"],
        }

    async def test_settings_dialog_offers_the_switch(self):
        editor = FlowDash(DEMO, notifications=False)
        widget = editor._settings_view._extra_form._widgets["node_previews"]
        assert widget.label == "Preview values on the canvas"
        assert DashboardSettings.param.node_previews.default is False


class TestHandleTops:
    async def test_handles_are_placed_at_their_port_rows(self):
        editor = FlowDash(DEMO, notifications=False)
        spec = editor._flow.node_types["Demo__filter"]
        tops_in, tops_out = port_tops(2, 1)
        assert [(h["id"], h["top"]) for h in spec["inputs"]] == [
            ("rows", f"var(--fd-port-in-0, {round(tops_in[0], 2)}px)"),
            ("minimum", f"var(--fd-port-in-1, {round(tops_in[1], 2)}px)"),
        ]
        assert [(h["id"], h["top"]) for h in spec["outputs"]] == [
            ("kept", f"var(--fd-port-out-0, {round(tops_out[0], 2)}px)")
        ]
        assert tops_in == [59.0, 81.0]
        assert tops_out == [103.0]

    async def test_card_rows_match_the_handle_tops(self):
        editor, ids = demo_editor()
        filter_card = card(editor, ids["filter"])
        handles, css_vars = filter_card.handle_layout()
        tops_in, tops_out = port_tops(2, 1)
        assert handles["in:rows"]["top"] == tops_in[0]
        assert handles["in:minimum"]["top"] == tops_in[1]
        assert handles["out:kept"]["top"] == tops_out[0]
        assert css_vars == {
            "--fd-port-in-0": f"{tops_in[0]:.2f}px",
            "--fd-port-in-1": f"{tops_in[1]:.2f}px",
            "--fd-port-out-0": f"{tops_out[0]:.2f}px",
        }
        markup = filter_card.__panel__().object
        for port_id in ("rows", "minimum", "kept"):
            assert f'data-port="{port_id}"' in markup


class _FakeDoc:
    """Collects the callbacks a served document would run on its event loop."""

    def __init__(self):
        self.session_context = object()
        self.ticks = []
        self.timeouts = []

    def add_next_tick_callback(self, callback):
        self.ticks.append(callback)

    def add_timeout_callback(self, callback, timeout):
        self.timeouts.append((callback, timeout))

    def run(self):
        while self.ticks or self.timeouts:
            while self.ticks:
                self.ticks.pop(0)()
            if self.timeouts:
                self.timeouts.pop(0)[0]()


@pytest.fixture
def served(monkeypatch):
    """Run scheduled callbacks only when the test says so, as the event loop would."""
    monkeypatch.setattr(node_preview, "set_curdoc", lambda doc: contextlib.nullcontext())
    monkeypatch.setattr(pn.state, "execute", lambda callback, schedule=None: callback())
    doc = _FakeDoc()
    monkeypatch.setattr(NodeCard, "_document", lambda self: doc)
    # Previews are built on a worker thread; here straight away, delivered on the next tick.
    monkeypatch.setattr(
        node_card_module,
        "run_off_loop",
        lambda work, then, doc: doc.add_next_tick_callback(partial(then, work())),
    )
    return doc


class TestCoalescing:
    async def test_runs_straight_away_without_a_session(self):
        calls = []
        coalesce = Coalescer(lambda: calls.append(1), lambda: None)
        coalesce()
        coalesce()
        assert calls == [1, 1]

    async def test_burst_in_a_session_runs_once_with_the_latest_value(self, served):
        seen, state = [], {"value": 0}
        coalesce = Coalescer(lambda: seen.append(state["value"]), lambda: served, delay=0.15)
        for value in range(50):
            state["value"] = value
            coalesce()
        # The timer is set from the event loop, whatever thread the calls came from.
        assert len(served.ticks) == 1 and served.timeouts == []
        served.ticks.pop()()
        assert len(served.timeouts) == 1 and served.timeouts[0][1] == 150
        served.timeouts.pop()[0]()
        assert seen == [49]
        coalesce()
        assert len(served.ticks) == 1

    async def test_calls_from_worker_threads_only_schedule_a_tick(self, served):
        class SlowCoalescer(Coalescer):
            # Widens the gap between checking and setting the flag, so racing threads meet in it.
            @property
            def _pending(self):
                pending = self.__dict__.get("pending", False)
                time.sleep(0.001)
                return pending

            @_pending.setter
            def _pending(self, value):
                self.__dict__["pending"] = value

        coalesce = SlowCoalescer(lambda: None, lambda: served)
        workers = [threading.Thread(target=coalesce) for _ in range(16)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert len(served.ticks) == 1 and served.timeouts == []

    def test_renders_from_two_threads_do_not_mix(self):
        editor, ids = demo_editor()
        node_card = card(editor, ids["filter"])
        inside = []

        def build(original=node_card._build_preview):
            inside.append(1)
            assert len(inside) == 1
            time.sleep(0.005)
            preview = original()
            inside.pop()
            return preview

        node_card._build_preview = build
        workers = [threading.Thread(target=node_card._render) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        assert node_card.__panel__().height == node_card.height

    async def test_cancelled_call_never_runs(self, served):
        seen = []
        coalesce = Coalescer(lambda: seen.append(1), lambda: served)
        coalesce()
        served.ticks.pop()()
        coalesce.cancel()
        served.run()
        coalesce()
        served.run()
        assert seen == []

    async def test_slider_drag_sends_one_card_update(self, served):
        editor, ids = demo_editor()
        pane = card(editor, ids["filter"]).__panel__()
        events = []
        pane.param.watch(events.append, "object")
        slider = editor._component_instances[ids["slider"]]
        for value in np.linspace(0, 3, 40):
            slider.value = float(value)
        assert events == []
        served.run()
        assert len(events) == 1
        assert "1 row, 4 columns" in pane.object

    async def test_removed_node_is_not_updated_by_a_pending_timer(self, served):
        editor = FlowDash(DEMO, notifications=False, node_previews=True)
        src = editor.add_component("Demo/turbines")
        pane = card(editor, src).__panel__()
        events = []
        pane.param.watch(events.append, "object")
        editor._component_instances[src].size = 2
        assert served.ticks
        editor.remove_component(src)
        served.run()
        assert events == []
