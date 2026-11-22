"""Building an :class:`ExportDocument` from the report a dashboard reads as.

The builder reads :func:`~panel_flowdash.report.report_blocks`, so a
dashboard without a report of its own exports the one derived from its
groups and tiles. Each node is asked for its blocks in this order:

1. a node the user may not use becomes a :class:`Placeholder`, and nothing
   of it is read;
2. a block shown as a table, and a data node, gives its first output as a
   :class:`Table`;
3. the component's ``__export__(ctx)`` method, when it has one, returns a
   block, a list of blocks, or None to fall back to the next step;
4. the panes of the node's view, as its tile shows them, are read by type:
   Vega and Vega-Lite charts become images through vl-convert, tables
   their first rows, Markdown, HTML and text their text, images their
   bytes; anything else becomes a :class:`Placeholder`.

Values are written out only as plain data (text, numbers, dates); any other
object is named by its type, so a value holding a credential never shows it.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import enum
import logging
import math
import re
import time
import typing as t
from dataclasses import dataclass, field

from panel_flowdash.export.document import (
    KPI,
    Block,
    Chart,
    ExportDocument,
    Heading,
    Image,
    Placeholder,
    Provenance,
    Section,
    Table,
    Text,
)
from panel_flowdash.value_text import is_plain, object_label, redact_secrets, safe_str

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

logger = logging.getLogger("panel_flowdash")

#: The name of the method a component defines to give its own export blocks.
EXPORT_HOOK = "__export__"

#: Rows of a table an export shows by default.
MAX_ROWS = 50
MAX_CELL = 500
CHART_WIDTH = 640
CHART_HEIGHT = 320

RESTRICTED_TEXT = "You do not have access to this item, so it is not included."
MISSING_TEXT = "This item's component is not available, so it is not included."
PENDING_TEXT = "Not answered yet."
EMPTY_TEXT = "This output has nothing to show yet."
FAILED_TEXT = "This output failed to compute, so it is not included."
INTERACTIVE_TEXT = "{kind}, interactive only, not included in this export."
NO_CHARTS_TEXT = (
    "A chart is left out: rendering charts needs vl-convert-python "
    "(pip install 'panel-flowdash[export]')."
)
REMOTE_DATA_TEXT = (
    "A chart that loads data or images from a URL is left out: exports fetch no URLs."
)


class ExportUnavailable(RuntimeError):
    """An export format needs a package that is not installed; the message says which."""


@dataclass
class ExportContext:
    """What a component's ``__export__(ctx)`` is told about the export being built.

    Attributes
    ----------
    format
        The format the document will be written as (``"html"``, ``"pdf"``,
        ``"docx"``, ``"md"``, ``"ipynb"``), or None when not known.
    max_rows
        How many rows of a table to include.
    node
        The name of the node being exported.
    """

    format: str | None = None
    max_rows: int = MAX_ROWS
    node: str | None = None
    warnings: list[str] = field(default_factory=list)

    def chart(self, spec: dict, title: str = "", caption: str = "") -> Chart | Placeholder:
        """Return a Vega or Vega-Lite *spec* as a :class:`Chart`, or a Placeholder if it cannot be drawn."""
        block = render_chart(spec, self.format, title=title, caption=caption)
        block.node = self.node
        return block

    def table(self, frame: t.Any, caption: str = "") -> Table | Placeholder:
        """Return the first :attr:`max_rows` rows of a DataFrame as a :class:`Table`."""
        block = table_block(frame, self.max_rows, caption=caption)
        block.node = self.node
        return block


# --------------------------------------------------------------------------
# Values as text
# --------------------------------------------------------------------------


def _year(value: t.Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and float(value).is_integer()
        and 1000 <= value <= 2999
    )


def cell_text(value: t.Any, years: bool | None = None) -> str:
    """Return a table cell as text: plain data formatted, any other object named by its type.

    Numbers get thousands separators, except years: whole numbers from 1000
    to 2999, or all numbers of a column *years* says holds years.
    """
    if type(value).__module__ == "numpy" and getattr(value, "ndim", None) == 0:
        try:
            value = value.item()
        except Exception:
            pass
    if value is None:
        return ""
    if not is_plain(value) or isinstance(value, bytes):
        return object_label(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)) and not isinstance(value, enum.Enum):
        if isinstance(value, float) and not math.isfinite(value):
            return "" if math.isnan(value) else str(value)
        if years or (years is None and _year(value)):
            return f"{value:.0f}" if float(value).is_integer() else f"{value:g}"
        if isinstance(value, int) or (value.is_integer() and abs(value) < 1e15):
            return f"{value:,.0f}"
        if 1000 <= abs(value) < 1e15:
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{value:.6g}"
    try:
        text = str(value)
    except Exception:
        return type(value).__name__
    if text in ("NaT", "<NA>", "nan"):
        return ""
    text = strip_control(text)
    return text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…"


def _frame(value: t.Any, rows: int) -> tuple[t.Any, int] | None:
    """Return the first *rows* rows of a table-like *value* as pandas, with its length."""
    try:
        import pandas as pd
    except ImportError:
        return None
    if isinstance(value, pd.Series):
        value = value.to_frame()
    if isinstance(value, pd.DataFrame):
        return value.head(rows), len(value)
    # Polars, Arrow and other frames that convert themselves.
    if callable(getattr(value, "to_pandas", None)) and callable(getattr(value, "head", None)):
        try:
            return value.head(rows).to_pandas(), len(value)
        except Exception:
            return None
    return None


def table_block(value: t.Any, max_rows: int = MAX_ROWS, caption: str = "") -> Table | Placeholder:
    """Return a DataFrame's first *max_rows* rows as a :class:`Table`, or a Placeholder if it is none."""
    if value is None:
        return Placeholder(EMPTY_TEXT)
    frame = _frame(value, max_rows)
    if frame is None:
        return Placeholder(f"A {type(value).__name__} is not a table, so it is not included.")
    head, total = frame
    columns = [safe_str(c) for c in head.columns]
    years = [
        head.iloc[:, i].dtype.kind in "iuf"
        and all(_year(v) for v in head.iloc[:, i].dropna().tolist())
        and head.iloc[:, i].notna().any()
        for i in range(head.shape[1])
    ]
    rows = [
        [cell_text(v, y) for v, y in zip(row, years, strict=True)]
        for row in head.itertuples(index=False, name=None)
    ]
    return Table(columns=columns, rows=rows, total_rows=total, caption=caption)


