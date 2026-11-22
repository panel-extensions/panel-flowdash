"""The built-in sections of the project file the admin view edits: ``general`` and ``theme``.

Each setting names the app parameter it configures (its ``target``). When
the app starts a session, a value the project file sets is used for every
parameter the app was not given in code; one given in code wins and the
admin view shows the setting locked.
"""

from __future__ import annotations

import html
import typing as t
from collections.abc import Callable, Mapping
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewer

from panel_flowdash.admin.form import Group, Setting, SettingsForm, current_app, settings_loader
from panel_flowdash.history import BURST_SECONDS, HISTORY_MAX_BYTES
from panel_flowdash.project import Project, ProjectSection, register_section

_MB = 2**20


def _home_options() -> dict[str, t.Any]:
    options: dict[str, t.Any] = {"Dashboard grid": ""}
    app = current_app()
    if app is not None:
        dashboards = app.store.list_accessible(app._identity, default_allow=app._default_allow())
        for dashboard in sorted(dashboards, key=lambda d: d.title.lower()):
            options[dashboard.title] = dashboard.dashboard_id
    return options


GENERAL_GROUPS = (
    Group(
        "Dashboards",
        (
            Setting(
                "edit_workspace",
                "Edit workspace",
                "Where Edit lands: the wiring canvas, the tile layout or both.",
                kind="choice",
                options={"Dataflow": "dataflow", "Layout": "layout", "Split": "split"},
                default="dataflow",
                target="editor_options.mode",
                from_target={"wiring": "dataflow", "dashboard": "layout", "split": "split"}.get,
            ),
            Setting(
                "home_dashboard",
                "Home page",
                "The dashboard grid, or one dashboard by id or title.",
                kind="select",
                options=_home_options,
                default=None,
                allow_none=True,
                target="home_dashboard",
            ),
            Setting(
                "live_values",
                "Live values on ports",
                "Open a port's value on click, on hover or not at all.",
                kind="choice",
                options={"Click": "click", "Hover": "hover", "Off": "off"},
                default="click",
                target="editor_options.popup_trigger",
                from_target=lambda value: "off" if value == "none" else value,
            ),
        ),
    ),
    Group(
        "Edit history",
        (
            Setting(
                "history",
                "Record edit history",
                "Every edit becomes a version people can undo, browse and restore.",
                default=True,
                target="editor_options.history",
            ),
            Setting(
                "history_coalesce",
                "Group edits within",
                "A burst of edits becomes one version.",
                kind="number",
                unit="Seconds",
                bounds=(0, None),
                step=0.5,
                default=BURST_SECONDS,
                target="editor_options.history_coalesce",
            ),
            Setting(
                "history_keep",
                "Keep at most",
                "Whichever limit is reached first; the latest version is always kept.",
                kind="numbers",
                parts=(
                    Setting(
                        "history_limit",
                        "Versions",
                        kind="number",
                        unit="Versions",
                        integer=True,
                        bounds=(1, None),
                        default=500,
                        allow_none=True,
                        target="editor_options.history_limit",
                    ),
                    Setting(
                        "history_max_days",
                        "Days",
                        kind="number",
                        unit="Days",
                        integer=True,
                        bounds=(0, None),
                        default=90,
                        allow_none=True,
                        target="editor_options.history_max_days",
                    ),
                    Setting(
                        "history_max_mb",
                        "MB",
                        kind="number",
                        unit="MB",
                        integer=True,
                        bounds=(0, None),
                        default=HISTORY_MAX_BYTES // _MB,
                        allow_none=True,
                        target="editor_options.history_max_bytes",
                        from_target=lambda value: None if value is None else round(value / _MB),
                    ),
                ),
            ),
        ),
    ),
    Group(
        "Explore and the assistant",
        (
            Setting(
                "explore",
                "Explore view",
                "Ask about a dashboard's data, or keep a read-only record of its turns.",
                kind="choice",
                options={"Ask": "ask", "Read only": "read_only"},
                default="ask",
                target="explore_agent",
                from_target=lambda value: "read_only" if value is False else "ask",
            ),
            Setting(
                "agent_palette",
                "Assistant may run commands",
                "Give the agent the command palette, e.g. to open or refresh a dashboard.",
                default=True,
                target="agent_palette",
            ),
            Setting(
                "provenance",
                "Show how a node was made",
                "“How was this made?” on nodes an agent request created.",
                default=True,
                target="editor_options.provenance",
            ),
        ),
        column=1,
    ),
    Group(
        "New dashboard defaults",
        (
            Setting(
                "tile_cards",
                "Frame tiles",
                "Draw tiles as cards. Each dashboard can turn it off.",
                default=True,
                target="editor_options.tile_cards",
            ),
            Setting(
                "tile_titles",
                "Show tile titles",
                "Off: components show their own headings.",
                default=False,
                target="editor_options.tile_titles",
            ),
            Setting(
                "node_previews",
                "Preview values on the canvas",
                "Cards show the first rows, a sparkline or a few items.",
                default=False,
                target="editor_options.node_previews",
            ),
            Setting(
                "controls_position",
                "Controls",
                "Where controls go.",
                kind="choice",
                options={"Above": "top", "Left": "left"},
                default="top",
                target="controls_position",
            ),
        ),
        column=1,
    ),
)

