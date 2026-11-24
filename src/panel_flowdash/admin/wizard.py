"""The setup wizard: a first admin's walk through the project file's main sections.

It opens right after the admin link claims a project that has no settings
yet, and from Admin, General whenever an admin wants it. Each step edits
one or more project file sections with the same forms and loaders as the
admin view; nothing is written until Review's Finish, which writes every
changed section with `Project.set`. Cancel leaves the file as it was.

An extension adds a step with its section's ``wizard_view(project)``, which
returns a `WizardStep` or a `SettingsForm`. A ``"Data"`` section's step
takes the place of the built-in Data step, which only shows the served
data folder; any other comes after People.
"""

from __future__ import annotations

import copy
import difflib
import logging
import os
import re
import types
import typing as t
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin.access import entry_matches
from panel_flowdash.admin.ai import NOT_ADMIN, _redacted_log
from panel_flowdash.admin.form import Group, SettingsForm, current_app, current_section, using_app
from panel_flowdash.admin.kit import KEY_SVG, KIT_CSS, esc
from panel_flowdash.admin.permissions import POLICY_GROUPS, _clean
from panel_flowdash.admin.sections import (
    GENERAL_SETTINGS,
    THEME_SETTINGS,
    ThemePreview,
    _project_directory,
)
from panel_flowdash.ai import PROVIDERS, KeyCheck, check_key, list_models, provider_name
from panel_flowdash.project import (
    _FILE_LOCK,
    PROJECT_FILE,
    Problem,
    Project,
    ProjectError,
    ProjectSection,
    _dump,
    _parse,
    _plain,
    _read,
    sections,
)
from panel_flowdash.secret_store import (
    SecretError,
    describe_secret,
    encrypt_secret,
    encryption_available,
    env_reference,
    resolve_secret,
)
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

logger = logging.getLogger(__name__)

__all__ = [
    "SETUP_SECTION",
    "SetupWizard",
    "WizardStep",
    "check_section",
    "project_is_new",
    "wizard_steps",
]

# The admin route of the wizard, ``/admin/setup``.
SETUP_SECTION = "setup"

# Below this width the stepper gives way to a compact bar.
NARROW_WIDTH = 900

_HIDE_WHEN_NARROW = (
    f"@media (max-width: {NARROW_WIDTH - 0.05}px) {{ :host {{ display: none !important; }} }}"
)
_HIDE_WHEN_WIDE = (
    f"@media (min-width: {NARROW_WIDTH}px) {{ :host {{ display: none !important; }} }}"
)

DATA_SUFFIXES = (
    ".csv",
    ".tsv",
    ".parquet",
    ".json",
    ".jsonl",
    ".xlsx",
    ".xls",
    ".arrow",
    ".feather",
    ".duckdb",
    ".db",
    ".sqlite",
)

_WIZARD_CSS = """
.found { border: 1px solid var(--mui-palette-divider); border-radius: 4px; }
.found .row { display: flex; gap: 14px; padding: 10px 16px; align-items: flex-start; }
.found .row + .row { border-top: 1px solid var(--mui-palette-divider); }
.found svg { width: 22px; height: 22px; fill: var(--mui-palette-primary-text, var(--mui-palette-primary-main)); flex: 0 0 auto;
  margin-top: 2px; }
.found .what { font-size: 0.9375rem; letter-spacing: 0.15px; color: var(--mui-palette-text-primary); }
.found .detail { font-size: 0.8125rem; color: var(--mui-palette-text-secondary); letter-spacing: 0.2px;
  overflow-wrap: anywhere; }
.files { margin: 0; padding: 0; list-style: none; border: 1px solid var(--mui-palette-divider);
  border-radius: 4px; }
.files li { display: flex; justify-content: space-between; gap: 12px; padding: 8px 16px;
  font-size: 0.875rem; color: var(--mui-palette-text-primary); }
.files li + li { border-top: 1px solid var(--mui-palette-divider); }
.files code, .k code, .folder { font-family: "Roboto Mono", ui-monospace, monospace;
  font-size: 0.8125rem; }
.files .size { color: var(--mui-palette-text-secondary); white-space: nowrap; }
.folder { display: block; padding: 10px 14px; border: 1px dashed var(--mui-palette-divider);
  border-radius: 4px; color: var(--mui-palette-text-primary); overflow-wrap: anywhere; }
"""

_DIFF_CSS = """
:host { min-width: 0; }
.diff { border: 1px solid var(--mui-palette-divider); border-radius: 4px; overflow: hidden;
  background: var(--mui-palette-background-paper); }
.diff .head { display: flex; align-items: baseline; gap: 10px; padding: 10px 16px;
  border-bottom: 1px solid var(--mui-palette-divider); font-size: 0.9375rem; }
.diff .head b { font-family: "Roboto Mono", ui-monospace, monospace; font-weight: 500; }
.diff .head span { font-size: 0.75rem; color: var(--mui-palette-text-secondary); }
.diff pre { margin: 0; padding: 8px 0; font: 0.8125rem/1.55 "Roboto Mono", ui-monospace, monospace;
  overflow-x: auto; color: var(--mui-palette-text-primary); }
.diff pre div { padding: 0 16px; white-space: pre; }
.diff .add { background: rgba(var(--mui-palette-success-mainChannel) / 0.14); }
.diff .del { background: rgba(var(--mui-palette-error-mainChannel) / 0.12);
  color: var(--mui-palette-text-secondary); text-decoration: line-through; }
.diff .add::before { content: "+ "; color: var(--mui-palette-success-main); }
.diff .del::before { content: "- "; color: var(--mui-palette-error-main); }
.diff .ctx::before { content: "  "; }
.diff .empty { padding: 16px; font-size: 0.875rem; color: var(--mui-palette-text-secondary); }
"""

_ICONS = {
    "file": "M14 2H6c-1.1 0-2 .9-2 2v16c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V8zm2 16H8v-2h8zm0-4H8v-2h8zm-3-5V3.5L18.5 9z",
    "key": KEY_SVG,
    "robot": "M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3M7.5 11.5c0-.83.67-1.5 1.5-1.5s1.5.67 1.5 1.5S9.83 13 9 13s-1.5-.67-1.5-1.5M16 17H8v-2h8zm-1-4c-.83 0-1.5-.67-1.5-1.5S14.17 10 15 10s1.5.67 1.5 1.5S15.83 13 15 13",
    "store": "M2 20h20v-4H2zm2-3h2v2H4zM2 4v4h20V4zm4 3H4V5h2zm-4 7h20v-4H2zm2-3h2v2H4z",
}


