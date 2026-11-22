"""The bar above a dashboard: view actions in view mode, editing controls in edit mode."""

from __future__ import annotations

import base64
import threading
import time

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewer

from panel_flowdash.ui.fullscreen import TOGGLE_CLASS
from panel_flowdash.ui.health import DashboardHealth
from panel_flowdash.ui.health_popover import HealthPopover
from panel_flowdash.ui.popover_access import (
    NON_MODAL_POPUP_SX,
    MenuAccess,
    PopoverAccess,
    link_popup,
)
from panel_flowdash.ui.text import escape_markdown, markdown_pane
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

# Below this width the edit actions only fit one row with Settings as an icon.
_WIDE_BREAKPOINT = 1280
_HIDE_WHEN_NARROW = (
    f"@media (max-width: {_WIDE_BREAKPOINT - 0.05}px) {{ :host {{ display: none !important; }} }}"
)
_HIDE_WHEN_WIDE = (
    f"@media (min-width: {_WIDE_BREAKPOINT}px) {{ :host {{ display: none !important; }} }}"
)

# Below these widths the top bar's labelled actions show only their icons, so
# the actions, search, assistant and theme toggle fit one row: the view
# actions below COMPACT_BREAKPOINT and the wider edit actions below
# EDIT_COMPACT_BREAKPOINT. The labels stay in the DOM, so the buttons keep their
# accessible names.
COMPACT_BREAKPOINT = 1024
EDIT_COMPACT_BREAKPOINT = _WIDE_BREAKPOINT

# Material UI's sm breakpoint. On narrower screens, phones, the top bar only
# has room for the title, the health chip or save state, Save, the toolbar
# extras and the overflow menu, so the other actions move into that menu.
PHONE_BREAKPOINT = 600
_HIDE_ON_PHONE = (
    f"@media (max-width: {PHONE_BREAKPOINT - 0.05}px) {{ :host {{ display: none !important; }} }}"
)


class PhoneQuery(JSComponent):
    """Reports whether the browser window is narrower than ``breakpoint``, as a phone's is."""

    breakpoint = param.Integer(default=PHONE_BREAKPOINT, doc="Window width in pixels.")

    phone = param.Boolean(default=False, doc="Whether the window is narrower than the breakpoint.")

    _esm = """
    export function render({ model }) {
      const query = window.matchMedia(`(max-width: ${model.breakpoint - 0.05}px)`)
      const sync = () => {
        if (model.phone !== query.matches) { model.phone = query.matches }
      }
      sync()
      query.addEventListener("change", sync)
      model.on("remove", () => query.removeEventListener("change", sync))
    }
    """


# A button's label sets its line height. Without the label the icon keeps that
# line height, so an icon-only button is as tall as a labelled one.
LABEL_LINE_HEIGHT = "calc(0.8125rem * 1.75)"

# Icon-only controls without a tooltip of their own show their name like the
# MUI tooltips of the buttons beside them: beside the control with an arrow,
# after the same delay. Only a device that can hover shows them on hover, so a
# tap on a touch screen does not leave one open.
_TOOLTIP_CSS = """
  position: absolute;
  top: 50%;
  z-index: 1500;
  padding: 4px 8px;
  border-radius: var(--mui-shape-borderRadius, 4px);
  background: var(--mui-palette-Tooltip-bg, rgba(97, 97, 97, 0.92));
  color: var(--mui-palette-common-white, #fff);
  font-family: inherit;
  font-size: 0.6875rem;
  font-weight: 500;
  line-height: 1.4em;
  letter-spacing: normal;
  text-transform: none;
  white-space: nowrap;
  pointer-events: none;
  animation: fd-tooltip-in 150ms 500ms both;
"""
_TOOLTIP_ARROW_CSS = """
  content: "";
  position: absolute;
  top: 50%;
  z-index: 1500;
  width: 8px;
  height: 8px;
  background: var(--mui-palette-Tooltip-bg, rgba(97, 97, 97, 0.92));
  pointer-events: none;
  animation: fd-tooltip-in 150ms 500ms both;
"""
_TOOLTIP_PLACEMENTS = {
    "right": (
        "left: calc(100% + 14px); transform: translateY(-50%);",
        "left: calc(100% + 10px); transform: translateY(-50%) rotate(45deg);",
    ),
    "left": (
        "right: calc(100% + 14px); transform: translateY(-50%);",
        "right: calc(100% + 10px); transform: translateY(-50%) rotate(45deg);",
    ),
}
TOOLTIP_KEYFRAMES = "@keyframes fd-tooltip-in { from { opacity: 0; } to { opacity: 1; } }"


