"""The side sheet that shows what a tile's outputs currently hold."""

from __future__ import annotations

import html
import io
import pprint
import typing as t

import panel as pn
import panel_material_ui as pmui

from panel_flowdash.ui.text import escape_markdown, text_typography
from panel_flowdash.ui.tile import status_chip_sx
from panel_flowdash.value_text import is_plain, object_label

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

SHEET_WIDTH = 420
MAX_ROWS = 10
MAX_COLUMNS = 8
MAX_COLUMN_CHIPS = 12
# Rows copied by "Copy as Python"; a literal of a large frame is not useful code.
MAX_COPY_ROWS = 50
# Below this width the sheet covers the page, so the workspace keeps its width.
_WIDE = 900

_RESERVE_CSS = f"""
@media (min-width: {_WIDE}px) {{
  :host(.flowdash-inspecting) {{ padding-right: {SHEET_WIDTH}px; }}
}}
"""

_TABLE_CSS = """
:host { overflow-x: auto; }
table {
  border: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  border-collapse: separate;
  border-radius: 8px;
  border-spacing: 0;
  font-size: 0.8125rem;
  width: 100%;
}
th, td {
  border: none;
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  padding: 6px 10px;
  text-align: left;
  white-space: nowrap;
}
tr:last-child td { border-bottom: none; }
th { font-weight: 500; text-transform: none; }
td.flowdash-num, th.flowdash-num { text-align: right; font-variant-numeric: tabular-nums; }
"""

_CHIP_SX = {
    "bgcolor": "action.hover",
    "display": "inline-block",
    "& p": {"m": 0},
    "borderRadius": "8px",
    "fontSize": "0.75rem",
    "lineHeight": "22px",
    "px": 1,
    "whiteSpace": "nowrap",
}

_STATUS_COLORS = {"Computed": "success", "Failed": "error", "No value yet": "info"}


def make_inspect_sheet() -> pmui.Drawer:
    """Create the persistent right sheet: beside the tiles on wide screens, full screen on phones."""
    return pmui.Drawer(
        anchor="right",
        variant="persistent",
        open=False,
        size=SHEET_WIDTH,
        css_classes=["flowdash-inspect-sheet"],
        sx={
            "borderColor": "divider",
            "boxSizing": "border-box",
            "p": 2,
            "width": {"xs": "100%", "sm": SHEET_WIDTH},
            # Above the page's app bar, like the page's own context bar, so the
            # sheet's header is never hidden behind it.
            "zIndex": 1202,
        },
    )


def reserve_space(layout: pn.layout.ListLike, sheet_open: bool):
    """Pad *layout* by the sheet's width on wide screens, so no tile sits under the sheet."""
    if _RESERVE_CSS not in layout.stylesheets:
        layout.stylesheets = [*layout.stylesheets, _RESERVE_CSS]
    classes = [c for c in layout.css_classes if c != "flowdash-inspecting"]
    layout.css_classes = [*classes, "flowdash-inspecting"] if sheet_open else classes


def sheet_header(
    title: str, on_close: Callable[[], None], caption: str = "Inspect data"
) -> pn.Row:
    """Title the sheet with what is inspected, and offer to close it."""
    close = pmui.IconButton(
        icon="close", description="Close inspector", size="small", margin=0, align="start"
    )
    close.on_click(lambda _event: on_close())
    return pn.Row(
        pn.Column(
            pmui.Typography(caption, variant="caption", margin=0, sx={"color": "text.secondary"}),
            text_typography(title, variant="h6", margin=0),
            margin=0,
            sizing_mode="stretch_width",
        ),
        close,
        css_classes=["flowdash-inspect-header"],
        margin=(0, 0, 8, 0),
        sizing_mode="stretch_width",
    )


def value_status(value: t.Any, failed: bool) -> str:
    """Return the status label of an output value."""
    if failed:
        return "Failed"
    return "No value yet" if value is None else "Computed"


def describe_value(value: t.Any) -> tuple[str | None, str | None]:
    """Return the type name and a short shape of *value*, without walking its contents."""
    if value is None or isinstance(value, Exception):
        return None, None
    type_name = type(value).__name__
    shape = getattr(value, "shape", None)
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            rows, cols = value.shape
            return type_name, f"{_count(rows, 'row')}, {_count(cols, 'column')}"
        if isinstance(value, pd.Series):
            return type_name, _count(len(value), "value")
    except ImportError:
        pass
    if isinstance(shape, tuple) and shape:
        return type_name, " x ".join(str(n) for n in shape)
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return type_name, _count(len(value), "item")
    return type_name, None


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def short_dtype(series) -> str:
    """Return a reader-friendly dtype such as str, int, float, bool or datetime."""
    import pandas as pd

    dtype = series.dtype
    kind = getattr(dtype, "kind", None)
    if isinstance(dtype, pd.CategoricalDtype):
        return "category"
    names = {"f": "float", "i": "int", "u": "int", "b": "bool", "M": "datetime", "m": "timedelta"}
    if kind in names:
        return names[kind]
    if kind == "O" or isinstance(dtype, pd.StringDtype):
        # Only a sample is inferred, so a large frame is never scanned.
        inferred = pd.api.types.infer_dtype(series.head(50), skipna=True)
        return {"string": "str", "empty": "str"}.get(inferred, inferred)
    return str(dtype)


def facts(rows: list[tuple[str, str | None]]) -> pn.Column:
    """Lay out labelled facts in two columns, leaving out rows without a value."""
    return pn.Column(
        *(
            pn.Row(
                pmui.Typography(
                    label, variant="body2", width=110, margin=0, sx={"color": "text.secondary"}
                ),
                text_typography(text, variant="body2", margin=0, sizing_mode="stretch_width"),
                margin=(0, 0, 6, 0),
                sizing_mode="stretch_width",
            )
            for label, text in rows
            if text
        ),
        css_classes=["flowdash-inspect-facts"],
        margin=0,
        sizing_mode="stretch_width",
    )