# Characters XML 1.0, and so a Word file, cannot hold.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def strip_control(text: str) -> str:
    """Return *text* without the control characters a Word or XML file cannot hold."""
    return _CONTROL.sub("", text)


_MD_INLINE = re.compile(r"([\\`*_\[\]~|$])")
_MD_BLOCK = re.compile(r"^([#+=>-])", re.MULTILINE)
_MD_ORDERED = re.compile(r"^(\d+)([.)])", re.MULTILINE)
# How escape_markdown keeps MathJax delimiters out of the app's panes, which exports do not need.
_MATH_SPAN = re.compile(r"<span>&#(36|92);</span>")


def markdown_text(text: t.Any) -> str:
    """Escape plain *text* so Markdown shows it as written, without raw HTML."""
    import html

    lines = "\n".join(line.strip() for line in str("" if text is None else text).splitlines())
    escaped = _MD_INLINE.sub(r"\\\1", html.escape(lines, quote=False))
    return _MD_ORDERED.sub(r"\1\\\2", _MD_BLOCK.sub(r"\\\1", escaped))


def clean_markdown(markdown: str) -> str:
    """Return Markdown the app escaped for its own panes as plain Markdown, a dollar sign escaped by a backslash."""
    return _MATH_SPAN.sub(lambda m: "\\" + chr(int(m.group(1))), markdown or "")


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------


def _fetches(value: t.Any) -> bool:
    """Whether a chart spec may load something: a ``url`` anywhere in it, or an image mark."""
    if isinstance(value, dict):
        if isinstance(value.get("url"), (str, dict)):
            return True
        mark = value.get("mark")
        if mark == "image" or (isinstance(mark, dict) and mark.get("type") == "image"):
            return True
        if value.get("type") == "image" and "encode" in value:
            return True
        return any(_fetches(v) for k, v in value.items() if k != "values")
    if isinstance(value, list):
        return any(_fetches(v) for v in value)
    return False


