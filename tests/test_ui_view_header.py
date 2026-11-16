"""Tests for the view-mode header: description, health, refresh period, controls bar, pipeline."""

import gc
import re
import sys
import weakref

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.io.state import set_curdoc
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.app import DASH_ROUTE_PREFIX, FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.dataflow_engine import InputFailure
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.controls_bar import compact_inputs
from panel_flowdash.ui.dashboard_bar import format_period
from panel_flowdash.ui.issue_banner import ISSUE_CLASS
from panel_flowdash.ui.pipeline import Stage, pipeline_stages
from tests.test_editor import CHART, COMPONENTS, SELECTOR, SHOUTER, Shouter, _session_doc
from tests.test_navigation import FakeLocation


@register(page=False, component=True, sidebar=True, title="Filter", provides=["ticker"])
def side_filter(config):
    return "filter"


SIDE = "Demo/filter"


class Raiser(Viewer):
    """Fails to publish its output for one ticker."""

    ticker = param.String(default="")

    @param.output(param.String)
    @param.depends("ticker")
    def checked(self):
        if self.ticker == "bad":
            raise ValueError("bad ticker")
        return self.ticker

    def __panel__(self):
        return "raiser"


RAISER = "Demo/raiser"


@pytest.fixture
def editor():
    return FlowDash({**COMPONENTS, SIDE: side_filter}, notifications=False)


def _view(editor):
    editor.editable = False
    return editor._dashboard_bar


def _supply(editor, instance_id, value="AAPL"):
    editor._dataflow_graph.get_state(instance_id).ticker = value


def _fail(editor, instance_id, source_id, port="ticker"):
    failures = editor._dataflow_graph.get_failures(instance_id)
    failures.failed = {port: InputFailure(source_id, "ticker", None)}


class TestDescription:
    async def test_behind_an_info_button_next_to_the_title(self, editor):
        editor.settings.description = "What the plant produced today."
        bar = _view(editor)
        assert bar.info_button.description == "What the plant produced today."
        assert bar.lead.objects[:2] == [bar.title, bar.info_button]

    async def test_left_out_when_empty(self, editor):
        bar = _view(editor)
        assert bar.info_button not in bar.lead.objects

    async def test_not_shown_in_edit_mode(self, editor):
        editor.settings.description = "Shown only when viewing."
        assert editor._dashboard_bar.info_button not in editor._dashboard_bar.lead

    async def test_is_a_saved_setting(self, editor):
        editor.new_dashboard("Plant")
        editor.settings.description = "Saved with the dashboard."
        assert editor.save().settings["description"] == "Saved with the dashboard."


class TestRefreshMeta:
    @pytest.mark.parametrize(
        ("seconds", "text"),
        [
            (60, "1 min"),
            (300, "5 min"),
            (900, "15 min"),
            (3600, "1 hour"),
            (7200, "2 hours"),
            (45, "45 s"),
        ],
    )
    async def test_format_period(self, seconds, text):
        assert format_period(seconds) == text

    async def test_label_names_the_period(self, editor):
        editor.settings.refresh_period = 900
        bar = _view(editor)
        bar.stamp_refresh()
        assert re.fullmatch(r"Refreshed \d\d:\d\d, auto-refresh every 15 min", bar.refresh_text)
        assert bar.refresh_button.description == f"Refresh now. {bar.refresh_text}."

    async def test_label_without_a_period(self, editor):
        bar = _view(editor)
        bar.stamp_refresh()
        assert re.fullmatch(r"Refreshed \d\d:\d\d", bar.refresh_text)

    async def test_period_menu_follows_the_setting(self, editor):
        bar = _view(editor)
        assert bar.refresh_period.label == "Off"
        editor.settings.refresh_period = 300
        assert bar.refresh_period.label == "5 min"
        checked = [
            i["label"] for i in bar.refresh_period.items if i["icon"] == "radio_button_checked"
        ]
        assert checked == ["Every 5 min"]

    async def test_choosing_a_period_applies_it_without_marking_changes(self, editor):
        bar = _view(editor)
        item = next(i for i in bar.refresh_period.items if i["label"] == "Every hour")
        bar._on_period_chosen(item)
        assert editor.settings.refresh_period == 3600
        assert not editor.dirty
        assert bar.refresh_text.endswith("auto-refresh every 1 hour")

    async def test_split_button_sits_next_to_refresh(self, editor):
        bar = _view(editor)
        actions = bar._actions.objects
        assert actions.index(bar.refresh_period) == actions.index(bar.refresh_button) + 1