def tooltip_css(selector: str, content: str, placement: str = "right") -> str:
    """Return CSS showing *content* as a tooltip beside *selector* while it is hovered or focused.

    *selector* is completed with the hover or focus state by the caller's
    ``{state}`` placeholder, e.g. ``".pa-toggle{state}"``. *placement* is
    ``"right"`` or ``"left"``.
    """
    box, arrow = _TOOLTIP_PLACEMENTS[placement]

    def rules(state):
        target = selector.format(state=state)
        return (
            f"{target}::after {{ content: {content}; {_TOOLTIP_CSS} {box} }}"
            f"{target}::before {{ {_TOOLTIP_ARROW_CSS} {arrow} }}"
        )

    return f"@media (hover: hover) {{ {rules(':hover')} }} {rules(':focus-visible')}"


def icon_only_below(width: int, disabled_tooltip: str | None = None) -> str:
    """Return a stylesheet showing only the icons of a button or toggle group below *width*.

    The options of a toggle group show their names as tooltips; a button that
    is disabled, where its own tooltip cannot show, shows *disabled_tooltip*.
    """
    disabled = (
        tooltip_css(":host({state}) .MuiButton-root.Mui-disabled", f'"{disabled_tooltip}"')
        if disabled_tooltip
        else ""
    )
    return f"""
{TOOLTIP_KEYFRAMES}
@media (max-width: {width - 0.05}px) {{
  .MuiButton-root, .MuiToggleButton-root {{ font-size: 0 !important; min-width: 0 !important; }}
  .MuiButton-startIcon {{ margin: 0 !important; }}
  .MuiButton-root .material-icons, .MuiToggleButton-root .material-icons {{
    font-size: 20px !important;
    height: {LABEL_LINE_HEIGHT} !important;
    line-height: {LABEL_LINE_HEIGHT} !important;
  }}
  {tooltip_css(".MuiToggleButton-root{state}", "attr(aria-label)")}
  {disabled}
}}
"""


ICON_ONLY_WHEN_COMPACT = icon_only_below(COMPACT_BREAKPOINT)

# Done keeps its label longer than the view actions: its eye icon alone does not say
# it ends editing. Below this width the title and save state need the room.
DONE_COMPACT_BREAKPOINT = 900
_DONE_ICON_ONLY = icon_only_below(DONE_COMPACT_BREAKPOINT)

# Icon buttons are as tall as the text buttons beside them.
_ICON_BUTTON_SX = {"p": f"calc(({LABEL_LINE_HEIGHT} + 12px - 20px) / 2)"}
_ICON_ONLY_WHEN_NARROW = icon_only_below(EDIT_COMPACT_BREAKPOINT)

# The toggle groups are as tall as the outlined buttons beside them.
_TOGGLE_SX = {
    "& .MuiToggleButton-root": {"textTransform": "none", "px": 1.5, "py": "6px"},
    # Icon-only segments need less padding, which leaves the title room beside the Explore segment.
    "@media (max-width: 799.95px)": {"& .MuiToggleButton-root": {"px": 0.75}},
}

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


# Below this width the save state gives its room in the top bar to the edit
# actions: only the dot of unsaved changes shows, its text stays for screen readers.
_SAVE_STATE_DOT_ONLY = """
@media (max-width: 799.95px) {
  .MuiTypography-root { font-size: 0 !important; }
  .MuiTypography-root::before { margin-right: 0 !important; }
}
"""

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


_EXPLORE_LABEL = ":material/forum: Explore"
_REPORT_LABEL = ":material/description: Report"

# The view-mode toggle; people who may only view the dashboard get no Edit.
_VIEW_OPTIONS = {
    _EXPLORE_LABEL: "explore",
    _REPORT_LABEL: "report",
    ":material/visibility: View": "view",
    ":material/edit: Edit": "edit",
}
_READER_OPTIONS = {
    _EXPLORE_LABEL: "explore",
    _REPORT_LABEL: "report",
    ":material/visibility: View": "view",
}
# The views shown in place of the workspace, picked in the toggle and the tabs alike.
_SURFACES = ("explore", "report")

# Below this width the Report segment, second in both switchers, leaves them for
# the overflow menu, so the dashboard title keeps its room.
REPORT_MENU_BREAKPOINT = 1100
_REPORT_IN_MENU = f"""
@media (max-width: {REPORT_MENU_BREAKPOINT - 0.05}px) {{
  .MuiToggleButton-root:nth-child(2) {{ display: none !important; }}
}}
"""


