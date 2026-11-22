"""Building blocks of the admin sections: headings, chips and a table of Panel components."""

from __future__ import annotations

import html
import typing as t
from collections.abc import Sequence

import panel as pn
import panel_material_ui as pmui
from panel.util import edit_readonly

KIT_CSS = """
:host { min-width: 0; }
.k { font-family: inherit; font-size: 0.875rem; line-height: 1.43; letter-spacing: 0.15px;
  color: var(--mui-palette-text-primary); min-width: 0; }
.k .main { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.k .mono { display: block; font-family: "Roboto Mono", ui-monospace, monospace; font-size: 0.75rem;
  color: var(--mui-palette-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.k code { font-family: "Roboto Mono", ui-monospace, monospace; font-size: 0.8125rem; }
.k .muted { color: var(--mui-palette-text-secondary); }
.k .small { font-size: 0.75rem; letter-spacing: 0.4px; }
.k .lock { display: inline-flex; align-items: center; gap: 3px; font-size: 0.75rem;
  color: var(--mui-palette-text-secondary); letter-spacing: 0.4px; white-space: nowrap; }
.k svg { width: 14px; height: 14px; fill: currentColor; flex: 0 0 auto; }
.k .chip { display: inline-block; padding: 1px 7px; margin: 1px 4px 1px 0; border-radius: 10px;
  font-size: 0.75rem; line-height: 1.5; letter-spacing: 0.3px; white-space: nowrap;
  background: rgba(var(--mui-palette-text-primaryChannel) / 0.08);
  color: var(--mui-palette-text-primary); }
.k .chip.outlined { background: transparent; border: 1px solid var(--mui-palette-divider); }
.k .chip.success { background: rgba(var(--mui-palette-success-mainChannel) / 0.14);
  color: var(--mui-palette-success-dark, #1b5e20); }
.k .chip.error { background: rgba(var(--mui-palette-error-mainChannel) / 0.12);
  color: var(--mui-palette-error-main, #d32f2f); }
.k .chip.warning { background: rgba(var(--mui-palette-warning-mainChannel) / 0.14);
  color: var(--mui-palette-warning-dark, #e65100); }
.k .chip.primary { background: rgba(var(--mui-palette-primary-mainChannel) / 0.12);
  color: var(--mui-palette-primary-main); }
.k .spend { display: flex; justify-content: space-between; gap: 8px; font-size: 0.75rem; }
.k .bar { height: 4px; border-radius: 2px; margin-top: 4px;
  background: rgba(var(--mui-palette-text-primaryChannel) / 0.12); overflow: hidden; }
.k .bar i { display: block; height: 100%; background: var(--mui-palette-primary-main); }
.k .bar i.warning { background: var(--mui-palette-warning-main); }
.k .bar i.error { background: var(--mui-palette-error-main); }
.k .error-text { color: var(--mui-palette-error-main, #d32f2f); }
:host-context(.dark) .k .chip.success, :host-context([data-mui-color-scheme="dark"]) .k .chip.success {
  color: var(--mui-palette-success-light, #81c784); }
"""

HEADING_SX = {
    "color": "text.secondary",
    "fontSize": "0.75rem",
    "fontWeight": 500,
    "letterSpacing": "1.2px",
    "textTransform": "uppercase",
}

LOCK_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 '
    "3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2m-6 "
    "9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2m3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 "
    '1.39 3.1 3.1z"/></svg>'
)

KEY_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 '
    "0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4zM7 14c-1.1 0-2-.9-2-2s.9-2 "
    '2-2 2 .9 2 2-.9 2-2 2"/></svg>'
)

LINK_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 16l-4-4V8.82C14.16 8.4 15 7.3 15 '
    "6c0-1.66-1.34-3-3-3S9 4.34 9 6c0 1.3.84 2.4 2 2.82V12l-4 4H3v5h5v-3.05l4-4.2 4 4.2V21h5v-5z"
    '"/></svg>'
)


def esc(value: t.Any) -> str:
    """Return *value* as escaped text."""
    return html.escape(str(value), quote=True)


def chip(text: str, tone: str = "") -> str:
    """Return a small chip; *tone* is ``success``, ``error``, ``warning``, ``primary`` or ``outlined``."""
    return f'<span class="chip {tone}">{esc(text)}</span>'