def _convert(function: t.Callable, spec: dict, **options: t.Any) -> t.Any:
    # vl-convert 1.x takes a per-call allowlist; 2.x only a global one, which other
    # users of vl-convert in the process rely on, so specs that fetch are refused first.
    try:
        return function(spec, allowed_base_urls=[], **options)
    except TypeError as exc:
        if "allowed_base_urls" not in str(exc):
            raise
    return function(spec, **options)


#: The font exported charts are measured and drawn in: vl-convert measures text with
#: Liberation Sans, which has Arial's metrics, so a browser drawing the SVG in Arial
#: fits labels in the space they were given.
CHART_FONT = "Arial, Helvetica, sans-serif"
LABEL_LIMIT = 160


def _sized(spec: dict) -> dict:
    spec = copy.deepcopy(spec)
    for key, default in (("width", CHART_WIDTH), ("height", CHART_HEIGHT)):
        if spec.get(key) == "container":
            spec[key] = default
    autosize = spec.get("autosize")
    if isinstance(autosize, dict) and str(autosize.get("type", "")).startswith("fit"):
        spec["autosize"] = {**autosize, "type": "pad"}
    elif isinstance(autosize, str) and autosize.startswith("fit"):
        spec["autosize"] = "pad"
    if not _is_vega(spec):
        config = spec["config"] = dict(spec.get("config") or {})
        config["font"] = CHART_FONT
        axis = config["axis"] = dict(config.get("axis") or {})
        axis.setdefault("labelLimit", LABEL_LIMIT)
        spec.setdefault("padding", 8)
    return spec


def _is_vega(spec: dict) -> bool:
    schema = str(spec.get("$schema", ""))
    return "/vega/" in schema and "vega-lite" not in schema


def render_chart(
    spec: dict, format: str | None = None, title: str = "", caption: str = ""
) -> Chart | Placeholder:
    """Draw a Vega or Vega-Lite *spec* with vl-convert: as SVG for HTML and PDF, as PNG otherwise.

    A spec loading data from a URL is not drawn, since an export fetches nothing.
    """
    try:
        import vl_convert as vlc
    except ImportError:
        return Placeholder(NO_CHARTS_TEXT, title=title)
    if not isinstance(spec, dict) or _fetches(spec):
        return Placeholder(REMOTE_DATA_TEXT, title=title)
    spec = _sized(spec)
    vega = _is_vega(spec)
    svg = png = None
    try:
        if format in (None, "html", "pdf"):
            svg = _convert(vlc.vega_to_svg if vega else vlc.vegalite_to_svg, spec)
        if format not in ("html", "pdf"):
            png = _convert(vlc.vega_to_png if vega else vlc.vegalite_to_png, spec, scale=2)
    except Exception as exc:
        logger.warning("Could not draw a chart for an export: %s", redact_secrets(str(exc)))
        return Placeholder("This chart could not be drawn, so it is not included.", title=title)
    return Chart(svg=svg, png=png, title=title, caption=caption)


# --------------------------------------------------------------------------
# Reading a node's view
# --------------------------------------------------------------------------

_HEADING_VARIANTS = ("h1", "h2", "h3", "h4", "h5", "h6", "subtitle1", "subtitle2")
_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|gif|webp|svg\+xml));base64,([A-Za-z0-9+/=\s]+)$")
_INTERACTIVE = {
    "DeckGL": "A map",
    "Bokeh": "A chart",
    "HoloViews": "A chart",
    "Plotly": "A chart",
    "ECharts": "A chart",
    "Perspective": "A table",
    "IPyWidget": "A widget",
    "ReactiveHTML": "A component",
    "JSComponent": "A component",
    "ReactComponent": "A component",
    "AnyWidgetComponent": "A component",
}


