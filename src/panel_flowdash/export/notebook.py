"""The notebook writer: Markdown cells for prose, and code cells that load the dashboard and show a node.

Each node's cell keeps the output it had when exported, so the notebook
reads without running; running it computes the nodes again from the
dashboard embedded in its first code cell.
"""

from __future__ import annotations

import base64
import json

from panel_flowdash.export.build import ExportUnavailable
from panel_flowdash.export.document import (
    Block,
    Chart,
    Code,
    ExportDocument,
    Heading,
    Image,
    Placeholder,
    Section,
    Table,
)
from panel_flowdash.export.markdown import _block as markdown_block, provenance_markdown


def _nbformat():
    try:
        import nbformat
    except ImportError:
        raise ExportUnavailable(
            "Notebook export needs nbformat: pip install 'panel-flowdash[export]'."
        ) from None
    return nbformat


def _string_literal(text: str) -> str:
    """Return *text* as a Python expression: one ``repr``'d string per line, in parentheses."""
    lines = text.splitlines(keepends=True) or [""]
    return "(\n" + "".join(f"    {line!r}\n" for line in lines) + ")"


def preamble(dashboard_yaml: str | None) -> str:
    """Return the first code cell: imports, and the dashboard loaded from its embedded YAML."""
    lines = [
        "import panel as pn",
        "",
        "from panel_flowdash.export import load_dashboard, show_node",
        "",
        "pn.extension()",
    ]
    if dashboard_yaml:
        lines += [
            "",
            "# The dashboard as it was exported. Pass the components the app uses to",
            '# compute its nodes, e.g. load_dashboard(DASHBOARD, components="components/").',
            f"DASHBOARD = {_string_literal(dashboard_yaml)}",
            "",
            "dashboard = load_dashboard(DASHBOARD)",
        ]
    return "\n".join(lines)


def _outputs(nbformat, block: Block) -> list:
    data: dict[str, str] = {}
    if isinstance(block, Chart):
        if block.png:
            data["image/png"] = base64.b64encode(block.png).decode("ascii")
        elif block.svg:
            data["image/svg+xml"] = block.svg
        data["text/plain"] = block.title or "Chart"
    elif isinstance(block, Image) and block.data and block.mime in ("image/png", "image/jpeg"):
        data[block.mime] = base64.b64encode(block.data).decode("ascii")
        data["text/plain"] = block.alt or "Image"
    elif isinstance(block, Table):
        from panel_flowdash.export.html import _table

        data["text/html"] = _table(block)
        data["text/plain"] = f"Table of {block.total_rows:,} rows"
    else:
        text = markdown_block(block, 1)
        if not text:
            return []
        data["text/markdown"] = text
        data["text/plain"] = text
    return [nbformat.v4.new_output("display_data", data=data)]


def _cells(nbformat, blocks: list[Block], level: int, live: bool) -> list:
    cells: list = []
    markdown: list[str] = []

    def flush():
        if markdown:
            cells.append(nbformat.v4.new_markdown_cell("\n\n".join(markdown)))
            markdown.clear()

    shown: set[str] = set()
    for block in blocks:
        node = getattr(block, "node", None)
        if isinstance(block, Section):
            flush()
            if block.title:
                markdown.append(markdown_block(Heading(block.title, level), level))
            cells += _cells(nbformat, block.blocks, level + 1, live)
            continue
        if isinstance(block, Code) and block.language in ("python", "py"):
            flush()
            cells.append(nbformat.v4.new_code_cell(block.source))
            continue
        if live and node and not isinstance(block, (Placeholder, Heading)):
            if node in shown:
                # Its cell already shows the node; keep only this block's output with it.
                cells[-1].outputs += _outputs(nbformat, block)
                continue
            flush()
            shown.add(node)
            cell = nbformat.v4.new_code_cell(f"show_node(dashboard, {json.dumps(node)})")
            cell.outputs = _outputs(nbformat, block)
            cells.append(cell)
            continue
        text = markdown_block(block, level)
        if text:
            markdown.append(text)
    flush()
    return cells


def write_notebook(document: ExportDocument, dashboard_yaml: str | None = None) -> str:
    """Return *document* as a Jupyter notebook (nbformat 4).

    With *dashboard_yaml*, the dashboard file, the notebook loads it and
    each node's cell shows that node; without it the cells only hold the
    exported outputs, as Markdown.
    """
    nbformat = _nbformat()
    cells = [
        nbformat.v4.new_markdown_cell(
            markdown_block(Heading(document.title, 0), 0) + "\n\n" + provenance_markdown(document)
        ),
    ]
    if dashboard_yaml:
        cells.append(nbformat.v4.new_code_cell(preamble(dashboard_yaml)))
    cells += _cells(nbformat, document.blocks, 1, live=bool(dashboard_yaml))
    notebook = nbformat.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            "language_info": {"name": "python"},
        },
    )
    return nbformat.writes(notebook)


__all__ = ["preamble", "write_notebook"]