def heading(text: str) -> pmui.Typography:
    """Return a section heading for the sheet."""
    return pmui.Typography(text, variant="subtitle2", margin=(16, 0, 8, 0), sx={"fontWeight": 500})


def _cell(value: t.Any) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return html.escape(str(value))


def _rows_html(frame) -> str:
    """Render the first rows as an HTML table, with numeric columns right aligned."""
    import pandas as pd

    numeric = [pd.api.types.is_numeric_dtype(dtype) for dtype in frame.dtypes]
    classes = [' class="flowdash-num"' if n else "" for n in numeric]
    header = "".join(
        f"<th{cls}>{html.escape(str(column))}</th>"
        for cls, column in zip(classes, frame.columns, strict=True)
    )
    body = "".join(
        "<tr>"
        + "".join(f"<td{cls}>{_cell(v)}</td>" for cls, v in zip(classes, row, strict=True))
        + "</tr>"
        for row in frame.itertuples(index=False, name=None)
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


class _Code:
    """A value that pprint writes as the given source."""

    def __init__(self, source: str):
        self.source = source

    def __repr__(self) -> str:
        return self.source


def _literal(value: t.Any) -> t.Any:
    import pandas as pd

    if value is None or value is pd.NaT or (isinstance(value, float) and value != value):
        return None
    if value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return _Code(f"pd.{type(value).__name__}({str(value)!r})")
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        return {_literal(k): _literal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_literal(v) for v in value)
    return value if is_plain(value) else object_label(value)


def python_literal(frame) -> str:
    """Python that rebuilds the first rows of *frame*, for pasting into a notebook."""
    shown = frame.head(MAX_COPY_ROWS)
    data = {str(column): [_literal(v) for v in shown[column].tolist()] for column in shown.columns}
    source = pprint.pformat(data, width=88, sort_dicts=False)
    note = ""
    if len(frame) > len(shown):
        note = f"# The first {len(shown):,} of {len(frame):,} rows\n"
    return f"import pandas as pd\n\n{note}df = pd.DataFrame({source})\n"


def dataframe_preview(frame, filename: str) -> pn.Column:
    """Columns with their dtypes, the first rows and export actions; never the whole frame."""
    chips = [
        pmui.Typography(
            f"{escape_markdown(column)} <span style='opacity: 0.7'>"
            f"{escape_markdown(short_dtype(frame[column]))}</span>",
            variant="caption",
            margin=(0, 6, 6, 0),
            sx=_CHIP_SX,
        )
        for column in list(frame.columns)[:MAX_COLUMN_CHIPS]
    ]
    hidden = len(frame.columns) - MAX_COLUMN_CHIPS
    if hidden > 0:
        chips.append(
            pmui.Typography(
                f"+{hidden} more",
                variant="caption",
                margin=(0, 6, 6, 0),
                sx={**_CHIP_SX, "bgcolor": "transparent"},
            )
        )
    head = frame.iloc[:MAX_ROWS, :MAX_COLUMNS]
    shown = f"First {_count(len(head), 'row')}"
    if len(frame.columns) > MAX_COLUMNS:
        shown += f" and {MAX_COLUMNS} of {len(frame.columns)} columns"
    download = pmui.FileDownload(
        callback=lambda: io.StringIO(frame.to_csv(index=False)),
        filename=f"{filename}.csv",
        label="Download CSV",
        icon="download",
        size="small",
        margin=(0, 8, 0, 0),
    )
    # Released Panel warns about `button_style` when a Material FileDownload is
    # constructed with `variant`; drop once holoviz/panel#8752 is the minimum.
    download.variant = "outlined"
    copy = pmui.Button(
        label="Copy as Python",
        icon="content_copy",
        size="small",
        variant="text",
        margin=0,
    )
    copy.js_on_click(
        args={"code": python_literal(frame)}, code="navigator.clipboard.writeText(code)"
    )
    return pn.Column(
        heading("Columns"),
        pn.FlexBox(*chips, margin=0, sizing_mode="stretch_width"),
        heading("First rows"),
        pn.pane.HTML(
            _rows_html(head),
            css_classes=["flowdash-inspect-rows"],
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_TABLE_CSS],
        ),
        pmui.Typography(
            shown, variant="caption", margin=(4, 0, 12, 0), sx={"color": "text.secondary"}
        ),
        pn.Row(download, copy, margin=0),
        margin=0,
        sizing_mode="stretch_width",
    )


def output_section(
    *,
    status: str,
    rows: list[tuple[str, str | None]],
    preview: Viewable | None,
    extra: Viewable | None = None,
) -> pn.Column:
    """Describe one output: its status, facts about it and a preview of the value."""
    chip = pmui.Typography(
        status, variant="caption", margin=(0, 0, 12, 0), sx=status_chip_sx(_STATUS_COLORS[status])
    )
    objects: list[Viewable] = [pn.Row(chip, margin=0), facts(rows)]
    if preview is not None:
        objects.append(preview)
    if extra is not None:
        objects.append(extra)
    return pn.Column(*objects, margin=(12, 0, 0, 0), sizing_mode="stretch_width")


def is_dataframe(value: t.Any) -> bool:
    """Whether *value* is a pandas DataFrame, without importing pandas for others."""
    try:
        import pandas as pd
    except ImportError:
        return False
    return isinstance(value, pd.DataFrame)
