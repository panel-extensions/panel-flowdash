"""The frame and title bar of a dashboard tile in view mode."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel.viewable import Viewable

# The frame is drawn only in view mode; while arranging, TileGrid draws its own
# card around the tile, so the tile must not add a second border.
_FRAME_CSS = """
:host(.flowdash-tile-framed) {
  background-color: var(--mui-palette-background-paper, var(--panel-surface-color, #fff));
  border: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  border-radius: 12px;
  box-sizing: border-box;
}
"""

_STATUS = {
    "failed": ("Failed", "error"),
    "waiting": ("Waiting", "warning"),
}

_ACTION_SX = {"color": "text.secondary"}

_ELLIPSIS = {"overflow": "hidden", "textOverflow": "ellipsis", "whiteSpace": "nowrap"}

# A MenuButton without a label is used as an icon button; its drop-down arrow
# would read as a second control.
_MORE_SX = {
    "color": "text.secondary",
    "minWidth": 0,
    "px": 0.75,
    "& .MuiButton-startIcon": {"m": 0},
    "& .MuiButton-endIcon": {"display": "none"},
}


def status_chip_sx(color: str) -> dict:
    """Style a small tinted chip with a leading dot, readable in light and dark mode."""
    main = f"var(--mui-palette-{color}-main)"
    return {
        "alignItems": "center",
        "bgcolor": f"color-mix(in srgb, {main} 16%, transparent)",
        "borderRadius": "12px",
        "color": "text.primary",
        "display": "inline-flex",
        "fontSize": "0.75rem",
        "fontWeight": 500,
        "gap": "6px",
        "lineHeight": "20px",
        "px": 1,
        "whiteSpace": "nowrap",
        "&::before": {
            "bgcolor": f"{color}.main",
            "borderRadius": "50%",
            "content": '""',
            "height": 8,
            "width": 8,
        },
    }


class Tile(pn.Column):
    """A component's view in a frame with a title bar.

    The title bar holds the title, an optional subtitle, a status chip and
    the tile actions: Inspect, Expand (a full-screen dialog of the tile) and
    a More menu. Callers choose which actions apply with :meth:`set_actions`
    and react to them through ``on_inspect`` and ``on_show_in_editor``.

    ``title`` may be a reactive expression; ``label`` is the plain name the
    actions' accessible names and the expand dialog use. ``view`` is the
    wrapped view; the tile only moves it into the expand dialog and back, so
    it is never re-created.
    """

    def __init__(
        self,
        view: Viewable,
        *,
        title: t.Any,
        label: str,
        subtitle: str | None = None,
        on_inspect: Callable[[], None] | None = None,
        on_show_in_editor: Callable[[], None] | None = None,
        **params,
    ):
        self.view = view
        self.on_inspect = on_inspect
        self.on_show_in_editor = on_show_in_editor
        self._inspect = False
        self._show_in_editor = False
        self.title = pmui.Typography(
            title,
            variant="subtitle1",
            margin=0,
            sx={"fontWeight": 500, "lineHeight": 1.4, **_ELLIPSIS},
        )
        self.subtitle = pmui.Typography(
            subtitle or "",
            variant="caption",
            margin=0,
            visible=bool(subtitle),
            sx={"color": "text.secondary", "display": "block", **_ELLIPSIS},
        )
        self.status = pmui.Typography(
            "",
            variant="caption",
            margin=(0, 4),
            align="center",
            visible=False,
            css_classes=["flowdash-tile-status"],
        )
        self.inspect_button = pmui.IconButton(
            icon="data_object",
            description=f"Inspect data for {label}",
            size="small",
            margin=0,
            align="center",
            visible=False,
            sx=_ACTION_SX,
        )
        self.expand_button = pmui.IconButton(
            icon="open_in_full",
            description=f"Expand {label}",
            size="small",
            margin=0,
            align="center",
            sx=_ACTION_SX,
        )
        self.more_menu = pmui.MenuButton(
            label="",
            icon="more_vert",
            description=f"More for {label}",
            items=[],
            size="small",
            variant="text",
            margin=0,
            align="center",
            sx=_MORE_SX,
        )
        self.header = pn.Row(
            pn.Column(self.title, self.subtitle, margin=0, sizing_mode="stretch_width"),
            self.status,
            self.inspect_button,
            self.expand_button,
            self.more_menu,
            css_classes=["flowdash-tile-header"],
            margin=(10, 8, 6, 16),
            sizing_mode="stretch_width",
        )
        self.body = pn.Column(view, margin=(0, 8, 8, 8), sizing_mode="stretch_both")
        self._expanded_note = pmui.Typography(
            "Shown in full screen",
            variant="body2",
            margin=16,
            sx={"color": "text.secondary"},
        )
        self.expand_dialog = pmui.Dialog(
            title=label,
            full_screen=True,
            show_close_button=True,
            close_on_click=True,
            open=False,
        )
        self.inspect_button.on_click(self._on_inspect)
        self.expand_button.on_click(self.expand)
        self.more_menu.on_click(self._on_menu)
        self.expand_dialog.param.watch(self._on_dialog_open, "open")
        params.setdefault("css_classes", ["flowdash-tile", "flowdash-tile-framed"])
        super().__init__(
            self.header,
            self.body,
            self.expand_dialog,
            margin=0,
            sizing_mode="stretch_both",
            stylesheets=[_FRAME_CSS],
            **params,
        )
        self._sync_menu()

    @property
    def expanded(self) -> bool:
        return self.expand_dialog.open

    def set_chrome(self, shown: bool):
        """Show the frame and title bar (view mode), or leave both to the grid (arranging)."""
        self.header.visible = shown
        classes = [c for c in self.css_classes if c != "flowdash-tile-framed"]
        self.css_classes = [*classes, "flowdash-tile-framed"] if shown else classes

    def set_actions(self, *, inspect: bool, show_in_editor: bool):
        """Offer Inspect and Show in editor only where they apply to the current viewer."""
        self._inspect = inspect
        self._show_in_editor = show_in_editor
        self.inspect_button.visible = inspect
        self._sync_menu()

    def set_status(self, status: str | None):
        """Show the 'failed' or 'waiting' chip, or none."""
        if status is None:
            self.status.visible = False
            return
        label, color = _STATUS[status]
        self.status.param.update(object=label, sx=status_chip_sx(color), visible=True)

    def expand(self, _event=None):
        """Show the tile's view in a full-screen dialog until the dialog closes."""
        if self.expanded:
            return
        self.body[:] = [self._expanded_note]
        self.expand_dialog.param.update(objects=[self.view], open=True)

    def _on_dialog_open(self, event):
        if event.new:
            return
        self.expand_dialog.objects = []
        self.body[:] = [self.view]

    def _on_inspect(self, _event=None):
        if self.on_inspect is not None:
            self.on_inspect()

    def _sync_menu(self):
        items = [{"label": "Expand", "icon": "open_in_full", "action": "expand"}]
        if self._inspect:
            items.insert(0, {"label": "Inspect data", "icon": "data_object", "action": "inspect"})
        if self._show_in_editor:
            items.append({"label": "Show in editor", "icon": "account_tree", "action": "editor"})
        self.more_menu.items = items

    def _on_menu(self, item):
        action = item.get("action") if isinstance(item, dict) else None
        if action == "expand":
            self.expand()
        elif action == "inspect":
            self._on_inspect()
        elif action == "editor" and self.on_show_in_editor is not None:
            self.on_show_in_editor()
