"""An output's own repr never reaches the UI or the agent: a source's repr lists its secrets."""

import datetime as dt
import json
from dataclasses import dataclass

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.agent.preview import preview_editor, sample_value
from panel_flowdash.editor import FlowDash
from panel_flowdash.text_components import fill_template
from panel_flowdash.ui.inspect_sheet import python_literal
from panel_flowdash.ui.node_preview import format_value, summarize_value
from panel_flowdash.ui.status_bar import select_on_canvas
from panel_flowdash.value_text import held_table_names, object_label, safe_repr, safe_str

SECRETS = ("hunter2", "sk-secret")


class SecretSource(param.Parameterized):
    """Stands in for a Lumen source, whose parameters hold a connection URI and a token."""

    uri = param.String(default="postgresql://u:hunter2@h/db")
    token = param.String(default="sk-secret")
    tables = param.Dict(default={"turbines": "SELECT 1", "farms": "SELECT 2"})


@dataclass
class Credentials:
    password: str = "hunter2"


@register(
    page=False,
    component=True,
    title="Secret source",
    config=["expr"],
    provides=[{"key": "source"}, {"key": "sources"}],
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

    def __panel__(self):
        return "secret"


NODE = "Demo/secret"


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
    "value", [SecretSource(), Credentials(), [SecretSource()], {"a": Credentials()}]
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


@pytest.mark.parametrize("port", ["source", "sources"])
def test_value_popup_never_shows_secrets(editor, port):
    node = editor.add_component(NODE)
    value = getattr(editor.graph.get_state(node), port)
    popup = editor._build_value_popup("Secret", None, value)
    text = _all_text(popup)
    _assert_no_secrets(text)
    assert "SecretSource" in text


def test_value_popup_shows_type_name_and_tables(editor):
    preview = editor._value_preview(SecretSource(name="turbines"))
    assert preview.object == "SecretSource · turbines · `turbines`, `farms`"


def test_inspector_never_shows_secrets(editor):
    node = editor.add_component(NODE)
    select_on_canvas(editor._flow, node_id=node)
    editor._inspector._node_tabs.active = 1
    text = _all_text(editor._inspector._data_column)
    _assert_no_secrets(text)
    assert "SecretSource · turbines · turbines, farms" in text
    spec = editor._component_specs[NODE]
    sections = editor._inspection_sections(node, spec)
    _assert_no_secrets(_all_text(*(section for _, section in sections)))


@pytest.mark.parametrize("port", ["source", "sources"])
def test_code_panel_result_never_shows_secrets(editor, port):
    node = editor.add_component(NODE)
    assert editor.open_code_panel(node, "expr")
    panel = editor.code_panel
    panel.outputs.value = port
    text = _all_text(*panel._result_body, panel._result_footer)
    _assert_no_secrets(text)
    assert "SecretSource" in text


def test_agent_preview_never_shows_secrets(editor):
    node = editor.add_component(NODE)
    report = preview_editor(editor, {node: "secret"})
    text = json.dumps(report, default=str)
    _assert_no_secrets(text)
    assert report["nodes"]["secret"]["outputs"]["source"]["value"] == "SecretSource · turbines"
