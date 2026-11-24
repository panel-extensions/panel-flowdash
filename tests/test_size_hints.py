"""Tests for the ``default_size``, ``min_size`` and ``max_size`` tile hints."""

import logging

import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.agent import DashboardOps
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import FILL_HEIGHT, FlowDash, _fills_tile


@register(
    page=False,
    component=True,
    title="Sized",
    default_size={"width": 600, "height": 300},
    min_size={"width": 240, "height": 200},
    max_size={"width": 960, "height": 500},
)
class Sized(Viewer):
    value = param.Integer(default=0)

    @param.output(param.Integer)
    def doubled(self):
        return self.value * 2

    def __panel__(self):
        return pn.pane.Markdown("sized")


@register(page=False, component=True, title="Plain")
def plain(config):
    return pn.pane.Markdown("plain")


@register(page=False, component=True, title="Legacy", default_size={"w": 6, "h": 4})
def legacy(config):
    return pn.pane.Markdown("legacy")


SIZED = "Test/sized"
PLAIN = "Test/plain"
LEGACY = "Test/legacy"


def _editor(**kwargs):
    return FlowDash({SIZED: Sized, PLAIN: plain, LEGACY: legacy}, notifications=False, **kwargs)


def _model(items, tile_layout, **kwargs):
    return DashboardModel(
        dashboard_id="d1",
        user_id="alice",
        title="Sized",
        items=[DashboardItem(instance_id=i, component_id=c) for i, c in items],
        tile_layout=tile_layout,
        **kwargs,
    )


class TestSpec:
    def test_all_hints_reach_the_spec(self):
        spec = _editor()._component_specs[SIZED]

        assert spec.default_size == {"width": 600, "height": 300}
        assert spec.min_size == {"width": 240, "height": 200}
        assert spec.max_size == {"width": 960, "height": 500}

    def test_unsupported_keys_are_dropped_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_flowdash"):
            spec = _editor()._component_specs[LEGACY]

        assert spec.default_size is None
        assert "default_size['w']" in caplog.text


class TestDefaultSize:
    def test_new_tile_starts_at_its_default_size(self):
        editor = _editor()
        editor.add_component(PLAIN)
        editor.add_component(SIZED)
        editor.mode = "dashboard"

        # 600px of the 1200px the grid assumes without a reference width.
        assert editor._tile_grid.layout == [{}, {"width": 50.0, "height": 300}]

    def test_default_width_is_relative_to_the_reference_width(self):
        editor = _editor()
        editor.load_model(_model([("s", SIZED)], [], reference_width=800))
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [{"width": 75.0, "height": 300}]

    def test_stored_entry_wins_over_the_default(self):
        editor = _editor()
        stored = {"index": 0, "width": 40, "height": 250, "visible": True}
        editor.load_model(_model([("s", SIZED)], [{**stored, "instance_id": "s"}]))
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [stored]

    def test_default_size_is_saved_with_the_layout(self):
        editor = _editor()
        instance_id = editor.add_component(SIZED)
        editor.mode = "dashboard"

        assert editor.to_model().tile_layout == [
            {"width": 50.0, "height": 300, "instance_id": instance_id}
        ]


class TestBounds:
    def test_layout_change_is_kept_within_bounds(self):
        editor = _editor()
        editor.add_component(SIZED)
        editor.add_component(PLAIN)
        editor.mode = "dashboard"

        editor._tile_grid.param.update(
            layout=[
                {"index": 0, "width": 10, "height": 1000, "visible": True},
                {"index": 1, "width": 10, "height": 1000, "visible": True},
            ],
            reference_width=1200,
        )

        assert editor._tile_grid.layout == [
            {"index": 0, "width": 20.0, "height": 500, "visible": True},
            {"index": 1, "width": 10, "height": 1000, "visible": True},
        ]

    def test_stored_layout_is_kept_within_bounds_on_load(self):
        editor = _editor()
        model = _model(
            [("s", SIZED)],
            [{"index": 0, "width": 100, "height": 50, "visible": True, "instance_id": "s"}],
            reference_width=1600,
        )
        editor.load_model(model)
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [
            {"index": 0, "width": 60.0, "height": 200, "visible": True}
        ]

    def test_entries_without_sizes_are_left_alone(self):
        editor = _editor()
        editor.load_model(_model([("s", SIZED)], [{"i": "s", "x": 0, "instance_id": "s"}]))
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [{"i": "s", "x": 0}]

    def test_tile_carries_its_width_bounds(self):
        editor = _editor()
        editor.add_component(SIZED)
        editor.add_component(PLAIN)
        editor.mode = "dashboard"

        sized, plain_view = editor._tile_grid.objects
        assert (sized.min_width, sized.max_width) == (240, 960)
        assert not sized.inspect_button.visible
        assert isinstance(plain_view.view, pn.pane.Markdown)
        assert plain_view.min_width is None

    def test_bounded_tile_keeps_its_inspect_button(self):
        editor = _editor(inspect_outputs=True)
        editor.add_component(SIZED)
        editor.editable = False
        editor.mode = "dashboard"

        (tile,) = editor._tile_grid.objects
        assert (tile.min_width, tile.max_width) == (240, 960)
        assert tile.inspect_button.visible


VEGA = {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "mark": "bar"}
CONTAINER = {**VEGA, "width": "container", "height": "container"}


@register(page=False, component=True, title="Filling")
def filling(config):
    return pn.pane.Vega(CONTAINER)


