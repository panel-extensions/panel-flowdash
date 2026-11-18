"""Compact previews of a node's current value, shown inside its card on the wiring canvas.

A preview is a small, escaped HTML snippet with a fixed height per kind: a
table for a DataFrame, a sparkline for numbers, chips for a list, a few
``key: value`` lines for a dict, a line of text for anything else. Each kind
keeps its height while the value changes, so a card only resizes when the kind
of its value does, and every preview is capped in rows, columns, characters
and points, so a large value never reaches the page.

A component can supply its own preview by defining ``__flowdash_preview__``;
see :func:`hook_preview`.
"""

from __future__ import annotations

import datetime as dt
import heapq
import html
import logging
import math
import numbers
import reprlib
import typing as t
import weakref
from dataclasses import dataclass
from functools import partial

import panel as pn
from panel.io.state import set_curdoc

from panel_flowdash.util import is_failure

logger = logging.getLogger(__name__)

PREVIEW_HOOK = "__flowdash_preview__"

MAX_ROWS = 3
MAX_COLUMNS = 3
MAX_CELL_CHARS = 24
MAX_CHIPS = 8
MAX_CHIP_CHARS = 24
MAX_ENTRIES = 3
MAX_TEXT_CHARS = 240
MAX_POINTS = 48
# Below this many numbers a sparkline says less than the numbers themselves.
MIN_POINTS = 3
# A hook's text up to this long, on one line, gets a one-line preview.
MAX_LINE_CHARS = 48

# Builds the text of a container item by item, so a large one is never written out whole.
_REPR = reprlib.Repr()
_REPR.maxstring = MAX_TEXT_CHARS
_REPR.maxother = MAX_TEXT_CHARS
_REPR.maxlist = _REPR.maxtuple = _REPR.maxset = _REPR.maxfrozenset = _REPR.maxdict = 8
_REPR.maxlevel = 2

_PAD = 5
_CAPTION = 16
_GAP = 3
_LINE = 16

# The height of each kind of preview, including its padding.
HEIGHTS = {
    "value": 22,
    "table": _PAD + _CAPTION + _GAP + (MAX_ROWS + 1) * _LINE + _PAD,
    "series": _PAD + _CAPTION + _GAP + 26 + _PAD,
    "chips": _PAD + _CAPTION + _GAP + 20 + _PAD,
    "entries": _PAD + _CAPTION + _GAP + MAX_ENTRIES * _LINE + _PAD,
    "text": _PAD + MAX_ENTRIES * _LINE + _PAD,
}

PREVIEW_CSS = """
.fd-preview {
  position: absolute;
  inset: auto 8px 6px 8px;
  box-sizing: border-box;
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--fd-surface);
  color: var(--fd-muted);
  font-size: 11px;
  line-height: 16px;
  overflow: hidden;
}
.fd-preview-value {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  line-height: 22px;
  white-space: nowrap;
}
.fd-pv-key { flex: none; max-width: 50%; overflow: hidden; text-overflow: ellipsis; }
.fd-pv-val {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--fd-text);
  font-weight: 500;
}
.fd-pv-caption {
  height: 16px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-pv-body { margin-top: 3px; }
.fd-pv-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
  color: var(--fd-text);
  font-variant-numeric: tabular-nums;
}
.fd-pv-table th, .fd-pv-table td {
  height: 16px;
  padding: 0 6px 0 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: left;
  font-weight: 400;
}
.fd-pv-table th { color: var(--fd-muted); font-weight: 500; }
.fd-pv-table tr:first-child th { box-shadow: inset 0 -1px var(--fd-divider); }
.fd-pv-table .fd-num { text-align: right; }
.fd-pv-spark { display: block; width: 100%; height: 26px; overflow: visible; }
.fd-pv-spark polyline {
  fill: none;
  stroke: var(--fd-primary);
  stroke-width: 1.5;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.fd-pv-chips { display: flex; gap: 4px; overflow: hidden; height: 20px; }
.fd-pv-chip {
  flex: none;
  max-width: 96px;
  height: 20px;
  box-sizing: border-box;
  padding: 0 7px;
  border-radius: 10px;
  border: 1px solid var(--fd-divider);
  line-height: 18px;
  color: var(--fd-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.fd-pv-entry { display: flex; gap: 6px; height: 16px; white-space: nowrap; }
.fd-pv-entry .fd-pv-val { font-weight: 400; }
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
.fd-pv-caption-row { display: flex; gap: 6px; justify-content: space-between; }
.fd-pv-caption-row span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fd-pv-caption-row span:last-child { flex: none; }
"""


@dataclass(frozen=True)
class Preview:
    """A rendered preview: its *kind*, its fixed *height* in px and its escaped *html*."""

    kind: str
    html: str

    @property
    def height(self) -> int:
        """The height the card reserves for this kind of preview."""
        return HEIGHTS[self.kind]


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
    return _clip(str(value), limit)


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


def _caption(left: str, right: str = "") -> str:
    if not right:
        return f'<div class="fd-pv-caption">{_esc(left)}</div>'
    return (
        f'<div class="fd-pv-caption fd-pv-caption-row"><span>{_esc(left)}</span>'
        f"<span>{_esc(right)}</span></div>"
    )