class TestHealth:
    async def test_all_tiles_up_to_date(self, editor):
        editor.add_component(SELECTOR)
        editor.add_component(SELECTOR)
        bar = _view(editor)
        assert bar.health_chip.label == "2/2 up to date"
        assert bar.health_popover.__panel__() in bar.lead.objects

    async def test_one_tile(self, editor):
        editor.add_component(SELECTOR)
        assert _view(editor).health_chip.label == "1/1 up to date"

    async def test_no_chip_without_tiles(self, editor):
        bar = _view(editor)
        assert bar.health_popover.__panel__() not in bar.lead.objects

    async def test_unconnected_required_input_waits(self, editor):
        editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        bar = _view(editor)
        assert bar.health.waiting == [chart]
        assert bar.health_chip.label == "1 waiting"

    async def test_a_value_arriving_clears_the_wait(self, editor):
        editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        bar = _view(editor)
        _supply(editor, chart)
        assert bar.health.waiting == []
        assert bar.health_chip.label == "2/2 up to date"

    async def test_failures_are_counted(self, editor):
        source = editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        bar = _view(editor)
        _fail(editor, chart, source)
        assert bar.health.failed == [chart]
        assert bar.health.origins == [source]
        assert bar.health_chip.label == "1 failed"
        assert bar.health.status(source) == "failed"

    async def test_failed_and_waiting(self, editor):
        source = editor.add_component(SELECTOR)
        failed = editor.add_component(CHART)
        editor.add_component(CHART)
        bar = _view(editor)
        _fail(editor, failed, source)
        assert bar.health_chip.label == "1 failed \u00b7 1 waiting"

    async def test_sidebar_components_are_not_tiles(self, editor):
        editor.add_component(SIDE)
        editor.add_component(SELECTOR)
        assert _view(editor).health.tiles == [editor._tile_items[1]["instance_id"]]

    async def test_removed_components_are_forgotten(self, editor):
        editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        bar = _view(editor)
        editor.remove_component(chart)
        assert bar.health.waiting == []
        assert bar.health_chip.label == "1/1 up to date"

    async def test_clearing_forgets_everything(self, editor):
        editor.add_component(CHART)
        bar = _view(editor)
        editor.clear()
        assert bar.health.tiles == []
        assert bar.health.waiting == []

    async def test_graph_nodes_do_not_keep_the_editor_alive(self):
        doc, _ = _session_doc()
        with set_curdoc(doc):
            editor = FlowDash(COMPONENTS, notifications=False)
            chart = editor.add_component(CHART)
        # A session can hold a node's state or failures past the editor's lifetime.
        graph = editor._dataflow_graph
        held = (graph.get_state(chart), graph.get_failures(chart))
        ref = weakref.ref(editor)
        del editor, graph
        gc.collect()
        assert ref() is None
        assert None not in held


@pytest.mark.failures
class TestPropagatedFailures:
    async def test_a_failing_output_fails_the_tile_downstream(self):
        editor = FlowDash(
            {RAISER: Raiser, SHOUTER: Shouter}, notifications=False, propagate_failures=True
        )
        raiser = editor.add_component(RAISER)
        shouter = editor.add_component(SHOUTER)
        editor.connect(raiser, "checked", shouter, "ticker")
        bar = _view(editor)
        editor._dataflow_graph.get_state(raiser).ticker = "bad"
        assert bar.health.failed == [shouter]
        assert bar.health.origins == [raiser]
        assert bar.health_chip.label == "1 failed"
        stages = editor._dashboard_bar.health_popover.pipeline.stages
        assert [(s.label, s.status) for s in stages] == [
            ("raiser", "failed"),
            ("shouter", "failed"),
        ]
        editor._dataflow_graph.get_state(raiser).ticker = "fine"
        assert bar.health.failed == []
        assert bar.health_chip.label == "2/2 up to date"


