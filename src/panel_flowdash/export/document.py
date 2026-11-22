"""The content of an export: plain data built once from a report and handed to every writer."""

from __future__ import annotations

import base64
import datetime as dt
import typing as t
from dataclasses import asdict, dataclass, field, fields


@dataclass
class Heading:
    """A heading, level 1 to 3 below the document's title."""

    text: str
    level: int = 1


@dataclass
class Text:
    """Prose as Markdown, raw HTML shown as text."""

    markdown: str
    node: str | None = None


@dataclass
class KPI:
    """A headline number: its label, the value as text and a muted line after it."""

    label: str
    value: str
    detail: str = ""
    node: str | None = None


@dataclass
class Chart:
    """A chart as an image: SVG markup, PNG bytes or both."""

    svg: str | None = None
    png: bytes | None = None
    title: str = ""
    caption: str = ""
    node: str | None = None


@dataclass
class Table:
    """The first rows of a table as text, and how many rows it has in all."""

    columns: list[str]
    rows: list[list[str]]
    total_rows: int
    caption: str = ""
    node: str | None = None


@dataclass
class Image:
    """An image, embedded as bytes of a *mime* type, or linked by an http(s) *url*."""

    data: bytes | None = None
    mime: str = "image/png"
    url: str | None = None
    alt: str = ""
    caption: str = ""
    node: str | None = None


@dataclass
class Code:
    """Source code, e.g. a node's SQL, shown verbatim."""

    source: str
    language: str = ""
    title: str = ""
    node: str | None = None


@dataclass
class Placeholder:
    """Something the export leaves out, and why: a restricted node or a question not answered yet."""

    reason: str
    title: str = ""
    node: str | None = None


@dataclass
class Section:
    """A titled run of blocks: a heading of the report and what follows it, a group or a question."""

    title: str
    blocks: list[Block] = field(default_factory=list)
    level: int = 1
    page_break_before: bool = False


Block = Heading | Text | KPI | Chart | Table | Image | Code | Placeholder | Section

BLOCK_TYPES: tuple[type, ...] = (
    Heading,
    Text,
    KPI,
    Chart,
    Table,
    Image,
    Code,
    Placeholder,
    Section,
)


@dataclass
class Provenance:
    """Where an export came from: the dashboard, when, and the values it was computed with.

    Attributes
    ----------
    dashboard
        The dashboard's title.
    generated_at
        When the export was built, in UTC.
    generator
        The package and version that built it, e.g. ``panel-flowdash 0.1.0``.
    parameters
        ``(name, value)`` of the dashboard's parameters, values as text.
    filters
        ``(label, value)`` of the controls and global filters applied, values as text.
    """

    dashboard: str
    generated_at: dt.datetime
    generator: str = ""
    parameters: list[tuple[str, str]] = field(default_factory=list)
    filters: list[tuple[str, str]] = field(default_factory=list)

    @property
    def generated_text(self) -> str:
        """The generation time as a reader sees it, e.g. ``2026-11-22 10:30 UTC``."""
        return self.generated_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass
class ExportDocument:
    """A report as plain data: its title, provenance and blocks, which every writer reads."""

    title: str
    provenance: Provenance
    blocks: list[Block] = field(default_factory=list)

    def walk(self) -> t.Iterator[Block]:
        """Yield every block, those in sections included, in document order."""

        def walk(blocks):
            for block in blocks:
                yield block
                if isinstance(block, Section):
                    yield from walk(block.blocks)

        return walk(self.blocks)

    def to_dict(self) -> dict[str, t.Any]:
        """Return the document as JSON-serializable data, bytes as base64 and each block with its ``type``."""
        return {
            "title": self.title,
            "provenance": {
                **asdict(self.provenance),
                "generated_at": self.provenance.generated_at.isoformat(),
            },
            "blocks": [_block_dict(block) for block in self.blocks],
        }


def _block_dict(block: Block) -> dict[str, t.Any]:
    data: dict[str, t.Any] = {"type": type(block).__name__}
    for f in fields(block):
        value = getattr(block, f.name)
        if isinstance(value, bytes):
            value = base64.b64encode(value).decode("ascii")
        elif f.name == "blocks":
            value = [_block_dict(child) for child in value]
        data[f.name] = value
    return data


__all__ = [
    "BLOCK_TYPES",
    "KPI",
    "Block",
    "Chart",
    "Code",
    "ExportDocument",
    "Heading",
    "Image",
    "Placeholder",
    "Provenance",
    "Section",
    "Table",
    "Text",
]
