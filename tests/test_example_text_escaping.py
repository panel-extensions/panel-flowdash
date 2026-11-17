"""The example components show values from upstream components as text, not Markdown."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import panel as pn

from panel_flowdash.ui.text import escape_markdown

EXAMPLES = Path(__file__).parent.parent / "examples"
TRICKY = "<b>Tex</b>as *now* $$x$$"


def _load(relative: str):
    path = EXAMPLES / relative
    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_category_display_escapes_the_category():
    display = _load("simple_dataflow/Components/display.py")
    pane = display.app(SimpleNamespace(category=TRICKY))
    assert pane.object == f"## Selected: {escape_markdown(TRICKY)}"


def test_state_filter_escapes_the_state_name():
    state_filter = _load("complex_dataflow/Components/state_filter.py")
    view = state_filter.app(dataset=pd.DataFrame({"t_state": [TRICKY, TRICKY]}))
    (summary,) = view.__panel__().select(pn.pane.Markdown)
    assert summary.object == f"**2** turbines in {escape_markdown(TRICKY)}"
