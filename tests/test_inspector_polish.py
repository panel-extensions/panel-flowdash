"""Field labels, literal text and connected input rows in the inspector."""

import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.ui.input_editor import _as_text
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.ui.text import field_label
from tests.test_ui_inspector import ROWS, TABLE_COMPONENTS


class _Fields(param.Parameterized):
    sql = param.String()
    api_url = param.String()
    row_id = param.String()
    page_size = param.Integer()
    sqlite_path = param.String()
    query = param.String(label="Query sql")


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("sql", "SQL"),
        ("api_url", "API URL"),
        ("row_id", "Row ID"),
        ("page_size", "Page size"),
        ("sqlite_path", "Sqlite path"),
        ("query", "Query sql"),
    ],
)
def test_field_label_capitalises_acronyms_but_keeps_explicit_labels(name, label):
    assert field_label(_Fields.param[name]) == label


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("TX", "TX"),
        ("New York", "New York"),
        ("12", "'12'"),
        ("None", "'None'"),
        (" padded", "' padded'"),
        ("(open", "'(open'"),
        (3, "3"),
        ([1, 2], "[1, 2]"),
        (None, ""),
    ],
)
def test_a_literal_string_is_written_without_quotes_when_it_reads_back_as_itself(value, text):
    assert _as_text(value) == text


def _texts(objects):
    return " ".join(
        str(getattr(o, "object", "")) for o in pn.Column(*objects).select(pn.viewable.Viewable)
    )


@register(page=False, component=True, title="Peek")
class _Peek(Viewer):
    rows = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return "peek"


def test_a_connected_optional_input_is_not_called_optional():
    editor = FlowDash(
        {**TABLE_COMPONENTS, "Demo/peek": _Peek},
        notifications=False,
        include_builtin_components=False,
    )
    src = editor.add_component(ROWS)
    peek = editor.add_component("Demo/peek")
    select_on_canvas(editor._flow, node_id=peek)
    assert "optional" in _texts([_row(editor)])
    editor.connect(src, "rows", peek, "rows")
    select_on_canvas(editor._flow, node_id=src)
    select_on_canvas(editor._flow, node_id=peek)
    assert "optional" not in _texts([_row(editor)])


def _row(editor):
    return next(
        o
        for o in pn.Column(*editor._inspector._body).select(pn.viewable.Viewable)
        if "fd-input-rows" in getattr(o, "css_classes", [])
    )


@register(page=False, component=True, title="Query", config=["sql"])
class _Query(Viewer):
    value = param.Parameter(default=None)
    sql = param.String(default="SELECT 1")

    def __panel__(self):
        return "query"


def test_the_config_form_shows_sql_in_capitals_and_bare_text_is_a_string():
    editor = FlowDash(
        {"Demo/query": _Query}, notifications=False, include_builtin_components=False
    )
    query = editor.add_component("Demo/query")
    select_on_canvas(editor._flow, node_id=query)
    body = pn.Column(*editor._inspector._body)
    form = next(iter(body.select(pn.Param)))
    assert form._widgets["sql"].label == "SQL"
    literal = editor._inspector._literal_editors["value"]
    literal._text.value = "TX"
    assert editor.input_values(query) == {"value": "TX"}
    assert literal._text.value == "TX"
