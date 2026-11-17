"""Full screen set at construction, without a controls bar and from kiosk links with other parameters."""

import panel as pn

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.fullscreen import PRESENTING_CLASS
from tests.test_dashboard_fullscreen import _open, app  # noqa: F401
from tests.test_editor import COMPONENTS, SELECTOR


@register(page=False, component=True, sidebar=True, title="Side")
def side_panel(config):
    return "side"


SIDE = "Demo/side"


def _presenting(editor) -> bool:
    return PRESENTING_CLASS in editor._view.css_classes


class TestConstructor:
    async def test_fullscreen_at_construction_presents_the_dashboard(self):
        editor = FlowDash(COMPONENTS, notifications=False, editable=False, fullscreen=True)
        assert editor.fullscreen
        assert editor._fullscreen_controller.active
        assert _presenting(editor)
        assert editor._fullscreen_overlay.visible
        assert not editor._dashboard_bar.__panel__().visible

    async def test_fullscreen_at_construction_is_refused_in_edit_mode(self):
        editor = FlowDash(COMPONENTS, notifications=False, editable=True, fullscreen=True)
        assert not editor.fullscreen
        assert not editor._fullscreen_controller.active
        assert not _presenting(editor)
        # The bar holds Save and Done in edit mode.
        assert editor._dashboard_bar.__panel__().visible


class TestWithoutControlsBar:
    async def test_full_screen_shows_the_sidebar_views_in_the_controls_bar(self):
        editor = FlowDash(
            {**COMPONENTS, SIDE: side_panel}, notifications=False, controls_bar=False
        )
        editor.add_component(SIDE)
        editor.add_component(SELECTOR)
        editor.editable = False
        assert editor._view_controls.controls == []

        editor.fullscreen = True
        assert editor._view_controls.controls == list(editor.sidebar)

        editor.fullscreen = False
        assert editor._view_controls.controls == []

    async def test_app_moves_the_controls_out_of_its_sidebar_while_presenting(self, app):  # noqa: F811
        _open(app)
        editor = app._editor
        sidebar = [pn.pane.Markdown("control")]
        editor.param.update(controls_bar=False, sidebar=sidebar)
        assert app._sidebar_views == sidebar

        editor.fullscreen = True
        assert app._sidebar_views == []

        editor.fullscreen = False
        assert app._sidebar_views == sidebar


class TestKioskQuery:
    async def test_kiosk_link_keeps_the_other_parameters(self, app):  # noqa: F811
        _open(app, "?theme=dark&kiosk=1&lang=de")
        assert app._editor.fullscreen
        assert pn.state.location.search == "?theme=dark&kiosk=1&lang=de"

    async def test_leaving_kiosk_drops_only_the_kiosk_parameter(self, app):  # noqa: F811
        _open(app, "?theme=dark&kiosk=1")
        app._editor._fullscreen_controller.active = False
        assert pn.state.location.search == "?theme=dark"

    async def test_a_kiosk_value_other_than_1_is_not_kiosk(self, app):  # noqa: F811
        _open(app, "?kiosk=10")
        assert not app._editor.fullscreen
