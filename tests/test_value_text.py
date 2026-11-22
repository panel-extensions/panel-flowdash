"""An output's own repr never reaches the UI or the agent: a source's repr lists its secrets."""

import datetime as dt
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import editor as editor_module, register
from panel_flowdash.agent.preview import preview_editor, sample_value
from panel_flowdash.dataflow_engine import exception_text
from panel_flowdash.editor import FlowDash
from panel_flowdash.text_components import fill_template
from panel_flowdash.ui.inspect_sheet import python_literal
from panel_flowdash.ui.inspector import summarize_input_html
from panel_flowdash.ui.node_preview import format_value, summarize_value
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.util import ReactiveError
from panel_flowdash.value_text import (
    held_table_names,
    is_plain,
    object_label,
    safe_frame,
    safe_repr,
    safe_str,
)

SECRETS = ("hunter2", "sk-secret")


class SecretSource(param.Parameterized):
    """Stands in for a Lumen source, whose parameters hold a connection URI and a token."""

    uri = param.String(default="postgresql://u:hunter2@h/db")
    token = param.String(default="sk-secret")
    tables = param.Dict(default={"turbines": "SELECT 1", "farms": "SELECT 2"})


@dataclass
class Credentials:
    password: str = "hunter2"


class SecretText:
    """An object whose own text is a secret."""

    def __str__(self):
        return "sk-secret"

    __repr__ = __str__


def secret_frame():
    frame = pd.DataFrame({"x": [1, 2, 3], "o": [SecretSource(), SecretText(), Credentials()]})
    frame.attrs["uri"] = "postgresql://u:hunter2@h/db"
    return frame


@register(
    page=False,
    component=True,
    title="Secret source",
    config=["expr"],
    provides=[
        {"key": "source"},
        {"key": "sources"},
        {"key": "nested"},
        {"key": "text"},
        {"key": "frame", "type": "DataFrame"},
    ],
)
class SecretNode(Viewer):
    expr = param.String(
        default="x", metadata={"flowdash": {"widget": "code", "language": "python"}}
    )

    @param.output(param.Parameter)
    def source(self):
        return SecretSource(name="turbines")

    @param.output(param.Parameter)
    def sources(self):
        return {"main": SecretSource(), "creds": [Credentials()]}

    @param.output(param.Parameter)
    def nested(self):
        return {"a": [{"b": (SecretSource(), Credentials())}], "c": SecretText()}

    @param.output(param.Parameter)
    def text(self):
        return SecretText()

    @param.output(param.DataFrame)
    def frame(self):
        return secret_frame()

    def __panel__(self):
        return "secret"


NODE = "Demo/secret"
PORTS = ["source", "sources", "nested", "text", "frame"]


@pytest.fixture
def editor():
    editor = FlowDash({NODE: SecretNode}, notifications=False, include_builtin_components=False)
    return editor


def _all_text(*viewables) -> str:
    texts = []
    for viewable in viewables:
        for obj in pn.panel(viewable).select(lambda o: hasattr(o, "param")):
            for name in ("object", "label", "text", "title", "value"):
                value = getattr(obj, name, None) if name in obj.param else None
                if isinstance(value, str):
                    texts.append(value)
    return "\n".join(texts)


def _frame_text(viewable) -> str:
    """The cells a table widget or pane renders, as the browser would get them."""
    texts = []
    for obj in pn.panel(viewable).select(
        lambda o: isinstance(o, (pn.pane.DataFrame, pn.widgets.Tabulator))
    ):
        frame = obj.value if isinstance(obj, pn.widgets.Tabulator) else obj.object
        texts.append(frame.to_string())
        if isinstance(obj, pn.pane.DataFrame):
            texts.append(
                obj._get_properties(None)["text"] if hasattr(obj, "_get_properties") else ""
            )
    return "\n".join(texts)


def _assert_no_secrets(text: str):
    for secret in SECRETS:
        assert secret not in text


