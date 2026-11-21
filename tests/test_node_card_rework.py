"""Tests for stacked-port node cards, preview kinds, folding, collapse, pop-out and port colors."""

import contextlib
import itertools
import threading
import time
import typing as t
from functools import partial

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import (
    DashboardModel,
    MemoryDashboardStore,
    port_type_color,
    register,
    register_port_type,
)
from panel_flowdash.agent.yaml_format import FORMAT_VERSION, dashboard_from_yaml, dashboard_to_yaml
from panel_flowdash.dashboard_store import MODEL_VERSION
from panel_flowdash.editor import FlowDash
from panel_flowdash.port_types import _ALIASES, _COLORS, _SUPERTYPES
from panel_flowdash.ui import node_card as node_card_module, node_preview as node_preview_module
from panel_flowdash.ui.node_card import NodeCard
from panel_flowdash.ui.node_preview import (
    HEIGHTS,
    MAX_ROWS,
    build_preview,
    hook_preview,
    render_vega_lite_svg,
    sanitize_svg,
)


class _LazyPipeline:
    """Opaque lazy pipeline stand-in whose str() is a Python repr."""

    def __repr__(self):
        return "<Pipeline turbines_by_year>"


@register(page=False, component=True, title="Lumen SQL", icon="code")
class MultiOutputSQL(Viewer):
    source = param.Parameter(default=None, metadata={"flowdash": {"required": True}})

    @param.output(param.Parameter)
    @param.depends("source")
    def pipeline(self):
        return _LazyPipeline()

    @param.output(param.DataFrame)
    @param.depends("source")
    def data(self):
        return pd.DataFrame(
            {
                "p_year": [2014, 2015, 2016, 2017, 2018],
                "turbines": [2470, 3712, 3820, 3302, 3511],
                "capacity_mw": [4854, 8598, 8203, 7017, 7588],
                "extra": ["a", "b", "c", "d", "e"],
            }
        )

    def __panel__(self):
        return "sql"


@register(page=False, component=True, title="Output Aware Hook", icon="code")
class OutputAwareHook(Viewer):
    @param.output(param.Parameter)
    def pipeline(self):
        return _LazyPipeline()

    @param.output(param.DataFrame)
    def data(self):
        return pd.DataFrame({"a": [1, 2]})

    def __flowdash_preview__(self, output=None):
        if output == "pipeline":
            return "Lazy SQL query on turbines"
        return pd.DataFrame({"year": [2020, 2021], "mw": [100, 200]})

    def __panel__(self):
        return "hook"


@register(page=False, component=True, title="Join State", icon="merge")
class ManyOptionalInputs(Viewer):
    turbines = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )
    population = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )
    opt_a = param.String(default=None, allow_None=True)
    opt_b = param.List(default=None, allow_None=True)
    opt_c = param.Number(default=None, allow_None=True)

    @param.output(param.DataFrame)
    @param.depends("turbines")
    def joined(self):
        return pd.DataFrame({"state": ["TX", "IA"], "mw": [12000, 8000]})

    def __panel__(self):
        return "join"


