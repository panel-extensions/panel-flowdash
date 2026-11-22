"""Report exports: the document built from a report, and the HTML, Markdown, Word, notebook and PDF writers."""

import io
import json
import types
import zipfile

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest

from panel_flowdash import FlowDash, register
from panel_flowdash.export import (
    KPI,
    Chart,
    ExportUnavailable,
    Heading,
    Placeholder,
    Section,
    Table,
    Text,
    build_document,
    cell_text,
    check_format,
    export_report,
    export_report_async,
    render_chart,
    table_block,
    write,
)
from panel_flowdash.export.build import PENDING_TEXT, RESTRICTED_TEXT

SECRET = "sk-live-0123456789abcdef"


class Credentials:
    """An object whose every textual form gives its secret away."""

    def __str__(self):
        return SECRET

    __repr__ = __str__

    def __format__(self, spec):
        return SECRET


@register(
    page=False,
    component=True,
    title="Sales",
    provides=[
        {"key": "frame", "type": "DataFrame"},
        {"key": "total", "type": "float"},
        {"key": "creds", "type": "any"},
    ],
)
def sales(config):
    config.frame = pd.DataFrame(
        {
            "region": [f"R{i}" for i in range(60)],
            "amount": [i * 1000.5 for i in range(60)],
            "conn": [Credentials()] * 60,
        }
    )
    config.total = 1234.6
    config.creds = Credentials()
    return "sales"


def _spec(frame):
    if frame is None:
        return None
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": frame[["region", "amount"]].head(5).to_dict(orient="records")},
        "mark": "bar",
        "encoding": {
            "x": {"field": "region", "type": "nominal"},
            "y": {"field": "amount", "type": "quantitative"},
        },
        "width": "container",
    }


@register(
    page=False,
    component=True,
    title="Bars",
    tags=["view", "chart"],
    extensions=["vega"],
    requires=[{"key": "frame", "type": "DataFrame"}],
)
class bars(pn.viewable.Viewer):
    frame = param.DataFrame(default=None, allow_None=True)

    def __panel__(self):
        return pn.Column(
            pmui.Typography("Sales by region", variant="subtitle2"),
            pn.pane.Vega(pn.bind(_spec, self.param.frame)),
        )


@register(page=False, component=True, title="Leak", tags=["view"])
class leak(pn.viewable.Viewer):
    """A view showing an object that holds a credential."""

    def __panel__(self):
        return pn.Column(pn.pane.Str(Credentials()), pn.pane.Markdown("Shown"))


@register(page=False, component=True, title="Own", tags=["view"])
class own(pn.viewable.Viewer):
    """A component exporting itself."""

    def __export__(self, ctx):
        return [Heading("Own heading", 2), Text(f"Rows up to {ctx.max_rows}")]

    def __panel__(self):
        return pn.pane.Markdown("not this")


@register(page=False, component=True, title="Broken", tags=["view"])
class broken(pn.viewable.Viewer):
    def __export__(self, ctx):
        raise RuntimeError(f"password={SECRET}")

    def __panel__(self):
        return pn.pane.Markdown("broken")


@register(page=False, component=True, title="Vault", provides=[{"key": "value", "type": "int"}])
def vault(config):
    config.value = 42
    return "vault"


COMPONENTS = {
    "E/sales": sales,
    "E/bars": bars,
    "E/leak": leak,
    "E/own": own,
    "E/broken": broken,
    "E/vault": vault,
}

DOC = """\
flowdash: 1
title: Sales
turns:
- {id: t1, actor: user, at: '2026-11-22T10:00:00Z'}
- {id: t2, status: pending, question: Which region grew fastest}
groups:
  detail: {label: Detail by region}
nodes:
  sales: {component: E/sales, position: [0, 0]}
  kpi: {component: Widgets/KPI, config: {label: Revenue, prefix: '$'}, position: [300, 0]}
  note:
    component: Text/Markdown
    config: {text: 'Total is **{{ value:,.0f }}**'}
    position: [300, 200]
  bars: {component: E/bars, group: detail, position: [600, 0]}
  leak: {component: E/leak, position: [600, 200]}
  own: {component: E/own, position: [600, 400]}
  vault: {component: E/vault, position: [900, 0]}
edges:
- {from: sales.total, to: kpi.value}
- {from: sales.total, to: note.value}
- {from: sales.frame, to: bars.frame}
layout:
  tiles:
  - {node: kpi}
  - {node: note}
  - {node: bars}
  - {node: sales}
  - {node: leak}
  - {node: own}
  - {node: vault}
"""