class TestIssueBanner:
    @pytest.mark.parametrize(
        ("failed", "waiting", "total", "lead", "rest"),
        [
            (
                1,
                1,
                5,
                "1 tile failed and 1 is waiting for input.",
                "Other tiles show current data.",
            ),
            (2, 3, 5, "2 tiles failed and 3 are waiting for input.", ""),
            (1, 0, 3, "1 tile failed.", "Other tiles show current data."),
            (0, 2, 3, "2 tiles are waiting for input.", "Other tiles show current data."),
            (0, 0, 3, "", ""),
        ],
    )
    async def test_sentence(self, editor, failed, waiting, total, lead, rest):
        health = editor._dashboard_bar.health
        ids = [f"t{i}" for i in range(total)]
        health.param.update(tiles=ids, failed=ids[:failed], waiting=ids[failed : failed + waiting])
        assert health.sentence() == (lead, rest)

    async def test_shown_only_with_issues(self, editor):
        editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        banner = editor._dashboard_bar.health_popover.banner
        editor.editable = False
        assert banner._banner.visible
        assert banner._lead.object == "1 tile is waiting for input."
        _supply(editor, chart)
        assert not banner._banner.visible

    async def test_show_issues_marks_the_affected_tiles(self, editor):
        source = editor.add_component(SELECTOR)
        failed = editor.add_component(CHART)
        waiting = editor.add_component(CHART)
        editor.editable = False
        _fail(editor, failed, source)
        messages = []
        banner = editor._dashboard_bar.health_popover.banner
        banner._focus._send_msg = messages.append
        assert banner.show_issues() == [failed, waiting]
        grid = editor._tile_grid
        marked = [
            i
            for i, view in zip(editor._grid_ids, grid.objects, strict=True)
            if ISSUE_CLASS in view.css_classes
        ]
        assert marked == [failed, waiting]
        assert messages == [{"cls": ISSUE_CLASS}]

    async def test_show_issues_unmarks_resolved_tiles(self, editor):
        source = editor.add_component(SELECTOR)
        chart = editor.add_component(CHART)
        editor.editable = False
        banner = editor._dashboard_bar.health_popover.banner
        banner._focus._send_msg = lambda msg: None
        banner.show_issues()
        _supply(editor, chart)
        _fail(editor, chart, source)
        banner.show_issues()
        editor._dataflow_graph.get_failures(chart).failed = {}
        assert banner.show_issues() == []
        _fail(editor, chart, source)
        banner.show_issues()
        index = editor._grid_ids.index(chart)
        assert ISSUE_CLASS in editor._tile_grid.objects[index].css_classes
        assert ISSUE_CLASS not in editor._tile_grid.objects[1 - index].css_classes


class TestPipelineStages:
    @staticmethod
    def _edges(*pairs):
        return [{"source": s, "target": t} for s, t in pairs]

    async def test_source_transform_views(self):
        edges = self._edges(("src", "flt"), ("flt", "a"), ("flt", "b"), ("flt", "c"))
        stages = pipeline_stages(edges, {"src": "Source", "flt": "Filter"}, lambda _: "ok")
        assert [s.label for s in stages] == ["Source", "Filter", "3 views"]
        assert stages[-1].nodes == ("a", "b", "c")

    async def test_chains_keep_their_order(self):
        edges = self._edges(("src", "flt"), ("flt", "gate"), ("gate", "chart"), ("flt", "table"))
        titles = {"src": "Source", "flt": "Filter", "gate": "Gate", "chart": "Chart"}
        stages = pipeline_stages(edges, titles, lambda _: "ok")
        assert [s.label for s in stages] == ["Source", "Filter", "Gate", "2 views"]

    async def test_one_view_keeps_its_title(self):
        stages = pipeline_stages(self._edges(("a", "b")), {"a": "A", "b": "B"}, lambda _: "ok")
        assert stages == [Stage("A", "ok", ("a",)), Stage("B", "ok", ("b",))]

    async def test_worst_status_wins(self):
        edges = self._edges(("src", "a"), ("src", "b"), ("src", "c"))
        status = {"a": "waiting", "b": "failed"}.get
        stages = pipeline_stages(edges, {}, lambda node: status(node, "ok"))
        assert [s.status for s in stages] == ["ok", "failed"]

    async def test_many_sources_collapse(self):
        edges = self._edges(("a", "v"), ("b", "v"), ("c", "v"))
        stages = pipeline_stages(edges, {}, lambda _: "ok")
        assert [s.label for s in stages] == ["3 sources", "v"]

    async def test_nothing_wired(self):
        assert pipeline_stages([], {}, lambda _: "ok") == []