def data_url_image(url: str, alt: str = "", node: str | None = None) -> Image | None:
    """Return a base64 image data URL as an :class:`Image`, an http(s) URL as a linked one, else None."""
    url = (url or "").strip()
    match = _DATA_URL.match(url)
    if match:
        try:
            data = base64.b64decode(match.group(2), validate=False)
        except Exception:
            return None
        return Image(data=data, mime=match.group(1), alt=alt, node=node)
    if re.match(r"^https?://", url, re.IGNORECASE):
        return Image(url=url, alt=alt, node=node)
    return None


class _ViewReader:
    """Collect the blocks the visible panes of a view show, in order."""

    def __init__(self, ctx: ExportContext):
        self.ctx = ctx
        self.blocks: list[Block] = []
        self.texts: list[str] = []
        self.left_out: list[str] = []

    def text(self, markdown: str, strong: bool = False):
        markdown = markdown.strip()
        if markdown:
            self.texts.append(f"**{markdown}**" if strong else markdown)

    def flush(self):
        if self.texts:
            self.blocks.append(Text("\n\n".join(self.texts)))
            self.texts = []

    def add(self, block: Block):
        self.flush()
        self.blocks.append(block)

    def read(self, obj: t.Any, depth: int = 0):
        import panel as pn

        if depth > 30 or obj is None or getattr(obj, "visible", True) is False:
            return
        name = type(obj).__name__
        if isinstance(obj, pn.widgets.Tabulator) or isinstance(obj, pn.widgets.DataFrame):
            self.add(self.ctx.table(obj.value))
            return
        if isinstance(obj, pn.widgets.Widget):
            return
        if isinstance(obj, pn.pane.Vega):
            spec = obj.object
            if isinstance(spec, dict):
                self.add(self.ctx.chart(spec))
            elif spec is not None and callable(getattr(spec, "to_dict", None)):
                try:
                    self.add(self.ctx.chart(spec.to_dict()))
                except Exception:
                    self.add(Placeholder("This chart could not be drawn, so it is not included."))
            return
        if isinstance(obj, pn.pane.DataFrame):
            self.add(self.ctx.table(obj.object))
            return
        if isinstance(obj, pn.pane.Matplotlib):
            self.add(self._matplotlib(obj.object))
            return
        if isinstance(obj, pn.pane.image.ImageBase):
            self.add(self._image(obj))
            return
        if name == "Typography":
            value = obj.object
            if isinstance(value, str) and "material-icons" not in value:
                variant = str(getattr(obj, "variant", "") or "")
                self.text(clean_markdown(value), strong=variant in _HEADING_VARIANTS)
            return
        if isinstance(obj, pn.pane.Markdown):
            if isinstance(obj.object, str):
                self.text(clean_markdown(obj.object))
            return
        if isinstance(obj, pn.pane.HTML):
            self._markup(obj)
            return
        if isinstance(obj, pn.pane.Str):
            self.text(markdown_text(safe_str(obj.object)))
            return
        if isinstance(obj, pn.viewable.Viewer):
            self.read(obj.__panel__(), depth + 1)
            return
        inner = getattr(obj, "_inner_layout", None)
        if inner is not None:
            self.read(inner, depth + 1)
            return
        objects = getattr(obj, "objects", None)
        if isinstance(obj, pn.layout.Panel) and isinstance(objects, list):
            for child in objects:
                self.read(child, depth + 1)
            return
        if isinstance(obj, (pn.layout.Spacer, pn.layout.Divider)):
            return
        kind = next(
            (label for cls in type(obj).__mro__ if (label := _INTERACTIVE.get(cls.__name__))),
            "A component",
        )
        if kind not in self.left_out:
            self.left_out.append(kind)
            self.add(Placeholder(INTERACTIVE_TEXT.format(kind=kind)))

    def _markup(self, obj: t.Any):
        from panel_flowdash.text_components import html_to_text

        value = obj.object
        if not isinstance(value, str) or "material-icons" in value:
            return
        self.text(markdown_text(html_to_text(value)))

    def _image(self, obj: t.Any) -> Block:
        value = obj.object
        alt = getattr(obj, "alt_text", None) or ""
        mime = {
            "PNG": "image/png",
            "JPG": "image/jpeg",
            "GIF": "image/gif",
            "SVG": "image/svg+xml",
            "WebP": "image/webp",
        }.get(type(obj).__name__, "image/png")
        if isinstance(value, bytes):
            return Image(data=value, mime=mime, alt=alt)
        if isinstance(value, str):
            if mime == "image/svg+xml" and value.lstrip().startswith("<"):
                return Image(data=value.encode(), mime=mime, alt=alt)
            image = data_url_image(value, alt)
            if image is not None:
                return image
        return Placeholder("An image is left out: exports read no local files.")

    @staticmethod
    def _matplotlib(figure: t.Any) -> Block:
        import io

        if figure is None:
            return Placeholder(EMPTY_TEXT)
        try:
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", dpi=144, bbox_inches="tight")
        except Exception:
            return Placeholder("This chart could not be drawn, so it is not included.")
        return Chart(png=buffer.getvalue())


