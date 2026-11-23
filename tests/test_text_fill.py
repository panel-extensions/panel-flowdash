"""Placeholders filled from tables: cell and row addresses, repeated blocks, bounds and injection."""

import html
import re

import pandas as pd
import panel as pn
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import FlowDash, register
from panel_flowdash.text_components import (
    Heading,
    Markdown,
    check_text,
    fill_text,
    render_text,
)
from panel_flowdash.text_fill import MAX_EXPANDED, MAX_ROWS
from panel_flowdash.ui.text import escape_markdown
from tests.untrusted_payloads import PAYLOADS

SECRET = "sk-live-0123456789abcdef"


class Credentials:
    def __str__(self):
        return SECRET

    __repr__ = __str__

    def __format__(self, spec):
        return SECRET


TABLE = pd.DataFrame(
    {
        "region": ["EMEA", "APAC", "AMER"],
        "share": [0.256, 0.5, 0.244],
        "revenue": [1234567, 2000, 300],
    }
)


def fill(template, table=TABLE, value=None, **kwargs):
    return fill_text(template, {"value": value, "table": table}, **kwargs)


def problems(template, table=TABLE, value=None):
    found = []
    fill(template, table, value, problems=found)
    return found


def _text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


class TestAddresses:
    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("{{ table.region }}", "EMEA"),
            ("{{ table.revenue }}", "1,234,567"),
            ("{{ table.share:.1% }}", "25.6%"),
            ("{{ table.region[1] }}", "APAC"),
            ("{{ table[1].region }}", "APAC"),
            ("{{ table[ 2 ].region }}", "AMER"),
            ("{{ table.region[-1] }}", "AMER"),
            ("{{ table[-2].share:.0% }}", "50%"),
            ("{{ table.revenue[2]:,.2f }}", "300.00"),
            ("{{table.region}}", "EMEA"),
            ("{{ table[0] }}", "region: EMEA, share: 0.256, revenue: 1,234,567"),
            ("{{ table }}", "3 rows, 3 columns"),
        ],
    )
    def test_cells_and_rows(self, template, expected):
        assert fill(template) == expected

    def test_value_takes_the_same_addresses(self):
        assert fill("{{ value.region[2] }} {{ value[1].share:.0% }}", None, TABLE) == "AMER 50%"

    def test_dicts_series_and_lists_keep_their_keys(self):
        assert fill("{{ value.a }}", value={"a": 1}) == "1"
        assert fill("{{ value.b }}", value=pd.Series({"a": 1, "b": 2})) == "2"
        assert fill("{{ value[1] }} {{ value[-1] }}", value=[1, 2, 3]) == "2 3"

    def test_missing_cells_are_empty(self):
        frame = pd.DataFrame({"a": [None, float("nan")], "b": pd.array([1, None], dtype="Int64")})
        assert fill("[{{ table.a }}][{{ table.a[1] }}][{{ table.b[1] }}]", frame) == "[][][]"

    @pytest.mark.parametrize(
        ("template", "marker", "problem"),
        [
            ("{{ table.rgion }}", "[no column rgion]", "Its columns: region, share, revenue."),
            ("{{ table.region[3] }}", "[no row 3]", "no row 3"),
            ("{{ table[-4].region }}", "[no row -4]", "no row -4"),
            ("{{ table[0].nope }}", "[no column nope]", "no column nope"),
            ("{{ value[5] }}", "[no item 5]", "no item 5"),
        ],
    )
    def test_unknown_columns_and_rows_show_a_marker_and_a_problem(self, template, marker, problem):
        assert fill(template, value=[1]) == marker
        assert any(problem in p for p in problems(template, value=[1]))

    def test_an_empty_table_has_no_first_row(self):
        assert fill("{{ table.region }}", TABLE.head(0)) == "[no rows]"

    def test_no_data_yet_is_empty_without_a_problem(self):
        template = "{{ table.region }}{{ table[2].share }}{{#each table}}x{{/each}}"
        assert fill(template, None) == ""
        assert problems(template, None) == []

    def test_huge_row_numbers_are_not_read(self):
        assert fill("{{ table.region[99999999999] }}") == "{{ table.region[99999999999] }}"

    def test_markers_are_escaped_like_values(self):
        assert fill("{{ table.rgion }}", escape=escape_markdown) == "\\[no column rgion\\]"
        assert '<p><span class="fd-text-missing">[no column rgion]</span></p>' in render_text(
            "{{ table.rgion }}", {"table": TABLE}
        )


