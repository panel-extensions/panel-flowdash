"""The bar above a dashboard: view actions in view mode, editing controls in edit mode."""

from __future__ import annotations

import base64
import threading
import time

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.ui.fullscreen import TOGGLE_CLASS
from panel_flowdash.ui.health import DashboardHealth
from panel_flowdash.ui.health_popover import HealthPopover
from panel_flowdash.ui.text import escape_markdown
from panel_flowdash.util import close_dialog, confirm, open_dialog

# Overflow menus show only an icon: MenuButton always renders a dropdown arrow.
_OVERFLOW_SX = {
    "minWidth": 0,
    "px": 1,
    "& .MuiButton-startIcon": {"m": 0},
    "& .MuiButton-endIcon": {"display": "none"},
}

# MenuButton items take no colour, but the menu is portaled into the button's
# own shadow root, so a stylesheet colours the destructive last entry.
_DANGER_LAST_ITEM = """
li.MuiMenuItem-root:last-of-type {
  color: var(--mui-palette-error-main);
}
"""

_TOGGLE_SX = {"& .MuiToggleButton-root": {"textTransform": "none", "px": 1.5}}

_DIRTY_DOT_SX = {
    "display": "inline-flex",
    "alignItems": "center",
    "&::before": {
        "content": '""',
        "display": "inline-block",
        "width": "8px",
        "height": "8px",
        "borderRadius": "50%",
        "bgcolor": "warning.main",
        "mr": "6px",
    },
}


_PERIODS = [
    ("Off", 0),
    ("Every minute", 60),
    ("Every 5 min", 300),
    ("Every 15 min", 900),
    ("Every hour", 3600),
]


def format_period(seconds: float) -> str:
    """Return a refresh period as "15 min", "1 hour" or "30 s"."""
    seconds = int(seconds)
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} hour{'' if hours == 1 else 's'}"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