def _kpi_from(blocks: list[Block]) -> list[Block]:
    """Read a KPI tile drawn as text, a label then a number, as a :class:`KPI`."""
    if len(blocks) != 1 or not isinstance(blocks[0], Text):
        return blocks
    from panel_flowdash.text_components import html_to_text, render_markdown

    lines = [
        html_to_text(render_markdown(part, None)) for part in blocks[0].markdown.split("\n\n")
    ]
    lines = [line for line in lines if line]
    if len(lines) < 2 or len(lines) > 4:
        return blocks
    return [KPI(label=lines[0], value=lines[1], detail=" ".join(lines[2:]))]


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------


def _call_hook(instance: t.Any, ctx: ExportContext) -> list[Block] | None:
    hook = getattr(instance, EXPORT_HOOK, None)
    if hook is None or not callable(hook):
        return None
    try:
        result = hook(ctx)
    except Exception as exc:
        logger.exception("The export hook of %s failed", type(instance).__name__)
        ctx.warnings.append(redact_secrets(str(exc)))
        return [Placeholder("This item could not be exported.")]
    if result is None:
        return None
    blocks = list(result) if isinstance(result, (list, tuple)) else [result]
    from panel_flowdash.export.document import BLOCK_TYPES

    if not all(isinstance(b, BLOCK_TYPES) for b in blocks):
        logger.warning(
            "The export hook of %s returned something other than export blocks",
            type(instance).__name__,
        )
        return [Placeholder("This item could not be exported.")]
    return blocks


