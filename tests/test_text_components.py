"""The built-in Markdown, Notes, Heading and Divider components."""

import html
import re

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import panel_reactflow as pr
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash, register
from panel_flowdash.agent import (
    component_schema,
    dashboard_from_yaml,
    dashboard_to_yaml,
    validate_dashboard,
)
from panel_flowdash.text_components import (
    TEXT_COMPONENTS,
    Heading,
    Markdown,
    Notes,
    escape_inline_markdown,
    fill_template,
    format_value,
    html_to_text,
)
from panel_flowdash.ui.roles import component_role
from panel_flowdash.widgets import BUILTIN_COMPONENTS
from tests.untrusted_payloads import PAYLOADS


@register(page=False, component=True, title="Summary")
class Summary(Viewer):
    summary = param.Dict(default={"state": "Texas", "turbines": 19046, "share": 0.264})

    @param.output(param.Dict)
    def out(self):
        return self.summary

    def __panel__(self):
        return pn.pane.Str("summary")


def _html(pane) -> str:
    """The HTML a Markdown pane puts in the page, after the one decode its model applies."""
    return html.unescape(pane._transform_object(pane.object)["object"])


def _text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


@pytest.fixture
def editor():
    editor = FlowDash({"Demo/summary": Summary}, notifications=False)
    yield editor
    editor.dispose()


class TestTemplate:
    @pytest.mark.parametrize(
        ("template", "value", "expected"),
        [
            ("{{ value }}", "Texas", "Texas"),
            ("{{value}}", 3, "3"),
            ("{{ value }}", 19046, "19,046"),
            ("{{ value }}", 0.1 + 0.2, "0.3"),
            ("{{ value:.1% }}", 0.264, "26.4%"),
            ("{{ value:,.0f }}", 1234.5, "1,234"),
            ("{{ value:bad }}", 3, "3"),
            (
                "{{ value.state }} has {{ value.turbines }}",
                {"state": "TX", "turbines": 5},
                "TX has 5",
            ),
            ("{{ value.missing }}", {"state": "TX"}, ""),
            ("{{ value.state }}", "not a mapping", ""),
            ("{{ value }}", None, ""),
            ("{{ value }}", ["a", "b"], "a, b"),
            ("{{ value }}", True, "True"),
            ("{{ other }} {{ value }}", 1, "{{ other }} 1"),
            ("No placeholders", 1, "No placeholders"),
        ],
    )
    def test_fill(self, template, value, expected):
        assert fill_template(template, value) == expected

    def test_series_key(self):
        row = pd.Series({"state": "TX", "turbines": 12})
        assert fill_template("{{ value.state }}: {{ value.turbines }}", row) == "TX: 12"

    def test_dataframe_is_summarized(self):
        frame = pd.DataFrame({"a": range(1200), "b": range(1200)})
        assert format_value(frame) == "1,200 rows, 2 columns"

    def test_values_are_escaped(self):
        assert fill_template("*{{ value }}*", "a*b", escape_inline_markdown) == r"*a\*b*"

    def test_escape_keeps_a_value_on_one_line(self):
        assert escape_inline_markdown("# a\n\n- b") == r"\# a \- b"