REPORT = """\
report:
  title: Sales review
  blocks:
  - heading: Summary
  - text: Revenue is {{ sales.total:,.0f }} with {{ sales.creds }}.
  - node: kpi
  - node: note
  - heading: Detail
    page_break: true
  - node: bars
    caption: The first five regions
  - node: sales
    show: table
  - node: vault
  - node: leak
  - node: own
  - turn: t2
"""


def _editor(**params):
    return FlowDash(
        components=COMPONENTS,
        notifications=False,
        user="alice",
        history_coalesce=0,
        **params,
    )


@pytest.fixture
def editor():
    editor = _editor()
    editor.new_dashboard("Sales")
    editor.import_dashboard(DOC + REPORT)
    yield editor
    editor.dispose()


@pytest.fixture
def restricted(editor):
    """The same dashboard, opened by someone who may not use the Vault component."""
    other = _editor(authorize_component=lambda entry: entry.app_id != "E/vault")
    other.load_model(editor.to_model())
    yield other
    other.dispose()


def _sections(document):
    return {b.title: b for b in document.blocks if isinstance(b, Section)}


def _texts(data: bytes, format: str) -> str:
    """Return every text part of an exported file, a Word file's XML parts included."""
    if format == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return "\n".join(
                archive.read(name).decode("utf-8", "replace")
                for name in archive.namelist()
                if name.endswith((".xml", ".rels"))
            )
    return data.decode("utf-8")


class TestDocument:
    def test_report_blocks_become_sections_and_blocks(self, editor):
        document = build_document(editor)
        assert document.title == "Sales review"
        summary, detail = _sections(document)["Summary"], _sections(document)["Detail"]
        assert not summary.page_break_before and detail.page_break_before
        text, kpi, note = summary.blocks
        assert text == Text("Revenue is 1,235 with Credentials.")
        assert kpi == KPI(label="Revenue", value="$1,234.60", node="kpi")
        assert note == Text("Total is **1,235**", node="note")

    def test_nodes_export_through_hooks_views_and_tables(self, editor):
        blocks = _sections(build_document(editor))["Detail"].blocks
        title, chart, table, vault, *rest = blocks
        assert title == Text("**Sales by region**", node="bars")
        assert isinstance(chart, Chart) and chart.caption == "The first five regions"
        assert chart.svg.lstrip().startswith("<svg") and chart.png.startswith(b"\x89PNG")
        assert isinstance(table, Table) and table.node == "sales"
        assert table.columns == ["region", "amount", "conn"]
        assert len(table.rows) == 50 and table.total_rows == 60
        assert table.rows[2] == ["R2", "2,001", "Credentials"]
        assert vault.node == "vault" and not isinstance(vault, Placeholder)
        assert Heading("Own heading", 2) in rest and Text("Rows up to 50", node="own") in rest

    def test_charts_are_drawn_only_as_the_format_needs(self, editor):
        chart = next(b for b in build_document(editor, "html").walk() if isinstance(b, Chart))
        assert chart.svg and chart.png is None
        chart = next(b for b in build_document(editor, "docx").walk() if isinstance(b, Chart))
        assert chart.png and chart.svg is None

    def test_pending_questions_are_left_out_or_listed_never_run(self, editor):
        ran = []
        editor.run_pending = ran.append
        assert not any(
            isinstance(b, Placeholder) and b.reason == PENDING_TEXT
            for b in build_document(editor).walk()
        )
        document = build_document(editor, include_pending=True)
        pending = [b for b in document.walk() if getattr(b, "reason", None) == PENDING_TEXT]
        assert [b.title for b in pending] == ["Which region grew fastest"]
        assert ran == []

    def test_restricted_nodes_become_placeholders(self, restricted):
        document = build_document(restricted)
        vault = [b for b in document.walk() if getattr(b, "node", None) == "vault"]
        assert vault == [Placeholder(RESTRICTED_TEXT, node="vault")]

    def test_a_failing_hook_is_left_out_without_its_message(self, editor):
        editor.add_component("E/broken", instance_id="broken")
        editor.set_report({"blocks": [{"node": "broken"}]})
        (block,) = build_document(editor).blocks
        assert block == Placeholder("This item could not be exported.", node="broken")

    def test_derived_report_exports_groups_as_sections(self, editor):
        editor.set_report(None)
        document = build_document(editor)
        assert document.title == "Sales"
        assert "Detail by region" in _sections(document)

    def test_provenance_lists_generation_time_and_filters(self, editor):
        document = build_document(editor)
        assert document.provenance.dashboard == "Sales"
        assert document.provenance.generator.startswith("panel-flowdash ")
        assert document.provenance.generated_at.tzinfo is not None
        assert json.dumps(document.to_dict())

    def test_provenance_lists_parameters_with_credentials_masked(self, editor):
        editor.declare_parameter("caption", "kpi", "caption", type="uri")
        item = next(i for i in editor._tile_items if i["instance_id"] == "kpi")
        item["config"]["caption"] = f"postgresql://bob:{SECRET}@db/sales"
        (parameter,) = build_document(editor).provenance.parameters
        assert parameter == ("caption", "postgresql://bob:***@db/sales")

    async def test_async_export_waits_for_outputs(self, editor):
        file = await export_report_async(editor, "md", timeout=2)
        assert file.filename == "Sales_review.md"
        assert file.data.decode().startswith("# Sales review")