class _Builder:
    def __init__(self, editor: FlowDash, format: str | None, include_pending: bool, max_rows: int):
        from panel_flowdash.agent.yaml_format import node_names

        self.editor = editor
        self.format = format
        self.include_pending = include_pending
        self.max_rows = max_rows
        self.items = {item["instance_id"]: item for item in editor._tile_items}
        self.names = node_names(editor._tile_items)
        self.used: set[str] = set()
        self.warnings: list[str] = []

    def ctx(self, instance_id: str) -> ExportContext:
        ctx = ExportContext(
            format=self.format, max_rows=self.max_rows, node=self.names.get(instance_id)
        )
        ctx.warnings = self.warnings
        return ctx

    def node(self, instance_id: str, block: t.Mapping | None = None) -> list[Block]:
        from panel_flowdash.agent.preview import output_failures

        editor = self.editor
        block = block or {}
        item = self.items.get(instance_id)
        if item is None or instance_id in self.used:
            return []
        self.used.add(instance_id)
        name = self.names.get(instance_id)
        caption = block.get("caption") or ""
        if editor._node_restricted(instance_id):
            return [Placeholder(RESTRICTED_TEXT, node=name)]
        if "placeholder" in item:
            return [Placeholder(MISSING_TEXT, node=name)]
        ctx = self.ctx(instance_id)
        spec = editor._component_specs.get(item["component_id"])
        blocks: list[Block] | None = None
        if self._shows_table(item, block):
            port = spec.outputs[0].name
            if port in output_failures(editor, instance_id):
                blocks = [Placeholder(FAILED_TEXT)]
            else:
                state = editor.graph.get_state(instance_id)
                blocks = [ctx.table(getattr(state, port, None))]
        if blocks is None:
            instance = editor._component_instances.get(instance_id)
            blocks = _call_hook(instance, ctx)
        if blocks is None:
            view = self._view(instance_id)
            if view is None:
                blocks = [Placeholder(EMPTY_TEXT)]
            else:
                reader = _ViewReader(ctx)
                reader.read(view)
                reader.flush()
                blocks = reader.blocks
                if editor._tile_box(item).kind == "kpi":
                    blocks = _kpi_from(blocks)
        for b in blocks:
            if getattr(b, "node", None) is None and hasattr(b, "node"):
                b.node = name
        if caption:
            last = blocks[-1] if blocks else None
            if isinstance(last, (Chart, Table, Image)) and not last.caption:
                last.caption = caption
            else:
                blocks.append(Text(f"*{markdown_text(caption)}*"))
        return blocks

    def _shows_table(self, item: dict, block: t.Mapping) -> bool:
        from panel_flowdash.port_types import port_types_compatible

        editor = self.editor
        spec = editor._component_specs.get(item["component_id"])
        if spec is None or not spec.outputs:
            return False
        if block.get("show") == "table":
            return True
        if editor._node_role(item["component_id"]) in ("view", "widget"):
            return False
        return port_types_compatible(spec.outputs[0].type, "DataFrame")

    def _view(self, instance_id: str):
        editor = self.editor
        for index, item in enumerate(editor._tile_items):
            if item["instance_id"] == instance_id:
                return editor._tile_objects[index] if index < len(editor._tile_objects) else None
        return None

    def build(self) -> ExportDocument:
        from panel_flowdash.report import block_kind, group_members, report_blocks, turn_outputs

        editor = self.editor
        resolved = report_blocks(editor)
        body = {b["node"] for b in resolved.blocks if "node" in b}
        filters = [iid for iid in resolved.filters if iid not in body]
        self.used.update(filters)
        turns = {turn.id: turn for turn in editor.turns}
        top: list[Block] = []
        section: Section | None = None

        def add(blocks: list[Block]):
            (section.blocks if section is not None else top).extend(blocks)

        def close():
            nonlocal section
            if section is not None and section.blocks:
                top.append(section)
            elif section is not None and section.title:
                top.append(Heading(section.title, section.level))
            section = None

        for block in resolved.blocks:
            kind = block_kind(block)
            page_break = bool(block.get("page_break"))
            if kind == "heading":
                close()
                section = Section(
                    block["heading"], level=block.get("level", 1), page_break_before=page_break
                )
                continue
            if page_break:
                close()
                section = Section("", page_break_before=True)
            if kind == "text":
                from panel_flowdash.report import fill_report_text

                add([Text(clean_markdown(fill_report_text(editor, block["text"])))])
            elif kind == "node":
                add(self.node(block["node"], block))
            elif kind == "group":
                group = editor._groups.get(block["group"])
                if group is None:
                    continue
                blocks = [
                    b for iid in group_members(editor, block["group"]) for b in self.node(iid)
                ]
                if blocks:
                    add([Section(group.label or block["group"], blocks)])
            else:
                turn = turns.get(block["turn"])
                if turn is None:
                    continue
                add(self.turn(turn, turn_outputs))
        close()
        return ExportDocument(
            title=resolved.title, provenance=self.provenance(filters), blocks=top
        )

    def turn(self, turn, turn_outputs) -> list[Block]:
        editor = self.editor
        if turn.status == "pending":
            if not self.include_pending:
                return []
            return [Placeholder(PENDING_TEXT, title=turn.question or "A planned question")]
        redacted = editor.run_redacted(turn.run) if turn.run is not None else editor.read_only
        who = "Answered by the assistant" if turn.actor == "agent" else "Edited by hand"
        title = turn.question if turn.question and not redacted else who
        blocks = [b for iid in turn_outputs(editor, turn.id) for b in self.node(iid)]
        return [Section(title, blocks)] if blocks else []

    def provenance(self, filters: list[str]) -> Provenance:
        import panel_flowdash
        from panel_flowdash.global_filters import filter_label
        from panel_flowdash.report import placeholder_text
        from panel_flowdash.ui.tile_layout import tile_title

        editor = self.editor
        applied: list[tuple[str, str]] = []
        for iid in filters:
            if editor._node_restricted(iid) or iid not in self.items:
                continue
            item = self.items[iid]
            spec = editor._component_specs.get(item["component_id"])
            state = editor.graph.get_state(iid)
            if spec is None or not spec.outputs or state is None:
                continue
            text = placeholder_text(getattr(state, spec.outputs[0].name, None))
            if text:
                label = (item.get("config") or {}).get("label")
                title = label.strip() if isinstance(label, str) and label.strip() else None
                applied.append((title or tile_title(editor, item), redact_secrets(text)))
        global_filters = editor._global_filters
        for column, keep in global_filters.active().items():
            entry = global_filters.entry(column) or {"column": column}
            if keep.op == "in":
                text = ", ".join(cell_text(v) for v in keep.values)
            else:
                text = " to ".join("…" if v is None else cell_text(v) for v in keep.values)
            applied.append((filter_label(entry), text))
        parameters = []
        for name, parameter in editor.parameters.items():
            item = self.items.get(parameter.node)
            if item is None or editor._node_restricted(parameter.node):
                continue
            value = (item.get("config") or {}).get(parameter.field)
            text = redact_secrets(placeholder_text(value))
            parameters.append((name, text))
        current = editor.dashboard
        return Provenance(
            dashboard=current.title if current is not None else "Untitled",
            generated_at=dt.datetime.now(dt.UTC).replace(microsecond=0),
            generator=f"panel-flowdash {panel_flowdash.__version__}",
            parameters=parameters,
            filters=applied,
        )


