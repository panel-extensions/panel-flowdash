"""Compact previews of a node's current value, shown inside its card on the wiring canvas.

A preview is a small, escaped HTML snippet with a fixed height per kind: a
table for a DataFrame, a sparkline for numbers, chips for a list, a few
``key: value`` lines for a dict, a KPI tile, a chart thumbnail or a line of
text for anything else. Each kind keeps its height while the value changes,
so a card only resizes when the kind of its value does, and every preview is
capped in rows, columns, characters and points, so a large value never reaches
the page.

A component can supply its own preview by defining ``__flowdash_preview__``;
see :func:`hook_preview`.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import heapq
import html
import inspect
import itertools
import logging
import math
import numbers
import threading
import typing as t
import weakref
from collections import defaultdict
from dataclasses import dataclass
from functools import partial
from html.parser import HTMLParser

import panel as pn
from panel.io.state import set_curdoc

from panel_flowdash.util import is_failure
from panel_flowdash.value_text import SafeRepr, safe_str

logger = logging.getLogger(__name__)

PREVIEW_HOOK = "__flowdash_preview__"
PREVIEW_KIND_KEY = "__flowdash_preview_kind__"

#: The colour of chart marks, in the card thumbnails and the pop-out alike.
CHART_COLOR = "#2a9d8f"

MAX_ROWS = 4
MAX_COLUMNS = 3
MAX_CELL_CHARS = 24
MAX_CHIPS = 8
MAX_CHIP_CHARS = 24
MAX_ENTRIES = 3
MAX_TEXT_CHARS = 240
MAX_POINTS = 48
MAX_CHART_POINTS = 120
# Below this many numbers a sparkline says less than the numbers themselves.
MIN_POINTS = 3
# A hook's text up to this long, on one line, gets a one-line preview.
MAX_LINE_CHARS = 48

# Builds the text of a container item by item, so a large one is never written out whole.
_REPR = SafeRepr()
_REPR.maxstring = MAX_TEXT_CHARS
_REPR.maxother = MAX_TEXT_CHARS
_REPR.maxlist = _REPR.maxtuple = _REPR.maxset = _REPR.maxfrozenset = _REPR.maxdict = 8
_REPR.maxlevel = 2

_PV_TOP = 6
_PV_HEAD = 22
_PV_GAP = 4
_PV_BOT = 8
_PV_CHROME = _PV_TOP + _PV_HEAD + _PV_GAP + _PV_BOT
_LINE = 16

# The height of each kind of preview, including its header and padding.
HEIGHTS = {
    "value": 22,
    "table": _PV_CHROME + 22 + MAX_ROWS * 19 + 22,
    "series": _PV_CHROME + 36,
    "chips": _PV_CHROME + 36,
    "entries": _PV_CHROME + 10 + MAX_ENTRIES * _LINE,
    "text": _PV_CHROME + 10 + MAX_ENTRIES * _LINE,
    "kpi": _PV_CHROME + 84,
    "chart": _PV_CHROME + 112,
}

PREVIEW_CSS = """
.fd-preview {
  box-sizing: border-box;
  padding: 6px 8px 8px;
  color: var(--fd-muted);
  font-size: 11px;
  line-height: 16px;
  overflow: hidden;
}
.fd-preview-value {
  margin: 4px 8px 6px;
  padding: 0 4px 0 8px;
  border-radius: 6px;
  background: var(--fd-surface);
  display: flex;
  align-items: center;
  gap: 6px;
  line-height: 22px;
  white-space: nowrap;
}
.fd-pvh {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  margin-bottom: 4px;
}
.fd-pv-meta {
  color: var(--fd-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}
.fd-pv-meta-right {
  flex: none;
  color: var(--fd-muted);
  font-size: 10.5px;
  white-space: nowrap;
  margin-left: 4px;
}
.fd-pv-grow { flex: 1; min-width: 4px; }
.fd-popout-btn {
  flex: none;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--fd-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.fd-popout-btn:hover {
  background: var(--fd-hover);
  color: var(--fd-text);
}
.fd-popout-btn .fd-ms { font-size: 14px; }
.fd-preview-value .fd-popout-btn {
  width: 18px;
  height: 18px;
  margin-left: auto;
}
.fd-preview-value .fd-popout-btn .fd-ms { font-size: 13px; }
.fd-pvb {
  border-radius: 6px;
  background: var(--fd-surface);
  overflow: hidden;
  position: relative;
}
.fd-pv-key { flex: none; max-width: 50%; overflow: hidden; text-overflow: ellipsis; }
.fd-pv-val {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--fd-text);
  font-weight: 500;
}
.fd-pv-table-wrap {
  display: flex;
  flex-direction: column;
  height: 120px;
}
.fd-pv-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  color: var(--fd-text);
  font-variant-numeric: tabular-nums;
}
.fd-pv-table th, .fd-pv-table td {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: left;
}
.fd-pv-table th {
  height: 22px;
  padding: 0 6px;
  color: var(--fd-muted);
  font-size: 10.5px;
  font-weight: 500;
  border-bottom: 1px solid var(--fd-divider);
  box-sizing: border-box;
}
.fd-pv-table td {
  height: 19px;
  padding: 0 6px;
  font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px;
  line-height: 19px;
  box-sizing: border-box;
}
.fd-pv-table .fd-num { text-align: right; }
.fd-pv-foot {
  margin-top: auto;
  height: 22px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 0 6px;
  font-size: 10.5px;
  color: var(--fd-muted);
  border-top: 1px solid var(--fd-divider);
  white-space: nowrap;
}
.fd-pv-foot span { overflow: hidden; text-overflow: ellipsis; }
.fd-pv-spark-wrap { padding: 5px 8px; height: 36px; box-sizing: border-box; }
.fd-pv-spark { display: block; width: 100%; height: 26px; overflow: visible; }
.fd-pv-spark polyline {
  fill: none;
  stroke: var(--fd-viz-1, var(--fd-primary));
  stroke-width: 1.6;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.fd-pv-chips {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px;
  height: 36px;
  box-sizing: border-box;
  overflow: hidden;
}
.fd-pv-chip {
  flex: none;
  max-width: 96px;
  height: 20px;
  box-sizing: border-box;
  padding: 0 7px;
  border-radius: 10px;
  border: 1px solid var(--fd-divider);
  background: var(--fd-chip-bg);
  line-height: 18px;
  color: var(--fd-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-pv-entries { padding: 5px 8px; height: 58px; box-sizing: border-box; }
.fd-pv-entry { display: flex; gap: 6px; height: 16px; white-space: nowrap; }
.fd-pv-entry .fd-pv-val { font-weight: 400; }
.fd-pv-text-wrap { padding: 5px 8px; height: 58px; box-sizing: border-box; }
.fd-pv-text {
  color: var(--fd-text);
  white-space: pre-line;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  max-height: 48px;
}
.fd-pv-kpi {
  padding: 8px 10px;
  height: 84px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
}
.fd-kpi-row {
  display: flex;
  align-items: baseline;
  gap: 5px;
  min-width: 0;
}
.fd-kpi-val {
  font-size: 24px;
  line-height: 28px;
  font-weight: 500;
  letter-spacing: -0.3px;
  color: var(--fd-text);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-kpi-unit {
  font-size: 12px;
  line-height: 18px;
  font-weight: 500;
  color: var(--fd-muted);
  padding: 0 5px;
  border-radius: 4px;
  background: var(--fd-chip-bg);
  white-space: nowrap;
  flex: none;
}
.fd-kpi-label {
  font-size: 11px;
  line-height: 15px;
  color: var(--fd-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-kpi-delta {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  line-height: 15px;
  color: var(--fd-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-kpi-delta.fd-pos { color: var(--mui-palette-success-main, #2e7d32); }
.fd-kpi-delta.fd-neg { color: var(--mui-palette-error-main, #d32f2f); }
.fd-kpi-delta .fd-ms { font-size: 13px; }
"""

#: The rules of a chart thumbnail, in a card and in the inspector's Data tab.
CHART_CSS = """
.fd-pv-chart {
  height: 112px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 3px;
}
.fd-pv-chart svg {
  display: block;
  width: 100%;
  height: 100%;
}
.fd-ch-ax { stroke: var(--fd-divider); stroke-width: 1; }
.fd-ch-ln {
  fill: none;
  stroke: var(--fd-viz-1, #2a9d8f);
  stroke-width: 1.8;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.fd-ch-ar { fill: var(--fd-viz-1-soft, rgba(42, 157, 143, 0.18)); }
.fd-ch-pt {
  fill: var(--fd-card-bg, #ffffff);
  stroke: var(--fd-viz-1, #2a9d8f);
  stroke-width: 1.4;
}
.fd-ch-br { fill: var(--fd-viz-1, #2a9d8f); }
.fd-ch-txt {
  fill: var(--fd-muted);
  font-family: Roboto, system-ui, sans-serif;
  font-size: 9.5px;
}
.fd-pv-chart-empty {
  color: var(--fd-muted);
  font-size: 11px;
}
"""

PREVIEW_CSS += CHART_CSS


@dataclass(frozen=True)
class Preview:
    """A rendered preview: its *kind*, its fixed *height* in px and its escaped *html*."""

    kind: str
    html: str
    size: int | None = None

    @property
    def height(self) -> int:
        """The height the card reserves for this preview."""
        return self.size if self.size is not None else HEIGHTS[self.kind]

    def with_height(self, height: int) -> Preview:
        """Return this preview drawn in a block *height* px tall."""
        old = f'class="fd-preview fd-preview-{self.kind}" style="height: {self.height}px"'
        new = f'class="fd-preview fd-preview-{self.kind}" style="height: {height}px"'
        return Preview(self.kind, self.html.replace(old, new, 1), height)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _number(value: t.Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _float(value: t.Any) -> float:
    """Return *value* as a float, or NaN for a missing value such as ``pd.NA`` or an overflow."""
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return math.nan


def _text(value: t.Any, limit: int) -> str:
    """Return a short text for any value without writing out a large one in full."""
    if isinstance(value, str):
        return _clip(value, limit)
    shape = getattr(value, "shape", None)
    if isinstance(shape, tuple) and len(shape) > 1:
        return f"{type(value).__name__} of shape {' x '.join(map(str, shape))}"
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return _clip(_REPR.repr(value), limit)
    return _clip(safe_str(value), limit)


def _format_time(value: t.Any) -> str | None:
    if type(value).__name__ == "datetime64":
        value = value.astype("datetime64[us]").item()
    if isinstance(value, dt.datetime):
        try:
            if value.time() == dt.time(0) and value.tzinfo is None:
                return value.strftime("%Y-%m-%d")
            return value.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return str(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    return None


def format_value(value: t.Any, limit: int = MAX_CELL_CHARS) -> str:
    """Format one value for a cell or a line, e.g. ``1,234`` or ``0.1235``."""
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, numbers.Integral):
        # Below 10,000 an integer is as often a year or an id as a quantity.
        return f"{int(value):,}" if abs(int(value)) >= 10_000 else str(int(value))
    if isinstance(value, numbers.Real):
        value = _float(value)
        if math.isnan(value):
            return "NaN"
        return f"{value:,.4g}" if abs(value) < 1e15 else f"{value:.3g}"
    if (time := _format_time(value)) is not None:
        return time
    return _text(value, limit)


def _popout_button() -> str:
    return (
        '<button class="fd-popout-btn fd-card-action" type="button" '
        'data-fd-action="popout" aria-label="Pop out preview" title="Pop out preview">'
        '<span class="fd-ms" aria-hidden="true">open_in_full</span></button>'
    )


def _header(left: str, right: str = "", *, popout: bool = True) -> str:
    meta = f'<span class="fd-pv-meta" title="{_esc(left)}">{_esc(left)}</span>' if left else ""
    extra = f'<span class="fd-pv-meta-right">{_esc(right)}</span>' if right else ""
    btn = _popout_button() if popout else ""
    return f'<div class="fd-pvh">{meta}<span class="fd-pv-grow"></span>{extra}{btn}</div>'


def _block(kind: str, header: str, body: str, height: int | None = None) -> Preview:
    return Preview(
        kind,
        f'<div class="fd-preview fd-preview-{kind}" style="height: {height or HEIGHTS[kind]}px">'
        f'{header}<div class="fd-pvb">{body}</div></div>',
        height,
    )


def value_preview(label: str, text: str, *, popout: bool = True) -> Preview:
    """Preview a single value as ``label  value`` on one line."""
    key = f'<span class="fd-pv-key">{_esc(label)}</span>' if label else ""
    btn = _popout_button() if popout else ""
    return Preview(
        "value",
        f'<div class="fd-preview fd-preview-value" style="height: {HEIGHTS["value"]}px">'
        f'{key}<span class="fd-pv-val" title="{_esc(text)}">{_esc(text)}</span>{btn}</div>',
    )


def text_preview(text: str, *, caption: str = "") -> Preview:
    """Preview a snippet of plain text, up to three lines; markup is shown as typed.

    A short single line gets the one-line box of a single value.
    """
    text = _clip(str(text).strip(), MAX_TEXT_CHARS)
    if "\n" not in text and len(text) <= MAX_LINE_CHARS:
        return value_preview(caption, text)
    return _block(
        "text",
        _header(caption),
        f'<div class="fd-pv-text-wrap"><div class="fd-pv-text">{_esc(text)}</div></div>',
    )


def _is_table(value: t.Any) -> bool:
    shape = getattr(value, "shape", None)
    return (
        isinstance(shape, tuple)
        and len(shape) == 2
        and hasattr(value, "columns")
        and hasattr(value, "head")
    )


def _is_series(value: t.Any) -> bool:
    shape = getattr(value, "shape", None)
    return isinstance(shape, tuple) and len(shape) == 1 and hasattr(value, "dtype")


def _is_kpi_indicator(value: t.Any) -> bool:
    indicators = getattr(pn, "indicators", None)
    if indicators is None:
        return False
    types = tuple(
        cls
        for name in ("Number", "Trend")
        if isinstance(cls := getattr(indicators, name, None), type)
    )
    return bool(types) and isinstance(value, types)


def _preview_kind_dict(value: t.Any) -> str | None:
    if isinstance(value, t.Mapping):
        kind = value.get(PREVIEW_KIND_KEY)
        if kind in ("kpi", "chart"):
            return kind
    return None


def preview_priority(value: t.Any) -> int:
    """Rank how informative a value's preview is (lower is better)."""
    if value is None or is_failure(value):
        return 3
    if _is_table(value) or _preview_kind_dict(value) is not None or _is_kpi_indicator(value):
        return 0
    if (
        _is_series(value)
        or isinstance(
            value,
            (
                str,
                bool,
                numbers.Real,
                dt.date,
                dt.datetime,
                list,
                tuple,
                set,
                frozenset,
                t.Mapping,
            ),
        )
        or describe_object(value) is not None
    ):
        return 1
    return 2


def table_preview(value: t.Any, caption: str) -> Preview:
    """Preview the first rows of the first columns of a DataFrame."""
    all_columns = list(value.columns)
    columns = all_columns[:MAX_COLUMNS]
    head = value.head(MAX_ROWS)
    if hasattr(head, "iloc"):
        cells = [
            [head.iloc[i, j] for j in range(len(columns))] for i in range(min(len(head), MAX_ROWS))
        ]
    else:
        series = [list(head[c])[:MAX_ROWS] for c in columns]
        cells = [list(row) for row in zip(*series, strict=False)]
    numeric = [bool(cells) and all(_number(row[j]) for row in cells) for j in range(len(columns))]

    def cell(tag, val, j):
        text = format_value(val)
        css = ' class="fd-num"' if numeric[j] else ""
        return f'<{tag}{css} title="{_esc(text)}">{_esc(text)}</{tag}>'

    rows = ["<tr>" + "".join(cell("th", c, j) for j, c in enumerate(columns)) + "</tr>"]
    rows += ["<tr>" + "".join(cell("td", v, j) for j, v in enumerate(r)) + "</tr>" for r in cells]
    extra_cols = len(all_columns) - len(columns)
    total_rows = value.shape[0]
    left_foot = f"+{_plural(extra_cols, 'column')}" if extra_cols > 0 else ""
    right_foot = f"first {len(cells)} of {total_rows:,}" if total_rows > len(cells) else ""
    foot = (
        f'<div class="fd-pv-foot"><span>{_esc(left_foot)}</span><span>{_esc(right_foot)}</span></div>'
        if (left_foot or right_foot)
        else ""
    )
    # Short tables shrink the card instead of leaving an empty block under their rows.
    wrap = 22 + max(len(cells), 1) * 19 + (22 if foot else 0)
    body = (
        f'<div class="fd-pv-table-wrap" style="height: {wrap}px">'
        f'<table class="fd-pv-table">{"".join(rows)}</table>{foot}</div>'
    )
    return _block("table", _header(caption), body, _PV_CHROME + wrap)


def kpi_preview(
    value: t.Any,
    *,
    unit: str = "",
    label: str = "",
    delta: t.Any = "",
    caption: str = "",
) -> Preview:
    """Preview a single KPI metric with optional unit, label and delta."""
    val_text = _clip(value, 24) if isinstance(value, str) else format_value(value, 24)
    unit_html = f'<span class="fd-kpi-unit">{_esc(_clip(str(unit), 16))}</span>' if unit else ""
    label_html = (
        f'<div class="fd-kpi-label" title="{_esc(str(label))}">{_esc(_clip(str(label), 60))}</div>'
        if label
        else ""
    )
    delta_html = ""
    if delta is not None and str(delta).strip():
        delta_text = format_value(delta, 32) if _number(delta) else _clip(str(delta).strip(), 40)
        if _number(delta) and float(delta) > 0 and not delta_text.startswith("+"):
            delta_text = f"+{delta_text}"
        cls = "fd-kpi-delta"
        icon = ""
        if delta_text.startswith("+"):
            cls += " fd-pos"
            icon = '<span class="fd-ms" aria-hidden="true">trending_up</span>'
        elif delta_text.startswith("-"):
            cls += " fd-neg"
            icon = '<span class="fd-ms" aria-hidden="true">trending_down</span>'
        delta_html = f'<div class="{cls}">{icon}<span>{_esc(delta_text)}</span></div>'
    body = (
        '<div class="fd-pv-kpi">'
        f'<div class="fd-kpi-row"><span class="fd-kpi-val" title="{_esc(val_text)}">{_esc(val_text)}</span>'
        f"{unit_html}</div>{label_html}{delta_html}</div>"
    )
    return _block("kpi", _header(caption), body)


def _indicator_kpi_preview(indicator: t.Any, label: str = "") -> Preview:
    cls_name = type(indicator).__name__
    title = (
        getattr(indicator, "title", "")
        or getattr(indicator, "label", "")
        or getattr(indicator, "name", "")
        or label
    )
    if title in ("Number", "Trend"):
        title = label
    if cls_name == "Number":
        raw = getattr(indicator, "value", None)
        fmt = getattr(indicator, "format", "{value}") or "{value}"
        try:
            formatted = fmt.format(value=raw) if raw is not None else "-"
        except Exception:
            formatted = format_value(raw)
        return kpi_preview(formatted, label=title, caption=label or title)
    # Trend indicator
    data = getattr(indicator, "data", None) or {}
    ys = list(data.get("y", [])) if isinstance(data, t.Mapping) else []
    if ys:
        latest = ys[-1]
        delta = ""
        if len(ys) >= 2 and _number(ys[-1]) and _number(ys[-2]):
            diff = _float(ys[-1]) - _float(ys[-2])
            if math.isfinite(diff):
                delta = f"{diff:+.4g}"
        return kpi_preview(latest, label=title, delta=delta, caption=label or title)
    return kpi_preview("No data", label=title, caption=label or title)


_SAFE_SVG_TAGS = frozenset(
    {
        "svg",
        "g",
        "path",
        "line",
        "rect",
        "circle",
        "ellipse",
        "polyline",
        "polygon",
        "text",
        "tspan",
    }
)
_SAFE_SVG_ATTRS = frozenset(
    {
        "viewbox",
        "width",
        "height",
        "preserveaspectratio",
        "class",
        "fill",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-dasharray",
        "stroke-linecap",
        "stroke-linejoin",
        "opacity",
        "d",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "points",
        "transform",
        "text-anchor",
        "dominant-baseline",
        "font-size",
        "font-weight",
        "aria-hidden",
        "vector-effect",
    }
)
_SVG_CASE_ATTRS = {
    "viewbox": "viewBox",
    "preserveaspectratio": "preserveAspectRatio",
}


def _safe_svg_attr_value(name: str, value: str) -> str | None:
    lower = value.strip().lower()
    if "javascript:" in lower or "expression(" in lower or "url(" in lower:
        return None
    if name == "class":
        # Keep only simple CSS class tokens.
        tokens = [tok for tok in value.split() if tok.replace("-", "").replace("_", "").isalnum()]
        return " ".join(tokens)
    return value


class _SVGSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        low = tag.lower()
        if self._skip_depth or low not in _SAFE_SVG_TAGS:
            self._skip_depth += 1
            return
        safe_attrs = []
        for name, val in attrs:
            key = name.lower()
            if key not in _SAFE_SVG_ATTRS or val is None:
                continue
            clean = _safe_svg_attr_value(key, val)
            if clean is None:
                continue
            out_name = _SVG_CASE_ATTRS.get(key, key)
            safe_attrs.append(f'{out_name}="{_esc(clean)}"')
        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        self.parts.append(f"<{low}{attr_str}>")

    def handle_endtag(self, tag: str):
        low = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if low in _SAFE_SVG_TAGS:
            self.parts.append(f"</{low}>")

    def handle_data(self, data: str):
        if not self._skip_depth and data:
            self.parts.append(_esc(data))


def sanitize_svg(raw_svg: str) -> str:
    """Sanitize an SVG snippet to an allowlist of geometric SVG tags and attributes."""
    parser = _SVGSanitizer()
    parser.feed(str(raw_svg))
    parser.close()
    result = "".join(parser.parts)
    return result if "<svg" in result else ""


def _extract_rows(data: t.Any, spec: t.Mapping) -> list[dict[str, t.Any]]:
    if data is None and isinstance(spec.get("data"), t.Mapping):
        data = spec["data"].get("values")
    if data is None:
        return []
    if _is_table(data):
        cols = list(data.columns)
        count = data.shape[0]
        if count == 0:
            return []
        if count > MAX_CHART_POINTS and hasattr(data, "iloc"):
            step = (count - 1) / (MAX_CHART_POINTS - 1)
            indices = [round(i * step) for i in range(MAX_CHART_POINTS)]
            sampled = data.iloc[indices]
        else:
            sampled = data.head(MAX_CHART_POINTS)
        if hasattr(sampled, "to_dict"):
            return list(sampled.to_dict(orient="records"))
        return [
            {c: sampled[c][i] for c in cols} for i in range(min(len(sampled), MAX_CHART_POINTS))
        ]
    if isinstance(data, (list, tuple)):
        records = [r for r in data if isinstance(r, t.Mapping)]
        return _sample(records, MAX_CHART_POINTS)
    return []


def _mark_type(spec: t.Mapping) -> tuple[str, bool]:
    mark = spec.get("mark")
    if mark is None and isinstance(spec.get("layer"), list) and spec["layer"]:
        first = spec["layer"][0]
        if isinstance(first, t.Mapping):
            mark = first.get("mark")
    if isinstance(mark, t.Mapping):
        return str(mark.get("type") or "line").lower(), bool(mark.get("point"))
    if isinstance(mark, str) and mark:
        return mark.lower(), False
    return "line", False


def _encoding(spec: t.Mapping) -> t.Mapping:
    enc = spec.get("encoding")
    if isinstance(enc, t.Mapping) and enc:
        return enc
    if isinstance(spec.get("layer"), list) and spec["layer"]:
        first = spec["layer"][0]
        if isinstance(first, t.Mapping) and isinstance(first.get("encoding"), t.Mapping):
            return first["encoding"]
    return {}


def _to_numeric_axis(values: list[t.Any]) -> list[float] | None:
    out: list[float] = []
    for v in values:
        if _number(v):
            f = _float(v)
            if not math.isfinite(f):
                return None
            out.append(f)
        elif isinstance(v, (dt.datetime, dt.date)):
            if isinstance(v, dt.datetime):
                out.append(v.timestamp())
            else:
                out.append(dt.datetime(v.year, v.month, v.day).timestamp())
        elif isinstance(v, str):
            try:
                out.append(float(v))
            except ValueError:
                try:
                    out.append(dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    return None
        else:
            return None
    return out


def _short_tick(value: t.Any) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return (
            value.strftime("%Y-%m") if isinstance(value, dt.datetime) else value.isoformat()[:10]
        )
    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        f = _float(value)
        return f"{f:.3g}" if math.isfinite(f) else ""
    return _clip(str(value), 10)


def _aggregate_rows(
    rows: list[dict[str, t.Any]],
    x_field: str | None,
    y_field: str | None,
    y_agg: str | None,
) -> list[tuple[t.Any, float]]:
    if y_agg == "count" and x_field is not None:
        counts: dict[t.Any, int] = defaultdict(int)
        order: list[t.Any] = []
        for r in rows:
            k = r.get(x_field)
            if k is None:
                continue
            if k not in counts:
                order.append(k)
            counts[k] += 1
        return [(k, float(counts[k])) for k in order]
    if y_agg in ("sum", "mean", "min", "max") and x_field is not None and y_field is not None:
        groups: dict[t.Any, list[float]] = defaultdict(list)
        order = []
        for r in rows:
            k, y = r.get(x_field), _float(r.get(y_field))
            if k is None or not math.isfinite(y):
                continue
            if k not in groups:
                order.append(k)
            groups[k].append(y)
        out = []
        for k in order:
            vals = groups[k]
            if y_agg == "sum":
                v = sum(vals)
            elif y_agg == "mean":
                v = sum(vals) / len(vals)
            elif y_agg == "min":
                v = min(vals)
            else:
                v = max(vals)
            out.append((k, v))
        return out
    pairs = []
    for i, r in enumerate(rows):
        x_val = r.get(x_field) if x_field is not None else i
        y_val = _float(r.get(y_field)) if y_field is not None else math.nan
        if x_val is not None and math.isfinite(y_val):
            pairs.append((x_val, y_val))
    return pairs


def render_vega_lite_svg(
    spec: t.Mapping,
    data: t.Any = None,
    *,
    width: int = 240,
    height: int = 104,
) -> tuple[str, str]:
    """Render a crisp, theme-aware inline SVG thumbnail from a Vega-Lite spec and rows.

    Returns ``(svg_html, auto_caption)``. Falls back to a muted placeholder if
    the spec has no plottable rows.
    """
    mark, has_points = _mark_type(spec)
    enc = _encoding(spec)
    rows = _extract_rows(data, spec)
    x_enc = enc.get("x") if isinstance(enc.get("x"), t.Mapping) else {}
    y_enc = enc.get("y") if isinstance(enc.get("y"), t.Mapping) else {}
    color_enc = enc.get("color") if isinstance(enc.get("color"), t.Mapping) else {}
    x_field = x_enc.get("field")
    y_field = y_enc.get("field")
    y_agg = y_enc.get("aggregate")

    if rows and (x_field is None or (y_field is None and y_agg != "count")):
        keys = list(rows[0].keys())
        num_keys = [k for k in keys if any(_number(r.get(k)) for r in rows[:10])]
        non_num = [k for k in keys if k not in num_keys]
        if x_field is None:
            x_field = non_num[0] if non_num else (keys[0] if keys else None)
        if y_field is None and y_agg != "count":
            y_field = next(
                (k for k in num_keys if k != x_field), num_keys[0] if num_keys else None
            )

    caption = f"{mark} · {_plural(len(rows), 'point')}" if rows else f"Vega-Lite {mark}"
    if not rows:
        return '<div class="fd-pv-chart-empty">No data to plot</div>', caption

    pad_l, pad_r, pad_t, pad_b = 10.0, 10.0, 10.0, 18.0
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    base_y = height - pad_b

    if mark == "rect" and x_field and y_field:
        c_field = color_enc.get("field")
        xs_u = list(dict.fromkeys(r.get(x_field) for r in rows if r.get(x_field) is not None))[:12]
        ys_u = list(dict.fromkeys(r.get(y_field) for r in rows if r.get(y_field) is not None))[:8]
        if xs_u and ys_u:
            cw = plot_w / len(xs_u)
            ch = plot_h / len(ys_u)
            vals = [
                _float(r.get(c_field))
                for r in rows
                if c_field and math.isfinite(_float(r.get(c_field)))
            ]
            vmin, vmax = (min(vals), max(vals)) if vals else (0.0, 1.0)
            span = vmax - vmin or 1.0
            xi = {v: i for i, v in enumerate(xs_u)}
            yi = {v: i for i, v in enumerate(ys_u)}
            parts = [f'<svg viewBox="0 0 {width} {height}" aria-hidden="true">']
            for r in rows:
                xv, yv = r.get(x_field), r.get(y_field)
                if xv in xi and yv in yi:
                    op = (
                        0.25 + 0.75 * ((_float(r.get(c_field)) - vmin) / span)
                        if c_field and math.isfinite(_float(r.get(c_field)))
                        else 0.7
                    )
                    op = max(0.15, min(1.0, op))
                    parts.append(
                        f'<rect class="fd-ch-br" x="{pad_l + xi[xv] * cw + 1:.1f}" '
                        f'y="{pad_t + yi[yv] * ch + 1:.1f}" width="{max(1.0, cw - 2):.1f}" '
                        f'height="{max(1.0, ch - 2):.1f}" rx="1.5" fill-opacity="{op:.2f}"/>'
                    )
            parts.append(
                f'<line class="fd-ch-ax" x1="{pad_l:.1f}" x2="{width - pad_r:.1f}" y1="{base_y:.1f}" y2="{base_y:.1f}"/>'
            )
            parts.append("</svg>")
            return "".join(parts), f"rect · {len(xs_u)} \u00d7 {len(ys_u)}"

    pairs = _aggregate_rows(rows, x_field, y_field, y_agg)
    if not pairs:
        return f'<div class="fd-pv-chart-empty">Vega-Lite {_esc(mark)}</div>', caption

    x_vals = [p[0] for p in pairs]
    y_vals = [p[1] for p in pairs]
    num_x = _to_numeric_axis(x_vals) if mark != "bar" else None
    if num_x is not None and mark in ("line", "area"):
        order = sorted(range(len(pairs)), key=lambda i: num_x[i])
        x_vals = [x_vals[i] for i in order]
        y_vals = [y_vals[i] for i in order]
        num_x = [num_x[i] for i in order]

    n = len(y_vals)
    y_min = min(0.0, min(y_vals)) if mark in ("bar", "area") else min(y_vals)
    y_max = max(y_vals)
    if y_max == y_min:
        y_min -= 1.0
        y_max += 1.0
    y_span = (y_max - y_min) * 1.08

    def map_y(v: float) -> float:
        return base_y - ((v - y_min) / y_span) * plot_h

    if mark == "bar":
        # Every bar is drawn, sampled evenly once they would be thinner than 2px.
        shown = _sample(list(zip(x_vals, y_vals, strict=True)), max(1, int(plot_w // 2)))
        m = len(shown)
        bw = plot_w / m
        parts = [f'<svg viewBox="0 0 {width} {height}" aria-hidden="true">']
        xs_centers = []
        for i, (_, yv) in enumerate(shown):
            y_pos = map_y(yv)
            bh = max(1.0, base_y - y_pos)
            bx = pad_l + i * bw + bw * 0.16
            bwidth = max(2.0, bw * 0.68)
            xs_centers.append(bx + bwidth / 2)
            parts.append(
                f'<rect class="fd-ch-br" x="{bx:.1f}" y="{y_pos:.1f}" '
                f'width="{bwidth:.1f}" height="{bh:.1f}" rx="1.5"/>'
            )
        parts.append(
            f'<line class="fd-ch-ax" x1="{pad_l:.1f}" x2="{width - pad_r:.1f}" '
            f'y1="{base_y:.1f}" y2="{base_y:.1f}"/>'
        )
        for x_pos, label in _bar_ticks([x for x, _ in shown], xs_centers, pad_l, width - pad_r):
            parts.append(
                f'<text class="fd-ch-txt" x="{x_pos:.1f}" y="{height - 4:.1f}" '
                f'text-anchor="middle">{_esc(label)}</text>'
            )
        parts.append("</svg>")
        return "".join(parts), f"bar · {_plural(n, 'bar')}"

    if num_x is not None and len(set(num_x)) > 1:
        xmin, xmax = min(num_x), max(num_x)
        xspan = xmax - xmin
        xs = [pad_l + ((xv - xmin) / xspan) * plot_w for xv in num_x]
    else:
        xs = [pad_l + (i * plot_w / max(n - 1, 1)) for i in range(n)]
    ys = [map_y(yv) for yv in y_vals]

    parts = [f'<svg viewBox="0 0 {width} {height}" aria-hidden="true">']
    parts.append(
        f'<line class="fd-ch-ax" x1="{pad_l:.1f}" x2="{width - pad_r:.1f}" '
        f'y1="{base_y:.1f}" y2="{base_y:.1f}"/>'
    )
    if mark in ("point", "circle", "tick"):
        for x_pt, y_pt in zip(xs, ys, strict=True):
            parts.append(f'<circle class="fd-ch-pt" cx="{x_pt:.1f}" cy="{y_pt:.1f}" r="2.6"/>')
    else:
        d = "M" + " L".join(f"{x_pt:.1f},{y_pt:.1f}" for x_pt, y_pt in zip(xs, ys, strict=True))
        if n >= 2:
            parts.append(
                f'<path class="fd-ch-ar" d="{d} L{xs[-1]:.1f},{base_y:.1f} L{xs[0]:.1f},{base_y:.1f} Z"/>'
            )
        parts.append(f'<path class="fd-ch-ln" d="{d}"/>')
        if has_points or n <= 18:
            for x_pt, y_pt in zip(xs, ys, strict=True):
                parts.append(f'<circle class="fd-ch-pt" cx="{x_pt:.1f}" cy="{y_pt:.1f}" r="2.2"/>')

    tick_idxs = _tick_indices(n, max_ticks=4)
    for k, i in enumerate(tick_idxs):
        anchor = "start" if k == 0 else ("end" if k == len(tick_idxs) - 1 else "middle")
        parts.append(
            f'<text class="fd-ch-txt" x="{xs[i]:.1f}" y="{height - 4:.1f}" '
            f'text-anchor="{anchor}">{_esc(_short_tick(x_vals[i]))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts), f"{mark} · {_plural(n, 'point')}"


# A generous estimate of the width of one character of a tick label, in px.
_TICK_CHAR_W = 5.6


def _bar_ticks(
    values: t.Sequence, centers: t.Sequence[float], left: float, right: float
) -> list[tuple[float, str]]:
    """Return ``(x, label)`` for the bar ticks that fit between *left* and *right* without overlapping.

    Category names are often longer than the space under a few thin bars,
    so fewer ticks are drawn until the labels fit, or at least until the
    labels left after cutting each to the room it has tell the ticks apart.
    The outermost labels are moved inward to stay inside the plot.
    """
    labels = [_clip(v, MAX_CELL_CHARS) if isinstance(v, str) else _short_tick(v) for v in values]
    choices = []
    for max_ticks in (5, 4, 3, 2):
        idxs = _tick_indices(len(labels), max_ticks)
        gap = min(
            (centers[b] - centers[a] for a, b in itertools.pairwise(idxs)),
            default=right - left,
        )
        # A label pushed inward from the edge may reach two thirds of the way to its neighbour.
        budget = int((gap * 2 / 3 - 2) / _TICK_CHAR_W)
        shown = [(i, _cut(labels[i], budget)) for i in idxs]
        choices.append(shown)
        if all(text == labels[i] for i, text in shown):
            break
    else:
        distinct = [c for c in choices if len({text for _, text in c}) == len(c)]
        shown = distinct[0] if distinct else choices[-1]
    ticks = []
    for i, label in shown:
        if not label:
            continue
        half = len(label) * _TICK_CHAR_W / 2
        ticks.append((min(max(centers[i], left + half), right - half), label))
    return ticks


def _cut(label: str, budget: int) -> str:
    """Cut *label* to *budget* characters, ending in an ellipsis, or to nothing if there is no room."""
    if len(label) <= budget:
        return label
    return label[: budget - 1] + "…" if budget > 1 else ""


def _tick_indices(count: int, max_ticks: int = 4) -> list[int]:
    if count <= 0:
        return []
    if count <= max_ticks:
        return list(range(count))
    step = (count - 1) / (max_ticks - 1)
    return list(dict.fromkeys(round(i * step) for i in range(max_ticks)))


def chart_svg(payload: t.Mapping, *, width: int = 240, height: int = 104) -> tuple[str, str]:
    """Return ``(html, caption)`` drawing a chart payload (Vega-Lite spec or inline SVG).

    The caption is the payload's own, else one naming the mark and its points.
    """
    custom_caption = str(payload.get("caption") or "")
    if payload.get("svg"):
        safe = sanitize_svg(str(payload["svg"]))
        return safe or '<div class="fd-pv-chart-empty">Chart preview</div>', custom_caption
    spec = payload.get("spec")
    if isinstance(spec, t.Mapping):
        svg, auto_caption = render_vega_lite_svg(
            spec, payload.get("data"), width=width, height=height
        )
        return svg, custom_caption or auto_caption
    return '<div class="fd-pv-chart-empty">Chart preview</div>', custom_caption


def chart_preview(payload: t.Mapping, caption: str = "") -> Preview:
    """Preview a chart from a structured chart payload (Vega-Lite spec or inline SVG)."""
    body, auto_caption = chart_svg(payload)
    return _block(
        "chart",
        _header(str(payload.get("caption") or "") or caption or auto_caption or "chart"),
        f'<div class="fd-pv-chart">{body}</div>',
    )


def _sample(values: t.Sequence, limit: int) -> list:
    """Return at most *limit* evenly spaced items of *values*, keeping the last."""
    count = len(values)
    if count <= limit:
        return list(values)
    step = (count - 1) / (limit - 1)
    return [values[round(i * step)] for i in range(limit)]


def sparkline_svg(numbers_: t.Sequence[float]) -> str:
    """Draw *numbers_* as a polyline in a 100 by 24 box, skipping non-finite values."""
    points = [(i, v) for i, v in enumerate(map(_float, numbers_)) if math.isfinite(v)]
    if not points:
        return ""
    low = min(v for _, v in points)
    high = max(v for _, v in points)
    last = max(len(numbers_) - 1, 1)
    span = high - low
    coords = " ".join(
        f"{i * 100 / last:.1f},{(22 - (v - low) * 20 / span) if span else 12:.1f}"
        for i, v in points
    )
    return (
        '<svg class="fd-pv-spark" viewBox="0 0 100 24" preserveAspectRatio="none" '
        f'aria-hidden="true"><polyline points="{coords}" '
        'vector-effect="non-scaling-stroke"/></svg>'
    )


def series_preview(values: t.Sequence, caption: str) -> Preview | None:
    """Preview numbers as a sparkline, with their count and range; None if too few."""
    sample = _sample(values, MAX_POINTS)
    finite = [v for v in map(_float, sample) if math.isfinite(v)]
    if len(finite) < MIN_POINTS:
        return None
    span = f"{format_value(min(finite))} to {format_value(max(finite))}"
    return _block(
        "series",
        _header(caption, span),
        f'<div class="fd-pv-spark-wrap">{sparkline_svg(sample)}</div>',
    )


def chips_preview(items: t.Sequence, caption: str) -> Preview:
    """Preview the first few items of a list as chips."""
    chips = "".join(
        f'<span class="fd-pv-chip" title="{_esc(text)}">{_esc(text)}</span>'
        for text in (format_value(item, MAX_CHIP_CHARS) for item in list(items[:MAX_CHIPS]))
    )
    return _block("chips", _header(caption), f'<div class="fd-pv-chips">{chips}</div>')


def entries_preview(mapping: t.Mapping, caption: str) -> Preview:
    """Preview the first few entries of a dict as ``key: value`` lines."""
    lines = []
    for index, (key, value) in enumerate(mapping.items()):
        if index == MAX_ENTRIES:
            break
        text = summarize_value(value, limit=MAX_TEXT_CHARS // 4)
        lines.append(
            f'<div class="fd-pv-entry"><span class="fd-pv-key">'
            f"{_esc(format_value(key))}:</span>"
            f'<span class="fd-pv-val">{_esc(text)}</span></div>'
        )
    return _block(
        "entries", _header(caption), f'<div class="fd-pv-entries">{"".join(lines)}</div>'
    )


def describe_object(value: t.Any) -> str | None:
    """Name a chart or other rich object, e.g. ``"HoloViews Curve of y over x"``.

    Returns None for values that are not a recognized chart, so the caller
    falls back to the value's own text.
    """
    module = type(value).__module__.split(".")[0]
    name = type(value).__name__
    if module == "holoviews":
        kdims = [d.name for d in getattr(value, "kdims", [])][:2]
        vdims = [d.name for d in getattr(value, "vdims", [])][:2]
        if vdims and kdims:
            return f"HoloViews {name} of {', '.join(vdims)} over {', '.join(kdims)}"
        if hasattr(value, "keys") and not kdims:
            return f"HoloViews {name} of {len(value.keys()):,} items"
        return f"HoloViews {name}"
    if module == "bokeh":
        return "Bokeh figure" if name == "figure" else f"Bokeh {name}"
    if module == "matplotlib" and name == "Figure":
        return "Matplotlib figure"
    if module == "plotly":
        return "Plotly figure"
    if module == "altair" or module.startswith("vega"):
        return f"Vega-Lite {name}"
    if module in ("panel", "panel_material_ui"):
        return f"Panel {name}"
    return None


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count:,} {singular if count == 1 else plural or singular + 's'}"


def summarize_value(value: t.Any, limit: int = 60) -> str:
    """Describe a port value in a few words, e.g. ``"72,357 rows, 29 columns"``."""
    if value is None:
        return "No value yet"
    if is_failure(value):
        return "Failed"
    if (special := _preview_kind_dict(value)) is not None:
        if special == "kpi":
            val = value.get("value", "")
            unit = value.get("unit", "")
            return f"{val} {unit}".strip() or str(value.get("label") or "KPI")
        return str(value.get("caption") or "Chart")
    if _is_kpi_indicator(value):
        val = getattr(value, "value", None)
        if val is not None:
            return format_value(val, limit)
        return getattr(value, "title", "") or type(value).__name__
    shape = getattr(value, "shape", None)
    if isinstance(shape, tuple) and len(shape) == 2 and hasattr(value, "columns"):
        return f"{_plural(shape[0], 'row')}, {_plural(shape[1], 'column')}"
    if isinstance(shape, tuple) and len(shape) == 1 and hasattr(value, "dtype"):
        return _plural(shape[0], "value")
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > 3:
            return _plural(len(value), "item")
        text = ", ".join(_text(item, limit) for item in value)
        if len(text) > limit:
            return _plural(len(value), "item")
        return text or "Empty"
    if isinstance(value, dict):
        return _plural(len(value), "entry", "entries")
    described = describe_object(value)
    if described is not None:
        return described
    return _text(value, limit)


def _count(items: t.Sized) -> str:
    return _plural(len(items), "item")


def build_preview(value: t.Any, label: str = "", showing: bool = False) -> Preview | None:
    """Build the preview of one value, or None when there is nothing to show.

    Parameters
    ----------
    value
        The value to preview.
    label
        The name of the port holding it, shown beside a single value.
    showing
        Whether the value is one a view shows rather than one it publishes,
        which the caption above a table, sparkline, chips or entries says.
    """
    if value is None or is_failure(value):
        return None

    def caption(text):
        return f"Showing {text}" if showing else text

    special = _preview_kind_dict(value)
    if special == "kpi":
        return kpi_preview(
            value.get("value", ""),
            unit=str(value.get("unit") or ""),
            label=str(value.get("label") or ""),
            delta=value.get("delta", ""),
            caption=str(value.get("caption") or label),
        )
    if special == "chart":
        return chart_preview(value, caption=label)
    if _is_kpi_indicator(value):
        return _indicator_kpi_preview(value, label=label)
    if isinstance(value, str):
        return value_preview(label, _clip(value, MAX_TEXT_CHARS))
    if _is_table(value):
        return table_preview(value, caption(summarize_value(value)))
    if _is_series(value):
        if getattr(value.dtype, "kind", None) in ("i", "u", "f") and value.shape[0] >= MIN_POINTS:
            preview = series_preview(_numpy_sample(value), caption(summarize_value(value)))
            if preview is not None:
                return preview
        return chips_preview(_numpy_sample(value, MAX_CHIPS), caption(summarize_value(value)))
    if isinstance(value, (list, tuple)):
        if not value:
            return value_preview(label, "Empty")
        head = _sample(value, MAX_POINTS)
        if len(value) >= MIN_POINTS and all(_number(v) for v in head):
            preview = series_preview(head, caption(_count(value)))
            if preview is not None:
                return preview
        return chips_preview(value, caption(_count(value)))
    if isinstance(value, (set, frozenset)):
        if not value:
            return value_preview(label, "Empty")
        return chips_preview(heapq.nsmallest(MAX_CHIPS, value, key=str), caption(_count(value)))
    if isinstance(value, t.Mapping):
        if not value:
            return value_preview(label, "Empty")
        return entries_preview(value, caption(summarize_value(value)))
    described = describe_object(value)
    if described is not None:
        return value_preview("", described)
    return value_preview(label, format_value(value, MAX_TEXT_CHARS))


def _numpy_sample(value: t.Any, limit: int = MAX_POINTS) -> list:
    """Evenly sample a Series or 1-d array without copying all of it."""
    count = value.shape[0]
    getter = value.iloc if hasattr(value, "iloc") else value
    if count <= limit:
        return list(getter[:count])
    step = (count - 1) / (limit - 1)
    return [getter[round(i * step)] for i in range(limit)]


def _hook_accepts_output(hook: t.Callable) -> bool:
    try:
        sig = inspect.signature(hook)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == "output" and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def call_preview_hook(instance: t.Any, output: str | None = None) -> t.Any:
    """Invoke ``instance.__flowdash_preview__``, passing ``output=`` if accepted."""
    hook = getattr(instance, PREVIEW_HOOK, None)
    if hook is None or not callable(hook):
        return None
    try:
        if _hook_accepts_output(hook):
            return hook(output=output)
        return hook()
    except Exception:
        logger.exception("The preview hook of %s failed", type(instance).__name__)
        return None


def hook_preview(instance: t.Any, label: str = "", output: str | None = None) -> Preview | None:
    """Ask a component for its own preview through ``__flowdash_preview__``.

    If the hook accepts an ``output`` keyword argument, *output* is passed so
    multi-output components can return a preview for the selected output port.
    Returns None (or a blank string) to keep the default preview, a ``str``
    shown as plain text (up to three lines, never as HTML or Markdown), or any
    other value, which is previewed like an output value.
    """
    value = call_preview_hook(instance, output=output)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        return text_preview(value, caption=label)
    return build_preview(value, label)


# How many previews are built at once, on threads of their own, so a slow
# preview hook (a database query, say) never blocks the event loop.
PREVIEW_WORKERS = 4

_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=PREVIEW_WORKERS, thread_name_prefix="flowdash-preview"
            )
        return _POOL


def run_off_loop(work: t.Callable[[], t.Any], then: t.Callable[[t.Any], None], doc) -> None:
    """Call *work* on a worker thread, then pass its result to *then* on *doc*'s event loop.

    *work* should catch its own errors; one it raises is logged and *then* is
    not called. Nothing is delivered once the session has ended.
    """

    def done(future: concurrent.futures.Future):
        try:
            result = future.result()
        except Exception:
            logger.exception("Building a preview failed")
            return
        try:
            doc.add_next_tick_callback(partial(_deliver, then, result, doc))
        except Exception:
            # The session is being torn down, so there is nothing left to update.
            pass

    _executor().submit(work).add_done_callback(done)


def _deliver(then: t.Callable[[t.Any], None], result: t.Any, doc):
    with set_curdoc(doc):
        then(result)


def _arm(ref: weakref.ref[Coalescer], doc):
    # Runs on the event loop, where timeouts may be added.
    if (coalescer := ref()) is None or coalescer._cancelled:
        return
    try:
        doc.add_timeout_callback(partial(_fire, ref, doc), coalescer.delay * 1000)
    except Exception:
        # The session is being torn down, so there is nothing left to update.
        with coalescer._lock:
            coalescer._pending = False


def _fire(ref: weakref.ref[Coalescer], doc):
    # Weak, so a pending timeout does not keep a removed card alive.
    if (coalescer := ref()) is None:
        return
    with coalescer._lock:
        coalescer._pending = False
    if coalescer._cancelled:
        return
    with set_curdoc(doc):
        pn.state.execute(coalescer._callback, schedule=True)


class Coalescer:
    """Call *callback* once for each burst of calls, *delay* seconds after the first.

    *document* returns the served document the callback updates, or None
    when there is none, in which case the callback runs straight away. In a
    session the calls of a burst, e.g. a slider being dragged, collapse into
    one call on the event loop, which then sees the latest values. Calls may
    come from any thread: the timer is set from the event loop.
    """

    def __init__(
        self,
        callback: t.Callable[[], None],
        document: t.Callable[[], t.Any],
        delay: float = 0.15,
    ):
        self._callback = callback
        self._document = document
        self.delay = delay
        self._pending = False
        self._cancelled = False
        self._lock = threading.Lock()

    def __call__(self):
        """Run the callback now, or schedule it once if a session is being served."""
        if self._cancelled:
            return
        doc = self._document()
        if doc is None:
            self._callback()
            return
        with self._lock:
            if self._pending:
                return
            self._pending = True
        try:
            doc.add_next_tick_callback(partial(_arm, weakref.ref(self), doc))
        except Exception:
            with self._lock:
                self._pending = False

    def cancel(self):
        """Drop a pending call and ignore every later one."""
        self._cancelled = True