PRIMARY = "#0072B5"

SWATCHES = (PRIMARY, "#006D77", "#5E35B1", "#C2185B", "#E65100", "#37474F")

# Fonts offered for the interface, with the Google Fonts family each loads (None: built in).
FONTS: dict[str, str | None] = {
    "Roboto": None,
    "Inter": "Inter:wght@400;500;600",
    "IBM Plex Sans": "IBM+Plex+Sans:wght@400;500;600",
    "Source Sans 3": "Source+Sans+3:wght@400;500;600",
    "Open Sans": "Open+Sans:wght@400;500;600",
    "System UI": None,
}


def _project_directory(project: Project) -> Path | None:
    if project.directory is not None:
        return project.directory
    project_dir = project.context.get("project_dir") if project.context else None
    return Path(project_dir) if project_dir is not None else None


def resolve_logo(value: str, directory: Path | None) -> str | None:
    """Return the logo *value* names: an http(s) URL, or a file inside *directory*.

    None for anything else, such as a path that leads out of the project
    directory with ``..``, an absolute path elsewhere or another URL scheme.
    """
    if "://" in value:
        return value if value.lower().startswith(("http://", "https://")) else None
    if directory is None:
        return None
    root = directory.resolve()
    path = (root / value).resolve()
    return str(path) if path.is_relative_to(root) else None


def _check_logo(value: str, section) -> str | None:
    if resolve_logo(value, _project_directory(section.project)) is None:
        return "`logo` is an http(s) URL or the path of a file inside the project directory."
    return None


THEME_GROUPS = (
    Group(
        "Colours",
        (
            Setting(
                "color_scheme",
                "Colour scheme",
                "What people see before they pick light or dark themselves.",
                kind="choice",
                options={"Light": "light", "Dark": "dark"},
                default="light",
                target="page_options.dark_theme",
                from_target=lambda value: "dark" if value else "light",
            ),
            Setting(
                "primary_color",
                "Primary colour",
                "Buttons, the selected rail item, links and focus rings.",
                kind="color",
                swatches=SWATCHES,
                default=PRIMARY,
                target="page_options.theme_config.palette.primary",
                from_target=lambda value: value.get("main") if isinstance(value, dict) else value,
            ),
        ),
    ),
    Group(
        "Type and shape",
        (
            Setting(
                "font_family",
                "Font",
                "The interface font. Fonts other than Roboto load from Google Fonts.",
                kind="select",
                options={name: name for name in FONTS},
                default="Roboto",
                target="page_options.theme_config.typography.fontFamily",
                from_target=lambda value: value.split(",")[0].strip().strip("\"'"),
            ),
            Setting(
                "border_radius",
                "Corner radius",
                "Cards, buttons and fields.",
                kind="slider",
                unit="px",
                bounds=(0, 16),
                integer=True,
                default=4,
                target="page_options.theme_config.shape.borderRadius",
            ),
        ),
    ),
    Group(
        "Branding",
        (
            Setting(
                "title",
                "Title",
                "In the top bar and the browser tab.",
                kind="text",
                default="FlowDash",
                target="title",
            ),
            Setting(
                "logo",
                "Logo",
                "An image URL, or a path relative to the project file.",
                kind="text",
                unit="logo.svg",
                default=None,
                allow_none=True,
                target="page_options.logo",
                validate=_check_logo,
            ),
        ),
    ),
)


