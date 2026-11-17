"""Tests for the previews of each node's value inside its card on the wiring canvas."""

import contextlib
import html
import re
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
from panel_flowdash.ui import node_preview
from panel_flowdash.ui.node_card import (
    NodeCard,
    card_height,
    handle_rules,
    handle_stylesheet,
    port_tops,
    preview_class,
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


_TAGS = {"div", "span", "table", "tr", "th", "td", "svg", "polyline"}
_ATTRIBUTES = {
    "class",
    "style",
    "title",
    "viewbox",
    "preserveaspectratio",
    "aria-hidden",
    "points",
    "vector-effect",
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

    async def test_table_with_fewer_rows_keeps_its_height(self):
        short = build_preview(pd.DataFrame({"a": [1]}), "rows")
        empty = build_preview(pd.DataFrame({"a": []}), "rows")
        assert short.height == empty.height == HEIGHTS["table"]

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

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    async def test_untrusted_text_is_escaped_everywhere(self, name):
        payload = PAYLOADS[name]
        values = [
            payload,
            [payload, payload],
            {payload: payload},
            pd.DataFrame({payload: [payload]}),
            pd.Series([payload]),
        ]
        for value in values:
            preview = build_preview(value, payload)
            _assert_escaped(preview.html)

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
        editor = FlowDash(DEMO, notifications=False)
        raising = editor.add_component("Demo/raising")
        assert card(editor, raising).preview.kind == "value"
        assert "fallback" in card(editor, raising).preview.html
        assert "The preview hook of Raising failed" in caplog.text

    async def test_hook_follows_the_component(self):
        editor = FlowDash(DEMO, notifications=False)
        note = editor.add_component("Demo/note")
        preview = card(editor, note).preview
        assert preview.kind == "text"
        assert "Capacity *grew* 12% &lt;b&gt;this&lt;/b&gt; year." in preview.html
        editor._component_instances[note].text = "Updated"
        assert "Updated" in card(editor, note).__panel__().object

    async def test_hook_watchers_are_released(self):
        editor = FlowDash(DEMO, notifications=False)
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
            "note": "text",
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
        assert pane.height == base - 22 + HEIGHTS["table"]
        editor._component_instances[ids["slider"]].value = 2.2
        assert "2 rows, 4 columns" in pane.object
        assert pane.height == base - 22 + HEIGHTS["table"]

    async def test_previewing_card_carries_its_handle_class(self):
        editor, ids = demo_editor()
        pane = card(editor, ids["filter"]).__panel__()
        assert pane.css_classes == [preview_class(["rows", "minimum"], ["kept"])]
        assert card(editor, ids["waiting"]).__panel__().css_classes == []

    async def test_toggle_follows_the_dashboard_setting(self):
        editor, ids = demo_editor()
        filtered = card(editor, ids["filter"])
        assert editor.settings.node_previews and filtered.show_preview
        editor.settings.node_previews = False
        assert not filtered.show_preview and filtered.preview is None
        assert filtered.__panel__().height == card_height(2, 1)
        assert filtered.__panel__().css_classes == []
        assert 'class="fd-summary"' in filtered.__panel__().object
        assert editor._status_bar.preview_toggle.value is False
        assert editor.dirty
        editor._status_bar.preview_toggle.value = True
        assert editor.settings.node_previews and filtered.preview.kind == "table"

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
        editor = FlowDash(DEMO, notifications=False)
        src = editor.add_component("Demo/turbines")
        pane = card(editor, src).__panel__()
        events = []
        pane.param.watch(events.append, ["object", "height", "css_classes"])
        editor.graph.get_state(src).param.trigger("rows")
        assert events == []

    async def test_settings_dialog_offers_the_switch(self):
        editor = FlowDash(DEMO, notifications=False)
        widget = editor._settings_view._extra_form._widgets["node_previews"]
        assert widget.label == "Preview values on the canvas"
        assert DashboardSettings.param.node_previews.default is True


class TestHandleRules:
    async def test_handles_are_pinned_beside_their_labels(self):
        rules = handle_rules(["rows", "minimum"], ["kept"])
        tops_in, tops_out = port_tops(2, 1)
        css_class = preview_class(["rows", "minimum"], ["kept"])
        assert (
            f'.react-flow__node:has(.{css_class}) .react-flow__handle-left[data-handleid="rows"]'
            f" {{ top: {tops_in[0]:.2f}px !important; }}" in rules
        )
        assert f'[data-handleid="kept"] {{ top: {tops_out[0]:.2f}px' in rules
        assert tops_in == [card_height(2, 1) / 3, card_height(2, 1) * 2 / 3]

    async def test_port_names_are_escaped_in_selectors(self):
        rules = handle_rules(['a"] * { color: red } [x="', "b\\c</style>"], [])
        assert '"a\\22 ] * { color: red } [x=\\22 "' in rules
        assert '"b\\5c c\\3c /style\\3e "' in rules
        assert rules.count("{ top:") == 2

    async def test_class_depends_on_the_port_layout(self):
        assert preview_class(["a"], ["b"]) != preview_class(["a", "b"], [])
        assert re.fullmatch(r"fd-pv-[0-9a-f]{12}", preview_class(["a"], []))
        assert handle_rules([], []) == ""

    async def test_canvas_stylesheet_covers_every_component(self):
        editor = FlowDash(DEMO, notifications=False)
        css = editor._flow.stylesheets[-1]
        assert css == handle_stylesheet(editor._component_specs.values())
        assert preview_class(["rows", "minimum"], ["kept"]) in css
        assert preview_class(["start", "end"], ["selected"]) in css


class _FakeDoc:
    def __init__(self):
        self.session_context = object()
        self.timeouts = []

    def add_timeout_callback(self, callback, timeout):
        self.timeouts.append((callback, timeout))


class TestCoalescing:
    async def test_runs_straight_away_without_a_session(self):
        calls = []
        coalesce = Coalescer(lambda: calls.append(1))
        coalesce()
        coalesce()
        assert calls == [1, 1]

    async def test_burst_in_a_session_runs_once_with_the_latest_value(self, monkeypatch):
        doc = _FakeDoc()
        monkeypatch.setattr(node_preview, "_session_doc", lambda: doc)
        monkeypatch.setattr(node_preview, "set_curdoc", lambda doc: contextlib.nullcontext())
        monkeypatch.setattr(pn.state, "execute", lambda callback, schedule=None: callback())
        seen, state = [], {"value": 0}
        coalesce = Coalescer(lambda: seen.append(state["value"]), delay=0.15)
        for value in range(50):
            state["value"] = value
            coalesce()
        assert len(doc.timeouts) == 1 and doc.timeouts[0][1] == 150
        doc.timeouts.pop()[0]()
        assert seen == [49]
        coalesce()
        assert len(doc.timeouts) == 1

    async def test_slider_drag_sends_one_card_update(self, monkeypatch):
        editor, ids = demo_editor()
        pane = card(editor, ids["filter"]).__panel__()
        events = []
        pane.param.watch(events.append, "object")
        doc = _FakeDoc()
        monkeypatch.setattr(node_preview, "_session_doc", lambda: doc)
        monkeypatch.setattr(node_preview, "set_curdoc", lambda doc: contextlib.nullcontext())
        monkeypatch.setattr(pn.state, "execute", lambda callback, schedule=None: callback())
        slider = editor._component_instances[ids["slider"]]
        for value in np.linspace(0, 3, 40):
            slider.value = float(value)
        assert events == []
        while doc.timeouts:
            doc.timeouts.pop()[0]()
        assert len(events) == 1
        assert "1 row, 4 columns" in pane.object