def _icon(name: str) -> str:
    svg = _ICONS[name]
    if svg.startswith("<svg"):
        return svg
    return f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{svg}"/></svg>'


def _size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _model_id(model: t.Any) -> str:
    if isinstance(model, Mapping):
        return str(model.get("id", ""))
    return str(getattr(model, "id", model))


def _model_label(model: t.Any) -> str:
    if isinstance(model, Mapping):
        return str(model.get("label") or "")
    return str(getattr(model, "label", None) or "")


def given_model(app: FlowDashApp | None) -> tuple[str, str]:
    """Return the id and label of the model the app was started with, or empty ones.

    That is the app's `agent_model` (with its label from `agent_models`
    when listed there), else the first of `agent_models`.
    """
    if app is None:
        return "", ""
    models = list(getattr(app, "agent_models", None) or [])
    default = getattr(app, "agent_model", None)
    if default:
        match = next((m for m in models if _model_id(m) == default), None)
        return default, _model_label(match) if match is not None else ""
    if models:
        return _model_id(models[0]), _model_label(models[0])
    return "", ""


def data_files(directory: Path | None, limit: int = 50) -> list[Path]:
    """Return the data files directly in *directory*, by name."""
    if directory is None or not directory.is_dir():
        return []
    try:
        found = [
            p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in DATA_SUFFIXES
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: p.name.lower())[:limit]


# ---------------------------------------------------------------------------
# Reading and checking sections without writing them
# ---------------------------------------------------------------------------


def _file_data(project: Project) -> dict[str, t.Any]:
    """Return the project file's raw sections as the file has them now."""
    if project.path is not None:
        path = Path(project.path)
        if path.is_file():
            try:
                return _parse(_read(path), project.path)[0]
            except ProjectError:
                return dict(project.data)
        return {}
    return dict(project.data)


def project_is_new(project: Project) -> bool:
    """Whether the project file has no settings yet: missing, empty, or only its admins.

    Claiming the admin link writes ``general.admins``, so a file holding only
    that is still new.
    """
    try:
        data = _file_data(project)
    except OSError:
        return False
    rest = {k: v for k, v in data.items() if k != "general"}
    general = {k: v for k, v in (data.get("general") or {}).items() if k != "admins"}
    return not rest and not general


def section_data(name: str, value: t.Any) -> t.Any:
    """Return *value* as plain YAML data, the way `Project.set` writes section *name*."""
    section = sections().get(name)
    data = section.dump(value) if section is not None and section.dump is not None else value
    return _plain(data)


def check_section(project: Project, name: str, value: t.Any) -> list[Problem]:
    """Return why the section's loader would refuse *value*, without writing anything."""
    section = sections().get(name)
    if section is None:
        return []
    candidate = Project(
        project.path, {**project.data, name: section_data(name, value)}, {}, project.context
    )
    problems: list[Problem] = []
    candidate._load(name, section, problems)
    return problems


# Shown on Review for a pasted key, which is encrypted only when Finish writes it.
# It has the shape of an encrypted value, so the loader checks the rest of the section.
PENDING_SECRET = {"encrypted": "pending", "key_id": "pending"}

_ENCRYPTED = re.compile(r"^(\s*encrypted:\s*).+$")


def _section_yaml(name: str, data: t.Any) -> list[str]:
    if data is None:
        return []
    lines = _dump({name: data}).splitlines()
    # An encrypted key is shown as such, not as the ciphertext.
    return [_ENCRYPTED.sub(r"\1(stored encrypted)", line) for line in lines]


def diff_lines(old: Mapping[str, t.Any], new: Mapping[str, t.Any]) -> list[tuple[str, str]]:
    """Return each changed section's YAML lines as ``(kind, text)``, kind ``add``, ``del`` or ``ctx``."""
    lines: list[tuple[str, str]] = []
    for name, data in new.items():
        before = _section_yaml(name, old.get(name))
        after = _section_yaml(name, data)
        for line in difflib.ndiff(before, after):
            tag, text = line[:2], line[2:]
            if tag == "? ":
                continue
            lines.append(({"+ ": "add", "- ": "del"}.get(tag, "ctx"), text))
    return lines


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class WizardStep(Viewer):
    """A step of the setup wizard.

    `changes` returns the sections to write, by name, each a value for
    `Project.set`; it is asked when Review opens, and Finish writes exactly
    what it returned then. It may raise ``ValueError`` to say why the step
    cannot be written as it is. `summary` returns the lines of the step's
    card on Review.
    """

    title = param.String(default="", doc="The step's label in the stepper.")

    caption = param.String(default="", doc="The line under the label.")

    heading = param.String(default="", doc="The step's page title.")

    description = param.String(default="", doc="The text under the page title.")

    icon = param.String(default="settings", doc="The step's icon on Review.")

    def __init__(self, project: Project, **params):
        super().__init__(**params)
        self.project = project

    def changes(self) -> dict[str, t.Any]:
        """Return the sections this step writes, by name; none by default."""
        return {}

    def summary(self) -> list[str]:
        """Return what the step will do, one line each, for its card on Review."""
        return []

    def confirmations(self) -> list[str]:
        """Return what the admin must confirm on Review before Finish writes this step."""
        return []

    def finalize(self, name: str, value: t.Any) -> t.Any:
        """Return section *name*'s reviewed *value* as Finish writes it; it as is by default.

        A step whose Review shows a placeholder, such as a key to encrypt,
        replaces it here, so nothing is made before the admin finishes.
        """
        return value

    def view(self):
        """Return the step's page below its title."""
        return pn.Column()

    def __panel__(self):  # noqa: D105
        return self.view()


