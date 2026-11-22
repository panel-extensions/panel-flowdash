"""A settings form for a project file section, as the admin view shows it.

A section's ``settings_view`` may return any Panel component. Returning a
`SettingsForm` instead lets the admin view drive it: the page's Save writes
the form's value with `Project.set`, Discard resets it, the unsaved dot
follows its ``dirty`` and a rejected value's problems are shown at the
fields their ``Problem.keys`` name.
"""

from __future__ import annotations

import contextlib
import contextvars
import html
import logging
import re
import typing as t
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.project import (
    Problem,
    Project,
    ProjectError,
    Section,
    SectionError,
    _parse,
    sections,
)
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger(__name__)

__all__ = ["Group", "Setting", "SettingsForm", "current_app", "settings_loader"]

KINDS = ("switch", "choice", "select", "number", "text", "color", "slider", "numbers", "names")

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_APP: contextvars.ContextVar[FlowDashApp | None] = contextvars.ContextVar(
    "panel_flowdash_admin_app", default=None
)


def current_app() -> FlowDashApp | None:
    """Return the app whose admin view is building a section's settings view, if any."""
    return _APP.get()


@contextlib.contextmanager
def using_app(app: FlowDashApp | None):
    """Make *app* the `current_app` while a settings view is built."""
    token = _APP.set(app)
    try:
        yield
    finally:
        _APP.reset(token)


@dataclass(frozen=True)
class Setting:
    """One setting of a section: a key of the section's mapping and how it is edited.

    ``kind`` picks the control: ``"switch"`` (on or off), ``"choice"`` (a
    row of toggle buttons), ``"select"``, ``"number"``, ``"text"``,
    ``"color"`` (swatches and a hex field), ``"slider"``, ``"names"`` (a
    list of names, e.g. users, with a field to add one) or ``"numbers"``,
    which lays out its ``parts`` (number settings) in a row under the label.
    ``options`` maps labels to values, or is a callable returning that
    mapping when the form is built. ``target`` names the app parameter the
    setting configures, e.g. ``"home_dashboard"`` or
    ``"editor_options.mode"``; one the app was given in code shows locked,
    with the parameter's value turned into the setting's by ``from_target``.
    ``validate(value, section)`` returns why a value the loader reads is
    not acceptable, beyond what ``kind`` checks. ``normalize(name, app)``
    rewrites a name typed into a ``"names"`` setting before it is added.
    """

    key: str
    label: str
    description: str = ""
    kind: str = "switch"
    default: t.Any = None
    options: Mapping[str, t.Any] | Callable[[], Mapping[str, t.Any]] | None = None
    unit: str = ""
    bounds: tuple[float | None, float | None] | None = None
    step: float | None = None
    integer: bool = False
    allow_none: bool = False
    target: str | None = None
    from_target: Callable[[t.Any], t.Any] | None = None
    validate: Callable[[t.Any, Section], str | None] | None = None
    parts: tuple[Setting, ...] = ()
    swatches: tuple[str, ...] = ()
    normalize: Callable[[str, t.Any], str] | None = None

    def __post_init__(self):  # noqa: D105
        if self.kind not in KINDS:
            raise ValueError(f"Setting {self.key!r}: unknown kind {self.kind!r}.")

    def resolved_options(self) -> dict[str, t.Any]:
        """Return the options' labels and values."""
        options = self.options() if callable(self.options) else self.options
        return dict(options or {})

    def leaves(self) -> tuple[Setting, ...]:
        """Return the settings holding a value: the parts of a ``"numbers"`` row, else itself."""
        return self.parts if self.kind == "numbers" else (self,)

    def check(self, value: t.Any) -> str | None:
        """Return why *value* is not valid for this setting, or None."""
        if value is None:
            return None if self.allow_none else f"`{self.key}` needs a value."
        if self.kind == "switch":
            return None if isinstance(value, bool) else f"`{self.key}` is true or false."
        if self.kind in ("choice", "select") and not callable(self.options):
            allowed = list(self.resolved_options().values())
            if value not in allowed:
                names = ", ".join(repr(v) for v in allowed if v is not None)
                return f"`{self.key}` is one of {names}."
            return None
        if self.kind in ("number", "slider"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"`{self.key}` is a number."
            if self.integer and not float(value).is_integer():
                return f"`{self.key}` is a whole number."
            low, high = self.bounds or (None, None)
            if low is not None and value < low:
                return f"`{self.key}` is at least {low:g}."
            if high is not None and value > high:
                return f"`{self.key}` is at most {high:g}."
            return None
        if self.kind == "names":
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item.strip() for item in value
            ):
                return f"`{self.key}` is a list of names."
            return None
        if self.kind == "color":
            if not isinstance(value, str) or not _HEX_COLOR.match(value):
                return f"`{self.key}` is a hex colour such as #0072B5."
            return None
        if not isinstance(value, str):
            return f"`{self.key}` is text."
        return None