class TestHelpers:
    def test_cell_text(self):
        assert cell_text(None) == cell_text(float("nan")) == ""
        assert cell_text(2012) == "2012"
        assert cell_text(1234567.891) == "1,234,567.89"
        assert cell_text(0.256) == "0.256"
        assert cell_text(Credentials()) == "Credentials"
        assert cell_text("x" * 600).endswith("…")

    def test_year_columns_have_no_separators(self):
        block = table_block(pd.DataFrame({"year": [2012.0, None], "mw": [2012.0, 12.5]}))
        assert block.rows == [["2012", "2,012"], ["", "12.5"]]

    def test_table_block_caps_rows(self):
        block = table_block(pd.DataFrame({"a": range(10)}), 3)
        assert block.rows == [["0"], ["1"], ["2"]] and block.total_rows == 10
        assert isinstance(table_block("text"), Placeholder)

    def test_charts_loading_urls_are_not_drawn(self):
        spec = {"data": {"url": "https://example.com/data.csv"}, "mark": "bar"}
        assert isinstance(render_chart(spec), Placeholder)
        image = {"data": {"values": [{"u": "x"}]}, "mark": "image"}
        assert isinstance(render_chart(image), Placeholder)

    def test_unknown_format(self):
        with pytest.raises(ValueError, match="Unknown export format"):
            check_format("pptx")
        assert check_format(".DOCX").name == "docx"