class TestControlsBar:
    async def test_sidebar_components_show_in_the_controls_bar(self, editor):
        editor.add_component(SIDE)
        editor.add_component(SELECTOR)
        editor.editable = False
        controls = editor._view_controls
        assert controls.controls == editor.sidebar
        assert controls._row.visible
        assert editor._workspace_area.objects[0] is controls._row

    async def test_hidden_when_empty(self, editor):
        editor.add_component(SELECTOR)
        editor.editable = False
        assert not editor._view_controls._row.visible

    async def test_shown_while_laying_out(self, editor):
        editor.add_component(SIDE)
        editor.mode = "dashboard"
        assert editor._view_controls._row.visible

    async def test_switched_off(self, editor):
        editor.add_component(SIDE)
        editor.editable = False
        editor.controls_bar = False
        assert editor._view_controls.controls == []
        assert len(editor.sidebar) == 1

    async def test_pipeline_in_view_mode(self, editor):
        side = editor.add_component(SIDE)
        chart = editor.add_component(CHART)
        editor.connect(side, "ticker", chart, "ticker")
        editor.editable = False
        stages = editor._dashboard_bar.health_popover.pipeline.stages
        assert [s.label for s in stages] == ["Filter", "Chart"]

    async def test_pipeline_follows_failures(self, editor):
        side = editor.add_component(SIDE)
        chart = editor.add_component(CHART)
        editor.connect(side, "ticker", chart, "ticker")
        editor.editable = False
        _fail(editor, chart, side)
        stages = editor._dashboard_bar.health_popover.pipeline.stages
        assert [s.status for s in stages] == ["failed", "failed"]

    async def test_controls_are_not_rewrapped(self, editor):
        editor.add_component(SIDE)
        editor.editable = False
        wrapper = editor._view_controls._controls[0]
        editor.add_component(SELECTOR)
        assert editor._view_controls._controls[0] is wrapper

    async def test_inputs_use_the_small_size(self):
        text, select = pmui.TextInput(label="Find"), pmui.Select(options=["a", "b"])
        compact_inputs(pn.Column(text, select))
        assert text.size == "small"
        assert select.visual_size == "small"
        assert select.size == 1


class TestControlsPosition:
    async def test_defaults_to_the_top_row(self, editor):
        assert editor.controls_position == "top"
        assert editor.settings.controls_position == "top"
        assert editor._view_controls.position == "top"

    async def test_left_panel_beside_the_tiles(self):
        editor = FlowDash(
            {**COMPONENTS, SIDE: side_filter}, notifications=False, controls_position="left"
        )
        editor.add_component(SIDE)
        editor.editable = False
        controls = editor._view_controls
        assert not controls._row.visible
        assert controls._panel.visible
        assert controls._panel.objects[1:] == list(controls._wrappers.values())
        row = editor._workspace_area.objects[0]
        assert row.objects[:2] == [controls._panel, controls._strip]
        assert editor._tile_grid in row.objects[2]

    async def test_left_panel_collapses_to_a_strip(self):
        editor = FlowDash(
            {**COMPONENTS, SIDE: side_filter}, notifications=False, controls_position="left"
        )
        editor.add_component(SIDE)
        editor.editable = False
        controls = editor._view_controls
        controls._hide.clicks += 1
        assert controls.collapsed
        assert not controls._panel.visible
        assert controls._strip.visible
        controls._show.clicks += 1
        assert controls._panel.visible

    async def test_a_dashboard_setting_overrides_the_default(self, editor):
        editor.add_component(SIDE)
        editor.editable = False
        editor.settings.controls_position = "left"
        assert editor._view_controls.position == "left"
        assert editor._view_controls._panel.visible
        assert not editor._view_controls._row.visible
        editor.settings.controls_position = "top"
        assert editor._workspace_area.objects[0] is editor._view_controls._row

    async def test_saved_with_the_dashboard(self, editor):
        editor.new_dashboard("Plant")
        editor.settings.controls_position = "left"
        assert editor.save().settings["controls_position"] == "left"