def test_object_label_names_type_and_a_given_name_only():
    assert object_label(SecretSource(name="turbines")) == "SecretSource · turbines"
    assert object_label(SecretSource()) == "SecretSource"
    assert object_label(Credentials()) == "Credentials"


def test_plain_data_is_written_out_and_objects_named():
    assert safe_str(dt.date(2026, 1, 2)) == "2026-01-02"
    assert safe_str(1.5) == "1.5"
    assert safe_repr("a") == "'a'"
    assert safe_repr([1, SecretSource(name="x"), {"k": Credentials()}]) == (
        "[1, SecretSource · x, {'k': Credentials}]"
    )


def test_held_table_names_reads_only_the_attribute():
    assert held_table_names(SecretSource()) == ["turbines", "farms"]
    assert held_table_names(object()) is None


@pytest.mark.parametrize(
    "value",
    [
        SecretSource(),
        Credentials(),
        SecretText(),
        [SecretSource()],
        {"a": Credentials()},
        {SecretText(): 1},
        {"a": [{"b": (SecretSource(), Credentials())}], "c": SecretText()},
        np.array(SecretSource(), dtype=object),
    ],
)
def test_text_helpers_never_show_secrets(value):
    _assert_no_secrets(summarize_value(value))
    _assert_no_secrets(format_value(value))
    _assert_no_secrets(fill_template("{{ value }}", value))
    _assert_no_secrets(json.dumps(sample_value(value)))


def test_agent_sample_names_the_source_and_its_tables():
    sample = sample_value(SecretSource(name="turbines"))
    assert sample == {
        "type": "SecretSource",
        "value": "SecretSource · turbines",
        "tables": ["turbines", "farms"],
    }


def test_copied_python_of_a_frame_names_objects_in_cells():
    frame = pd.DataFrame({"source": [SecretSource(name="a"), [Credentials()]]})
    source = python_literal(frame)
    _assert_no_secrets(source)
    assert "'SecretSource · a'" in source


@pytest.mark.parametrize("port", PORTS)
def test_value_popup_never_shows_secrets(editor, port):
    node = editor.add_component(NODE)
    value = getattr(editor.graph.get_state(node), port)
    popup = editor._build_value_popup("Secret", None, value)
    _assert_no_secrets(_all_text(popup) + _frame_text(popup))


def test_value_popup_shows_type_name_and_tables(editor):
    preview = editor._value_preview(SecretSource(name="turbines"))
    assert preview.object == "SecretSource · turbines · `turbines`, `farms`"


def test_inspector_never_shows_secrets(editor):
    node = editor.add_component(NODE)
    select_on_canvas(editor._flow, node_id=node)
    editor._inspector._node_tabs.active = 1
    text = _all_text(editor._inspector._data_column)
    _assert_no_secrets(text + _frame_text(editor._inspector._data_column))
    assert "SecretSource · turbines · turbines, farms" in text
    spec = editor._component_specs[NODE]
    sections = editor._inspection_sections(node, spec)
    _assert_no_secrets(_all_text(*(section for _, section in sections)))


@pytest.mark.parametrize("port", PORTS)
def test_code_panel_result_never_shows_secrets(editor, port):
    node = editor.add_component(NODE)
    assert editor.open_code_panel(node, "expr")
    panel = editor.code_panel
    panel.outputs.value = port
    body = pn.Column(*panel._result_body, panel._result_footer)
    _assert_no_secrets(_all_text(body) + _frame_text(body))


def test_agent_preview_never_shows_secrets(editor):
    node = editor.add_component(NODE)
    report = preview_editor(editor, {node: "secret"})
    text = json.dumps(report, default=str)
    _assert_no_secrets(text)
    assert report["nodes"]["secret"]["outputs"]["source"]["value"] == "SecretSource · turbines"


def test_safe_frame_labels_object_cells_without_touching_the_frame():
    frame = secret_frame()
    shown = safe_frame(frame)
    assert list(shown["o"]) == ["SecretSource", "SecretText", "Credentials"]
    assert list(shown["x"]) == [1, 2, 3]
    assert isinstance(frame["o"][0], SecretSource)
    _assert_no_secrets(shown.to_string())


