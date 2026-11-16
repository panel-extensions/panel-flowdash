"""Tests for the ``default_size``, ``min_size`` and ``max_size`` tile hints."""

import logging

import panel as pn
import param
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.dashboard_store import DashboardItem, DashboardModel
from panel_flowdash.editor import FlowDash


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
        assert not sized[1].visible
        assert isinstance(plain_view, pn.pane.Markdown)

    def test_bounded_tile_keeps_its_inspect_button(self):
        editor = _editor(inspect_outputs=True)
        editor.add_component(SIZED)
        editor.editable = False
        editor.mode = "dashboard"

        (tile,) = editor._tile_grid.objects
        assert (tile.min_width, tile.max_width) == (240, 960)
        assert tile[1].visible