def _font_stack(name: str) -> str:
    if name == "System UI":
        return 'system-ui, -apple-system, "Segoe UI", sans-serif'
    return f'"{name}", "Roboto", "Helvetica", "Arial", sans-serif'


# How a setting's value becomes its target parameter's value; others are used as they are.
CONVERT: dict[str, Callable[[t.Any], t.Any]] = {
    "edit_workspace": {"dataflow": "wiring", "layout": "dashboard", "split": "split"}.get,
    "live_values": lambda value: "none" if value == "off" else value,
    "history_max_mb": lambda value: None if value is None else int(value * _MB),
    "explore": lambda value: False if value == "read_only" else None,
    "color_scheme": lambda value: value == "dark",
    "primary_color": lambda value: {"main": value},
    "font_family": _font_stack,
}


def _leaves(groups) -> dict[str, Setting]:
    return {leaf.key: leaf for group in groups for s in group.settings for leaf in s.leaves()}


GENERAL_SETTINGS = _leaves(GENERAL_GROUPS)
THEME_SETTINGS = _leaves(THEME_GROUPS)


def project_overrides(project: Project | None, in_code: Callable[[str], bool]) -> dict[str, t.Any]:
    """Return the target values the project file's ``general`` and ``theme`` sections set.

    A target ``in_code`` says the app was given in code is left out, so
    code wins over the file.
    """
    overrides: dict[str, t.Any] = {}
    if project is None:
        return overrides
    for name, settings in (("general", GENERAL_SETTINGS), ("theme", THEME_SETTINGS)):
        values = project.get(name) or {}
        for key, value in values.items():
            setting = settings.get(key)
            if setting is None or setting.target is None or in_code(setting.target):
                continue
            if key == "logo" and value:
                value = resolve_logo(value, _project_directory(project))
                if value is None:
                    continue
            overrides[setting.target] = CONVERT.get(key, lambda v: v)(value)
    return overrides


def font_href(font_family: str | None) -> str | None:
    """Return the Google Fonts stylesheet of *font_family*, if it loads one."""
    family = FONTS.get(font_family or "")
    if family is None:
        return None
    return f"https://fonts.googleapis.com/css2?family={family}&display=swap"


class FontLoader(JSComponent):
    """Adds a font's stylesheet to the document head, where its font faces apply page-wide."""

    href = param.String(default=None, allow_None=True)

    _esm = """
    export function render({ model }) {
      const sync = () => {
        const href = model.href
        if (!href || document.head.querySelector(`link[data-flowdash-font="${href}"]`)) return
        const link = document.createElement("link")
        link.rel = "stylesheet"
        link.href = href
        link.dataset.flowdashFont = href
        document.head.appendChild(link)
      }
      sync()
      model.on("href", sync)
      return document.createElement("span")
    }
    """


# ---------------------------------------------------------------------------
# The theme preview
# ---------------------------------------------------------------------------

_BARS = (18, 30, 24, 38, 48, 42, 70, 60, 44, 30, 54, 66)