def _view(editor) -> str:
    """Return the entry of the View/Edit toggle the editor shows: a surface, or "view"."""
    return editor.surface if editor.surface in _SURFACES else "view"


def _tab(editor) -> str:
    """Return the edit tab the editor shows: a surface, or its mode."""
    return editor.surface if editor.surface in _SURFACES else editor.mode


class DashboardBar(Viewer):
    """One row of dashboard controls, with a view and an edit section.

    In view mode it shows the dashboard title, an info button opening the
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
    :meth:`_edit_actions`, and :meth:`_menu_items` for the menu entries
    (dispatched by :meth:`_on_overflow`). Call :meth:`sync` after changing
    anything they read. On phones the menu also stands in for the actions
    the top bar hides (:meth:`_phone_items`).
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
            variant="caption",
            sx={"color": "text.secondary", "whiteSpace": "nowrap"},
            margin=0,
            stylesheets=[_SAVE_STATE_DOT_ONLY],
        )
        self.info_button = pmui.IconButton(
            icon="info_outline",
            size="small",
            margin=0,
            color="default",
            description="About this dashboard",
            css_classes=["fd-description"],
            sx={"color": "text.secondary"},
        )
        self.info_button.on_click(
            lambda _event: self.info_popup.param.update(open=not self.info_popup.open)
        )
        self._about = pmui.Typography(
            variant="body2", margin=0, sx={"color": "text.secondary", "whiteSpace": "pre-wrap"}
        )
        self.info_popup = pmui.Popup(
            self._about,
            anchor_origin={"horizontal": "left", "vertical": "bottom"},
            transform_origin={"horizontal": "left", "vertical": "top"},
            elevation=4,
            hide_backdrop=True,
            css_classes=["fd-description-popover"],
            sx={
                **NON_MODAL_POPUP_SX,
                "& .MuiPopover-paper": {
                    **NON_MODAL_POPUP_SX["& .MuiPopover-paper"],
                    "p": 2,
                    "mt": 1,
                    "maxWidth": "min(420px, calc(100vw - 32px))",
                    "borderRadius": "12px",
                },
            },
        )
        info_access = PopoverAccess(
            button_class="fd-description",
            popup_class="fd-description-popover",
            label="About this dashboard",
            margin=0,
            width=0,
            height=0,
        )
        link_popup(self.info_popup, info_access)
        # The popup anchors to its parent, so it sits next to the button.
        self.info = pn.Row(
            self.info_button,
            self.info_popup,
            info_access,
            margin=0,
            align="center",
            styles={"width": "fit-content", "flex": "0 0 auto"},
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
            stylesheets=[ICON_ONLY_WHEN_COMPACT, _HIDE_ON_PHONE],
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
            stylesheets=[_HIDE_ON_PHONE],
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
            sx=_ICON_BUTTON_SX,
            stylesheets=[_HIDE_ON_PHONE],
        )
        self.mode_toggle = pmui.RadioButtonGroup(
            options=_VIEW_OPTIONS,
            value="view",
            size="small",
            margin=(0, 12, 0, 4),
            sx=_TOGGLE_SX,
            stylesheets=[ICON_ONLY_WHEN_COMPACT, _HIDE_ON_PHONE, _REPORT_IN_MENU],
        )
        self.mode_toggle.param.watch(self._on_mode_toggled, "value")

        self.mode_tabs = pmui.RadioButtonGroup(
            options={
                _EXPLORE_LABEL: "explore",
                _REPORT_LABEL: "report",
                ":material/account_tree: Dataflow": "wiring",
                ":material/dashboard: Layout": "dashboard",
                ":material/vertical_split: Split": "split",
            },
            value=_tab(editor),
            size="small",
            margin=(0, 12, 0, 4),
            sx=_TOGGLE_SX,
            stylesheets=[_ICON_ONLY_WHEN_NARROW, _HIDE_ON_PHONE, _REPORT_IN_MENU],
        )
        self.mode_tabs.param.watch(self._on_tab, "value")
        editor.param.watch(self._sync_switchers, ["surface", "mode", "read_only"])
        # The overflow menu offers the views the switchers have no room for.
        editor.param.watch(lambda _event: self.sync(), "surface")
        self.settings_button = pmui.Button(
            label="Settings",
            icon="tune",
            variant="text",
            size="small",
            margin=(0, 4),
            description="Settings",
            stylesheets=[_HIDE_WHEN_NARROW],
        )
        # On narrower screens only the icon shows, so the edit actions fit one row.
        self.settings_icon_button = pmui.IconButton(
            icon="tune",
            size="small",
            margin=(0, 4),
            color="primary",
            description="Settings",
            sx=_ICON_BUTTON_SX,
            stylesheets=[_HIDE_WHEN_WIDE, _HIDE_ON_PHONE],
        )
        self.settings_icon_button.on_click(lambda _event: self._click_settings())
        self.discard_button = pmui.Button(
            label="Discard",
            icon="undo",
            variant="outlined",
            size="small",
            margin=(0, 4),
            description="Discard",
            stylesheets=[
                icon_only_below(EDIT_COMPACT_BREAKPOINT, disabled_tooltip="Discard"),
                _HIDE_ON_PHONE,
            ],
        )
        self.discard_button.on_click(lambda _event: self._confirm_discard())
        self.save_button = pmui.Button(
            label="Save",
            icon="save",
            variant="contained",
            size="small",
            margin=(0, 4),
            description="Save",
            stylesheets=[icon_only_below(EDIT_COMPACT_BREAKPOINT, disabled_tooltip="Save")],
        )
        self.done_button = pmui.Button(
            label="Done",
            icon="visibility",
            variant="outlined",
            size="small",
            margin=(0, 4),
            description="Done",
            stylesheets=[_DONE_ICON_ONLY, _HIDE_ON_PHONE],
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
            css_classes=["fd-more-actions"],
        )
        self.overflow.on_click(self._on_overflow)
        self._overflow_access = MenuAccess(button_class="fd-more-actions", margin=0)
        self._divider = pmui.Divider(
            orientation="vertical",
            height=24,
            margin=(0, 8),
            align="center",
            stylesheets=[_HIDE_ON_PHONE],
        )
        # The menu cannot start a browser download itself, so a hidden button
        # receives the file and saves it as soon as its data arrives.
        self.download_button = pmui.FileDownload(
            callback=editor._download_callback,
            filename="dashboard.json",
            auto=True,
            visible=False,
        )
        self._phone_query = PhoneQuery(width=0, height=0, margin=0)
        self._phone_query.param.watch(lambda _event: self.sync(), "phone")
        self._narrow_query = PhoneQuery(
            breakpoint=REPORT_MENU_BREAKPOINT, width=0, height=0, margin=0
        )
        self._narrow_query.param.watch(lambda _event: self.sync(), "phone")

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
        # In a host header the lead and actions hide with the bar, e.g. in full screen.
        self.param.watch(lambda _event: self._sync_lead(), "visible")
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
        if self.editor._has_code_drafts() and not self.editor.read_only:
            # Held while viewing, until the code panel runs or discards them.
            items.append(self.save_state)
        if self._about.object:
            items.append(self.description if full else self.info)
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
        actions = [self.mode_toggle]
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
            self.settings_icon_button,
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
        if self._phone_query.phone:
            phone = self._phone_items()
        elif self._narrow_query.phone and self.editor.surface != "report":
            kind = "tab" if self.editor.editable else "mode"
            phone = [{"label": "Report", "icon": "description", "action": f"{kind}:report"}]
        else:
            phone = []
        items = self._menu_items()
        return [*phone, None, *items] if phone and items else phone or items

    def _phone_items(self) -> list[dict]:
        """Entries for the actions the top bar hides on phones, first in the overflow menu."""
        editor = self.editor
        if editor.editable:
            current = _tab(editor)
            items = [
                {"label": label, "icon": icon, "action": f"tab:{tab}"}
                for tab, label, icon in _PHONE_TABS
                if tab != current
            ]
            if not editor.read_only:
                items.append(_SETTINGS)
            if editor.dirty:
                items.append(_PHONE_DISCARD)
            return [*items, _PHONE_DONE]
        current = _view(editor)
        items = [
            {"label": label, "icon": icon, "action": f"mode:{mode}"}
            for mode, label, icon in _PHONE_MODES
            if mode != current and not (mode == "edit" and editor.read_only)
        ]
        return [*items, _PHONE_REFRESH, _PHONE_FULLSCREEN]

    def _menu_items(self) -> list[dict]:
        """Entries of the overflow menu on every screen."""
        editor = self.editor
        if not editor.editable:
            return [] if editor.read_only else [_DOWNLOAD, _DOWNLOAD_YAML, _SETTINGS]
        items = [_DOWNLOAD, _DOWNLOAD_YAML, _IMPORT]
        if editor._tile_items:
            items += [None, _CLEAR]
        return items

    def _on_overflow(self, item):
        action = item.get("action") if isinstance(item, dict) else None
        if action is None:
            return
        kind, _, value = action.partition(":")
        if kind == "mode":
            self.mode_toggle.value = value
        elif kind == "tab":
            self.mode_tabs.value = value
        elif action == "refresh":
            self.editor.param.trigger("refresh")
        elif action == "fullscreen":
            self.editor.fullscreen = True
        elif action == "discard":
            self._confirm_discard()
        elif action == "done":
            self.request_mode("view")
        elif action == "download":
            self.download()
        elif action == "download_yaml":
            self.download("yaml")
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
        # The hidden download button and the phone query ride along with the
        # actions, which are always rendered.
        actions = [
            *(self._edit_actions() if editing else self._view_actions()),
            self.download_button,
            self._phone_query,
            self._narrow_query,
            self._overflow_access,
        ]
        if list(self._actions) != actions:
            self._actions[:] = actions
        items = self._overflow_items()
        self.overflow.param.update(
            items=items,
            stylesheets=[_DANGER_LAST_ITEM] if items and items[-1] is _CLEAR else [],
        )
        self.save_button.disabled = editor.read_only or not (
            editor.dirty or editor._has_unsent_edits()
        )
        self.discard_button.disabled = not editor.dirty
        self.settings_button.disabled = editor.read_only
        self.settings_icon_button.disabled = editor.read_only
        self._sync_switchers()

    def _sync_switchers(self, *_events):
        """Show the open view in the View/Edit toggle and the edit tabs; readers get no Edit."""
        editor = self.editor
        options = _READER_OPTIONS if editor.read_only else _VIEW_OPTIONS
        view = _view(editor)
        if self.mode_toggle.options != options or self.mode_toggle.value != view:
            self.mode_toggle.param.update(options=options, value=view)
        tab = _tab(editor)
        if self.mode_tabs.value != tab:
            self.mode_tabs.value = tab

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
        if editor._has_code_drafts():
            return "Edits not run", True
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
        self.description.object = self._about.object = escape_markdown(description)
        if not description:
            self.info_popup.open = False
        label = format_period(period) if period else "Off"
        self.refresh_period.param.update(
            label=label,
            # The name includes the visible label.
            description=f"Update automatically: {label}",
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

    def _click_settings(self):
        self.settings_button.clicks += 1

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
            self.editor.surface = "workspace"
            self.request_mode("edit")
        elif event.new in _SURFACES:
            self.editor.surface = event.new
        elif event.new == "view":
            self.editor.surface = "workspace"

    def _on_tab(self, event):
        editor = self.editor
        if event.new in _SURFACES:
            editor.surface = event.new
        elif event.new is not None:
            with param.parameterized.batch_call_watchers(editor):
                editor.mode = event.new
                editor.surface = "workspace"

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
                markdown_pane("Save your changes before returning to the dashboard?"),
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

    def download(self, extension: str = "json"):
        """Send the canvas to the browser as a dashboard file, JSON or YAML."""
        self.editor._download_format = extension
        button = self.download_button
        # Resending identical data would not change the property, so no download would start.
        button.data = None
        button._transfer()

    def open_import(self):
        """Ask for a dashboard file to replace the canvas with."""
        chooser = pmui.FileInput(
            label="Choose file",
            accept=".json,.yaml,.yml",
            icon="upload_file",
            variant="outlined",
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
                markdown_pane(
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
_DOWNLOAD_YAML = {"label": "Download as YAML", "icon": "data_object", "action": "download_yaml"}
_IMPORT = {"label": "Import file", "icon": "upload_file", "action": "import"}
_SETTINGS = {"label": "Settings", "icon": "tune", "action": "settings"}
# The entries standing in for the top bar's actions on phones.
_PHONE_MODES = [
    ("explore", "Explore", "forum"),
    ("report", "Report", "description"),
    ("view", "View", "visibility"),
    ("edit", "Edit", "edit"),
]
_PHONE_TABS = [
    ("explore", "Explore", "forum"),
    ("report", "Report", "description"),
    ("wiring", "Dataflow", "account_tree"),
    ("dashboard", "Layout", "dashboard"),
]
_PHONE_REFRESH = {"label": "Refresh now", "icon": "refresh", "action": "refresh"}
_PHONE_FULLSCREEN = {"label": "Full screen", "icon": "fullscreen", "action": "fullscreen"}
_PHONE_DISCARD = {"label": "Discard changes", "icon": "undo", "action": "discard"}
_PHONE_DONE = {"label": "Done editing", "icon": "visibility", "action": "done"}
_CLEAR = {
    "label": "Clear canvas",
    "icon": "delete_sweep",
    "action": "clear",
}
