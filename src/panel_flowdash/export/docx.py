"""The Word writer, on python-docx: headings and Markdown as paragraphs, native tables, charts as PNG."""

from __future__ import annotations

import dataclasses
import html
import io
import re

from panel_flowdash.export.build import ExportUnavailable
from panel_flowdash.export.document import (
    KPI,
    Block,
    Chart,
    Code,
    ExportDocument,
    Heading,
    Image,
    Placeholder,
    Section,
    Table,
    Text,
)

#: Rows of a table a Word document holds at most, as Lumen's report export does.
MAX_ROWS = 50
#: The width of charts and images, in inches.
CHART_WIDTH = 6

_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~<$=])")
_INLINE = re.compile(r"\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+)`|\[([^\]]+)\]\([^)]*\)")


def _docx():
    try:
        import docx
    except ImportError:
        raise ExportUnavailable(
            "Word export needs python-docx: pip install 'panel-flowdash[export]'."
        ) from None
    return docx


def _plain(text: str) -> str:
    return html.unescape(_ESCAPE.sub(r"\1", text or ""))


def add_runs(paragraph, text: str):
    """Add Markdown *text* to a paragraph, as runs: ``**bold**``, ``*italic*``, ``code`` and link text."""
    position = 0
    for match in _INLINE.finditer(text):
        if match.start() > position:
            paragraph.add_run(_plain(text[position : match.start()]))
        bold, italic, code, link = match.groups()
        run = paragraph.add_run(_plain(bold or italic or code or link or ""))
        run.bold = bold is not None or None
        run.italic = italic is not None or None
        if code is not None:
            run.font.name = "Courier New"
        position = match.end()
    if position < len(text):
        paragraph.add_run(_plain(text[position:]))


def add_markdown(doc, text: str, level: int = 1):
    """Add Markdown as paragraphs: headings, bulleted and numbered lists, quotes and inline styles."""
    paragraph_lines: list[str] = []

    def flush():
        if paragraph_lines:
            add_runs(doc.add_paragraph(), " ".join(paragraph_lines))
            paragraph_lines.clear()

    fenced = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            fenced = not fenced
            continue
        if fenced:
            doc.add_paragraph().add_run(line).font.name = "Courier New"
            continue
        if not stripped:
            flush()
            continue
        if re.match(r"^#{1,6}\s", stripped):
            flush()
            depth = len(stripped) - len(stripped.lstrip("#"))
            doc.add_heading(_plain(stripped.lstrip("#").strip()), level=min(level + depth, 9))
        elif re.match(r"^[-*+]\s", stripped):
            flush()
            add_runs(doc.add_paragraph(style="List Bullet"), stripped[2:])
        elif re.match(r"^\d+[.)]\s", stripped):
            flush()
            add_runs(doc.add_paragraph(style="List Number"), stripped.split(None, 1)[1])
        elif stripped.startswith(">"):
            flush()
            add_runs(doc.add_paragraph(style="Quote"), stripped.lstrip("> "))
        else:
            paragraph_lines.append(stripped)
    flush()


def add_table(doc, block: Table, max_rows: int = MAX_ROWS):
    """Add a :class:`Table` as a native Word table, its header and at most *max_rows* rows."""
    if not block.columns:
        return
    table = doc.add_table(rows=1, cols=len(block.columns))
    table.style = "Table Grid"
    for cell, column in zip(table.rows[0].cells, block.columns, strict=False):
        cell.text = column
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in block.rows[:max_rows]:
        for cell, value in zip(table.add_row().cells, row, strict=False):
            cell.text = value
    shown = min(len(block.rows), max_rows)
    notes = [block.caption] if block.caption else []
    if block.total_rows > shown:
        notes.append(f"First {shown:,} of {block.total_rows:,} rows.")
    if notes:
        _caption(doc, " ".join(notes))


def _caption(doc, text: str):
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    run.italic = True


def add_picture(doc, data: bytes, caption: str = "") -> bool:
    """Add a PNG or JPEG image :data:`CHART_WIDTH` inches wide; False if Word cannot read it."""
    from docx.shared import Inches

    try:
        doc.add_picture(io.BytesIO(data), width=Inches(CHART_WIDTH))
    except Exception:
        return False
    if caption:
        _caption(doc, caption)
    return True


def _add(doc, block: Block, level: int, max_rows: int):
    if isinstance(block, Heading):
        doc.add_heading(block.text, level=min(block.level + 1, 9))
    elif isinstance(block, Text):
        add_markdown(doc, block.markdown, level)
    elif isinstance(block, KPI):
        paragraph = doc.add_paragraph()
        paragraph.add_run(f"{block.label}: ")
        paragraph.add_run(block.value).bold = True
        if block.detail:
            paragraph.add_run(f"  {block.detail}").italic = True
    elif isinstance(block, Chart):
        if block.title:
            doc.add_heading(block.title, level=min(level + 1, 9))
        if not (block.png and add_picture(doc, block.png, block.caption)):
            _caption(doc, "A chart is left out: it has no PNG image.")
    elif isinstance(block, Image):
        if block.data and block.mime in ("image/png", "image/jpeg", "image/gif"):
            if add_picture(doc, block.data, block.caption):
                return
        _caption(doc, f"Image: {block.alt or block.url or 'not included'}")
    elif isinstance(block, Table):
        add_table(doc, block, max_rows)
    elif isinstance(block, Code):
        if block.title:
            doc.add_heading(block.title, level=min(level + 1, 9))
        for line in block.source.splitlines() or [""]:
            doc.add_paragraph().add_run(line).font.name = "Courier New"
    elif isinstance(block, Placeholder):
        paragraph = doc.add_paragraph()
        if block.title:
            paragraph.add_run(f"{block.title}: ").bold = True
        paragraph.add_run(block.reason).italic = True
    elif isinstance(block, Section):
        if block.page_break_before:
            doc.add_page_break()
        if block.title:
            doc.add_heading(block.title, level=min(level, 9))
        for child in block.blocks:
            _add(doc, child, level + 1, max_rows)


def _clean(value):
    """Return *value* with control characters, which XML cannot hold, dropped from every string in it."""
    from panel_flowdash.export.build import strip_control

    if isinstance(value, str):
        return strip_control(value)
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clean(v) for v in value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        changes = {f.name: _clean(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return dataclasses.replace(value, **changes)
    return value


def write_docx(document: ExportDocument, max_rows: int = MAX_ROWS) -> bytes:
    """Return *document* as a Word file."""
    docx = _docx()
    document = _clean(document)
    doc = docx.Document()
    p = document.provenance
    doc.core_properties.title = document.title
    doc.core_properties.created = p.generated_at.replace(tzinfo=None)
    doc.core_properties.comments = f"Generated by {p.generator}" if p.generator else ""
    doc.add_heading(document.title, level=0)
    source = f" from the dashboard {p.dashboard}" if p.dashboard != document.title else ""
    _caption(doc, f"Generated {p.generated_text}{source}. Values are as they were then.")
    for label, value in [*p.filters, *p.parameters]:
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.add_run(f"{label}: ").bold = True
        paragraph.add_run(value)
    for block in document.blocks:
        _add(doc, block, 1, max_rows)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