_PREVIEW_CSS = """
:host { width: 100%; }
.previews { display: flex; flex-direction: column; gap: 16px; }
.app { border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden;
  background: var(--bg); color: var(--text); font-family: var(--font); }
.top { display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  border-bottom: 1px solid var(--line); font-size: 13px; }
.top b { font-weight: 600; white-space: nowrap; }
.top .crumb { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.top .search { margin-left: auto; width: 120px; height: 18px; border-radius: 9px; background: var(--fill); }
.body { display: flex; }
.rail { width: 44px; display: flex; flex-direction: column; align-items: center; gap: 12px;
  padding: 12px 0; border-right: 1px solid var(--line); }
.rail i { display: block; width: 10px; height: 10px; border-radius: 2px; background: var(--muted); opacity: .7; }
.rail .on { width: 26px; height: 14px; border-radius: 7px; background: var(--tint);
  display: flex; align-items: center; justify-content: center; }
.rail .on i { background: var(--primary); opacity: 1; }
.main { flex: 1; padding: 10px; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
.card { border: 1px solid var(--line); border-radius: var(--radius); padding: 6px 8px; background: var(--paper); }
.kpi small { display: block; font-size: 8px; letter-spacing: .6px; color: var(--muted); text-transform: uppercase; }
.kpi span { font-size: 15px; }
.actions { display: flex; gap: 6px; margin-bottom: 8px; }
.btn { font-size: 9px; padding: 3px 7px; border-radius: var(--radius); border: 1px solid var(--primary);
  color: var(--link); white-space: nowrap; }
.btn.fill { background: var(--primary); color: #fff; }
.chart { display: flex; align-items: flex-end; gap: 6px; height: 72px; }
.chart b { flex: 1; background: var(--primary); border-radius: min(var(--radius), 3px) min(var(--radius), 3px) 0 0; }
"""


def _mix(color: str, other: str, amount: float) -> str:
    """Mix *amount* of *other* into the hex *color*."""
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    other = other.lstrip("#")
    channels = [
        round(int(color[i : i + 2], 16) * (1 - amount) + int(other[i : i + 2], 16) * amount)
        for i in (0, 2, 4)
    ]
    return "#" + "".join(f"{c:02x}" for c in channels)


def preview_html(
    *, title: str, primary: str, font: str, radius: int, schemes: t.Sequence[str]
) -> str:
    """Return the preview of a light and/or dark mini app in the given theme."""
    try:
        light_link = primary
        dark_link = _mix(primary, "#ffffff", 0.35)
        tint = _mix(primary, "#ffffff", 0.82)
        dark_tint = _mix(primary, "#121212", 0.6)
    except ValueError:
        primary, light_link, dark_link, tint, dark_tint = (
            PRIMARY,
            PRIMARY,
            "#58A9DE",
            "#dcebf5",
            "#0b3a55",
        )
    palettes = {
        "light": {
            "bg": "#ffffff",
            "paper": "#ffffff",
            "text": "rgba(0,0,0,.87)",
            "muted": "rgba(0,0,0,.6)",
            "line": "rgba(0,0,0,.12)",
            "fill": "rgba(0,0,0,.06)",
            "tint": tint,
            "link": light_link,
        },
        "dark": {
            "bg": "#121212",
            "paper": "#1a1a1a",
            "text": "#ffffff",
            "muted": "rgba(255,255,255,.7)",
            "line": "rgba(255,255,255,.12)",
            "fill": "rgba(255,255,255,.08)",
            "tint": dark_tint,
            "link": dark_link,
        },
    }
    bars = "".join(f'<b style="height:{h}px"></b>' for h in _BARS)
    apps = []
    for scheme in schemes:
        tokens = {**palettes[scheme], "primary": primary, "radius": f"{radius}px", "font": font}
        style = ";".join(f"--{name}:{value}" for name, value in tokens.items())
        apps.append(
            f'<div class="app {scheme}" style="{html.escape(style)}">'
            f'<div class="top"><b>{html.escape(title)}</b>'
            '<span class="crumb">Dashboards \u203a Fleet Overview</span><span class="search"></span></div>'
            '<div class="body"><div class="rail"><i></i><span class="on"><i></i></span><i></i></div>'
            '<div class="main"><div class="kpis">'
            '<div class="card kpi"><small>Turbines</small><span>71,790</span></div>'
            '<div class="card kpi"><small>Capacity</small><span>138.2 GW</span></div>'
            '<div class="card kpi"><small>Projects</small><span>1,711</span></div></div>'
            '<div class="card"><div class="actions"><span class="btn fill">New dashboard</span>'
            f'<span class="btn">Share</span></div><div class="chart">{bars}</div></div>'
            "</div></div></div>"
        )
    return f'<div class="previews">{"".join(apps)}</div>'