@dataclass(frozen=True)
class Group:
    """A titled group of settings, in one of the form's columns."""

    title: str
    settings: Sequence[Setting]
    column: int = 0
    description: str = ""


def _leaves(groups: Sequence[Group]) -> dict[str, Setting]:
    return {leaf.key: leaf for group in groups for s in group.settings for leaf in s.leaves()}


def settings_loader(groups: Sequence[Group]) -> Callable:
    """Return a section loader that checks a mapping of the *groups*' settings.

    The loaded value is a dict of the keys the file sets. Every problem is
    reported at its key, an unknown key where it is written.
    """
    known = _leaves(groups)

    def load(section):
        value = section.value
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise SectionError(
                f"`{section.name}` is a mapping of settings, e.g. `{next(iter(known))}: ...`."
            )
        problems = []
        for key, item in value.items():
            setting = known.get(key)
            if setting is None:
                names = ", ".join(f"`{name}`" for name in known)
                problems.append(
                    section.problem((key,), f"Unknown setting {key!r}; known: {names}.", key=True)
                )
                continue
            message = setting.check(item)
            if message is None and item is not None and setting.validate is not None:
                message = setting.validate(item, section)
            if message:
                problems.append(section.problem((key,), message))
        if problems:
            raise ProjectError(problems)
        return dict(value)

    return load


_ROW_CSS = """
:host { width: 100%; }
.flowdash-setting-text { font-family: inherit; min-width: 0; }
.label { font-size: 1rem; line-height: 1.5; letter-spacing: 0.15px;
  color: var(--mui-palette-text-primary); display: flex; align-items: center;
  flex-wrap: wrap; column-gap: 8px; }
.lock { display: inline-flex; align-items: center; gap: 2px; font-size: 0.75rem;
  color: var(--mui-palette-text-secondary); letter-spacing: 0.4px; }
.lock svg { width: 14px; height: 14px; fill: currentColor; }
.lock code { font-family: inherit; }
.description { font-size: 0.8125rem; line-height: 1.45; letter-spacing: 0.25px;
  color: var(--mui-palette-text-secondary); margin-top: 2px; }
.problem { font-size: 0.8125rem; line-height: 1.45; margin-top: 4px;
  color: var(--mui-palette-error-main, #d32f2f); }
.problem code { font-family: "Roboto Mono", monospace; font-size: 0.75rem; }
"""

_LOCK_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8h-1V6c0-2.76-2.24-5-5-5S7 '
    "3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2m-6 "
    "9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2m3.1-9H8.9V6c0-1.71 1.39-3.1 3.1-3.1s3.1 "
    '1.39 3.1 3.1z"/></svg>'
)

_HEADING_SX = {
    "color": "text.secondary",
    "fontSize": "0.75rem",
    "fontWeight": 500,
    "letterSpacing": "1.2px",
    "textTransform": "uppercase",
}

_ROW_STYLES = {
    "border-bottom": "1px solid var(--mui-palette-divider)",
    "padding": "10px 0",
    "gap": "16px",
    "align-items": "center",
    "width": "100%",
}

# On a phone a row's control goes under its label rather than squeezing it.
_NARROW_ROW_CSS = "@media (max-width: 600px) { :host { flex-wrap: wrap; } }"

_CHOICE_SX = {
    "& .MuiToggleButton-root": {
        "px": 1.5,
        "py": 0.75,
        "fontSize": "0.8125rem",
        "whiteSpace": "nowrap",
    },
}

