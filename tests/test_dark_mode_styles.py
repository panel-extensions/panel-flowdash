"""The editor's stylesheets give React Flow, splitjs and Ace theme-aware colours."""

from panel_flowdash.editor import _FLOW_STYLESHEET
from panel_flowdash.ui.code_field import ACE_THEME_CSS, CodeField
from panel_flowdash.ui.code_panel import CodePanel
from panel_flowdash.ui.split_view import GUTTER_CSS, SplitView
from tests.test_code_panel import make_editor


def test_canvas_overrides_react_flow_dark_edge_and_attribution():
    assert ".react-flow.dark {" in _FLOW_STYLESHEET
    assert "--xy-edge-stroke: var(--mui-palette-text-disabled" in _FLOW_STYLESHEET
    assert ".react-flow.dark .react-flow__attribution {" in _FLOW_STYLESHEET


def test_split_gutter_uses_the_palette_divider():
    view = SplitView()
    assert GUTTER_CSS in view.split.stylesheets
    assert "--mui-palette-divider" in GUTTER_CSS


def test_code_editors_share_the_ace_theme_css():
    field = CodeField(value="x", language="python")
    assert ACE_THEME_CSS in field._make_editor(100, []).stylesheets
    editor, _query = make_editor()
    assert ACE_THEME_CSS in CodePanel(editor=editor).code.stylesheets