class DashboardBar(Viewer):
    """One row of dashboard controls, with a view and an edit section.

    In view mode it shows the dashboard title, an info button with the
    description, the health chip (whose popover holds the pipeline and any
    issues) and the view actions: the View/Edit control, Refresh (its tooltip
    says when the dashboard was last refreshed) with the auto-refresh period,
    the editor's ``toolbar_extra`` items, Full screen and an overflow menu
    (Download, Settings). In edit mode it shows the title with the save state, the
    Dataflow/Layout/Split tabs and the edit actions: Settings, an overflow menu
    (Download, Import file, Clear canvas), Discard, Save and Done.

    ``layout`` arranges these parts. With ``"compact"`` they form one line:
    the description sits behind an info button, and Refresh's tooltip says
    when the dashboard was last refreshed. When a host offers its header
    (``placement="header"``, FlowDashApp's top bar), the bar then renders
    nothing itself: the host places :attr:`lead` after its breadcrumb, which
    stands in for the title, and :attr:`actions` at its end (see
    :attr:`in_header`). With ``"full"`` the bar is always its own row, with a
    large title over a line holding the description, the health chip and
    when the dashboard was last refreshed.

    Each part of a section comes from one method returning a list of
    viewables, so a feature adds an item by extending that method:
    :meth:`_view_meta`, :meth:`_view_actions`, :meth:`_edit_status`,
    :meth:`_edit_actions`, and :meth:`_overflow_items` for the menu entries
    (dispatched by :meth:`_on_overflow`). Call :meth:`sync` after changing
    anything they read.
    """

    editor = param.Parameter(doc="The FlowDash editor the bar controls.")

    on_mode = param.Callable(
        default=None,
        doc="""
        Called with "edit" or "view" when the user asks to switch modes. When
        unset the bar switches the editor's `editable` itself, asking first
        when leaving edit mode with unsaved changes.""",
    )

    layout = param.Selector(
        default="compact",
        objects=["compact", "full"],
        doc="""
        "compact" keeps the bar to one line and lets a host header take it,
        "full" shows it as its own row with the title over a meta line.""",
    )

    placement = param.Selector(
        default="row",
        objects=["row", "header"],
        doc="""
        Whether a host offers its header for the bar ("header"), which then
        takes :attr:`lead` and :attr:`actions` and shows the dashboard's title
        while the layout is "compact", or not ("row").""",
    )

    visible = param.Boolean(default=True, doc="Whether the bar is shown.")

    def __init__(self, **params):
        super().__init__(**params)
        editor = self.editor
        self._saved_at: str | None = None

        self.title = pmui.Typography(
            variant="subtitle1",
            margin=0,
            sx={
                "whiteSpace": "nowrap",
                "overflow": "hidden",
                "textOverflow": "ellipsis",
                "fontWeight": 500,
            },
        )
        self.save_state = pmui.Typography(
            variant="caption", sx={"color": "text.secondary", "whiteSpace": "nowrap"}, margin=0
        )
        self.info_button = pmui.IconButton(
            icon="info_outline",
            size="small",
            margin=0,
            color="default",
            css_classes=["fd-description"],
            sx={"color": "text.secondary"},
        )
        self.description = pmui.Typography(
            variant="body2",
            margin=0,
            sx={"color": "text.secondary"},
            styles={"flex-basis": "100%"},
        )
        self.refresh_label = pmui.Typography(
            variant="caption", sx={"color": "text.secondary"}, margin=0
        )
        self.health = DashboardHealth(editor)
        self.health_popover = HealthPopover(editor=editor, health=self.health)
        self.health_chip = self.health_popover.chip
        self.refresh_text = ""
        self._refreshed_at: str | None = None

        # Refresh and the period menu sit flush, as one split button.
        self.refresh_button = pmui.Button(
            label="Refresh",
            icon="refresh",
            variant="outlined",
            size="small",
            margin=(0, 0, 0, 4),
            description="Refresh now",
            sx={"borderTopRightRadius": 0, "borderBottomRightRadius": 0},
        )
        self.refresh_button.on_click(lambda _event: editor.param.trigger("refresh"))
        self.refresh_period = pmui.MenuButton(
            label="Off",
            variant="outlined",
            size="small",
            description="Update automatically",
            margin=(0, 4, 0, 0),
            sx={
                "borderTopLeftRadius": 0,
                "borderBottomLeftRadius": 0,
                "ml": "-1px",
                "minWidth": 0,
                "whiteSpace": "nowrap",
                "color": "text.secondary",
            },
        )
        self.refresh_period.on_click(self._on_period_chosen)
        # The browser handles the click, as full screen needs the user's gesture.
        self.fullscreen_button = pmui.IconButton(
            icon="fullscreen",
            description="Full screen",
            size="small",
            margin=(0, 4),
            align="center",
            css_classes=[TOGGLE_CLASS],
        )
        self.mode_toggle = pmui.RadioButtonGroup(
            options={":material/visibility: View": "view", ":material/edit: Edit": "edit"},
            value="view",
            size="small",
            margin=(0, 12, 0, 4),
            sx=_TOGGLE_SX,
        )
        self.mode_toggle.param.watch(self._on_mode_toggled, "value")

        self.mode_tabs = pmui.RadioButtonGroup(
            options={
                ":material/account_tree: Dataflow": "wiring",
                ":material/dashboard: Layout": "dashboard",
                ":material/vertical_split: Split": "split",
            },
            value=editor.mode,
            size="small",
            margin=(0, 12, 0, 4),
            sx=_TOGGLE_SX,
        )
        self.mode_tabs.link(editor, value="mode", bidirectional=True)
        self.settings_button = pmui.Button(
            label="Settings", icon="tune", variant="text", size="small", margin=(0, 4)
        )
        self.discard_button = pmui.Button(
            label="Discard", variant="outlined", size="small", margin=(0, 4)
        )
        self.discard_button.on_click(lambda _event: self._confirm_discard())
        self.save_button = pmui.Button(
            label="Save", icon="save", variant="contained", size="small", margin=(0, 4)
        )
        self.done_button = pmui.Button(
            label="Done", icon="visibility", variant="outlined", size="small", margin=(0, 4)
        )
        self.done_button.on_click(lambda _event: self.request_mode("view"))

        self.overflow = pmui.MenuButton(
            icon="more_vert",
            label="",
            variant="text",
            size="small",
            color="default",
            description="More actions",
            margin=(0, 4),
            sx=_OVERFLOW_SX,
        )
        self.overflow.on_click(self._on_overflow)
        self._divider = pmui.Divider(
            orientation="vertical", height=24, margin=(0, 8), align="center"
        )
        # The menu cannot start a browser download itself, so a hidden button
        # receives the file and saves it as soon as its data arrives.
        self.download_button = pmui.FileDownload(
            callback=editor._download_callback,
            filename="dashboard.json",
            auto=True,
            visible=False,
        )

        self._lead = pn.FlexBox(
            align_items="center",
            flex_wrap="nowrap",
            gap="8px",
            margin=0,
            sizing_mode=None,
            styles={"min-width": "0", "flex": "0 1 auto"},
            css_classes=["fd-dashboard-lead"],
        )
        # The full layout's line under the title, which wraps so the
        # description can take a line of its own.
        self._meta = pn.FlexBox(
            align_items="center", flex_wrap="wrap", gap="2px 12px", margin=0, sizing_mode=None
        )
        self._title_block = pn.Column(self.title, self._meta, margin=(0, 8, 0, 0), min_width=0)
        self._actions = pn.FlexBox(
            align_items="center",
            flex_wrap="wrap",
            margin=0,
            sizing_mode=None,
            styles={"width": "fit-content", "max-width": "100%"},
            css_classes=["fd-dashboard-actions"],
        )
        # A FlexBox rather than a Paper, so the actions wrap below the title on narrow screens.
        self._bar = pn.FlexBox(
            self._lead,
            self._actions,
            align_items="center",
            flex_wrap="wrap",
            justify_content="space-between",
            margin=0,
            sizing_mode="stretch_width",
            visible=param.bind(
                lambda visible, *_: visible and not self.in_header,
                self.param.visible,
                self.param.placement,
                self.param.layout,
            ),
            css_classes=["fd-dashboard-bar"],
            styles={
                "padding": "6px 16px",
                "gap": "6px 16px",
                "background": "var(--mui-palette-background-paper)",
                "border-bottom": "1px solid var(--mui-palette-divider)",
            },
        )
        self.param.watch(lambda _event: self.sync(), ["layout", "placement"])
        editor.param.watch(self._on_dashboard_changed, "dashboard")
        editor.param.watch(self._on_saved, "saved")
        editor.settings.param.watch(self._sync_view_meta, ["description", "refresh_period"])
        self.health.param.watch(self._sync_view_meta, ["tiles"])

    @property
    def in_header(self) -> bool:
        """Whether a host header shows the lead and actions in place of the bar's own row."""
        return self.placement == "header" and self.layout == "compact"

    @property
    def lead(self):
        """The title's companions: the info button and health chip, or the save state."""
        return self._lead

    @property
    def actions(self):
        """The actions of the current mode."""
        return self._actions

    # ------------------------------------------------------------------
    # Sections, the extension points
    # ------------------------------------------------------------------

    def _view_meta(self) -> list:
        """Items with the title in view mode.

        The description, the health chip and, in the full layout, when the
        dashboard was last refreshed.
        """
        full = self.layout == "full"
        items = []
        if self.info_button.description:
            items.append(self.description if full else self.info_button)
        if self.health.tiles:
            items.append(self.health_popover.__panel__())
        if full and self.refresh_label.object:
            items.append(self.refresh_label)
        return items

    def _view_actions(self) -> list:
        """Actions on the right in view mode, the View/Edit control first.

        The order mirrors edit mode, where the Dataflow/Layout tabs come first,
        then the actions (``toolbar_extra`` such as Share) and the overflow menu.
        """
        editor = self.editor
        actions = [] if editor.read_only else [self.mode_toggle]
        actions += [
            self.refresh_button,
            self.refresh_period,
            *editor.toolbar_extra,
            self.fullscreen_button,
        ]
        if self._overflow_items():
            actions.append(self.overflow)
        return actions

    def _edit_status(self) -> list:
        """Items on the line under the title in edit mode."""
        return [self.save_state]

    def _edit_actions(self) -> list:
        """Actions on the right in edit mode, the tabs first."""
        return [
            self.mode_tabs,
            self.settings_button,
            *self.editor.toolbar_extra,
            self.overflow,
            self._divider,
            self.discard_button,
            self.save_button,
            self.done_button,
        ]

    def _overflow_items(self) -> list[dict]:
        """Entries of the overflow menu for the current mode.

        Each entry has an ``action`` key that :meth:`_on_overflow` dispatches on.
        A destructive entry goes last and is shown in the error colour.
        """
        editor = self.editor
        if not editor.editable:
            return [] if editor.read_only else [_DOWNLOAD, _SETTINGS]
        items = [_DOWNLOAD, _IMPORT]
        if editor._tile_items:
            items += [None, _CLEAR]
        return items

    def _on_overflow(self, item):
        action = item.get("action") if isinstance(item, dict) else None
        if action == "download":
            self.download()
        elif action == "import":
            self.open_import()
        elif action == "clear":
            self.editor._clear_dialog.open = True
        elif action == "settings":
            self.editor._settings_view.show("general")

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @pn.io.hold()
    def sync(self):
        """Reflect the editor's mode, dashboard and save state in the bar."""
        editor = self.editor
        editing = editor.editable
        dashboard = editor.dashboard
        self.title.object = (
            escape_markdown(dashboard.title) if dashboard is not None else "Untitled dashboard"
        )
        state, dirty = self._save_state_text()
        self.save_state.param.update(
            object=state,
            sx={
                "color": "text.secondary",
                "whiteSpace": "nowrap",
                **(_DIRTY_DOT_SX if dirty else {}),
            },
        )
        self._update_view_meta()
        self.health_popover.sync()
        self._sync_lead()
        # The hidden download button rides along with the actions, which are always rendered.
        actions = [
            *(self._edit_actions() if editing else self._view_actions()),
            self.download_button,
        ]
        if list(self._actions) != actions:
            self._actions[:] = actions
        items = self._overflow_items()
        self.overflow.param.update(
            items=items,
            stylesheets=[_DANGER_LAST_ITEM] if items and items[-1] is _CLEAR else [],
        )
        self.save_button.disabled = editor.read_only or not editor.dirty
        self.discard_button.disabled = not editor.dirty
        self.settings_button.disabled = editor.read_only
        if self.mode_toggle.value != "view":
            self.mode_toggle.value = "view"

    def _sync_lead(self):
        row = not self.in_header
        editing = self.editor.editable
        full = self.layout == "full"
        sub = self._edit_status() if editing else self._view_meta()
        self.title.variant = "h5" if full and not editing else "subtitle1"
        if full:
            # The title over a wrapping meta line, the way a page heading reads.
            if list(self._meta) != sub:
                self._meta[:] = sub
            lead, head = [], self._title_block
        else:
            lead, head = ([self.title, *sub] if row else sub), self._lead
        if list(self._lead) != lead:
            self._lead[:] = lead
        # In header placement the host renders the lead and actions, and a
        # model must not be rendered in two places. Hidden there, the actions
        # still carry the download button.
        parts = [head, self._actions] if row else []
        if list(self._bar) != parts:
            self._bar[:] = parts
        shown = row or self.visible
        self._lead.visible = shown
        self._actions.visible = shown

    def _save_state_text(self) -> tuple[str, bool]:
        editor = self.editor
        if editor.read_only:
            return ("Read-only" if editor.dashboard is not None else ""), False
        if editor.dirty:
            return "Unsaved changes", True
        if self._saved_at is not None:
            return f"Saved {self._saved_at}", False
        if editor.dashboard is not None:
            return "All changes saved", False
        return "", False

    def _update_view_meta(self):
        settings = self.editor.settings
        period = settings.refresh_period
        text = f"Refreshed {self._refreshed_at}" if self._refreshed_at else ""
        if text and period:
            text += f", auto-refresh every {format_period(period)}"
        self.refresh_text = text
        self.refresh_label.object = text
        self.refresh_button.description = f"Refresh now. {text}." if text else "Refresh now"
        description = settings.description.strip()
        # The tooltip also names the button for assistive technology.
        self.info_button.description = description or None
        self.description.object = description
        self.refresh_period.param.update(
            label=format_period(period) if period else "Off",
            items=[
                {
                    "label": label,
                    "icon": "radio_button_checked"
                    if seconds == period
                    else "radio_button_unchecked",
                    "period": seconds,
                }
                for label, seconds in _PERIODS
            ],
        )

    def _sync_view_meta(self, *_events):
        if self.editor.editable:
            return
        self._update_view_meta()
        self._sync_lead()

    def stamp_refresh(self):
        """Record that the dashboard was refreshed now."""
        self._refreshed_at = time.strftime("%H:%M")
        with pn.io.hold():
            self._update_view_meta()
            if not self.editor.editable:
                self._sync_lead()

    def _on_period_chosen(self, item):
        """Set the auto-refresh period for this session, without marking the dashboard changed."""
        if not isinstance(item, dict) or "period" not in item:
            return
        editor = self.editor
        editor._loading_settings = True
        try:
            editor.settings.refresh_period = item["period"]
        finally:
            editor._loading_settings = False

    def _on_dashboard_changed(self, _event):
        self._saved_at = None
        self.sync()

    def _on_saved(self, _event):
        self._saved_at = time.strftime("%H:%M")
        self.sync()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_mode_toggled(self, event):
        if event.new == "edit":
            self.request_mode("edit")

    def request_mode(self, mode: str):
        """Switch to *mode* ("edit" or "view") through `on_mode`, or directly."""
        if self.on_mode is not None:
            self.on_mode(mode)
        elif mode == "edit":
            self.editor.editable = True
        elif self.editor.dirty:
            self._confirm_leave()
        else:
            self.editor.editable = False
        self.sync()

    def _confirm_leave(self):
        """Ask what to do with unsaved changes before returning to view mode."""
        keep = pmui.Button(label="Keep editing", variant="text")
        discard = pmui.Button(label="Discard", color="error", variant="outlined")
        save = pmui.Button(label="Save", color="primary")
        answered = threading.Lock()

        def answer_with(handler):
            def answer(_event):
                if answered.acquire(blocking=False):
                    close_dialog()
                    handler()

            return answer

        def save_and_view():
            if self.editor._on_save_clicked():
                self.editor.editable = False

        def discard_and_view():
            self.discard()
            self.editor.editable = False

        keep.on_click(answer_with(lambda: None))
        discard.on_click(answer_with(discard_and_view))
        save.on_click(answer_with(save_and_view))
        open_dialog(
            "Unsaved changes",
            pn.Column(
                pn.pane.Markdown("Save your changes before returning to the dashboard?"),
                pn.Row(pn.layout.HSpacer(), keep, discard, save, sizing_mode="stretch_width"),
                sizing_mode="stretch_width",
            ),
        )

    def _confirm_discard(self):
        confirm(
            "Discard unsaved changes?",
            "Your changes since the last save are lost and the saved dashboard is reloaded.",
            on_confirm=lambda _event: self.discard(),
            confirm_label="Discard changes",
        )

    def discard(self):
        """Throw away unsaved changes, reloading the saved dashboard."""
        editor = self.editor
        model = editor.dashboard
        if editor.store is not None and model is not None:
            # Never shows a newer stored version to someone no longer allowed to view it.
            model = editor._load_stored_dashboard() or model
        if model is None:
            editor.clear()
        else:
            editor.load_model(model)
        editor.dirty = False

    def download(self):
        """Send the canvas to the browser as a dashboard file."""
        button = self.download_button
        # Resending identical data would not change the property, so no download would start.
        button.data = None
        button._transfer()

    def open_import(self):
        """Ask for a dashboard file to replace the canvas with."""
        chooser = pmui.FileInput(
            label="Choose file", accept=".json", icon="upload_file", variant="outlined"
        )

        def _on_file(event):
            if not event.new:
                return
            close_dialog()
            content = event.new
            if isinstance(content, str):
                content = base64.b64decode(content.split(",", 1)[-1])
            self.editor._on_files_dropped(
                [{"name": chooser.filename or "dashboard.json", "content": content.decode()}]
            )

        chooser.param.watch(_on_file, "value")
        open_dialog(
            "Import dashboard file",
            pn.Column(
                pn.pane.Markdown(
                    "Choose a downloaded dashboard file. You can also drop one onto the canvas."
                ),
                chooser,
                sizing_mode="stretch_width",
            ),
        )

    def __panel__(self):
        """Render the bar."""
        return self._bar


_DOWNLOAD = {"label": "Download", "icon": "download", "action": "download"}
_IMPORT = {"label": "Import file", "icon": "upload_file", "action": "import"}
_SETTINGS = {"label": "Settings", "icon": "tune", "action": "settings"}
_CLEAR = {
    "label": "Clear canvas",
    "icon": "delete_sweep",
    "action": "clear",
}