class TestMarkdown:
    def test_renders_markdown_and_follows_its_input(self, editor):
        source = editor.add_component("Demo/summary")
        node = editor.add_component(
            "Text/Markdown",
            config={"text": "**{{ value.state }}** has {{ value.turbines }} turbines"},
        )
        pane = editor._tile_objects[-1]
        assert _text(_html(pane)).strip() == "**** has  turbines"

        assert editor.connect(source, "out", node, "value") is True
        assert "<strong>Texas</strong> has 19,046 turbines" in _html(pane)

        editor.graph.get_config_state(node).text = "# {{ value.state }}"
        assert "<h1>Texas</h1>" in _html(pane)

    def test_input_is_optional_and_does_not_gate_the_view(self, editor):
        node = editor.add_component("Text/Markdown")
        spec = editor.component_specs["Text/Markdown"]
        assert [(p.name, p.required, p.blocking) for p in spec.inputs] == [("value", False, False)]
        assert editor._tile_objects[-1] is editor._component_instances[node]._pane

    def test_raw_html_in_the_text_is_shown_as_text(self):
        pane = Markdown(text='Hi <img src=x onerror="alert(1)"> <b>bold</b>')._pane
        rendered = _html(pane)
        assert "<img" not in rendered
        assert "<b>" not in rendered
        assert '<img src=x onerror="alert(1)">' in _text(rendered)

    def test_math_is_not_typeset(self):
        pane = Markdown()._pane
        assert pane.disable_math is True
        assert "disable_math" in pane._rename.values()

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_upstream_values_are_shown_as_text(self, name):
        payload = PAYLOADS[name]
        markdown = Markdown(text="Value: {{ value }}", value=payload)
        rendered = _html(markdown._pane)
        assert "evil.test" not in "".join(re.findall(r"<[^>]+>", rendered))
        assert "<img" not in rendered
        assert " ".join(payload.split()) in _text(rendered)

    def test_preview_is_the_filled_text(self):
        markdown = Markdown(text="# {{ value }}", value="Texas")
        assert markdown.__flowdash_preview__() == "# Texas"


class TestHeading:
    def test_text_and_subtitle_follow_config_and_input(self, editor):
        source = editor.add_component("Demo/summary")
        node = editor.add_component(
            "Text/Heading",
            config={"text": "Wind in {{ value.state }}", "subtitle": "", "size": "Title"},
        )
        heading = editor._component_instances[node]
        assert editor.connect(source, "out", node, "value") is True
        assert heading._title.object == "Wind in Texas"
        assert heading._title.variant == "h3"
        assert not heading._subtitle.visible

        editor.graph.get_config_state(node).param.update(subtitle="{{ value.turbines }} turbines")
        assert heading._subtitle.visible
        assert heading._subtitle.object == "19,046 turbines"

    def test_text_is_escaped(self):
        heading = Heading(text="<b>x</b> *y*", subtitle="[a](https://evil.test)")
        assert "<b>" not in heading._title.object
        assert r"\*y\*" in heading._title.object
        assert r"\[a\]" in heading._subtitle.object


class TestNotes:
    def test_editing_the_tile_saves_the_text(self, editor):
        node = editor.add_component("Text/Notes")
        notes = editor._component_instances[node]
        editor.dirty = False

        notes._editor.value = "<p>Typed <strong>here</strong></p>"

        assert editor.graph.get_config_state(node).text == "<p>Typed <strong>here</strong></p>"
        assert editor._tile_items[-1]["config"]["text"] == "<p>Typed <strong>here</strong></p>"
        assert editor.dirty
        restored = FlowDash({}, notifications=False)
        restored.load_model(editor.to_model(title="Notes"))
        assert restored._component_instances[node]._editor.value == notes._editor.value
        restored.dispose()

    def test_config_reaches_the_editor(self, editor):
        node = editor.add_component("Text/Notes", config={"text": "<p>Saved</p>"})
        notes = editor._component_instances[node]
        assert notes._editor.value == "<p>Saved</p>"
        editor.graph.get_config_state(node).text = "<p>Changed</p>"
        assert notes._editor.value == "<p>Changed</p>"

    def test_read_only_unless_the_dashboard_is_editable(self, editor):
        node = editor.add_component("Text/Notes", config={"placeholder": "Jot"})
        notes = editor._component_instances[node]
        assert (notes._editor.disabled, notes._editor.placeholder) == (False, "Jot")
        assert notes._editor.mode == "bubble"

        editor.editable = False
        assert (notes._editor.disabled, notes._editor.placeholder) == (True, "")
        notes._editor.value = "<p>Not saved</p>"
        assert editor.graph.get_config_state(node).text == ""

        editor.editable = True
        editor.read_only = True
        assert notes._editor.disabled

    def test_without_an_editor_it_is_editable(self):
        notes = Notes(text="<p>x</p>")
        notes._editor.value = "<p>y</p>"
        assert notes.text == "<p>y</p>"

    def test_removing_releases_the_editor_subscription(self, editor):
        node = editor.add_component("Text/Notes")
        before = len(editor._subscriptions())
        assert before
        editor.remove_component(node)
        assert len(editor._subscriptions()) < before

    def test_preview_is_plain_text(self):
        notes = Notes(text="<h2>Title</h2><p>Some <strong>bold</strong></p><ul><li>One</li></ul>")
        assert notes.__flowdash_preview__() == "Title\nSome bold\nOne"

    @pytest.mark.parametrize(
        ("markup", "text"),
        [
            ("", ""),
            ("<p>a</p><p>b</p>", "a\nb"),
            ("<p>a<br>b</p>", "a\nb"),
            ("<script>x</script><p>&lt;b&gt;</p>", "x\n<b>"),
        ],
    )
    def test_html_to_text(self, markup, text):
        assert html_to_text(markup) == text


