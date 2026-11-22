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

_SAFE_URL = re.compile(
    r"^(https?://|data:image/(png|jpe?g|gif|webp|svg\+xml);base64,)", re.IGNORECASE
)
_INLINE_ESCAPE = re.compile(r"([\\`*_\[\]~|$<>])")
_LINE_START = re.compile(r"^([#+=>-])", re.MULTILINE)
_LINE_ORDERED = re.compile(r"^(\d+)([.)])", re.MULTILINE)


def safe_url(url: str | None) -> str | None:
    """Return *url* when it is an http(s) URL or a base64 image data URL, else None."""
    url = (url or "").strip()
    return url if _SAFE_URL.match(url) else None


def _inline_text(text: str) -> str:
    return _INLINE_ESCAPE.sub(r"\\\1", text.replace("&", "&amp;"))


def _destination(url: str) -> str:
    return "<" + re.sub(r"[<>\s]", lambda m: f"%{ord(m.group(0)):02X}", url) + ">"


def _ticks(content: str, least: int = 1) -> str:
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    return "`" * max(least, longest + 1)


def _inline(tokens) -> str:
    parts: list[str] = []
    links: list[str] = []
    for token in tokens or []:
        kind = token.type
        if kind == "text":
            parts.append(_inline_text(token.content))
        elif kind == "softbreak":
            parts.append("\n")
        elif kind == "hardbreak":
            parts.append("\\\n")
        elif kind in ("em_open", "em_close"):
            parts.append("*")
        elif kind in ("strong_open", "strong_close"):
            parts.append("**")
        elif kind in ("s_open", "s_close"):
            parts.append("~~")
        elif kind == "code_inline":
            ticks = _ticks(token.content)
            pad = " " if token.content.startswith("`") or token.content.endswith("`") else ""
            parts.append(f"{ticks}{pad}{token.content}{pad}{ticks}")
        elif kind == "link_open":
            links.append(str(token.attrGet("href") or ""))
            parts.append("[")
        elif kind == "link_close":
            href = links.pop() if links else ""
            parts.append(f"]({_destination(href)})" if href else "]")
        elif kind == "image":
            src = safe_url(str(token.attrGet("src") or ""))
            alt = _inline_text(token.content)
            parts.append(f"![{alt}]({_destination(src)})" if src else alt)
        else:
            # Raw HTML and anything else is shown as the text it is.
            parts.append(_inline_text(token.content))
    return "".join(parts)


def _block_text(text: str) -> str:
    """Keep a paragraph's lines from starting a heading, list or quote they were not."""
    return _LINE_ORDERED.sub(r"\1\\\2", _LINE_START.sub(r"\\\1", text))


def safe_markdown(text: str) -> str:
    """Return Markdown re-rendered from its tokens, as the HTML writer reads it.

    Raw HTML comes out as text, links whose target the HTML writer refuses
    (``javascript:`` and the like) as their text only, and images only from
    http(s) or image data URLs, so a viewer that renders HTML in Markdown,
    such as Jupyter, runs nothing a report's text holds.
    """
    from panel_flowdash.text_components import _MARKDOWN

    blocks: list[str] = []
    stack: list[dict] = []
    lists: list[int | None] = []
    heading = ""
    table: list[list[str]] | None = None

    tight = False
    last_tight = False

    def emit(body: str):
        nonlocal last_tight
        separator = "\n" if tight and last_tight and stack else "\n\n"
        last_tight = tight and bool(stack)
        lines = []
        for line in body.split("\n"):
            prefix = ""
            for entry in stack:
                prefix += entry["rest"] if entry["used"] else entry["first"]
                entry["used"] = True
            lines.append((prefix + line).rstrip())
        blocks.append(separator + "\n".join(lines))

    for token in _MARKDOWN.parse(text or ""):
        kind = token.type
        if kind == "paragraph_open":
            tight = token.hidden
        elif kind in ("fence", "code_block", "hr", "html_block", "heading_open", "table_open"):
            tight = False
        if kind == "blockquote_open":
            stack.append({"first": "> ", "rest": "> ", "used": False})
        elif kind in ("blockquote_close", "list_item_close"):
            stack.pop()
        elif kind == "bullet_list_open":
            lists.append(None)
        elif kind == "ordered_list_open":
            lists.append(int(token.attrGet("start") or 1))
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            number = lists[-1] if lists else None
            marker = "- " if number is None else f"{number}. "
            if number is not None:
                lists[-1] = number + 1
            stack.append({"first": marker, "rest": " " * len(marker), "used": False})
        elif kind == "heading_open":
            heading = "#" * int(token.tag[1:]) + " "
        elif kind == "table_open":
            table = []
        elif kind == "tr_open" and table is not None:
            table.append([])
        elif kind == "table_close" and table:
            head, *rows = table
            lines = ["| " + " | ".join(head) + " |", "| " + " | ".join("---" for _ in head) + " |"]
            lines += ["| " + " | ".join(row) + " |" for row in rows]
            emit("\n".join(lines))
            table = None
        elif kind == "inline":
            content = _inline(token.children)
            if table is not None and table:
                table[-1].append(content.replace("\n", " ").replace("|", "\\|"))
            elif heading:
                emit(heading + _block_text(content.replace("\n", " ")))
                heading = ""
            else:
                emit(_block_text(content))
        elif kind in ("fence", "code_block"):
            fence = _ticks(token.content, 3)
            language = re.sub(r"[^\w+-]", "", (token.info or "").split(" ")[0])
            emit(f"{fence}{language}\n{token.content.rstrip()}\n{fence}")
        elif kind == "hr":
            emit("---")
        elif kind == "html_block":
            emit(_block_text(_inline_text(token.content.strip())))
    return "".join(b for b in blocks if b.strip()).strip("\n")


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
        return safe_markdown(block.markdown)
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
        elif safe_url(block.url):
            image = f"![{_plain(block.alt)}]({_destination(block.url.strip())})"
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
