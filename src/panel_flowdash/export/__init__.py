"""Exporting a dashboard's report as HTML, PDF, Word, Markdown or a Jupyter notebook.

An export is a snapshot: :func:`build_document` reads the report a
dashboard reads as (its own, or the one derived from its groups and tiles)
into an :class:`ExportDocument` of plain blocks, with the outputs as they
are now, and a writer turns that into a file. Questions of the report not
answered yet are never run for an export.

The HTML and Markdown writers need nothing more; charts need
``vl-convert-python``, Word ``python-docx`` and notebooks ``nbformat``
(``pip install 'panel-flowdash[export]'``), and PDF WeasyPrint (the ``pdf``
extra) or Playwright with Chromium. A missing package raises
:class:`ExportUnavailable` naming it.

A component exports itself by defining ``__export__(ctx)``, which returns
a block, a list of blocks or None to be read from its view; ``ctx`` is an
:class:`ExportContext` with helpers for charts and tables.
"""

from __future__ import annotations

import typing as t
from dataclasses import dataclass

from panel_flowdash.export.build import (
    EXPORT_HOOK,
    MAX_ROWS,
    ExportContext,
    ExportUnavailable,
    build_document,
    cell_text,
    render_chart,
    table_block,
    wait_for_outputs,
)
from panel_flowdash.export.document import (
    KPI,
    Block,
    Chart,
    Code,
    ExportDocument,
    Heading,
    Image,
    Placeholder,
    Provenance,
    Section,
    Table,
    Text,
)
from panel_flowdash.export.html import write_html
from panel_flowdash.export.markdown import write_markdown

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash


@dataclass(frozen=True)
class ExportFormat:
    """A format a report exports to: its label, file extension and media type."""

    name: str
    label: str
    extension: str
    mime: str