@register(page=False, component=True, title="Filling bounded", max_size={"height": 300})
def filling_bounded(config):
    return pn.Column(pn.pane.Markdown("a"), pn.pane.Vega(CONTAINER))


@register(page=False, component=True, title="Rendered")
class Rendered(Viewer):
    shown = param.Boolean(default=False)

    @param.depends("shown")
    def _render(self):
        return pn.pane.Vega(CONTAINER) if self.shown else pn.pane.Markdown("Waiting")

    def __panel__(self):
        return pn.panel(self._render)


FILLING = "Test/filling"
FILLING_BOUNDED = "Test/filling_bounded"
RENDERED = "Test/rendered"


def _fill_editor():
    return FlowDash(
        {
            SIZED: Sized,
            PLAIN: plain,
            FILLING: filling,
            FILLING_BOUNDED: filling_bounded,
            RENDERED: Rendered,
        },
        notifications=False,
    )


class TestFillHeight:
    def test_a_view_sized_by_its_tile_starts_at_the_fill_height(self):
        editor = _fill_editor()
        editor.add_component(PLAIN)
        editor.add_component(FILLING)
        editor.add_component(SIZED)
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [
            {},
            {"height": FILL_HEIGHT},
            {"width": 50.0, "height": 300},
        ]

    def test_the_fill_height_is_kept_within_bounds(self):
        editor = _fill_editor()
        editor.add_component(FILLING_BOUNDED)
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [{"height": 300}]

    def test_a_stored_height_wins_over_the_fill_height(self):
        editor = _fill_editor()
        stored = {"index": 0, "width": 40, "height": 250, "visible": True}
        editor.load_model(_model([("f", FILLING)], [{**stored, "instance_id": "f"}]))
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [stored]

    def test_the_fill_height_is_not_saved(self):
        editor = _fill_editor()
        stored = {"index": 0, "width": 40, "visible": True}
        editor.load_model(_model([("f", FILLING)], [{**stored, "instance_id": "f"}]))
        editor.mode = "dashboard"

        assert editor._tile_grid.layout == [{**stored, "height": FILL_HEIGHT}]
        assert editor.to_model().tile_layout == [{**stored, "instance_id": "f"}]

    def test_a_stored_fill_height_survives_reloading_the_dashboard(self):
        editor = _fill_editor()
        editor.load_model(_model([("f", FILLING)], [{"index": 0, "instance_id": "f"}]))
        editor.mode = "dashboard"

        stored = {"index": 0, "width": 50, "height": FILL_HEIGHT, "visible": True}
        editor.load_model(_model([("f", FILLING)], [{**stored, "instance_id": "f"}]))

        assert editor.to_model().tile_layout == [{**stored, "instance_id": "f"}]

    def test_a_resized_fill_height_is_saved(self):
        editor = _fill_editor()
        editor.load_model(_model([("f", FILLING)], [{"index": 0, "instance_id": "f"}]))
        editor.mode = "dashboard"

        resized = {"index": 0, "width": 50, "height": 420, "visible": True}
        editor._tile_grid._process_events({"layout": [resized]})

        assert editor.to_model().tile_layout == [{**resized, "instance_id": "f"}]

    def test_the_agent_reads_the_height_a_tile_without_one_is_shown_at(self):
        editor = _fill_editor()
        for component, name in ((PLAIN, "plain"), (FILLING, "fill"), (FILLING_BOUNDED, "bounded")):
            editor.add_component(component, instance_id=name)
        editor.mode = "dashboard"
        ops = DashboardOps(editor)
        rows = " | ".join(
            line for line in ops.describe_layout().splitlines() if line.startswith("Row")
        )

        assert "plain 100% auto (content)" in rows
        assert f"fill 100% auto ({FILL_HEIGHT}px)" in rows
        assert "bounded 100% auto (300px)" in rows

    def test_a_pane_that_renders_a_chart_later_sizes_its_tile_then(self):
        editor = _fill_editor()
        instance_id = editor.add_component(RENDERED)
        editor.mode = "dashboard"
        assert editor._tile_grid.layout == []

        editor._component_instances[instance_id].shown = True

        assert editor._tile_grid.layout == [{"height": FILL_HEIGHT}]

    def test_views_that_take_their_height_from_the_tile(self):
        stretched = pn.pane.Vega({**VEGA, "height": "container"}, sizing_mode="stretch_width")
        method = pn.panel(lambda: pn.pane.Vega(CONTAINER))

        assert _fills_tile(pn.pane.Vega(CONTAINER))
        assert _fills_tile(stretched)
        assert _fills_tile(method)
        assert _fills_tile(pn.Column(pn.pane.Markdown("a"), method))

    def test_views_with_a_height_of_their_own_are_not_filled(self):
        sized = pn.pane.Vega({**VEGA, "height": "container"}, height=200)
        spaced = pn.Column(pn.pane.Markdown("a"), pn.layout.VSpacer(), sizing_mode="stretch_both")

        assert not _fills_tile(sized)
        assert not _fills_tile(spaced)
        assert not _fills_tile(pn.pane.Vega({**VEGA, "height": 300}))
        assert not _fills_tile(pn.pane.Markdown("a", sizing_mode="stretch_both"))
        assert not _fills_tile(pn.pane.HTML("a", sizing_mode="stretch_both"))
        assert not _fills_tile(pn.pane.Str("a", sizing_mode="stretch_both"))
        assert not _fills_tile(pn.panel(lambda: pn.pane.Markdown("a")))