class ThemePreview(Viewer):
    """A mini app in the theme the form's controls describe, in light and dark."""

    def __init__(self, form_values: Callable[[], Mapping[str, t.Any]], project: Project):
        super().__init__()
        self._values = form_values
        self._schemes = pmui.RadioButtonGroup(
            options={"Both": "both", "Light": "light", "Dark": "dark"},
            value="both",
            button_style="contained",
            color="primary",
            size="small",
            margin=0,
        )
        self._html = pn.pane.HTML(
            "", sizing_mode="stretch_width", margin=0, stylesheets=[_PREVIEW_CSS]
        )
        where = Path(project.path).name if project.path else "the project file"
        self._note = pmui.Typography(
            f"Saved to {where} under theme. Pages opened after saving use it; "
            "values set in code win.",
            variant="caption",
            sx={"color": "text.secondary"},
            margin=(12, 0, 0, 0),
        )
        self._schemes.param.watch(lambda _event: self.refresh(), "value")
        self._layout = pmui.Paper(
            pn.Row(
                pmui.Typography("Preview", variant="subtitle1", sx={"fontWeight": 500}, margin=0),
                pn.layout.HSpacer(),
                self._schemes,
                margin=(0, 0, 12, 0),
                sizing_mode="stretch_width",
                styles={"align-items": "center"},
            ),
            self._html,
            self._note,
            variant="outlined",
            margin=(16, 0, 0, 0),
            sizing_mode="stretch_width",
            styles={"flex": "1 1 420px", "min-width": "0", "max-width": "520px"},
            sx={"p": 2},
            css_classes=["flowdash-theme-preview"],
        )
        self.refresh()

    def refresh(self):
        """Draw the preview from the form's current values."""
        values = self._values()
        scheme = self._schemes.value
        self._html.object = preview_html(
            title=values.get("title") or "FlowDash",
            primary=values.get("primary_color") or PRIMARY,
            font=_font_stack(values.get("font_family") or "Roboto"),
            radius=int(values.get("border_radius") or 0),
            schemes=["light", "dark"] if scheme == "both" else [scheme],
        )

    def __panel__(self):
        """Render the preview."""
        return self._layout


# ---------------------------------------------------------------------------
# Settings views and registration
# ---------------------------------------------------------------------------


def general_settings(project: Project, editable: bool) -> SettingsForm:
    """Build the General section of the admin view."""
    return SettingsForm(project, "general", GENERAL_GROUPS, editable=editable, columns=2)


def theme_settings(project: Project, editable: bool) -> SettingsForm:
    """Build the Theme section of the admin view, with its live preview."""
    holder: dict[str, SettingsForm] = {}
    preview = ThemePreview(lambda: holder["form"].values if "form" in holder else {}, project)
    form = SettingsForm(
        project, "theme", THEME_GROUPS, editable=editable, columns=1, aside=preview
    )
    holder["form"] = form
    for key in ("primary_color", "font_family", "border_radius", "title"):
        form._widgets[key].param.watch(lambda _event: preview.refresh(), "value")
    preview.refresh()
    return form


GENERAL = ProjectSection(
    "general",
    settings_loader(GENERAL_GROUPS),
    title="General",
    icon="tune",
    description=(
        "Behaviour and defaults for everyone using this app. Each dashboard's own "
        "settings still override the dashboard defaults."
    ),
    settings_view=general_settings,
    group="App",
)

THEME = ProjectSection(
    "theme",
    settings_loader(THEME_GROUPS),
    title="Theme",
    icon="palette",
    description="Colours, type and branding for every page, in light and dark.",
    settings_view=theme_settings,
    group="App",
)

BUILTIN_SECTIONS = ("general", "theme")

for _section in (GENERAL, THEME):
    register_section(_section)