class TestTwoWayConfig:
    def test_only_declared_params_are_synced_back(self, editor):
        node = editor.add_component("Text/Markdown", config={"text": "a"})
        editor.dirty = False
        editor._component_instances[node].text = "changed by the component"
        assert editor.graph.get_config_state(node).text == "a"
        assert not editor.dirty


@pytest.mark.parametrize("component_id", list(TEXT_COMPONENTS))
class TestEveryTextComponent:
    def test_is_a_builtin_view(self, component_id):
        assert BUILTIN_COMPONENTS[component_id] is TEXT_COMPONENTS[component_id]
        editor = FlowDash({}, notifications=False)
        entry = editor._component_entries[component_id]
        assert component_role(entry, (), ()) == "view"
        editor.dispose()

    def test_agent_schema_describes_it(self, component_id):
        editor = FlowDash({}, notifications=False)
        schema = component_schema(editor.component_specs[component_id])
        assert schema["description"]
        assert schema["outputs"] == []
        for prop in schema["config"]["properties"].values():
            assert prop.get("type") in {"string"} or "enum" in prop
            assert prop.get("description") or prop.get("enum")
        editor.dispose()

    @pytest.mark.filterwarnings("ignore:Both 'name' and 'label':PendingDeprecationWarning")
    def test_config_renders_a_schema_form(self, component_id):
        editor = FlowDash({}, notifications=False)
        instance_id = editor.add_component(component_id)
        node = next(n for n in editor._flow.nodes if n["id"] == instance_id)
        schema = editor._flow.node_types[node["type"]]["schema"]
        form = pr.SchemaEditor(node["data"], schema)
        assert set(form._form._widgets) == set(schema["properties"])
        editor.dispose()

    def test_config_round_trips_through_yaml(self, component_id):
        editor = FlowDash({}, notifications=False)
        config = {
            "Text/Markdown": {"text": "# Title\n\n{{ value:.1% }} of *all*"},
            "Text/Notes": {"text": "<p>Some <em>notes</em></p>", "placeholder": "Jot"},
            "Text/Heading": {"text": "Wind", "subtitle": "By state", "size": "Title"},
            "Text/Divider": {"label": "Details"},
        }[component_id]
        editor.add_component(component_id, config=config)
        exported = editor.export_dashboard()
        text = dashboard_to_yaml(exported)
        (item,) = dashboard_from_yaml(text)["items"]
        assert (item["component_id"], item["config"]) == (component_id, config)
        issues = validate_dashboard(text, specs=editor.component_specs)
        assert [str(i) for i in issues if i.severity == "error"] == []

        restored = FlowDash({}, notifications=False)
        restored.import_dashboard(text)
        (item,) = restored.export_dashboard()["items"]
        assert (item["component_id"], item["config"]) == (component_id, config)
        editor.dispose()
        restored.dispose()


def test_text_config_is_edited_in_a_growing_text_area():
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Text/Markdown")
    config = editor.graph.get_config_state(node)
    widget = pn.Param(config, parameters=["text"])._widgets["text"]
    assert isinstance(widget, pmui.TextAreaInput)
    assert (widget.rows, widget.auto_grow, widget.max_length) == (6, True, 100_000)
    editor.dispose()


def test_notes_text_is_left_out_of_the_settings_form():
    editor = FlowDash({}, notifications=False)
    node = editor.add_component("Text/Notes")
    form = pn.Param(
        editor.graph.get_config_state(node), parameters=["text", "placeholder"], show_name=False
    )
    assert [w.label for w in form.layout] == ["Placeholder"]
    editor.dispose()