class TestWriters:
    def test_html(self, editor):
        page = export_report(editor, "html").data.decode()
        assert page.startswith("<!doctype html>")
        assert "<title>Sales review</title>" in page and "@media print" in page
        assert 'class="fd-section fd-page-break"' in page
        assert "data:image/svg+xml;base64," in page
        assert "<th>region</th>" in page and "First 50 of 60 rows." in page
        assert '<div class="fd-kpi-value">$1,234.60</div>' in page
        assert "Generated " in page and "<script" not in page

    def test_markdown(self, editor):
        text = export_report(editor, "md", include_pending=True).data.decode()
        assert text.startswith("# Sales review\n")
        assert "## Summary" in text and "## Detail" in text
        assert "**Revenue:** \\$1,234.60" in text
        assert "| region | amount | conn |" in text and "| R2 | 2,001 | Credentials |" in text
        assert "![Chart](data:image/png;base64," in text
        assert "> **Which region grew fastest**: *Not answered yet.*" in text

    def test_docx(self, editor):
        docx = pytest.importorskip("docx")
        file = export_report(editor, "docx")
        assert file.filename == "Sales_review.docx"
        doc = docx.Document(io.BytesIO(file.data))
        headings = [
            p.text for p in doc.paragraphs if p.style.name.startswith(("Heading", "Title"))
        ]
        assert headings[:3] == ["Sales review", "Summary", "Detail"]
        assert "Own heading" in headings
        (table,) = doc.tables
        assert len(table.rows) == 51 and table.rows[0].cells[0].text == "region"
        assert len(doc.inline_shapes) == 1
        assert any("First 50 of 60 rows." in p.text for p in doc.paragraphs)
        assert doc.core_properties.title == "Sales review"

    def test_notebook(self, editor):
        nbformat = pytest.importorskip("nbformat")
        notebook = nbformat.reads(export_report(editor, "ipynb").data.decode(), as_version=4)
        nbformat.validate(notebook)
        cells = notebook.cells
        assert cells[0].cell_type == "markdown" and cells[0].source.startswith("# Sales review")
        assert "load_dashboard(DASHBOARD)" in cells[1].source
        assert "component: E/bars" in cells[1].source
        bars = next(c for c in cells if c.source == 'show_node(dashboard, "bars")')
        assert any("image/png" in output.data for output in bars.outputs)
        table = next(c for c in cells if c.source == 'show_node(dashboard, "sales")')
        assert "<th>region</th>" in table.outputs[0].data["text/html"]

    def test_notebook_loads_the_dashboard_it_embeds(self, editor):
        pytest.importorskip("nbformat")
        from panel_flowdash.export import load_dashboard, show_node
        from panel_flowdash.export.notebook import preamble

        text = export_report(editor, "ipynb").data.decode()
        source = json.loads(text)["cells"][1]["source"]
        source = "".join(source) if isinstance(source, list) else source
        assert preamble(None) in source
        namespace = {}
        exec(source.split("dashboard = load_dashboard")[0], namespace)
        loaded = load_dashboard(namespace["DASHBOARD"], components=COMPONENTS)
        try:
            assert show_node(loaded, "bars") is not None
            with pytest.raises(KeyError):
                show_node(loaded, "nope")
        finally:
            loaded.dispose()

    def test_pdf(self, editor):
        from panel_flowdash.export.pdf import available_engine

        if available_engine() is None:
            with pytest.raises(ExportUnavailable, match="WeasyPrint"):
                export_report(editor, "pdf")
            return
        try:
            data = export_report(editor, "pdf").data
        except ExportUnavailable:
            pytest.skip("Chromium is not installed for Playwright.")
        assert data.startswith(b"%PDF")

    @pytest.mark.parametrize("format", ["html", "md", "docx", "ipynb"])
    def test_no_secret_or_object_repr_in_any_format(self, editor, format):
        pytest.importorskip({"docx": "docx", "ipynb": "nbformat"}.get(format, "json"))
        editor.add_component("E/broken", instance_id="broken")
        report = editor.report
        editor.set_report({**report, "blocks": [*report["blocks"], {"node": "broken"}]})
        text = _texts(export_report(editor, format, include_pending=True).data, format)
        assert SECRET not in text
        assert " object at 0x" not in text
        assert "Credentials" in text

    def test_restricted_node_in_every_format(self, restricted):
        for format in ("html", "md"):
            assert RESTRICTED_TEXT in export_report(restricted, format).data.decode()


class TestAgent:
    def test_export_needs_approval_every_time(self):
        pytest.importorskip("pydantic_ai")
        from panel_flowdash.agent import REPORT_TOOLS, approval_policy

        assert "export_report" in REPORT_TOOLS
        policy = approval_policy()
        assert policy.requires_approval("export_report", {"format": "md"})

    async def test_the_tool_exports_and_reports_what_it_left_out(self, restricted):
        pytest.importorskip("pydantic_ai")
        from panel_flowdash.agent import DashboardOps
        from panel_flowdash.agent.tools import export_report as tool

        ctx = types.SimpleNamespace(deps=types.SimpleNamespace(ops=DashboardOps(restricted)))
        message = await tool(ctx, "md")
        assert message.startswith("Exported the report as Sales_review.md (")
        assert "1 item(s) are left out" in message

    async def test_unknown_format_is_a_mistake_to_fix(self, editor):
        from panel_flowdash.agent import DashboardOps, OperationError

        with pytest.raises(OperationError, match="Unknown export format"):
            await DashboardOps(editor).export_report("pptx")

    def test_write_takes_a_document(self, editor):
        document = build_document(editor, "md")
        assert write(document, "md").startswith(b"# Sales review")