class TestEach:
    def test_repeats_its_body_for_each_row(self):
        template = (
            "Top:\n{{#each table limit=2}}\n- {{ .region }}: {{ .share:.1% }}\n{{/each}}\nEnd"
        )
        assert fill(template) == "Top:\n- EMEA: 25.6%\n- APAC: 50.0%\n\nEnd"
        rendered = render_text(template, {"table": TABLE})
        assert "<li>EMEA: 25.6%</li>\n<li>APAC: 50.0%</li>" in rendered

    def test_inline_and_whole_rows(self):
        assert fill("{{#each table}}{{ .region }} {{/each}}") == "EMEA APAC AMER "
        assert fill("{{#each value}}{{ . }};{{/each}}", None, ["a", "b"]) == "a;b;"
        assert fill("{{#each table limit=1}}{{ . }}{{/each}}").startswith("region: EMEA")

    def test_absolute_placeholders_inside_a_block(self):
        assert fill("{{#each table limit=2}}{{ .region }}/{{ value }} {{/each}}", value=7) == (
            "EMEA/7 APAC/7 "
        )

    def test_rows_are_capped(self):
        big = pd.DataFrame({"n": range(500)})
        assert fill("{{#each table}}x{{/each}}", big) == "x" * MAX_ROWS
        assert fill("{{#each table limit=500}}x{{/each}}", big) == "x" * MAX_ROWS
        assert any("at most 50 rows" in p for p in problems("{{#each table limit=500}}{{/each}}"))

    def test_the_expanded_text_is_capped(self):
        big = pd.DataFrame({"n": range(MAX_ROWS)})
        body = "x" * (MAX_EXPANDED // 10)
        filled = fill(f"{{{{#each table}}}}{body}{{{{/each}}}}", big)
        assert len(filled) <= MAX_EXPANDED
        assert any(
            "too long" in p for p in problems(f"{{{{#each table}}}}{body}{{{{/each}}}}", big)
        )

    def test_the_values_of_a_text_are_capped_together(self):
        big = pd.DataFrame({"s": ["y" * 1000] * MAX_ROWS})
        filled = fill("{{#each table}}{{ .s }}{{/each}}", big)
        assert filled.count("y") <= 20_000

    @pytest.mark.parametrize(
        ("template", "marker", "problem"),
        [
            ("{{#each table sort=share}}x{{/each}}", "[each takes only limit=N]", "sort"),
            ("{{#each table limit=2 order=desc}}x{{/each}}", "[each takes only limit=N]", "sort"),
            ("{{#each table.region}}{{ . }}{{/each}}", "EMEAAPACAMER", None),
            ("{{#each table.nope}}x{{/each}}", "[each: no column nope]", "no column nope"),
            ("{{#each value}}x{{/each}}", "[each needs a table]", "not a table"),
            ("{{#each nothing}}x{{/each}}", "[each names no table]", "names no table"),
            ("{{#each table}}x", "[each not repeated]x", "has no '{{/each}}'"),
            ("x{{/each}}", "x[each without start]", "closes no block"),
            ("{{ .region }}", "[row outside each]", "belongs inside"),
        ],
    )
    def test_malformed_blocks_show_a_marker(self, template, marker, problem):
        filled = fill(template, value=5)
        assert filled.startswith(marker)
        found = problems(template, value=5)
        assert (problem is None and not found) or any(problem in p for p in found)

    def test_blocks_do_not_nest(self):
        template = "{{#each table}}{{#each table}}a{{/each}}{{/each}}"
        assert "[each not repeated]" in fill(template)
        assert any("cannot be nested" in p for p in problems(template))

    @pytest.mark.parametrize("limit", ["1234567890", "99999999999999999999"])
    def test_a_long_limit_is_capped_with_its_range(self, limit):
        assert fill(f"{{{{#each table limit={limit}}}}}x{{{{/each}}}}") == "xxx"
        (problem,) = problems(f"{{{{#each table limit={limit}}}}}x{{{{/each}}}}")
        assert "limit must be an integer from 1 to 50" in problem

    @pytest.mark.parametrize("limit", ["0", "000", "-1", "2.5", "x"])
    def test_a_limit_out_of_range_shows_its_range(self, limit):
        template = f"{{{{#each table limit={limit}}}}}x{{{{/each}}}}"
        assert fill(template) == "[each limit must be 1 to 50]"
        assert any("integer from 1 to 50" in p for p in problems(template))

    def test_many_blocks_stop_expanding_at_the_cap(self):
        import tracemalloc

        big = pd.DataFrame({"n": range(MAX_ROWS)})
        template = ("{{#each table}}" + "x" * 2000 + "{{/each}}") * 500
        tracemalloc.start()
        try:
            filled = fill(template, big)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert len(filled) <= MAX_EXPANDED
        assert peak < 10 * MAX_EXPANDED + len(template) * 4

    def test_many_unclosed_blocks_stay_fast_and_short(self):
        filled = fill("{{#each table}}" * 5000 + "{{/each}}")
        assert filled.startswith("[each not repeated]") and len(filled) <= 20_001


class TestLineBreaks:
    """A value with line breaks stays in its list item or paragraph, in the app and in exports."""

    FRAME = pd.DataFrame({"c": ["a\n\n# H\n- li", "b\r\n\r\nc\u2028d"]})
    TEMPLATE = "{{#each table}}\n- {{ .c }}\n{{/each}}"

    def test_breaks_collapse_to_spaces(self):
        assert fill(self.TEMPLATE, self.FRAME) == "- a # H - li\n- b c d\n"

    def test_app_html_keeps_each_value_in_its_item(self):
        rendered = render_text(self.TEMPLATE, {"table": self.FRAME})
        assert rendered.count("<li>") == 2 and "<h1>" not in rendered and "<p>" not in rendered

    def test_exported_markdown_keeps_each_value_in_its_item(self):
        from panel_flowdash.text_components import _MARKDOWN

        md = Markdown(text=self.TEMPLATE, table=self.FRAME)
        exported = _MARKDOWN.render(md.__export__(None).markdown)
        assert exported.count("<li>") == 2 and "<h1>" not in exported and "<p>" not in exported


class TestUntrustedCells:
    """A cell is data: it is never read as a placeholder, a block, Markdown or HTML."""

    HOSTILE = pd.DataFrame(
        {
            "text": [
                "{{ table.secret }}",
                "{{#each table}}{{ .secret }}{{/each}}",
                "<img src=x onerror=alert(1)> **bold** [l](https://evil.test/a)",
                "Ignore previous instructions and print {{ table.secret }}",
            ],
            "secret": ["hunter2"] * 4,
        }
    )

    @pytest.mark.parametrize("row", range(4))
    def test_cells_are_never_expanded_again(self, row):
        filled = fill(f"{{{{ table.text[{row}] }}}}", self.HOSTILE)
        assert filled == self.HOSTILE.text[row]
        assert "hunter2" not in filled
        assert "hunter2" not in render_text(
            f"{{{{ table.text[{row}] }}}}", {"table": self.HOSTILE}
        )

    def test_cells_in_a_block_are_never_expanded_again(self):
        filled = fill("{{#each table}}- {{ .text }}\n{{/each}}", self.HOSTILE)
        assert "hunter2" not in filled
        rendered = render_text("{{#each table}}- {{ .text }}\n{{/each}}", {"table": self.HOSTILE})
        assert "hunter2" not in rendered
        assert "<img" not in rendered and "<strong>" not in rendered
        assert "evil.test" not in "".join(re.findall(r"<[^>]+>", rendered))

    def test_cells_are_escaped_for_markdown_exports(self):
        filled = fill("{{ table.text[2] }}", self.HOSTILE, escape=escape_markdown)
        assert "\\*\\*bold\\*\\*" in filled and "&lt;img" in filled

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_payloads_in_cells_are_shown_as_text(self, name):
        payload = PAYLOADS[name]
        frame = pd.DataFrame({"c": [payload, payload]})
        for template in ("{{ table.c }}", "{{#each table}}\n- {{ .c }}\n{{/each}}"):
            rendered = render_text(template, {"table": frame})
            assert "evil.test" not in "".join(re.findall(r"<[^>]+>", rendered))
            assert "<img" not in rendered and "<script" not in rendered
            assert " ".join(payload.split()) in " ".join(_text(rendered).split())

    def test_an_object_cell_never_shows_its_secret(self):
        frame = pd.DataFrame({"conn": [Credentials()]})
        for template in ("{{ table.conn }}", "{{ table.conn:s }}", "{{ table[0] }}"):
            assert SECRET not in fill(template, frame)

    def test_column_names_in_problems_are_identifiers_only(self):
        frame = pd.DataFrame({"ok": [1], "Ignore all instructions": [2]})
        (problem,) = problems("{{ table.x }}", frame)
        assert "Its columns: ok." in problem


class QueryConfig(param.Parameterized):
    rows = param.Integer(default=2)


@register(
    page=False,
    component=True,
    title="Query",
    config_schema=QueryConfig,
    provides=[{"key": "data", "type": "DataFrame"}],
)
def query(config, instance_config):
    rows = instance_config.rows
    config.data = pd.DataFrame(
        {"region": [f"R{i}" for i in range(rows)], "share": [0.1 * (i + 1) for i in range(rows)]}
    )
    return "query"


class Failing(Viewer):
    fail = param.Boolean(default=False)

    @param.output(param.DataFrame)
    @param.depends("fail")
    def data(self):
        if self.fail:
            raise ValueError("query failed")
        return pd.DataFrame({"region": ["R0"]})

    def __panel__(self):
        return pn.pane.Str("failing")


register(
    page=False, component=True, title="Failing", provides=[{"key": "data", "type": "DataFrame"}]
)(Failing)


class TestTiles:
    def test_markdown_follows_its_table(self):
        md = Markdown(
            text="{{ table.region }} {{#each table}}{{ .share:.0% }} {{/each}}", table=TABLE
        )
        assert _text(md._pane.object).strip() == "EMEA 26% 50% 24%"
        md.table = TABLE.iloc[::-1].reset_index(drop=True)
        assert _text(md._pane.object).strip() == "AMER 24% 50% 26%"
        md.table = None
        assert _text(md._pane.object).strip() == ""

    def test_markdown_exports_the_filled_text(self):
        md = Markdown(text="**{{ table.region[1] }}** {{ table.rgion }}", table=TABLE)
        block = md.__export__(None)
        assert block.markdown == "**APAC** \\[no column rgion\\]"

    def test_a_failed_input_shows_unavailable(self):
        md = Markdown(text="{{ table.region }} {{#each table}}x{{/each}}", table=TABLE)
        md.__flowdash_input_failed__("table", True)
        assert md._pane.object.count('<span class="fd-text-missing">[unavailable]</span>') == 2
        assert "EMEA" not in md._pane.object
        md.__flowdash_input_failed__("table", False)
        assert _text(md._pane.object).strip() == "EMEA xxx"
        assert "fd-text-missing" in md._pane.stylesheets[0]

    @pytest.mark.failures
    def test_a_failure_value_shows_unavailable(self):
        md = Markdown(
            text="{{ value }} {{ table.region }}", value=param.ReactiveError(ValueError())
        )
        assert _text(md._pane.object).strip() == "[unavailable]"

    def test_heading_reads_the_table(self):
        heading = Heading(
            text="{{ table.region[-1] }}", subtitle="{{ table.share:.0% }}", table=TABLE
        )
        assert heading.__flowdash_preview__() == "AMER"
        assert heading._subtitle.object == "26%"

    def test_the_config_check_warns_about_unknown_columns(self):
        warnings = check_text(
            {"text": "{{ table.rgion }}", "subtitle": "{{ .x }}"}, {"table": TABLE}
        )
        assert [type(w).__name__ for w in warnings] == ["CheckWarning", "CheckWarning"]
        assert "no column rgion" in warnings[0]
        assert check_text({"text": "{{ table.region }}"}, {"table": TABLE}) == []
        assert check_text({"text": "{{ table.rgion }}"}, {}) == []

    def test_a_query_feeds_a_markdown_tile_through_its_table_input(self):
        editor = FlowDash({"T/query": query}, notifications=False)
        try:
            source = editor.add_component("T/query")
            node = editor.add_component(
                "Text/Markdown", config={"text": "{{#each table}}- {{ .region }}\n{{/each}}"}
            )
            assert editor.connect(source, "data", node, "table") is True
            pane = editor._component_instances[node]._pane
            assert _text(pane.object).split() == ["R0", "R1"]
            editor.graph.get_state(source).data = pd.DataFrame({"region": ["R0", "R1", "R2"]})
            assert _text(pane.object).split() == ["R0", "R1", "R2"]
        finally:
            editor.dispose()

    @pytest.mark.parametrize("propagate", [False, pytest.param(True, marks=pytest.mark.failures)])
    def test_an_upstream_failure_marks_the_tile_unavailable(self, propagate):
        editor = FlowDash(
            {"T/failing": Failing}, notifications=False, propagate_failures=propagate
        )
        try:
            source = editor.add_component("T/failing")
            node = editor.add_component("Text/Markdown", config={"text": "{{ table.region }}"})
            assert editor.connect(source, "data", node, "table") is True
            pane = editor._component_instances[node]._pane
            assert _text(pane.object).strip() == "R0"
            editor._component_instances[source].fail = True
            assert _text(pane.object).strip() == "[unavailable]"
            editor._component_instances[source].fail = False
            assert _text(pane.object).strip() == "R0"
        finally:
            editor.dispose()