class FormStep(WizardStep):
    """A step made of `SettingsForm` s: each form's changes are written to its section."""

    def __init__(self, project: Project, forms: Sequence[SettingsForm], body=None, **params):
        super().__init__(project, **params)
        self.forms = list(forms)
        self._body = body

    def changes(self) -> dict[str, t.Any]:
        """Return each form's section with its changes over what the file has now."""
        return {form.section: form.value() for form in self.forms if form.changes()}

    def summary(self) -> list[str]:
        """Return each changed setting and its new value."""
        lines = []
        for form in self.forms:
            for key, value in form.changes().items():
                setting = form._leaves[key]
                if isinstance(value, list):
                    shown = ", ".join(map(str, value)) or "none"
                elif setting.kind in ("choice", "select"):
                    labels = {v: k for k, v in setting.resolved_options().items()}
                    shown = labels.get(value, value)
                elif value is None:
                    shown = "none"
                else:
                    shown = value
                lines.append(f"{setting.label}: {shown}")
        return lines

    def view(self):
        """Return the forms, or the body they were given with."""
        if self._body is not None:
            return self._body
        return pn.Column(*self.forms, sizing_mode="stretch_width", margin=0)


class WelcomeStep(WizardStep):
    """What the app found when it started, and where its settings will be saved."""

    def __init__(self, project: Project, app: FlowDashApp | None = None, **params):
        params.setdefault("title", "Welcome")
        params.setdefault("caption", "Project")
        params.setdefault("icon", "folder")
        super().__init__(project, **params)
        title = (app.title if app is not None else None) or "FlowDash"
        self.heading = f"Set up {title}"
        self.description = (
            "A few choices to get this app ready for other people. Every step can be "
            "skipped, nothing is saved until you finish, and everything can be changed "
            "later in Admin."
        )
        self._app = app

    def found(self) -> list[tuple[str, str, str]]:
        """Return what the app found when it started: ``(icon, what, detail)``."""
        found = []
        files = data_files(_project_directory(self.project))
        if files:
            names = ", ".join(p.name for p in files[:4]) + (", ..." if len(files) > 4 else "")
            noun = "file" if len(files) == 1 else "files"
            found.append(("file", f"{len(files)} data {noun} in the project folder", names))
        for provider in PROVIDERS.values():
            if provider.env and os.environ.get(provider.env):
                found.append(
                    (
                        "key",
                        f"{provider.env} in the environment",
                        f"{provider.label} can be used without entering a key",
                    )
                )
        models = list(getattr(self._app, "agent_models", None) or [])
        if models:
            found.append(
                (
                    "robot",
                    f"{len(models)} model{'s' if len(models) != 1 else ''} given on start",
                    "People can pick them; the AI step can add more",
                )
            )
        if self._app is not None:
            try:
                count = len(
                    self._app.store.list_accessible(self._app._identity, default_allow=True)
                )
            except Exception:
                count = None
            if count is not None:
                found.append(
                    (
                        "store",
                        f"{count} dashboard{'s' if count != 1 else ''} so far"
                        if count
                        else "No dashboards yet",
                        type(self._app.store).__name__,
                    )
                )
        return found

    def view(self):
        """Return where settings are saved and what was found."""
        directory = _project_directory(self.project)
        path = Path(self.project.path) if self.project.path else None
        where = str(path) if path else (str(directory / PROJECT_FILE) if directory else None)
        left = pn.Column(
            pmui.Typography("Project folder", variant="overline", margin=(0, 0, 4, 0)),
            pn.pane.HTML(
                f'<span class="folder">{esc(directory or "not saved to a folder")}</span>',
                stylesheets=[_WIZARD_CSS],
                margin=0,
                sizing_mode="stretch_width",
            ),
            pmui.Typography(
                f"Settings are saved in {where}." if where else "Settings stay in memory.",
                variant="body2",
                sx={"color": "text.secondary"},
                margin=(8, 0, 0, 0),
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={"flex": "1 1 360px", "min-width": "0"},
        )
        rows = "".join(
            f'<div class="row">{_icon(icon)}<div><div class="what">{esc(what)}</div>'
            f'<div class="detail">{esc(detail)}</div></div></div>'
            for icon, what, detail in self.found()
        )
        right = pn.Column(
            pmui.Typography("Found when starting", variant="overline", margin=(0, 0, 4, 0)),
            pn.pane.HTML(
                f'<div class="found">{rows}</div>' if rows else "",
                stylesheets=[_WIZARD_CSS],
                margin=0,
                sizing_mode="stretch_width",
                visible=bool(rows),
            ),
            pmui.Typography(
                "Settings given on the command line or in code win and are shown locked.",
                variant="caption",
                sx={"color": "text.secondary"},
                margin=(8, 0, 0, 0),
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={"flex": "1 1 360px", "min-width": "0"},
            css_classes=["flowdash-wizard-found"],
        )
        return pmui.FlexBox(
            left,
            right,
            flex_wrap="wrap",
            sizing_mode="stretch_width",
            margin=0,
            sx={"columnGap": "40px", "rowGap": "24px", "alignItems": "flex-start"},
        )


class DataStep(WizardStep):
    """The data the app serves: the data files in the project folder. It writes nothing."""

    def __init__(self, project: Project, **params):
        params.setdefault("title", "Data")
        params.setdefault("caption", "Data folder")
        params.setdefault("heading", "Your data")
        params.setdefault(
            "description",
            "Components read the files in the project folder. Add files there to make them "
            "available; extensions such as Lumen add their own data sources here.",
        )
        params.setdefault("icon", "storage")
        super().__init__(project, **params)

    def summary(self) -> list[str]:
        """Return how many data files are served."""
        files = data_files(_project_directory(self.project))
        return [f"{len(files)} data file{'s' if len(files) != 1 else ''} in the project folder"]

    def view(self):
        """Return the data folder and its files."""
        directory = _project_directory(self.project)
        files = data_files(directory)
        items = "".join(
            f"<li><code>{esc(p.name)}</code><span class='size'>{_size(p.stat().st_size)}</span></li>"
            for p in files
        )
        listing = (
            f'<ul class="files">{items}</ul>'
            if items
            else '<p class="k">No data files in the folder yet.</p>'
        )
        return pn.Column(
            pmui.Typography("Served data folder", variant="overline", margin=(0, 0, 4, 0)),
            pn.pane.HTML(
                f'<span class="folder">{esc(directory or "none")}</span>',
                stylesheets=[_WIZARD_CSS],
                margin=(0, 0, 16, 0),
                sizing_mode="stretch_width",
            ),
            pn.pane.HTML(
                listing,
                stylesheets=[KIT_CSS, _WIZARD_CSS],
                margin=0,
                sizing_mode="stretch_width",
                css_classes=["flowdash-wizard-files"],
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={"max-width": "720px"},
        )


class AIStep(WizardStep):
    """A model provider, its key (a reference to an environment variable by default) and a model."""

    def __init__(self, project: Project, app: FlowDashApp | None = None, **params):
        params.setdefault("title", "AI")
        params.setdefault("caption", "Provider and model")
        params.setdefault("heading", "Connect an AI provider")
        params.setdefault(
            "description",
            "The assistant builds and edits dashboards and Explore answers questions about the "
            "data. Without a provider both are off and the rest of the app works as usual.",
        )
        params.setdefault("icon", "smart_toy")
        super().__init__(project, **params)
        self._app = app
        try:
            self._ai = copy.deepcopy(dict(current_section(project, "ai") or {}))
        except ProjectError:
            self._ai = {}
        hosted = {p.label: p.name for p in PROVIDERS.values() if not p.local and p.env}
        configured = [n for n in (self._ai.get("providers") or {}) if n in hosted.values()]
        from_env = [n for n in hosted.values() if os.environ.get(PROVIDERS[n].env or "")]
        model, label = given_model(app)
        given = [provider_name(model)] if provider_name(model) in hosted.values() else []
        start = (configured or given or from_env or ["openai"])[0]
        self._provider = pmui.Select(
            options=hosted, value=start, label="Provider", sizing_mode="stretch_width", margin=0
        )
        self._source = pmui.RadioBoxGroup(
            options={"Environment variable": "env", "Paste a key": "stored"},
            value="env",
            inline=True,
            margin=(12, 0, 0, 0),
            visible=encryption_available(),
        )
        self._env = pmui.TextInput(
            label="Environment variable",
            value=PROVIDERS[start].env or "",
            sizing_mode="stretch_width",
            margin=(16, 0, 0, 0),
            css_classes=["flowdash-wizard-env"],
        )
        self._key = pmui.PasswordInput(
            label="API key",
            sizing_mode="stretch_width",
            margin=(16, 0, 0, 0),
            visible=False,
            css_classes=["flowdash-wizard-key"],
        )
        # Panel's debug log records each value a widget receives; never this one's.
        self._key._log = types.MethodType(_redacted_log, self._key)
        self._pending_key: str | None = None
        self._key_note = pn.pane.HTML(
            "", stylesheets=[KIT_CSS], margin=(6, 0, 0, 0), sizing_mode="stretch_width"
        )
        self._test_button = pmui.Button(
            label="Test key", variant="outlined", margin=(12, 0, 0, 0), icon="network_check"
        )
        self._test_button.on_click(lambda _event: pn.state.execute(self.test))
        self._check = pn.pane.HTML(
            "",
            stylesheets=[KIT_CSS],
            margin=(8, 0, 0, 0),
            sizing_mode="stretch_width",
            css_classes=["flowdash-wizard-check"],
        )
        # Free text, suggesting the models the provider lists for the key.
        self._model = pmui.AutocompleteInput(
            label="Model",
            value=model,
            placeholder="e.g. openai:<model>",
            options=[],
            restrict=False,
            search_strategy="includes",
            min_characters=0,
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["flowdash-wizard-model"],
        )
        self._label = pmui.TextInput(
            label="Label", value=label, sizing_mode="stretch_width", margin=(16, 0, 0, 0)
        )
        self._provider.param.watch(self._on_provider, "value")
        self._source.param.watch(self._sync, "value")
        self._env.param.watch(self._sync, ["value", "value_input"])
        self.last_check: KeyCheck | None = None
        self._sync()

    def _on_provider(self, event):
        old, new = PROVIDERS[event.old], PROVIDERS[event.new]
        if (self._env.value or "") in ("", old.env):
            self._env.value = new.env or ""
        self._check.object = ""
        self._sync()
        pn.state.execute(self.suggest_models)

    async def suggest_models(self) -> list[str]:
        """Offer the models the picked provider lists for the key, if it can be read."""
        provider = self.provider
        try:
            key = self._resolved_key()
        except SecretError:
            key = None
        if not key:
            self._model.param.update(options=[], helper_text="")
            return []
        self._model.loading = True
        try:
            found = await list_models(provider, key)
        finally:
            self._model.loading = False
        if provider != self.provider:
            return []
        label = PROVIDERS[provider].label
        self._model.param.update(
            options=list(found.ids),
            helper_text=f"Models listed by {label}." if found.ids else found.message,
        )
        return list(found.ids)

    def _variable(self) -> str:
        return (self._env.value_input or self._env.value or "").strip()

    def _sync(self, *_events):
        stored = self._source.value == "stored"
        self._env.visible = not stored
        self._key.visible = stored
        if stored:
            self._key_note.object = (
                '<div class="k"><span class="muted small">Stored encrypted in the project file '
                "when you finish, never shown again.</span></div>"
            )
            return
        variable = self._variable()
        try:
            info = describe_secret(env_reference(variable))
        except SecretError as exc:
            self._key_note.object = f'<div class="k error-text small">{esc(exc)}</div>'
            return
        if info.usable:
            ends = f", ends in {esc(info.last4)}" if info.last4 else ""
            text = f"<code>{esc(variable)}</code> is set{ends}. The file keeps the reference only."
        else:
            text = (
                f"<code>{esc(variable)}</code> is not set in this server's environment. Set it "
                "before starting the server; the file keeps the reference only."
            )
        self._key_note.object = f'<div class="k"><span class="muted small">{text}</span></div>'

    @property
    def provider(self) -> str:
        """Return the provider picked."""
        return self._provider.value

    def _api_key(self) -> t.Any:
        """Return the project file value of the key: a reference, or the typed key encrypted."""
        if self._source.value == "stored":
            typed = self._key.value_input or self._key.value or ""
            if not typed:
                existing = (self._ai.get("providers") or {}).get(self.provider) or {}
                if existing.get("api_key") is not None:
                    return existing["api_key"]
                raise ValueError("Paste the key, or use an environment variable.")
            self._pending_key = typed
            return dict(PENDING_SECRET)
        return env_reference(self._variable())

    def finalize(self, name: str, value: t.Any) -> t.Any:
        """Encrypt the key pasted when Review opened, in place of its placeholder."""
        providers = (value or {}).get("providers") if name == "ai" else None
        config = (providers or {}).get(self.provider) or {}
        if config.get("api_key") != PENDING_SECRET:
            return value
        if not self._pending_key:
            raise ValueError("Paste the key again, or use an environment variable.")
        value = copy.deepcopy(value)
        value["providers"][self.provider]["api_key"] = encrypt_secret(self._pending_key)
        self._pending_key = None
        return value

    def _resolved_key(self) -> str | None:
        if self._source.value == "stored":
            return (self._key.value_input or self._key.value or "") or None
        return resolve_secret(env_reference(self._variable()))

    async def test(self) -> KeyCheck:
        """Ask the provider whether the key works, without saving it."""
        if self._app is not None and not self._app._still_admin():
            result = KeyCheck(False, NOT_ADMIN)
            self.last_check = result
            self._check.object = (
                f'<div class="k"><span class="chip error">Not tested</span> {esc(NOT_ADMIN)}</div>'
            )
            return result
        try:
            key = self._resolved_key()
        except SecretError as exc:
            result = KeyCheck(False, str(exc))
        else:
            result = await check_key(self.provider, key)
        self.last_check = result
        tone = "success" if result.ok else "error"
        self._check.object = (
            f'<div class="k"><span class="chip {tone}">'
            f"{'Connected' if result.ok else 'Not connected'}</span> {esc(result.message)}</div>"
        )
        if result.ok:
            await self.suggest_models()
        return result

    def changes(self) -> dict[str, t.Any]:
        """Return the ``ai`` section with the provider's key and the model added."""
        try:
            api_key = self._api_key()
        except SecretError as exc:
            raise ValueError(str(exc)) from None
        ai = copy.deepcopy(self._current())
        providers = dict(ai.get("providers") or {})
        providers[self.provider] = {**(providers.get(self.provider) or {}), "api_key": api_key}
        ai["providers"] = providers
        model = (self._model.value_input or self._model.value or "").strip()
        if model:
            label = (self._label.value_input or self._label.value or "").strip()
            models = [dict(m) for m in ai.get("models") or []]
            if not any(m.get("id") == model for m in models):
                models.append({"id": model, **({"label": label} if label else {})})
            ai["models"] = models
            if not ai.get("default"):
                ai["default"] = model
        return {"ai": {k: v for k, v in ai.items() if v not in (None, {}, [])}}

    def _current(self) -> dict[str, t.Any]:
        try:
            return dict(current_section(self.project, "ai") or {})
        except ProjectError:
            return dict(self._ai)

    def summary(self) -> list[str]:
        """Return the provider, where its key comes from and the model."""
        label = PROVIDERS[self.provider].label
        if self._source.value == "stored":
            source = "a key stored encrypted"
        else:
            source = f"${{{self._variable()}}}"
        lines = [f"Provider: {label}, key from {source}"]
        model = (self._model.value_input or self._model.value or "").strip()
        if model:
            lines.append(f"Model: {model}")
        if self.last_check is not None and not self.last_check.ok:
            lines.append(f"Test: {self.last_check.message}")
        return lines

    def view(self):
        """Return the provider and key on the left, the model on the right."""
        pn.state.execute(self.suggest_models)
        key = pmui.Paper(
            self._provider,
            self._source,
            self._env,
            self._key,
            self._key_note,
            pn.Row(self._test_button, margin=0),
            self._check,
            variant="outlined",
            margin=0,
            sizing_mode="stretch_width",
            sx={"p": 2.5},
            styles={"flex": "1 1 360px", "min-width": "0", "max-width": "520px"},
            css_classes=["flowdash-wizard-provider"],
        )
        models = list(getattr(self._app, "agent_models", None) or [])
        given = (
            [
                pmui.Typography(
                    "Given on start: " + ", ".join(_model_id(m) for m in models),
                    variant="caption",
                    sx={"color": "text.secondary"},
                    margin=(12, 0, 0, 0),
                )
            ]
            if models
            else []
        )
        model = pn.Column(
            pmui.Typography("Model people can pick", variant="overline", margin=(0, 0, 8, 0)),
            self._model,
            self._label,
            pmui.Typography(
                "Leave the model empty to add only the key. More models, limits and who may use "
                "them are in Admin, AI.",
                variant="caption",
                sx={"color": "text.secondary"},
                margin=(12, 0, 0, 0),
            ),
            *given,
            margin=0,
            sizing_mode="stretch_width",
            styles={"flex": "1 1 320px", "min-width": "0"},
        )
        return pmui.FlexBox(
            key,
            model,
            flex_wrap="wrap",
            sizing_mode="stretch_width",
            margin=0,
            sx={"columnGap": "32px", "rowGap": "24px", "alignItems": "flex-start"},
        )


def theme_step(project: Project) -> FormStep:
    """Return the Theme step: colour scheme, primary colour, title and logo, with the preview."""
    groups = (
        Group("Colours", (THEME_SETTINGS["color_scheme"], THEME_SETTINGS["primary_color"])),
        Group("Branding", (THEME_SETTINGS["title"], THEME_SETTINGS["logo"])),
    )
    holder: dict[str, SettingsForm] = {}
    preview = ThemePreview(lambda: holder["form"].values if "form" in holder else {}, project)
    preview._note.object = "Applied when you finish setup; values set in code win."
    form = SettingsForm(project, "theme", groups, aside=preview)
    holder["form"] = form
    for key in ("primary_color", "title"):
        form._widgets[key].param.watch(lambda _event: preview.refresh(), "value")
    preview.refresh()
    return FormStep(
        project,
        [form],
        title="Theme",
        caption="Colour and branding",
        heading="Make it yours",
        description="Colours, the title and a logo, for every page in light and dark.",
        icon="palette",
    )


class PeopleStep(FormStep):
    """Admins and the access policy; removing yourself as admin needs confirming."""

    def __init__(self, project: Project, forms, app: FlowDashApp | None = None, **params):
        super().__init__(project, forms, **params)
        self._app = app

    def removes_self(self) -> bool:
        """Whether the changes leave the admin running the wizard without admin."""
        identity = getattr(self._app, "_identity", None)
        if identity is None:
            return False
        admins, policy = self.forms
        if not any(entry_matches(e, identity) for e in admins.initial.get("admins", [])):
            return False
        if any(entry_matches(e, identity) for e in admins.values.get("admins", [])):
            return False
        code_groups = set(self._app._admin_groups()) if self._app is not None else set()
        groups = code_groups | set(policy.values.get("admin_groups") or [])
        return not identity.in_groups(groups)

    def confirmations(self) -> list[str]:
        """Ask to confirm when the admin removes themselves from the admins."""
        if not self.removes_self():
            return []
        user = self._app._identity.user
        return [
            f"You removed yourself ({user}) from the admins. After Finish you can no longer "
            "open Admin unless another admin adds you again."
        ]


def people_step(project: Project, app: FlowDashApp | None = None) -> FormStep:
    """Return the People step: admins, admin groups and what an unshared dashboard allows."""
    admins = SettingsForm(
        project, "general", (Group("Administrators", (GENERAL_SETTINGS["admins"],)),)
    )
    policy = SettingsForm(project, "permissions", POLICY_GROUPS, from_form=_clean)
    parts: list = []
    identity = getattr(app, "_identity", None)
    if identity is not None and identity.oauth_user is None:
        parts.append(
            pmui.Alert(
                object=(
                    "Without sign-in everyone who opens the app is the account that runs the "
                    "server, so admins are local accounts (local:<name>) and have admin rights "
                    "only in a browser on this machine. Deployed apps use OAuth and admin groups."
                ),
                severity="info",
                margin=(0, 0, 8, 0),
                sizing_mode="stretch_width",
            )
        )
    body = pn.Column(
        *parts,
        admins,
        policy,
        sizing_mode="stretch_width",
        margin=0,
        styles={"max-width": "760px"},
    )
    return PeopleStep(
        project,
        [admins, policy],
        app=app,
        body=body,
        title="People",
        caption="Permissions",
        heading="Who can do what",
        description="Who administers the app and what a dashboard nobody has shared allows.",
        icon="group",
    )


class _BrokenStep(WizardStep):
    def __init__(self, project: Project, message: str, **params):
        super().__init__(project, **params)
        self._message = message

    def summary(self) -> list[str]:
        return ["Could not be shown, so nothing is written"]

    def view(self):
        return pmui.Alert(object=self._message, severity="error", sizing_mode="stretch_width")


def _section_step(project: Project, section: ProjectSection) -> WizardStep:
    try:
        view = section.wizard_view(project)
    except Exception as exc:
        logger.exception("Could not build the setup step of section %r", section.name)
        return _BrokenStep(
            project,
            f"The {section.label} step could not be shown: {error_text(exc)}",
            title=section.label,
            heading=section.label,
        )
    if isinstance(view, WizardStep):
        view.title = view.title or section.label
        view.heading = view.heading or section.label
        view.description = view.description or section.description
        view.icon = view.icon if view.icon != "settings" else (section.icon or "settings")
        return view
    if isinstance(view, SettingsForm):
        return FormStep(
            project,
            [view],
            title=section.label,
            heading=section.label,
            description=section.description,
            icon=section.icon or "settings",
        )
    return _BrokenStep(
        project,
        f"The {section.label} step is a {type(view).__name__}, not a WizardStep or SettingsForm.",
        title=section.label,
        heading=section.label,
    )


def wizard_steps(project: Project, app: FlowDashApp | None = None) -> list[WizardStep]:
    """Return the wizard's steps before Review, extension steps included."""
    extra = [s for s in sections().values() if s.wizard_view is not None]
    data = [_section_step(project, s) for s in extra if s.group == "Data"]
    others = [_section_step(project, s) for s in extra if s.group != "Data"]
    return [
        WelcomeStep(project, app),
        *(data or [DataStep(project)]),
        AIStep(project, app),
        theme_step(project),
        people_step(project, app),
        *others,
    ]


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


class SetupWizard(Viewer):
    """The setup wizard: its steps, then Review, which writes them with Finish.

    *on_close(finished)* is called when Finish wrote the reviewed sections
    (True) or the wizard was cancelled (False).
    """

    step = param.Integer(default=0, doc="The index of the step shown; the last is Review.")

    def __init__(
        self,
        app: FlowDashApp | None,
        project: Project,
        *,
        on_close: Callable[[bool], None] | None = None,
        **params,
    ):
        super().__init__(**params)
        self._app = app
        self.project = project
        self._on_close = on_close
        with using_app(app if app is not None else current_app()):
            self.steps = wizard_steps(project, app)
        self.skipped: set[int] = set()
        self.plan: dict[str, t.Any] = {}
        self.problems: list[str] = []
        self.written: list[str] = []
        self._build()
        self.show(0)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self):
        title = (getattr(self._app, "title", None) or "FlowDash") if self._app else "FlowDash"
        self._stepper = pmui.StepperMenu(
            items=self._items(),
            active=0,
            non_linear=True,
            sizing_mode="stretch_width",
            margin=(0, 0, 0, 0),
            stylesheets=[_HIDE_WHEN_NARROW],
            css_classes=["flowdash-wizard-stepper"],
        )
        self._compact = pmui.Typography(
            "",
            variant="body2",
            sx={"color": "text.secondary", "fontWeight": 500},
            margin=0,
            stylesheets=[_HIDE_WHEN_WIDE],
            css_classes=["flowdash-wizard-compact"],
        )
        self._stepper.param.watch(self._on_stepper, "active")
        self._heading = pmui.Typography(
            "",
            variant="h5",
            margin=0,
            sx={"fontWeight": 400, "fontSize": "1.625rem", "lineHeight": 1.3},
        )
        self._description = pmui.Typography(
            "",
            variant="body1",
            margin=(6, 0, 20, 0),
            sx={"color": "text.secondary", "maxWidth": "760px"},
        )
        self._alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width"
        )
        self._body = pn.Column(sizing_mode="stretch_width", margin=0)
        self._confirm = pmui.Checkbox(
            label="I understand, finish anyway",
            value=False,
            margin=(0, 0, 16, 0),
            css_classes=["flowdash-wizard-confirm"],
        )
        self._confirm_alert = pmui.Alert(
            object="", severity="warning", visible=False, sizing_mode="stretch_width"
        )
        self._back = pmui.Button(
            label="Back", icon="arrow_back", variant="outlined", color="default", margin=0
        )
        self._cancel = pmui.Button(label="Cancel setup", variant="text", color="default", margin=0)
        self._note = pmui.Typography(
            "", variant="body2", sx={"color": "text.secondary"}, margin=(0, 4, 0, 0)
        )
        self._skip = pmui.Button(label="Skip", variant="text", color="default", margin=0)
        self._next = pmui.Button(
            label="Continue", end_icon="arrow_forward", variant="contained", margin=0
        )
        self._back.on_click(lambda _event: self.back())
        self._cancel.on_click(lambda _event: self.cancel())
        self._skip.on_click(lambda _event: self.skip())
        self._next.on_click(lambda _event: self.next())
        inner = {
            "max-width": "1100px",
            "width": "100%",
            "margin-left": "auto",
            "margin-right": "auto",
        }
        header = pn.Column(
            pn.Row(
                pmui.Typography(title, variant="h6", margin=0, sx={"fontWeight": 500}),
                pmui.Typography(
                    "Set up",
                    variant="h6",
                    margin=0,
                    sx={"color": "text.secondary", "fontWeight": 400},
                ),
                margin=(0, 0, 12, 0),
                styles={"gap": "16px", "align-items": "baseline", **inner},
            ),
            pn.Column(self._stepper, self._compact, margin=0, styles=inner),
            sizing_mode="stretch_width",
            margin=0,
            styles={
                "padding": "16px 24px",
                "border-bottom": "1px solid var(--mui-palette-divider)",
            },
        )
        content = pn.Column(
            pn.Column(
                self._heading,
                self._description,
                self._alert,
                self._confirm_alert,
                self._confirm,
                self._body,
                margin=0,
                sizing_mode="stretch_width",
                styles=inner,
            ),
            sizing_mode="stretch_width",
            margin=0,
            styles={"padding": "24px 24px 32px 24px", "flex": "1 1 auto"},
            css_classes=["flowdash-wizard-content"],
        )
        footer = pn.Row(
            self._back,
            self._cancel,
            pn.layout.HSpacer(),
            self._note,
            self._skip,
            self._next,
            sizing_mode="stretch_width",
            margin=0,
            styles={
                "padding": "12px 24px",
                "border-top": "1px solid var(--mui-palette-divider)",
                "align-items": "center",
                "gap": "8px",
                "flex-wrap": "wrap",
            },
            css_classes=["flowdash-wizard-footer"],
        )
        self._layout = pn.Column(
            header,
            content,
            footer,
            sizing_mode="stretch_width",
            margin=0,
            # Grows with its step, so the page scrolls, and fills the window when shorter.
            styles={"min-height": "calc(100vh - 61px)"},
            css_classes=["flowdash-wizard"],
        )

    def _items(self) -> list[dict[str, t.Any]]:
        items = []
        for i, step in enumerate(self.steps):
            item = {"label": step.title, "optional": False}
            if i in self.skipped:
                item["completed"] = False
            elif i < self.step:
                item["completed"] = True
            items.append(item)
        items.append({"label": "Review"})
        return items

    @property
    def review_index(self) -> int:
        """Return the index of Review, the last step."""
        return len(self.steps)

    @property
    def titles(self) -> list[str]:
        """Return the steps' labels, Review included."""
        return [step.title for step in self.steps] + ["Review"]

    # ------------------------------------------------------------------
    # Moving between steps
    # ------------------------------------------------------------------

    def _on_stepper(self, event):
        if event.new is not None and event.new != self.step:
            self.show(event.new)

    def show(self, index: int):
        """Show step *index*; Review when it is the last."""
        index = max(0, min(index, self.review_index))
        with pn.io.hold():
            self.step = index
            self._alert.visible = False
            self._confirm_alert.visible = False
            self._confirm.visible = False
            if index == self.review_index:
                self._show_review()
            else:
                step = self.steps[index]
                self._heading.object = step.heading or step.title
                self._description.object = step.description
                self._description.visible = bool(step.description)
                self._body[:] = [step]
            self._sync_footer()
            self._stepper.items = self._items()
            if self._stepper.active != index:
                self._stepper.active = index
            self._compact.object = (
                f"Step {index + 1} of {self.review_index + 1}: {self.titles[index]}"
            )

    def _sync_footer(self):
        index = self.step
        review = index == self.review_index
        self._back.visible = index > 0
        self._cancel.visible = index > 0
        self._skip.visible = not review
        self._skip.label = "Skip setup" if index == 0 else "Skip this step"
        title = getattr(self._app, "title", None) or "FlowDash"
        self._next.label = (
            "Start" if index == 0 else (f"Finish and open {title}" if review else "Continue")
        )
        self._next.end_icon = "check" if review else "arrow_forward"
        if review:
            count = len(self.plan)
            self._note.object = (
                "Nothing to save"
                if not count
                else f"Writes {count} section{'s' if count != 1 else ''} of {PROJECT_FILE}"
            )
        else:
            self._note.object = ""
        self._note.visible = review

    def next(self):
        """Go on to the next step, or finish on Review."""
        if self.step == self.review_index:
            return self.finish()
        self.skipped.discard(self.step)
        self.show(self.step + 1)
        return None

    def back(self):
        """Go back a step."""
        self.show(self.step - 1)

    def skip(self):
        """Leave this step's settings out and go on; on Welcome, leave the wizard."""
        if self.step == 0:
            self.cancel()
            return
        self.skipped.add(self.step)
        self.show(self.step + 1)

    def cancel(self):
        """Leave the wizard without writing anything."""
        if self._on_close is not None:
            self._on_close(False)

    # ------------------------------------------------------------------
    # Review and Finish
    # ------------------------------------------------------------------

    def review(self) -> dict[str, t.Any]:
        """Collect what each step not skipped writes, by section, and check it.

        The result is what Finish writes. Sections the file already has as
        they would be written are left out.
        """
        plan: dict[str, t.Any] = {}
        problems: list[str] = []
        confirmations: list[str] = []
        self._step_lines: dict[int, list[str]] = {}
        self._writers: dict[str, WizardStep] = {}
        for i, step in enumerate(self.steps):
            if i in self.skipped:
                continue
            try:
                changes = step.changes()
                self._step_lines[i] = step.summary()
                confirmations.extend(step.confirmations())
            except (ValueError, TypeError) as exc:
                problems.append(f"{step.title}: {error_text(exc)}")
                continue
            for name, value in changes.items():
                plan[name] = value
                self._writers[name] = step
        current = _file_data(self.project)
        plan = {
            name: value
            for name, value in plan.items()
            if section_data(name, value) != current.get(name)
        }
        for name, value in plan.items():
            problems.extend(
                f"{sections()[name].label if name in sections() else name}: {p.message}"
                for p in check_section(self.project, name, value)
            )
        self.plan = plan
        self.problems = problems
        self.confirmations = confirmations
        self._current = current
        return plan

    def diff(self) -> list[tuple[str, str]]:
        """Return the reviewed sections' YAML lines, old and new, as ``(kind, text)``."""
        return diff_lines(
            getattr(self, "_current", {}),
            {name: section_data(name, value) for name, value in self.plan.items()},
        )

    def _card(self, i: int, step: WizardStep):
        skipped = i in self.skipped
        lines = (
            ["Skipped, nothing is written"]
            if skipped
            else self._step_lines.get(i) or ["No changes"]
        )
        edit = pmui.Button(label="Edit", variant="text", size="small", margin=0)
        edit.on_click(lambda _event, i=i: self.show(i))
        return pmui.Paper(
            pn.Row(
                pmui.Typography(step.title, variant="subtitle1", sx={"fontWeight": 500}, margin=0),
                pn.layout.HSpacer(),
                edit,
                margin=0,
                sizing_mode="stretch_width",
                styles={"align-items": "center"},
            ),
            *[
                pmui.Typography(
                    line,
                    variant="body2",
                    sx={"color": "text.secondary", "overflowWrap": "anywhere"},
                    margin=(4, 0, 0, 0),
                )
                for line in lines
            ],
            variant="outlined",
            margin=0,
            sx={"p": 2},
            styles={"flex": "1 1 220px", "min-width": "0"},
            css_classes=["flowdash-wizard-card", f"flowdash-wizard-card-{i}"],
        )

    def _show_review(self):
        self.review()
        self._heading.object = "Review and finish"
        self._description.object = (
            "Nothing is saved until you finish. Everything here can be changed later in Admin."
        )
        self._description.visible = True
        if self.problems:
            self._alert.param.update(
                object="Fix these before finishing:\n\n"
                + "\n".join(f"- {p}" for p in self.problems),
                visible=True,
            )
        self._confirm.value = False
        if self.confirmations:
            self._confirm_alert.param.update(object="\n\n".join(self.confirmations), visible=True)
            self._confirm.visible = True
        lines = self.diff()
        where = Path(self.project.path).name if self.project.path else PROJECT_FILE
        body = (
            "".join(f'<div class="{kind}">{esc(text) or " "}</div>' for kind, text in lines)
            if lines
            else ""
        )
        diff = pn.pane.HTML(
            '<div class="diff"><div class="head"><b>'
            f"{esc(where)}</b><span>what will be written</span></div>"
            + (f"<pre>{body}</pre>" if body else '<div class="empty">No changes to write.</div>')
            + "</div>",
            stylesheets=[_DIFF_CSS],
            margin=0,
            sizing_mode="stretch_width",
            styles={"flex": "1 1 420px", "min-width": "0"},
            css_classes=["flowdash-wizard-diff"],
        )
        cards = pmui.FlexBox(
            *[
                self._card(i, step)
                for i, step in enumerate(self.steps)
                if not isinstance(step, WelcomeStep)
            ],
            flex_wrap="wrap",
            margin=0,
            sizing_mode="stretch_width",
            sx={"gap": "12px", "alignItems": "stretch"},
            styles={"flex": "1 1 420px", "min-width": "0"},
        )
        self._body[:] = [
            pmui.FlexBox(
                cards,
                diff,
                flex_wrap="wrap",
                margin=0,
                sizing_mode="stretch_width",
                sx={"columnGap": "24px", "rowGap": "24px", "alignItems": "flex-start"},
            )
        ]

    def finish(self) -> bool:
        """Write the reviewed sections with `Project.set`; False, writing nothing, on a problem."""
        if self.step != self.review_index:
            return False
        if self.problems:
            self._alert.visible = True
            return False
        if self._app is not None and not self._app._still_admin():
            self._alert.param.update(
                object="You are no longer an admin of this project, so setup was not saved.",
                visible=True,
            )
            return False
        if getattr(self, "confirmations", None) and not self._confirm.value:
            self._confirm_alert.visible = True
            return False
        written: list[str] = []
        try:
            with _FILE_LOCK:
                if _file_data(self.project) != self._current:
                    # Another admin saved since Review opened: their change must not be lost.
                    self.show(self.review_index)
                    self._alert.param.update(
                        object="The project file changed since Review opened. Review the "
                        "changes again, then finish.",
                        visible=True,
                    )
                    return False
                plan = {
                    name: self._writers[name].finalize(name, value)
                    if name in self._writers
                    else value
                    for name, value in self.plan.items()
                }
                for name, value in plan.items():
                    self.project.set(name, value)
                    written.append(name)
        except ProjectError as exc:
            self._fail([p.message for p in exc.problems], written)
            return False
        except ValueError as exc:
            self._fail([error_text(exc)], written)
            return False
        except OSError as exc:
            self._fail([f"Could not write the project file: {error_text(exc)}"], written)
            return False
        user = getattr(getattr(self._app, "_identity", None), "user", None) or "someone"
        logger.info("Admin: %s finished setup, writing %s.", user, ", ".join(written) or "nothing")
        self.written = written
        if self._on_close is not None:
            self._on_close(True)
        return True

    def _fail(self, messages: list[str], written: list[str]):
        saved = f" Saved so far: {', '.join(written)}." if written else ""
        self._alert.param.update(
            object="Setup could not be saved: " + " ".join(messages) + saved, visible=True
        )

    def __panel__(self):
        """Render the wizard."""
        return self._layout