# Kinds whose control goes under the label rather than beside it.
_STACKED = ("numbers", "color", "names")


def _code_spans(text: str) -> str:
    """Return escaped *text* with its `backticked` spans as code."""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", text)


def current_section(project: Project, name: str) -> t.Any:
    """Return section *name* as the project file has it now, loaded by its loader.

    The file is read again, so what another process wrote since the project
    was loaded counts; without a file, or if it cannot be read, the loaded
    value is returned. None when the section is not in the file.
    """
    path = Path(project.path) if project.path else None
    section = sections().get(name)
    if path is not None and path.is_file() and section is not None:
        try:
            data, _ = _parse(path.read_text(encoding="utf-8"), str(path))
            if name not in data:
                return None
            return section.loader(Section(name, data[name], project))
        except (OSError, UnicodeDecodeError, ProjectError, SectionError, TypeError, ValueError):
            logger.warning("Could not read %s again; using the loaded values.", path)
    return project.get(name)


class SettingsForm(Viewer):
    """The settings of one project file section, laid out in titled groups.

    ``project`` and ``section`` name what is edited. The form starts from
    ``to_form(project.get(section))`` (by default the section's value as a
    mapping) and `save` writes ``from_form(value)`` with ``Project.set``.
    Settings whose ``target`` the app was given in code, and those the
    project context lists under ``locked_settings[section]`` (a mapping of
    key to where it is set), are read-only with that source shown.
    """

    dirty = param.Boolean(default=False, doc="Whether a control differs from the saved value.")

    editable = param.Boolean(default=True, doc="Whether the settings may be changed.")

    problems = param.List(default=[], doc="The problems shown, from the last save or load.")

    def __init__(
        self,
        project: Project,
        section: str,
        groups: Sequence[Group],
        *,
        editable: bool = True,
        columns: int = 1,
        to_form: Callable[[t.Any], Mapping[str, t.Any]] | None = None,
        from_form: Callable[[dict[str, t.Any]], t.Any] | None = None,
        aside: t.Any = None,
        **params,
    ):
        super().__init__(editable=editable, **params)
        self.project = project
        self.section = section
        self._app = current_app()
        self.groups = list(groups)
        self._columns = columns
        self._to_form = to_form or (lambda value: dict(value or {}))
        self._from_form = from_form or (lambda value: value)
        self._settings = {s.key: s for group in self.groups for s in group.settings}
        self._leaves = _leaves(self.groups)
        self._widgets: dict[str, t.Any] = {}
        self._texts: dict[str, pn.pane.HTML] = {}
        self._row_errors: dict[str, str] = {}
        self._locks = self._find_locks()
        self._alert = pmui.Alert(
            object="", alert_type="error", visible=False, sizing_mode="stretch_width"
        )
        self._initial = self._read()
        self._layout = self._build(aside)
        self._sync_dirty()

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    def _read(self) -> dict[str, t.Any]:
        try:
            return dict(self._to_form(self.project.get(self.section)))
        except ProjectError as exc:
            self._pending_problems = exc.problems
            return {}

    def _effective(self, key: str) -> t.Any:
        if key in self._locked_values:
            return self._locked_values[key]
        return self._initial.get(key, self._leaves[key].default)

    @property
    def initial(self) -> dict[str, t.Any]:
        """Return the values the form started from, defaults filled in."""
        return {key: self._effective(key) for key in self._leaves}

    @property
    def values(self) -> dict[str, t.Any]:
        """Return every setting's current value, from the controls."""
        return {key: self._widget_value(key) for key in self._leaves}

    @property
    def locks(self) -> dict[str, str]:
        """Return where each locked setting is set, by key."""
        return dict(self._locks)

    def changes(self) -> dict[str, t.Any]:
        """Return the settings changed in the controls since the form was loaded or saved."""
        return {
            key: self._widget_value(key)
            for key in self._leaves
            if key not in self._locks and self._widget_value(key) != self._effective(key)
        }

    def value(self) -> t.Any:
        """Return the section's value to write: the `changes` over what the file has now.

        The file is read again, so settings another admin saved since this
        form was loaded are kept unless this form changed them too.
        """
        return self._from_form({**self._current(), **self.changes()})

    def _current(self) -> dict[str, t.Any]:
        """Return the section as the project file has it now, falling back to the loaded one."""
        try:
            return dict(self._to_form(current_section(self.project, self.section)))
        except ProjectError:
            return {}

    def _widget_value(self, key: str) -> t.Any:
        leaf = self._leaves[key]
        widget = self._widgets.get(key)
        if widget is None:
            return self._effective(key)
        value = widget.value
        if leaf.kind in ("number", "slider") and value is not None and leaf.integer:
            value = int(value)
        if leaf.kind in ("text", "select") and leaf.allow_none and value == "":
            value = None
        return value

    def _set_widget(self, key: str, value: t.Any):
        leaf = self._leaves[key]
        widget = self._widgets[key]
        if leaf.kind in ("text", "select") and value is None:
            value = ""
        if leaf.kind == "names":
            value = list(value or [])
            widget.options = list(dict.fromkeys([*value, *widget.value]))
        widget.value = value

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def save(self) -> bool:
        """Write the form's value to the project file; show the problems if it is rejected."""
        if self._app is not None and not self._app._still_admin():
            self.show_problems(
                [Problem("You are no longer an admin of this project, so this was not saved.")]
            )
            return False
        try:
            self.project.set(self.section, self.value())
        except ProjectError as exc:
            self.show_problems(exc.problems)
            return False
        except OSError as exc:
            self.show_problems([Problem(f"Could not write the project file: {error_text(exc)}")])
            return False
        self._initial = self._read()
        self.show_problems([])
        with param.parameterized.batch_call_watchers(self):
            for key in self._leaves:
                self._set_widget(key, self._effective(key))
        self._sync_dirty()
        return True

    def reset(self):
        """Put every control back to the saved value and clear the problems."""
        self._initial = self._read()
        for key in self._leaves:
            self._set_widget(key, self._effective(key))
        self.show_problems([])
        self._sync_dirty()

    def show_problems(self, problems: Sequence[Problem]):
        """Show *problems* at the fields their ``keys`` name, the others above the form."""
        self.problems = list(problems)
        by_key: dict[str, list[str]] = {}
        general: list[str] = []
        for problem in problems:
            key = problem.keys[0] if problem.keys else None
            if problem.section in (None, self.section) and key in self._leaves:
                by_key.setdefault(str(key), []).append(problem.message)
            else:
                general.append(problem.message)
        for key in self._leaves:
            messages = by_key.get(key, [])
            widget = self._widgets.get(key)
            if widget is not None and "error_state" in widget.param:
                widget.error_state = bool(messages)
        self._row_errors = {}
        for key, setting in self._settings.items():
            messages = [m for leaf in setting.leaves() for m in by_key.get(leaf.key, [])]
            self._row_errors[key] = " ".join(messages)
            self._render_text(setting)
        self._alert.param.update(object="\n\n".join(general), visible=bool(general))

    # ------------------------------------------------------------------
    # Locks
    # ------------------------------------------------------------------

    def _find_locks(self) -> dict[str, str]:
        locks: dict[str, str] = {}
        self._locked_values: dict[str, t.Any] = {}
        context = getattr(self.project, "context", None) or {}
        extra = (context.get("locked_settings") or {}).get(self.section) or {}
        app = current_app()
        for key, leaf in self._leaves.items():
            if key in extra:
                locks[key] = str(extra[key])
            elif leaf.target and app is not None:
                source = app._code_setting_source(leaf.target)
                if source is not None:
                    locks[key] = source
                    value = app._code_setting_value(leaf.target)
                    try:
                        value = leaf.from_target(value) if leaf.from_target else value
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if leaf.check(value) is None:
                        self._locked_values[key] = value
        return locks

    def lock(self, locks: Mapping[str, str]):
        """Make the settings *locks* names read-only, showing where each is set."""
        self._locks.update(locks)
        for setting in self._settings.values():
            self._sync_disabled(setting)
            self._render_text(setting)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self, aside):
        columns: list[list] = [[] for _ in range(max(self._columns, 1))]
        for group in self.groups:
            column = columns[min(group.column, len(columns) - 1)]
            column.append(self._heading(group))
            column.extend(self._row(setting) for setting in group.settings)
        panes = [
            pn.Column(
                *items,
                margin=0,
                sizing_mode="stretch_width",
                styles={"flex": "1 1 380px", "min-width": "0"},
            )
            for items in columns
        ]
        if aside is not None:
            panes.append(aside)
        body = pmui.FlexBox(
            *panes,
            flex_wrap="wrap",
            sizing_mode="stretch_width",
            margin=0,
            sx={"columnGap": "48px", "rowGap": "16px", "alignItems": "flex-start"},
        )
        problems = getattr(self, "_pending_problems", None)
        if problems:
            self.show_problems(problems)
        return pn.Column(self._alert, body, sizing_mode="stretch_width", margin=0)

    def _heading(self, group: Group):
        return pmui.Typography(
            group.title, variant="overline", sx=_HEADING_SX, margin=(16, 0, 2, 0)
        )

    def _row(self, setting: Setting):
        text = pn.pane.HTML(
            "",
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_ROW_CSS],
            styles={"flex": "1 1 auto", "min-width": "0"},
        )
        self._texts[setting.key] = text
        if setting.kind in _STACKED:
            parts = setting.parts if setting.kind == "numbers" else (setting,)
            inputs = [self._control(part) for part in parts]
            row = pn.Column(
                text,
                pn.Row(*inputs, margin=(8, 0, 0, 0), styles={"gap": "12px", "flex-wrap": "wrap"}),
                margin=0,
                sizing_mode="stretch_width",
                styles={**_ROW_STYLES, "align-items": "stretch", "gap": "0"},
                css_classes=["flowdash-setting", f"flowdash-setting-{setting.key}"],
            )
        else:
            control = self._control(setting)
            row = pn.Row(
                text,
                control,
                margin=0,
                sizing_mode="stretch_width",
                styles=_ROW_STYLES,
                stylesheets=[_NARROW_ROW_CSS],
                css_classes=["flowdash-setting", f"flowdash-setting-{setting.key}"],
            )
        self._render_text(setting)
        self._sync_disabled(setting)
        return row

    def _render_text(self, setting: Setting):
        text = self._texts.get(setting.key)
        if text is None:
            return
        locks = [self._locks[leaf.key] for leaf in setting.leaves() if leaf.key in self._locks]
        lock = (
            f'<span class="lock" title="Set in code">{_LOCK_ICON}{html.escape(locks[0])}</span>'
            if locks
            else ""
        )
        description = (
            f'<div class="description">{html.escape(setting.description)}</div>'
            if setting.description
            else ""
        )
        error = self._row_errors.get(setting.key, "")
        problem = (
            f'<div class="problem" role="alert">{_code_spans(html.escape(error))}</div>'
            if error
            else ""
        )
        text.object = (
            f'<div class="flowdash-setting-text"><div class="label">'
            f"<span>{html.escape(setting.label)}</span>{lock}</div>{description}{problem}</div>"
        )

    def _sync_disabled(self, setting: Setting):
        for leaf in setting.leaves():
            disabled = not self.editable or leaf.key in self._locks
            for widget in self._controls_of(leaf.key):
                widget.disabled = disabled

    def _controls_of(self, key: str) -> list:
        widget = self._widgets.get(key)
        if widget is None:
            return []
        return [widget, *getattr(widget, "_flowdash_parts", [])]

    @param.depends("editable", watch=True)
    def _on_editable(self):
        for setting in self._settings.values():
            self._sync_disabled(setting)

    def _control(self, setting: Setting):
        value = self._effective(setting.key)
        kind = setting.kind
        if kind == "switch":
            widget = pmui.Switch(value=bool(value), margin=(0, 4))
        elif kind == "choice":
            widget = pmui.RadioButtonGroup(
                options=setting.resolved_options(),
                value=value,
                button_style="contained",
                color="primary",
                size="small",
                margin=0,
                sx=_CHOICE_SX,
                styles={"flex": "0 0 auto"},
            )
        elif kind == "select":
            options = setting.resolved_options()
            if value is None and setting.allow_none:
                value = ""
            if value not in options.values():
                options[str(value)] = value
            widget = pmui.Select(
                options=options,
                value=value,
                width=220,
                margin=0,
                label=setting.unit,
                size="small",
            )
        elif kind == "number":
            cls = pmui.IntInput if setting.integer else pmui.FloatInput
            low, high = setting.bounds or (None, None)
            widget = cls(
                value=value,
                label=setting.unit,
                start=low,
                end=high,
                step=setting.step or 1,
                width=120,
                margin=0,
                size="small",
            )
        elif kind == "slider":
            low, high = setting.bounds or (0, 100)
            widget = pmui.IntSlider(
                value=value,
                start=int(low),
                end=int(high),
                step=int(setting.step or 1),
                width=220,
                margin=(0, 8),
            )
        elif kind == "color":
            widget = self._color(setting, value)
        elif kind == "names":
            widget = self._names(setting, value)
        else:
            widget = pmui.TextInput(
                value="" if value is None else str(value),
                placeholder=setting.unit,
                width=240,
                margin=0,
                size="small",
            )
        self._widgets[setting.key] = widget
        widget.param.watch(lambda _event: self._sync_dirty(), "value")
        return getattr(widget, "_flowdash_layout", widget)

    def _color(self, setting: Setting, value: str):
        hex_input = pmui.TextInput(value=value, width=130, margin=(0, 0, 0, 8), size="small")
        swatches = []
        for color in setting.swatches:
            button = pmui.Button(
                label="",
                description=color,
                variant="text",
                margin=(0, 2),
                width=36,
                height=36,
                sx=self._swatch_sx(color, value),
                css_classes=["flowdash-swatch"],
            )
            button.on_click(lambda _event, color=color: setattr(hex_input, "value", color))
            swatches.append((color, button))

        def _sync(_event=None):
            for color, button in swatches:
                button.sx = self._swatch_sx(color, hex_input.value)

        hex_input.param.watch(_sync, "value")
        hex_input._flowdash_parts = [button for _, button in swatches]
        hex_input._flowdash_layout = pn.Row(
            *(button for _, button in swatches),
            hex_input,
            margin=0,
            styles={"align-items": "center", "flex-wrap": "wrap"},
        )
        return hex_input

    def _names(self, setting: Setting, value: list[str] | None):
        names = list(value or [])
        chosen = pmui.MultiChoice(
            options=names, value=names, width=360, margin=0, placeholder="No one yet"
        )
        add = pmui.TextInput(
            placeholder=f"Add {setting.unit or 'a name'}, then Enter",
            width=240,
            margin=(0, 0, 0, 8),
            size="small",
        )

        app = self._app

        def _add(_event):
            name = (add.value_input or add.value or "").strip()
            if name and setting.normalize is not None:
                name = setting.normalize(name, app)
            if name and name not in chosen.value:
                chosen.options = [*chosen.options, name]
                chosen.value = [*chosen.value, name]
            add.value = ""

        def _sync_options(event):
            # A removed name drops out of the choices, so it is not offered back.
            chosen.options = [name for name in chosen.options if name in event.new]

        add.param.watch(_add, "enter_pressed")
        chosen.param.watch(_sync_options, "value")
        chosen._flowdash_parts = [add]
        chosen._flowdash_layout = pn.Row(
            chosen, add, margin=0, styles={"align-items": "center", "flex-wrap": "wrap"}
        )
        return chosen

    @staticmethod
    def _swatch_sx(color: str, selected: str | None) -> dict:
        chosen = (selected or "").lower() == color.lower()
        return {
            "minWidth": 0,
            "p": 0,
            "borderRadius": "50%",
            "bgcolor": color,
            "border": "3px solid",
            "borderColor": "background.paper",
            "boxShadow": f"0 0 0 2px {color}" if chosen else "none",
            "&:hover": {"bgcolor": color, "opacity": 0.85},
        }

    def _sync_dirty(self):
        self.dirty = any(
            key not in self._locks and self._widget_value(key) != self._effective(key)
            for key in self._leaves
        )

    def __panel__(self):
        """Render the form."""
        return self._layout
