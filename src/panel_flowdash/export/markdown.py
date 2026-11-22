"""The Markdown writer: one file, images as data URLs, tables as pipe tables."""

from __future__ import annotations

import base64
import re

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


def _plain(text: str) -> str:
    from panel_flowdash.export.build import markdown_text

    return markdown_text(" ".join((text or "").split()))


def _cell(text: str) -> str:
    return _plain(text).replace("|", "\\|")


def _image(alt: str, mime: str, data: bytes) -> str:
    return f"![{_plain(alt)}](data:{mime};base64,{base64.b64encode(data).decode('ascii')})"


def _table(block: Table) -> str:
    if not block.columns:
        return ""
    lines = [
        "| " + " | ".join(_cell(c) for c in block.columns) + " |",
        "| " + " | ".join("---" for _ in block.columns) + " |",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in block.rows]
    notes = []
    if block.caption:
        notes.append(_plain(block.caption))
    if block.total_rows > len(block.rows):
        notes.append(f"First {len(block.rows):,} of {block.total_rows:,} rows.")
    if notes:
        lines += ["", "*" + " ".join(notes) + "*"]
    return "\n".join(lines)


def _fence(source: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", source)), default=0)
    return "`" * max(3, longest + 1)


def _block(block: Block, level: int) -> str:
    if isinstance(block, Heading):
        return "#" * min(block.level + 1, 6) + " " + _plain(block.text)
    if isinstance(block, Text):
        return block.markdown.strip()
    if isinstance(block, KPI):
        line = f"**{_plain(block.label)}:** {_plain(block.value)}"
        return line + (f" ({_plain(block.detail)})" if block.detail else "")
    if isinstance(block, Chart):
        if block.png:
            image = _image(block.title or "Chart", "image/png", block.png)
        elif block.svg:
            image = _image(block.title or "Chart", "image/svg+xml", block.svg.encode("utf-8"))
        else:
            return ""
        title = f"**{_plain(block.title)}**\n\n" if block.title else ""
        caption = f"\n\n*{_plain(block.caption)}*" if block.caption else ""
        return title + image + caption
    if isinstance(block, Image):
        if block.data:
            image = _image(block.alt, block.mime, block.data)
        elif block.url:
            image = f"![{_plain(block.alt)}](<{block.url.replace('>', '%3E')}>)"
        else:
            return ""
        return image + (f"\n\n*{_plain(block.caption)}*" if block.caption else "")
    if isinstance(block, Table):
        return _table(block)
    if isinstance(block, Code):
        fence = _fence(block.source)
        title = f"**{_plain(block.title)}**\n\n" if block.title else ""
        return f"{title}{fence}{block.language}\n{block.source.rstrip()}\n{fence}"
    if isinstance(block, Placeholder):
        title = f"**{_plain(block.title)}**: " if block.title else ""
        return f"> {title}*{_plain(block.reason)}*"
    if isinstance(block, Section):
        parts = []
        if block.title:
            parts.append("#" * min(level + 1, 6) + " " + _plain(block.title))
        parts += [_block(child, level + 1) for child in block.blocks]
        return "\n\n".join(p for p in parts if p)
    return ""


def provenance_markdown(document: ExportDocument) -> str:
    """Return the provenance as a short paragraph and list."""
    p = document.provenance
    source = f" from the dashboard {_plain(p.dashboard)}" if p.dashboard != document.title else ""
    generator = f" by {_plain(p.generator)}" if p.generator else ""
    lines = [f"*Generated {p.generated_text}{source}{generator}. Values are as they were then.*"]
    rows = [*p.filters, *p.parameters]
    if rows:
        lines.append("")
        lines += [f"- **{_plain(k)}:** {_plain(v)}" for k, v in rows]
    return "\n".join(lines)


def write_markdown(document: ExportDocument) -> str:
    """Return *document* as Markdown."""
    parts = [f"# {_plain(document.title)}", provenance_markdown(document)]
    parts += [_block(block, 1) for block in document.blocks]
    return "\n\n".join(p for p in parts if p) + "\n"