def test_a_0d_object_array_is_not_plain():
    assert not is_plain(np.array(SecretSource(), dtype=object))
    assert is_plain(np.float64(1.5))


def test_exception_text_masks_credentials():
    exc = ValueError("cannot reach postgresql://u:hunter2@h/db?sslmode=x&token=sk-secret")
    assert exception_text(exc) == (
        "ValueError: cannot reach postgresql://u:***@h/db?sslmode=x&token=***"
    )
    assert exception_text(KeyError("password=hunter2")) == "KeyError: 'password=***'"


@pytest.mark.parametrize(
    "message, shown",
    [
        ("cannot reach redis://:hunter2@h/0", "cannot reach redis://:***@h/0"),
        (
            'bad config {"password": "hunter2", "user": "u"}',
            'bad config {"password": "***", "user": "u"}',
        ),
        (
            "bad config {'secret': 'sk-secret', 'api_key': 'sk-secret'}",
            "bad config {'secret': '***', 'api_key': '***'}",
        ),
        ("connect(password='hunter2')", "connect(password='***')"),
        ("sent Authorization: Bearer sk-secret", "sent Authorization: Bearer ***"),
        ("sent Authorization: hunter2", "sent Authorization: ***"),
        ("rejected Bearer sk-secret.42 for h", "rejected Bearer *** for h"),
    ],
)
def test_exception_text_masks_quoted_and_bearer_credentials(message, shown):
    assert exception_text(ValueError(message)) == f"ValueError: {shown}"


@pytest.mark.parametrize(
    "message",
    [
        "Invalid token",
        "Bearer token required.",
        "token: expired",
        '"max_tokens": 5',
        "http://h:8080/x",
    ],
)
def test_exception_text_keeps_messages_without_credentials(message):
    assert exception_text(ValueError(message)) == f"ValueError: {message}"


def test_safe_frame_labels_object_index_values_and_column_labels():
    frame = pd.DataFrame(
        [[1, 2], [3, 4]], index=[SecretSource(), "b"], columns=["x", SecretText()]
    )
    shown = safe_frame(frame)
    assert list(shown.index) == ["SecretSource", "b"]
    assert list(shown.columns) == ["x", "SecretText"]
    assert isinstance(frame.index[0], SecretSource)
    _assert_no_secrets(shown.to_html() + shown.to_string())
    nested = frame.set_axis(
        pd.MultiIndex.from_tuples([("a", SecretSource()), ("b", 1)], names=[SecretText(), "k"])
    )
    shown = safe_frame(nested)
    assert list(shown.index) == [("a", "SecretSource"), ("b", 1)]
    assert list(shown.index.names) == ["SecretText", "k"]
    _assert_no_secrets(shown.to_html())
    plain = pd.DataFrame({"x": [1]})
    assert safe_frame(plain) is plain


def test_inspector_input_summary_names_object_columns():
    frame = pd.DataFrame([[1, 2]], columns=["x", SecretText()])
    html = summarize_input_html(frame, "DataFrame")
    _assert_no_secrets(html)
    assert "SecretText" in html


def test_card_popout_table_never_shows_secrets(editor, monkeypatch):
    shown = []
    monkeypatch.setattr(editor_module, "open_dialog", lambda title, body, **kw: shown.append(body))
    node = editor.add_component(NODE)
    spec = editor._component_specs[NODE]
    editor._show_card_popout(node, spec, ("frame", secret_frame()))
    (body,) = shown
    _assert_no_secrets(_all_text(body) + _frame_text(body))
    assert "SecretText" in _frame_text(body)


def test_failed_output_message_never_shows_secrets(editor):
    node = editor.add_component(NODE)
    if ReactiveError is None:
        pytest.skip("this param has no ReactiveError")
    error = ReactiveError(ConnectionError("no route to postgresql://u:hunter2@h/db"))
    _assert_no_secrets(_all_text(editor._value_preview(error)))
    report = preview_editor(editor, {node: "secret"})
    _assert_no_secrets(json.dumps(report, default=str))