def _block(kind: str, caption: str, body: str) -> Preview:
    return Preview(
        kind,
        f'<div class="fd-preview fd-preview-{kind}" style="height: {HEIGHTS[kind]}px">'
        f'{caption}<div class="fd-pv-body">{body}</div></div>',
    )


def value_preview(label: str, text: str) -> Preview:
    """Preview a single value as ``label  value`` on one line."""
    key = f'<span class="fd-pv-key">{_esc(label)}</span>' if label else ""
    return Preview(
        "value",
        f'<div class="fd-preview fd-preview-value" style="height: {HEIGHTS["value"]}px">'
        f'{key}<span class="fd-pv-val" title="{_esc(text)}">{_esc(text)}</span></div>',
    )


def text_preview(text: str) -> Preview:
    """Preview a snippet of plain text, up to three lines; markup is shown as typed.

    A short single line gets the one-line box of a single value.
    """
    text = _clip(str(text).strip(), MAX_TEXT_CHARS)
    if "\n" not in text and len(text) <= MAX_LINE_CHARS:
        return value_preview("", text)
    return Preview(
        "text",
        f'<div class="fd-preview fd-preview-text" style="height: {HEIGHTS["text"]}px">'
        f'<div class="fd-pv-text">{_esc(text)}</div></div>',
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


def table_preview(value: t.Any, caption: str) -> Preview:
    """Preview the first rows of the first columns of a DataFrame."""
    columns = list(value.columns)[:MAX_COLUMNS]
    head = value.head(MAX_ROWS)
    if hasattr(head, "iloc"):
        cells = [
            [head.iloc[i, j] for j in range(len(columns))] for i in range(min(len(head), MAX_ROWS))
        ]
    else:
        series = [list(head[c])[:MAX_ROWS] for c in columns]
        cells = [list(row) for row in zip(*series, strict=False)]
    numeric = [bool(cells) and all(_number(row[j]) for row in cells) for j in range(len(columns))]

    def cell(tag, value, j):
        text = format_value(value)
        css = ' class="fd-num"' if numeric[j] else ""
        return f'<{tag}{css} title="{_esc(text)}">{_esc(text)}</{tag}>'

    rows = ["<tr>" + "".join(cell("th", c, j) for j, c in enumerate(columns)) + "</tr>"]
    rows += ["<tr>" + "".join(cell("td", v, j) for j, v in enumerate(r)) + "</tr>" for r in cells]
    return _block(
        "table", _caption(caption), f'<table class="fd-pv-table">{"".join(rows)}</table>'
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
    return _block("series", _caption(caption, span), sparkline_svg(sample))


def chips_preview(items: t.Sequence, caption: str) -> Preview:
    """Preview the first few items of a list as chips."""
    chips = "".join(
        f'<span class="fd-pv-chip" title="{_esc(text)}">{_esc(text)}</span>'
        for text in (format_value(item, MAX_CHIP_CHARS) for item in list(items[:MAX_CHIPS]))
    )
    return _block("chips", _caption(caption), f'<div class="fd-pv-chips">{chips}</div>')


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
    return _block("entries", _caption(caption), "".join(lines))


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


def hook_preview(instance: t.Any, label: str = "") -> Preview | None:
    """Ask a component for its own preview through ``__flowdash_preview__``.

    The hook is called without arguments on the live component instance. It
    returns None (or a blank string) to keep the default preview, a ``str`` shown as plain text
    (up to three lines, never as HTML or Markdown), or any other value, which
    is previewed like an output value: a DataFrame as a table, numbers as a
    sparkline and so on. If it raises, the error is logged and the default
    preview is used.
    """
    hook = getattr(instance, PREVIEW_HOOK, None)
    if hook is None or not callable(hook):
        return None
    try:
        value = hook()
    except Exception:
        logger.exception("The preview hook of %s failed", type(instance).__name__)
        return None
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        return text_preview(value)
    return build_preview(value, label)


def _arm(ref: weakref.ref[Coalescer], doc):
    # Runs on the event loop, where timeouts may be added.
    if (coalescer := ref()) is None or coalescer._cancelled:
        return
    try:
        doc.add_timeout_callback(partial(_fire, ref, doc), coalescer.delay * 1000)
    except Exception:
        # The session is being torn down, so there is nothing left to update.
        coalescer._pending = False


def _fire(ref: weakref.ref[Coalescer], doc):
    # Weak, so a pending timeout does not keep a removed card alive.
    if (coalescer := ref()) is None:
        return
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

    def __call__(self):
        """Run the callback now, or schedule it once if a session is being served."""
        if self._cancelled:
            return
        doc = self._document()
        if doc is None:
            self._callback()
            return
        if self._pending:
            return
        self._pending = True
        try:
            doc.add_next_tick_callback(partial(_arm, weakref.ref(self), doc))
        except Exception:
            self._pending = False

    def cancel(self):
        """Drop a pending call and ignore every later one."""
        self._cancelled = True