#: The formats a report exports to, by name.
FORMATS: dict[str, ExportFormat] = {
    f.name: f
    for f in (
        ExportFormat("html", "Web page (HTML)", "html", "text/html"),
        ExportFormat("pdf", "PDF", "pdf", "application/pdf"),
        ExportFormat(
            "docx",
            "Word",
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ExportFormat("md", "Markdown", "md", "text/markdown"),
        ExportFormat("ipynb", "Jupyter notebook", "ipynb", "application/x-ipynb+json"),
    )
}


@dataclass(frozen=True)
class ExportFile:
    """An exported file: its bytes, the name to save it under and its media type."""

    data: bytes
    filename: str
    mime: str
    document: ExportDocument


def check_format(format: str) -> ExportFormat:
    """Return the :class:`ExportFormat` called *format*, raising ValueError for an unknown one."""
    found = FORMATS.get(str(format).lower().lstrip("."))
    if found is None:
        raise ValueError(
            f"Unknown export format {format!r}; expected one of {', '.join(FORMATS)}."
        )
    return found


def dashboard_yaml(editor: FlowDash) -> str:
    """Return the dashboard as its YAML file, as Download writes it, credentials in it masked."""
    from panel_flowdash.agent.yaml_format import editor_restricted_edges
    from panel_flowdash.editor import export_text
    from panel_flowdash.value_text import redact_secrets

    data = editor.export_dashboard()
    return redact_secrets(export_text(data, "yaml", editor_restricted_edges(editor)))


def write(document: ExportDocument, format: str, *, dashboard: str | None = None) -> bytes:
    """Write *document* as *format*; *dashboard* is the dashboard's YAML a notebook loads."""
    name = check_format(format).name
    if name == "html":
        return write_html(document).encode("utf-8")
    if name == "md":
        return write_markdown(document).encode("utf-8")
    if name == "docx":
        from panel_flowdash.export.docx import write_docx

        return write_docx(document)
    if name == "ipynb":
        from panel_flowdash.export.notebook import write_notebook

        return write_notebook(document, dashboard).encode("utf-8")
    from panel_flowdash.export.pdf import write_pdf

    return write_pdf(document)


def export_filename(title: str, format: str) -> str:
    """Return the file name an export of a report titled *title* is saved under."""
    from panel_flowdash.editor import export_filename as filename

    return filename(title, check_format(format).extension)


def export_report(
    editor: FlowDash,
    format: str = "html",
    *,
    include_pending: bool = False,
    max_rows: int = MAX_ROWS,
) -> ExportFile:
    """Export the report of *editor*'s dashboard as it is now, with its user's rights.

    Nodes the user may not use are left out, with a note in their place.
    Use :func:`export_report_async` in a server, which first waits for
    outputs that are still computing.

    Parameters
    ----------
    editor
        The editor whose open dashboard is exported.
    format
        One of :data:`FORMATS`: ``"html"``, ``"pdf"``, ``"docx"``, ``"md"`` or ``"ipynb"``.
    include_pending
        Whether questions not answered yet are written as "Not answered yet",
        rather than left out. They are never run.
    max_rows
        How many rows of each table to include.

    Raises
    ------
    ExportUnavailable
        If the format needs a package that is not installed.
    """
    spec = check_format(format)
    document = build_document(
        editor, spec.name, include_pending=include_pending, max_rows=max_rows
    )
    dashboard = dashboard_yaml(editor) if spec.name == "ipynb" else None
    data = write(document, spec.name, dashboard=dashboard)
    return ExportFile(data, export_filename(document.title, spec.name), spec.mime, document)


async def export_report_async(
    editor: FlowDash,
    format: str = "html",
    *,
    include_pending: bool = False,
    max_rows: int = MAX_ROWS,
    timeout: float = 15.0,
) -> ExportFile:
    """Wait up to *timeout* seconds for the outputs to settle, then :func:`export_report`.

    The document is built on the event loop, which owns the session's
    objects; the file is written on a worker thread.
    """
    import asyncio

    spec = check_format(format)
    await wait_for_outputs(editor, timeout=timeout)
    document = build_document(
        editor, spec.name, include_pending=include_pending, max_rows=max_rows
    )
    dashboard = dashboard_yaml(editor) if spec.name == "ipynb" else None
    data = await asyncio.to_thread(write, document, spec.name, dashboard=dashboard)
    return ExportFile(data, export_filename(document.title, spec.name), spec.mime, document)


def load_dashboard(text: str, **params: t.Any) -> FlowDash:
    """Return a :class:`~panel_flowdash.FlowDash` holding the dashboard of a YAML or JSON file's *text*.

    *params* are passed to ``FlowDash``, e.g. ``components=`` naming the
    components the dashboard uses. An exported notebook starts with it.
    """
    from panel_flowdash.editor import FlowDash

    params.setdefault("notifications", False)
    editor = FlowDash(**params)
    editor.new_dashboard("Exported dashboard")
    editor.import_dashboard(text)
    return editor


def show_node(editor: FlowDash, name: str):
    """Return the view of the node called *name*, as its tile shows it."""
    from panel_flowdash.agent.yaml_format import node_names

    names = node_names(editor._tile_items)
    for index, item in enumerate(editor._tile_items):
        if name in (names.get(item["instance_id"]), item["instance_id"]):
            return editor._tile_objects[index]
    raise KeyError(f"The dashboard has no node called {name!r}.")


__all__ = [
    "EXPORT_HOOK",
    "FORMATS",
    "KPI",
    "MAX_ROWS",
    "Block",
    "Chart",
    "Code",
    "ExportContext",
    "ExportDocument",
    "ExportFile",
    "ExportFormat",
    "ExportUnavailable",
    "Heading",
    "Image",
    "Placeholder",
    "Provenance",
    "Section",
    "Table",
    "Text",
    "build_document",
    "cell_text",
    "check_format",
    "dashboard_yaml",
    "export_filename",
    "export_report",
    "export_report_async",
    "load_dashboard",
    "render_chart",
    "show_node",
    "table_block",
    "wait_for_outputs",
    "write",
]