@register(page=False, component=True, title="KPI Card", icon="speed")
class KPIComponent(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    def __flowdash_preview__(self):
        return {
            "__flowdash_preview_kind__": "kpi",
            "value": "150.4",
            "unit": "GW",
            "label": "Installed capacity, all years",
            "delta": "+6.5 GW in 2023",
            "caption": "sum(capacity_mw)",
        }

    def __panel__(self):
        return "kpi"


@register(page=False, component=True, title="Vega View", icon="bar_chart", config=["spec"])
class VegaView(Viewer):
    data = param.DataFrame(default=None, allow_None=True)
    spec = param.Dict(
        default={
            "mark": {"type": "line", "point": True},
            "encoding": {"x": {"field": "year"}, "y": {"field": "mw"}},
        },
        metadata={"flowdash": {"widget": "code", "language": "json"}},
    )

    def __flowdash_preview__(self):
        df = pd.DataFrame({"year": [2018, 2019, 2020, 2021], "mw": [4854, 8598, 16913, 13413]})
        return {
            "__flowdash_preview_kind__": "chart",
            "format": "vega-lite",
            "spec": self.spec,
            "data": df,
            "caption": "line · 4 points",
        }

    def __panel__(self):
        return "vega"


@register(page=False, component=True, title="Source", icon="storage")
class SimpleSource(Viewer):
    @param.output(param.DataFrame)
    def table(self):
        return pd.DataFrame({"x": range(120), "y": range(120)})

    def __panel__(self):
        return "src"


COMPONENTS = {
    "Demo/src": SimpleSource,
    "Demo/sql": MultiOutputSQL,
    "Demo/hook": OutputAwareHook,
    "Demo/join": ManyOptionalInputs,
    "Demo/kpi": KPIComponent,
    "Demo/vega": VegaView,
}


def _card(editor, instance_id) -> NodeCard:
    return next(n["view"] for n in editor._flow.nodes if n["id"] == instance_id)


def _click(editor, instance_id, action, port=""):
    """Act as the card bridge does for a click on an action inside a card."""
    editor._on_card_bridge_action({"node_id": instance_id, "action": action, "port": port})


@pytest.fixture
def restore_port_registry():
    aliases = dict(_ALIASES)
    supertypes = {k: set(v) for k, v in _SUPERTYPES.items()}
    colors = dict(_COLORS)
    yield
    _ALIASES.clear()
    _ALIASES.update(aliases)
    _SUPERTYPES.clear()
    _SUPERTYPES.update(supertypes)
    _COLORS.clear()
    _COLORS.update(colors)


class TestPortTypeColors:
    def test_builtin_light_and_dark_colors(self):
        assert port_type_color("DataFrame") == "#2A9D8F"
        assert port_type_color("DataFrame", dark=True) == "#3FB8A9"
        assert port_type_color("pd.DataFrame") == "#2A9D8F"
        assert port_type_color("list") == "#8E24AA"
        assert port_type_color("list", dark=True) == "#CE93D8"
        assert port_type_color("any") == "#8A8F98"
        assert port_type_color(None) == "#8A8F98"
        assert port_type_color("lumen.source") == "#6D4C41"
        assert port_type_color("lumen.pipeline") == "#0072B5"

    def test_custom_color_and_subtype_inheritance(self, restore_port_registry):
        register_port_type("GeoTable", color="#E65100")
        register_port_type("RasterTable", subtype_of=["GeoTable"])
        assert port_type_color("GeoTable") == "#E65100"
        assert port_type_color("RasterTable") == "#E65100"

    def test_unknown_type_has_deterministic_fallback(self):
        c1 = port_type_color("CustomThing")
        c2 = port_type_color("customthing")
        assert c1 == c2 and c1.startswith("#")


class TestPreviewKinds:
    def test_table_uses_four_rows_and_extra_columns_footer(self):
        df = pd.DataFrame({f"c{i}": range(10) for i in range(6)})
        pv = build_preview(df, "rows")
        assert MAX_ROWS == 4
        assert pv.kind == "table"
        assert pv.html.count("<tr>") == 5
        assert "+3 columns" in pv.html
        assert 'data-fd-action="popout"' in pv.html

    def test_kpi_dict_and_panel_indicators(self):
        pv = build_preview(
            {
                "__flowdash_preview_kind__": "kpi",
                "value": "150.4",
                "unit": "GW",
                "label": "Installed capacity",
                "delta": "+6.5 GW",
                "caption": "sum(capacity_mw)",
            }
        )
        assert pv.kind == "kpi"
        assert pv.height == HEIGHTS["kpi"]
        assert "150.4" in pv.html and "GW" in pv.html and "fd-pos" in pv.html

        num = pn.indicators.Number(label="Total", value=42.5, format="{value:.1f} MW")
        pv_num = build_preview(num, "kpi")
        assert pv_num.kind == "kpi" and "42.5 MW" in pv_num.html

        trend = pn.indicators.Trend(label="Sales", data={"x": [1, 2, 3], "y": [10, 15, 12]})
        pv_trend = build_preview(trend, "trend")
        assert pv_trend.kind == "kpi" and "12" in pv_trend.html and "fd-neg" in pv_trend.html

    @pytest.mark.parametrize("mark", ["line", "area", "bar", "point", "rect"])
    def test_vega_lite_chart_svg_thumbnails(self, mark):
        df = pd.DataFrame({"x": ["A", "B", "C", "D"], "y": [10, 25, 18, 30], "z": [1, 2, 3, 4]})
        spec = {
            "mark": mark,
            "encoding": {
                "x": {"field": "x"},
                "y": {"field": "y"},
                "color": {"field": "z"},
            },
        }
        pv = build_preview(
            {"__flowdash_preview_kind__": "chart", "format": "vega-lite", "spec": spec, "data": df}
        )
        assert pv.kind == "chart"
        assert pv.height == HEIGHTS["chart"]
        assert "<svg " in pv.html

    def test_empty_vega_lite_gracefully_falls_back(self):
        svg, cap = render_vega_lite_svg({"mark": "line"}, [])
        assert "No data to plot" in svg and "line" in cap

    def test_inline_svg_is_sanitized(self):
        dirty = '<svg viewBox="0 0 10 10"><script>alert(1)</script><rect x="0" y="0" width="5" height="5" onclick="evil()"/></svg>'
        clean = sanitize_svg(dirty)
        assert "<script" not in clean and "onclick" not in clean and "<rect" in clean

    def test_hook_preview_passes_output_when_accepted(self):
        inst = OutputAwareHook()
        pv_pipe = hook_preview(inst, output="pipeline")
        pv_data = hook_preview(inst, output="data")
        assert pv_pipe.kind == "value" and "Lazy SQL query" in pv_pipe.html
        assert pv_data.kind == "table" and "2020" in pv_data.html


class TestCardBehavior:
    async def test_multi_output_defaults_to_dataframe_and_switches(self):
        editor = FlowDash(COMPONENTS, notifications=False, node_previews=True)
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        editor.connect(src, "table", sql, "source")

        sql_card = _card(editor, sql)
        assert sql_card.title == "turbines_by_year"
        assert sql_card.subtitle == "Lumen SQL"
        assert sql_card.active_output == "data"
        assert sql_card.preview.kind == "table"
        assert (
            'class="fd-port fd-out fd-card-action fd-pv-on" data-port="data"'
            in sql_card.__panel__().object
        )

        # Switch preview to pipeline via action
        _click(editor, sql, "select_output", "pipeline")
        assert sql_card.preview_output == "pipeline"
        assert sql_card.active_output == "pipeline"
        assert sql_card.preview.kind == "value"
        assert (
            'class="fd-port fd-out fd-card-action fd-pv-on" data-port="pipeline"'
            in sql_card.__panel__().object
        )

    async def test_unconnected_optional_inputs_fold_and_connected_stay_visible(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        src = editor.add_component("Demo/src", instance_id="src")
        join = editor.add_component("Demo/join", instance_id="state_join")
        editor.connect(src, "table", join, "turbines")

        join_card = _card(editor, join)
        markup = join_card.__panel__().object
        assert "3 optional inputs not connected" in markup
        assert 'data-port="opt_a"' not in markup
        # Required missing input gets missing=True handle metadata
        handles, _ = join_card.handle_layout()
        assert handles["in:turbines"]["missing"] is False
        assert "from src.table" in handles["in:turbines"]["tooltip"]
        assert handles["in:population"]["missing"] is True
        assert handles["in:opt_a"]["hidden"] is True

        # Unfold optional inputs
        _click(editor, join, "toggle_optional")
        assert join_card.show_optional is True
        assert 'data-port="opt_a"' in join_card.__panel__().object
        handles_open, _ = join_card.handle_layout()
        assert handles_open["in:opt_a"]["hidden"] is False
        assert handles_open["in:opt_a"]["optional"] is True

    async def test_collapse_and_collapse_all_update_height_and_handles(self):
        editor = FlowDash(COMPONENTS, notifications=False, node_previews=True)
        src = editor.add_component("Demo/src", instance_id="turbines")
        kpi = editor.add_component("Demo/kpi", instance_id="total_capacity")
        editor.connect(src, "table", kpi, "data")

        kpi_card = _card(editor, kpi)
        assert kpi_card.height > 44
        _click(editor, kpi, "toggle_collapse")
        assert kpi_card.collapsed is True
        assert kpi_card.height == 44
        handles, css_vars = kpi_card.handle_layout()
        assert handles["in:data"]["top"] == 22.0
        assert css_vars["--fd-port-in-0"] == "22.00px"
        assert "unfold_more" in kpi_card.__panel__().object

        # Collapse all toggle in status bar
        editor._status_bar.collapse_toggle.value = True
        assert _card(editor, src).collapsed is True
        assert _card(editor, kpi).collapsed is True
        editor._status_bar.collapse_toggle.value = False
        assert _card(editor, src).collapsed is False
        assert _card(editor, kpi).collapsed is False

    async def test_popout_shows_the_first_rows_and_a_chart(self, monkeypatch):
        shown = []
        monkeypatch.setattr(
            "panel_flowdash.editor.open_dialog",
            lambda title, content, width="sm", **_: shown.append((title, content, width)),
        )
        editor = FlowDash(COMPONENTS, notifications=False, node_previews=True)
        src = editor.add_component("Demo/src", instance_id="turbines")
        vega = editor.add_component("Demo/vega", instance_id="capacity_chart")
        editor.connect(src, "table", vega, "data")

        _click(editor, src, "popout")
        title, content, width = shown[-1]
        assert title == "turbines.table"
        assert width == "lg"
        (table,) = content.select(pn.widgets.Tabulator)
        assert len(table.value) == 100
        assert table.disabled is True
        assert table.pagination is None
        assert table.formatters["x"] == {"type": "plaintext"}

        _click(editor, vega, "popout")
        _, content, _ = shown[-1]
        assert content.select(pn.pane.Vega)

    async def test_preview_output_and_collapsed_persist_without_version_bump(self):
        assert MODEL_VERSION == 4
        assert FORMAT_VERSION == 1
        editor = FlowDash(
            COMPONENTS, notifications=False, node_previews=True, store=MemoryDashboardStore()
        )
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        editor.connect(src, "table", sql, "source")

        _click(editor, sql, "select_output", "pipeline")
        _click(editor, src, "toggle_collapse")

        model = editor.to_model()
        assert model.version == 4
        by_id = {i.instance_id: i for i in model.items}
        assert by_id[sql].preview_output == "pipeline"
        assert by_id[src].collapsed is True
        # Defaults omitted from dict
        assert "collapsed" not in by_id[sql].to_dict()
        assert "preview_output" not in by_id[src].to_dict()

        # Round-trip via YAML v1
        text = dashboard_to_yaml(model)
        assert "flowdash: 1" in text
        assert "preview_output: pipeline" in text
        assert "collapsed: true" in text
        reloaded = DashboardModel.from_dict(
            {"dashboard_id": "d1", "user_id": "u", **dashboard_from_yaml(text)}
        )
        editor2 = FlowDash(COMPONENTS, notifications=False, node_previews=True)
        editor2.load_model(reloaded)
        assert _card(editor2, sql).preview_output == "pipeline"
        assert _card(editor2, src).collapsed is True

        # Backward compatibility: v3 model without new fields loads cleanly
        v3 = DashboardModel.from_dict(
            {
                "version": 3,
                "dashboard_id": "old",
                "user_id": "u",
                "title": "Legacy",
                "items": [{"instance_id": "n1", "component_id": "Demo/src", "x": 0, "y": 0}],
            }
        )
        assert v3.items[0].preview_output is None and v3.items[0].collapsed is False


def _style(editor, instance_id) -> dict:
    node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
    return node.get("style") or {}


class TestPreviewSizing:
    def test_bar_thumbnail_draws_every_bar_inside_the_plot(self):
        import re

        years = list(range(1982, 2021))
        spec = {
            "mark": "bar",
            "encoding": {
                "x": {"field": "year", "type": "ordinal"},
                "y": {"field": "mw", "type": "quantitative"},
            },
        }
        svg, caption = render_vega_lite_svg(
            spec, pd.DataFrame({"year": years, "mw": [1.25**i for i in range(len(years))]})
        )
        bars = re.findall(r'<rect class="fd-ch-br" x="([\d.]+)" y="[\d.]+" width="([\d.]+)"', svg)
        assert len(bars) == len(years)
        assert caption == "bar · 39 bars"
        assert float(bars[-1][0]) + float(bars[-1][1]) <= 240 - 10 + 0.1
        assert "2020" in svg

    def test_bar_thumbnail_ticks_fit_between_the_bars(self):
        import re

        states = ["Texas", "Iowa", "Oklahoma", "Kansas", "Illinois", "California"] * 4
        states = [f"{name} {i}" for i, name in enumerate(states)]
        spec = {"mark": "bar", "encoding": {"x": {"field": "s"}, "y": {"field": "v"}}}
        svg, _ = render_vega_lite_svg(spec, pd.DataFrame({"s": states, "v": range(24)}))
        ticks = re.findall(r'<text class="fd-ch-txt" x="([\d.]+)"[^>]*>([^<]*)</text>', svg)
        assert len(ticks) >= 2
        char = node_preview_module._TICK_CHAR_W
        spans = [(float(x) - len(t) * char / 2, float(x) + len(t) * char / 2) for x, t in ticks]
        assert spans[0][0] >= 10 - 0.1 and spans[-1][1] <= 240 - 10 + 0.1
        assert all(a[1] < b[0] for a, b in itertools.pairwise(spans))
        assert ticks[0][1] == "Texas 0" and ticks[-1][1].startswith("Californ")

    def test_bar_thumbnail_ticks_thin_out_until_labels_differ(self):
        import re

        names = [f"State number {i}" for i in range(50)]
        spec = {"mark": "bar", "encoding": {"x": {"field": "s"}, "y": {"field": "v"}}}
        svg, _ = render_vega_lite_svg(spec, pd.DataFrame({"s": names, "v": range(50)}))
        labels = re.findall(r'<text class="fd-ch-txt"[^>]*>([^<]*)</text>', svg)
        assert len(labels) >= 2
        assert len(set(labels)) == len(labels)
        assert labels[0] == "State number 0" and labels[-1] == "State number 49"

    def test_short_table_preview_fits_its_rows(self):
        one = build_preview(pd.DataFrame({"a": [1], "b": ["x"]}), "table")
        full = build_preview(pd.DataFrame({"a": range(10)}), "table")
        assert one.height < full.height == HEIGHTS["table"]
        assert f"height: {one.height}px" in one.html

    def test_popout_chart_fills_the_dialog_in_the_thumbnail_colour(self):
        from panel_flowdash.editor import _popout_chart
        from panel_flowdash.ui.node_preview import CHART_COLOR

        pane = _popout_chart(
            {"spec": {"mark": "bar"}, "data": pd.DataFrame({"a": [1, 2], "b": [3, 4]})}
        )
        assert pane.object["width"] == "container"
        assert pane.object["autosize"]["type"] == "fit"
        assert pane.object["config"]["mark"]["color"] == CHART_COLOR

    def test_port_rows_carry_no_dead_tooltips(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        src = editor.add_component("Demo/src", instance_id="turbines")
        assert "fd-port fd-out" in _card(editor, src)._ports_html()
        assert 'title="' not in _card(editor, src)._ports_html()


class TestCardViewEdits:
    async def test_switching_the_output_is_undoable(self):
        editor = FlowDash(COMPONENTS, notifications=False, node_previews=True, history_coalesce=0)
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        editor.connect(src, "table", sql, "source")

        _click(editor, sql, "select_output", "pipeline")
        assert editor.undo()
        assert _card(editor, sql).preview_output is None
        assert _card(editor, sql).active_output == "data"
        assert editor.redo()
        assert _card(editor, sql).preview_output == "pipeline"

    async def test_collapsing_is_no_edit(self):
        editor = FlowDash(
            COMPONENTS, notifications=False, history_coalesce=0, store=MemoryDashboardStore()
        )
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        editor.connect(src, "table", sql, "source")
        editor.save()
        assert not editor.dirty
        connected = _style(editor, sql)

        _click(editor, sql, "toggle_collapse")
        assert _card(editor, sql).collapsed
        assert _style(editor, sql) != connected
        assert not editor.dirty
        editor._collapse_all_nodes(True)
        assert _card(editor, src).collapsed
        assert not editor.dirty

        # The last edit is still the connection, and undoing it leaves the cards as they are.
        assert editor.undo()
        assert not editor._dataflow_graph.edges
        assert _card(editor, src).collapsed and _card(editor, sql).collapsed

    async def test_collapse_toggle_names_what_it_does(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        src = editor.add_component("Demo/src", instance_id="turbines")
        toggle = editor._status_bar.collapse_toggle
        assert toggle.label == "Collapse all"
        assert toggle.description == "Collapse all nodes (C)"
        _click(editor, src, "toggle_collapse")
        assert toggle.value and toggle.description == "Expand all nodes (C)"
        assert toggle.label == "Collapse all"
        toggle.value = False
        assert not _card(editor, src).collapsed
        assert toggle.description == "Collapse all nodes (C)"
        assert toggle.label == "Collapse all"

    async def test_outlines_and_handle_positions_keep_each_other(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        outline = {"borderColor": "green", "boxShadow": "0 0 0 2px green"}

        editor._set_node_outline(sql, outline)
        editor.connect(src, "table", sql, "source")
        _click(editor, sql, "toggle_collapse")
        style = _style(editor, sql)
        assert outline.items() <= style.items()
        assert style["--fd-port-in-0"] == "22.00px"

        editor._set_node_outline(sql, None)
        style = _style(editor, sql)
        assert "borderColor" not in style
        assert style["--fd-port-in-0"] == "22.00px"

    async def test_a_collapsed_card_never_shows_a_repr(self):
        editor = FlowDash(COMPONENTS, notifications=False)
        src = editor.add_component("Demo/src", instance_id="turbines")
        sql = editor.add_component("Demo/sql", instance_id="turbines_by_year")
        editor.connect(src, "table", sql, "source")
        _click(editor, sql, "select_output", "pipeline")
        _click(editor, sql, "toggle_collapse")
        card = _card(editor, sql)
        assert card.summary == "_LazyPipeline"
        assert "&lt;Pipeline" not in card.__panel__().object


class _Doc:
    """A served document that runs next-tick callbacks when told to."""

    session_context = object()

    def __init__(self):
        self.ticks = []

    def add_next_tick_callback(self, callback):
        self.ticks.append(callback)


class SlowHook(Viewer):
    calls: t.ClassVar[list[str]] = []

    @param.output(param.DataFrame)
    def data(self):
        return pd.DataFrame({"a": [1]})

    def __flowdash_preview__(self):
        SlowHook.calls.append(threading.current_thread().name)
        return pd.DataFrame({"queried": [1, 2, 3]})

    def __panel__(self):
        return "slow"


class TestPreviewsOffTheEventLoop:
    async def test_a_served_card_calls_the_preview_hook_on_a_worker_thread(self, monkeypatch):
        doc = _Doc()
        editor = FlowDash({"Demo/slow": SlowHook}, notifications=False, node_previews=False)
        node = editor.add_component("Demo/slow")
        card = _card(editor, node)
        monkeypatch.setattr(NodeCard, "_document", lambda self: doc)
        SlowHook.calls.clear()

        card.show_preview = True
        assert card.preview is None
        wait = time.monotonic() + 5
        while not doc.ticks and time.monotonic() < wait:
            time.sleep(0.01)
        assert SlowHook.calls and SlowHook.calls[0].startswith("flowdash-preview")
        assert threading.current_thread().name not in SlowHook.calls
        monkeypatch.setattr(
            node_preview_module, "set_curdoc", lambda doc: contextlib.nullcontext()
        )
        doc.ticks.pop()()
        assert card.preview.kind == "table"
        assert "queried" in card.__panel__().object

    async def test_a_superseded_preview_is_dropped(self, monkeypatch):
        doc = _Doc()
        editor = FlowDash({"Demo/slow": SlowHook}, notifications=False, node_previews=False)
        card = _card(editor, editor.add_component("Demo/slow"))
        monkeypatch.setattr(NodeCard, "_document", lambda self: doc)
        monkeypatch.setattr(
            node_card_module,
            "run_off_loop",
            lambda work, then, doc: doc.add_next_tick_callback(partial(then, work())),
        )
        card.show_preview = True
        card.collapsed = True
        for tick in doc.ticks:
            tick()
        assert card.preview is None
        assert card.height == 44


async def test_collapsed_handles_too_close_to_tell_apart_share_one_point():
    editor = FlowDash(COMPONENTS, notifications=False)
    join = editor.add_component("Demo/join", instance_id="state_join")
    _click(editor, join, "toggle_collapse")
    handles, _ = _card(editor, join).handle_layout()
    inputs = {key: meta["top"] for key, meta in handles.items() if key.startswith("in:")}
    assert len(inputs) == 5 and set(inputs.values()) == {22.0}
    assert all(meta["top"] == 22.0 for key, meta in handles.items() if key.startswith("out:"))