def build_document(
    editor: FlowDash,
    format: str | None = None,
    *,
    include_pending: bool = False,
    max_rows: int = MAX_ROWS,
) -> ExportDocument:
    """Return the report *editor*'s dashboard reads as, as an :class:`ExportDocument`, as it is now.

    Parameters
    ----------
    editor
        The editor whose dashboard is exported, with the rights of its user.
    format
        The format the document is for, so charts are drawn only as the
        image type it needs; None draws both.
    include_pending
        Whether questions not answered yet are written, as "Not answered
        yet"; they are never run for an export.
    max_rows
        How many rows of each table to include.
    """
    return _Builder(editor, format, include_pending, max_rows).build()


def _exported_nodes(editor: FlowDash) -> list[str]:
    from panel_flowdash.report import report_blocks

    resolved = report_blocks(editor)
    nodes = set(resolved.filters)
    for block in resolved.blocks:
        if "node" in block:
            nodes.add(block["node"])
    if any("group" in b or "turn" in b for b in resolved.blocks):
        nodes.update(item["instance_id"] for item in editor._tile_items)
    return [iid for iid in nodes if not editor._node_restricted(iid)]


def _snapshot(editor: FlowDash, nodes: list[str]) -> tuple:
    values = []
    for iid in nodes:
        state = editor.graph.get_state(iid)
        if state is None:
            continue
        for name in state.param:
            if name != "name":
                values.append(id(getattr(state, name, None)))
    return tuple(values)


async def wait_for_outputs(editor: FlowDash, timeout: float = 15.0, quiet: float = 0.3) -> bool:
    """Wait until the outputs an export reads stop changing for *quiet* seconds, at most *timeout*.

    Returns whether they settled. Outputs computed asynchronously, e.g. a
    query, arrive on the event loop, so this yields to it while waiting.
    """
    nodes = _exported_nodes(editor)
    deadline = time.monotonic() + timeout
    last = _snapshot(editor, nodes)
    still_since = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        current = _snapshot(editor, nodes)
        if current != last:
            last, still_since = current, time.monotonic()
        elif time.monotonic() - still_since >= quiet:
            return True
    return False


__all__ = [
    "EXPORT_HOOK",
    "MAX_ROWS",
    "ExportContext",
    "ExportUnavailable",
    "build_document",
    "cell_text",
    "data_url_image",
    "render_chart",
    "table_block",
    "wait_for_outputs",
]