def lock(text: str, where: str | None = None) -> str:
    """Return the lock note of something set in code: *text*, with *where* as its tooltip."""
    return f'<span class="lock" title="{esc(where or "Set in code")}">{LOCK_SVG}{esc(text)}</span>'


def cell(markup: str, **params) -> pn.pane.HTML:
    """Return an HTML cell in the kit's style; *markup* must already be escaped."""
    params.setdefault("margin", 0)
    params.setdefault("sizing_mode", "stretch_width")
    stylesheets = [KIT_CSS, *params.pop("stylesheets", [])]
    return pn.pane.HTML(f'<div class="k">{markup}</div>', stylesheets=stylesheets, **params)


def heading(text: str, *aside, margin=(24, 0, 8, 0)) -> pn.Row:
    """Return a section heading in overline style, with *aside* on the right."""
    return pn.Row(
        pmui.Typography(text, variant="overline", sx=HEADING_SX, margin=0),
        pn.layout.HSpacer(),
        *aside,
        margin=margin,
        sizing_mode="stretch_width",
        styles={"align-items": "center", "gap": "12px", "flex-wrap": "wrap"},
    )


_HEADER_SX = {"fontWeight": 500, "fontSize": "0.875rem", "letterSpacing": "0.15px"}


def _sized(obj, width: str):
    if width.endswith("fr"):
        flex = f"{width[:-2]} 1 0"
        styles = {"flex": flex, "min-width": "0"}
    else:
        styles = {"flex": f"0 0 {width}", "min-width": "0"}
    return pn.Column(obj, margin=0, styles={**styles, "align-self": "center"})


class Table(pn.viewable.Viewer):
    """A table of Panel components in an outlined box, with a header row and an optional footer.

    *columns* pairs each title with its width: ``"64px"`` or a share of what
    is left, ``"2fr"``. Each row is a sequence of components, one per column.
    """

    def __init__(
        self,
        columns: Sequence[tuple[str, str]],
        *,
        toolbar: t.Any = None,
        footer: t.Any = None,
        empty: str = "Nothing here yet.",
        css_classes: Sequence[str] = (),
    ):
        super().__init__()
        self._columns = list(columns)
        self._empty = empty
        header = pn.Row(
            *(
                _sized(pmui.Typography(title, sx=_HEADER_SX, margin=0), width)
                for title, width in self._columns
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles=self._row_styles(header=True),
        )
        self._rows = pn.Column(margin=0, sizing_mode="stretch_width")
        parts = [p for p in (toolbar, header, self._rows, footer) if p is not None]
        self._layout = pmui.Paper(
            *parts,
            variant="outlined",
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-table", *css_classes],
            sx={"overflow": "hidden"},
        )

    @staticmethod
    def _row_styles(header: bool = False) -> dict[str, str]:
        return {
            "gap": "12px",
            "padding": "10px 12px" if not header else "14px 12px",
            "align-items": "center",
            "border-bottom": "1px solid var(--mui-palette-divider)",
            "width": "100%",
            "box-sizing": "border-box",
        }

    def set_rows(self, rows: Sequence[tuple[str, Sequence[t.Any]]]):
        """Show *rows*, each a key (its CSS class suffix) and its cells."""
        if not rows:
            self._rows[:] = [
                cell(f'<span class="muted">{esc(self._empty)}</span>', margin=(14, 12))
            ]
            return
        self._rows[:] = [
            pn.Row(
                *(
                    _sized(obj, width)
                    for obj, (_, width) in zip(cells, self._columns, strict=False)
                ),
                margin=0,
                sizing_mode="stretch_width",
                styles=self._row_styles(),
                css_classes=["flowdash-admin-row", f"flowdash-admin-row-{_css(key)}"],
            )
            for key, cells in rows
        ]

    def __panel__(self):
        """Render the table."""
        return self._layout


def _css(key: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in key).strip("-").lower()


def caption(text: str, **params) -> pmui.Typography:
    """Return secondary caption text."""
    params.setdefault("margin", 0)
    return pmui.Typography(
        text,
        variant="caption",
        sx={"color": "text.secondary", "letterSpacing": "0.25px"},
        **params,
    )


def clear_text(widget) -> None:
    """Empty a text field, including what the browser last reported typing."""
    with edit_readonly(widget):
        widget.param.update(value="", value_input="")