def _create_project(tmp_path):
    section = tmp_path / "Plant"
    section.mkdir()
    (section / "__init__.py").write_text("")
    (section / "picker.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, sidebar=True, provides=['unit'])\n"
        "def app(config):\n"
        "    return 'picker'\n"
    )
    (section / "chart.py").write_text(
        "from panel_flowdash import register\n\n"
        "@register(page=False, component=True, requires=['unit'])\n"
        "def app(config):\n"
        "    return 'chart'\n"
    )


@pytest.fixture
async def make_app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    previous = pn.state._location
    pn.state._location = FakeLocation()
    store = DashboardStore(tmp_path / "test.db")

    async def make(**params):
        app = FlowDashApp(project_dir=tmp_path, store=store, **params)
        await app._ensure_components_loaded()
        app.notices = []
        app._notify = lambda severity, message, **kw: app.notices.append((severity, message))
        return app

    try:
        yield make
    finally:
        pn.state._location = previous
        sys.path.remove(str(tmp_path))


def _open_with_picker(app):
    editor = app._editor
    editor.new_dashboard("Plant")
    editor.add_component("Plant/picker")
    model = editor.save()
    pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
    app._load_dashboard_sync(model.dashboard_id, edit=False)
    return model


class TestApp:
    async def test_controls_stay_out_of_the_page_sidebar(self, make_app):
        app = await make_app()
        _open_with_picker(app)
        assert len(app._editor.sidebar) == 1
        assert app._sidebar_container.objects == []
        assert not app._page.sidebar_open
        assert app._editor._view_controls.controls == app._editor.sidebar

    async def test_page_sidebar_when_the_controls_bar_is_off(self, make_app):
        app = await make_app(editor_options={"controls_bar": False})
        _open_with_picker(app)
        assert app._sidebar_container.objects == list(app._editor.sidebar)
        assert app._page.sidebar_open

    async def test_loading_raises_no_notification(self, make_app):
        app = await make_app()
        _open_with_picker(app)
        assert not [n for n in app.notices if n[1].startswith("Loaded dashboard")]

    async def test_show_in_editor_opens_the_wiring_canvas(self, make_app):
        app = await make_app()
        editor = app._editor
        editor.new_dashboard("Plant")
        instance_id = editor.add_component("Plant/chart")
        model = editor.save()
        pn.state.location.pathname = f"{DASH_ROUTE_PREFIX}{model.dashboard_id}"
        app._load_dashboard_sync(model.dashboard_id, edit=False)
        editor.mode = "dashboard"
        assert editor.show_in_editor == app._show_in_editor
        assert editor._inspect_tiles[instance_id].more_menu.visible

        editor._on_show_in_editor(instance_id)

        assert editor.editable
        assert editor.mode == "wiring"
        assert pn.state.location.search == "?edit=true"

    async def test_contextbar_only_reserves_space_when_configured(self, make_app):
        app = await make_app()
        assert app._page.contextbar_variant == "temporary"
        app.contextbar = [pn.pane.Markdown("Assistant")]
        assert app._page.contextbar_variant == "persistent"
        app.contextbar = []
        assert app._page.contextbar_variant == "temporary"
        configured = await make_app(page_options={"contextbar_variant": "persistent"})
        assert configured._page.contextbar_variant == "persistent"
