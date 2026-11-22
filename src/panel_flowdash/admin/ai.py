"""The AI section of the admin view: model providers and their keys, and the models people may pick.

Each change is written to the project file's ``ai`` section as it is made,
and sessions started afterwards offer the models as it says. A key is
write-only: the page shows where it comes from and its last four
characters, never the key, and a key typed into the Replace key dialog is
cleared from the field as soon as it is saved or the dialog closes.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import types
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin.form import current_app, current_section
from panel_flowdash.admin.kit import (
    KEY_SVG,
    LINK_SVG,
    LOCK_SVG,
    Table,
    caption,
    cell,
    chip,
    clear_text,
    esc,
    heading,
)
from panel_flowdash.ai import (
    KEY_CHECKS,
    MODEL_SETTINGS,
    OLLAMA_URL,
    PROVIDERS,
    SURFACES,
    KeyCheck,
    ModelEntry,
    check_key,
    check_model_settings,
    key_status,
    ledger_for,
    load_ai,
    model_entries,
    provider_config,
    provider_key,
    provider_name,
)
from panel_flowdash.project import (
    _FILE_LOCK,
    Problem,
    Project,
    ProjectError,
    ProjectSection,
    register_section,
)
from panel_flowdash.secret_store import (
    INSTALL_HINT,
    SecretError,
    encrypt_secret,
    encryption_available,
    env_reference,
    resolve_secret,
)
from panel_flowdash.value_text import error_text

logger = logging.getLogger(__name__)

NOT_ADMIN = "You are no longer an admin of this project, so this was not done."

_AVATAR_COLORS = {
    "openai": "#10a37f",
    "anthropic": "#d97757",
    "openrouter": "#6467f2",
    "google-gla": "#4285f4",
    "groq": "#f55036",
    "mistral": "#fa520f",
    "ollama": "#455a64",
}

_CARD_CSS = """
.k .card-head { display: flex; align-items: flex-start; gap: 12px; }
.k .avatar { width: 36px; height: 36px; border-radius: 8px; color: #fff; font-weight: 500;
  display: flex; align-items: center; justify-content: center; font-size: 1rem; flex: 0 0 auto; }
.k .who { flex: 1 1 auto; min-width: 0; }
.k .who b { font-weight: 500; font-size: 1rem; display: block; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.k .who .line { display: flex; align-items: center; flex-wrap: wrap; column-gap: 8px; row-gap: 2px; }
.k .who .small { white-space: nowrap; }
.k .who .chip { margin: 0; }
.k .detail { display: flex; align-items: center; flex-wrap: wrap; column-gap: 6px; row-gap: 2px;
  margin-top: 12px; color: var(--mui-palette-text-secondary); min-width: 0; }
.k .detail code { overflow-wrap: anywhere; }
.k .detail .ends { white-space: nowrap; }
.k .detail code { color: var(--mui-palette-text-primary); }
"""

_STATUS_CSS = """
:host { align-self: flex-end; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 0.8125rem;
  letter-spacing: 0.25px; color: var(--mui-palette-text-secondary); white-space: nowrap; }
.status svg { width: 16px; height: 16px; fill: var(--mui-palette-success-main, #2e7d32); }
"""

_SAVED_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 '
    '10 10 10-4.48 10-10S17.52 2 12 2m-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8z"/></svg>'
)


def _initial(name: str) -> str:
    return "R" if name == "openrouter" else PROVIDERS[name].label[0]


def _dollars(amount: float) -> str:
    return f"${amount:,.2f}"


def _ago(when: dt.datetime) -> str:
    seconds = (dt.datetime.now(dt.UTC) - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return when.strftime("%-d %b")


def settings_summary(settings: t.Mapping[str, t.Any]) -> str:
    """Return a model's settings in a few words, e.g. ``temperature 0 · max tokens 1,000``."""
    parts = []
    for key, value in settings.items():
        label = MODEL_SETTINGS.get(key, (key,))[0].lower()
        if key == "max_tokens":
            parts.append(f"{label} {int(value):,}")
        elif key == "timeout":
            parts.append(f"{label} {value:g} s")
        else:
            parts.append(f"{label} {value:g}")
    return " · ".join(parts) or "default"


def offered_summary(entry: ModelEntry) -> str:
    """Return who a model is offered to, e.g. ``Assistant, Explore`` or ``Assistant · analysts``."""
    if not entry.offered_to:
        return "Off"
    text = ", ".join(SURFACES[s] for s in entry.offered_to)
    return f"{text} · {', '.join(entry.groups)}" if entry.groups else text


def _text(widget) -> str:
    """Return what a text field holds, including typing the browser has not committed."""
    value = widget.value_input if widget.value_input is not None else widget.value
    return (value or "").strip()


def _number(text: str, label: str, integer: bool) -> tuple[float | int | None, str | None]:
    """Return *text* as a number (None when empty), or why it is not one."""
    if not text:
        return None, None
    try:
        value = float(text.replace(",", "").lstrip("$"))
    except ValueError:
        return None, f"{label} is a number."
    if integer:
        if not value.is_integer():
            return None, f"{label} is a whole number."
        return int(value), None
    return value, None


def _redacted_log(widget, msg: str, *args, level: str = "debug") -> None:
    type(widget)._log(widget, msg, *("<redacted>" for _ in args), level=level)


class ProviderStatus(t.NamedTuple):
    """How a provider's card and its models' rows describe it."""

    source: str
    chip: str
    tone: str
    detail: str
    usable: bool
    rejected: bool


def provider_status(ai: t.Mapping[str, t.Any], name: str) -> ProviderStatus:
    """Return where provider *name*'s key comes from and whether it works, without the key."""
    provider = PROVIDERS[name]
    check = KEY_CHECKS.get(name)
    if provider.local:
        url = provider_config(ai, name).get("base_url") or OLLAMA_URL
        detail = f"{LINK_SVG}<code>{esc(url)}</code>"
        if check is None:
            return ProviderStatus("Local endpoint", "Not tested", "", detail, True, False)
        if check.ok:
            return ProviderStatus("Local endpoint", "Connected", "success", detail, True, False)
        return ProviderStatus("Local endpoint", "Unreachable", "error", detail, False, True)
    info, _ = key_status(ai, name)
    configured = provider_config(ai, name).get("api_key") is not None
    source = {"env": "Environment", "stored": "Stored secret"}.get(info.source or "", "Not set up")
    ends = f'<span class="ends">ends in {esc(info.last4)}</span>' if info.last4 else ""
    if info.source == "env":
        detail = f"{KEY_SVG}<code>{esc(info.env)}</code>{ends}"
    elif info.source == "stored":
        detail = f"{LOCK_SVG}stored encrypted{ends}"
    else:
        detail = ""
    if info.error:
        if info.source == "env" and not configured:
            return ProviderStatus(
                "Not set up",
                "Not set up",
                "",
                f"{KEY_SVG}set <code>{esc(info.env)}</code> or add a key",
                False,
                False,
            )
        tone_text = "Re-enter key" if info.source == "stored" else "Variable not set"
        return ProviderStatus(
            source,
            tone_text,
            "error",
            f"{LOCK_SVG if info.source == 'stored' else KEY_SVG}{esc(info.error)}",
            False,
            True,
        )
    if check is not None and not check.ok:
        reason = (
            check.message.split("(", 1)[1].split(")", 1)[0] if "(" in check.message else "Rejected"
        )
        return ProviderStatus(
            source,
            "Key rejected",
            "error",
            f"{LOCK_SVG}{esc(reason)}, {esc(_ago(check.when))}",
            False,
            True,
        )
    return ProviderStatus(source, "Connected", "success", detail, True, False)


class AISettings(Viewer):
    """The AI section: provider cards, the models table, the model drawer and the key dialog."""

    editable = param.Boolean(default=True)

    def __init__(self, project: Project, editable: bool = True, **params):
        super().__init__(editable=editable, **params)
        self.project = project
        self._app = current_app()
        self._alert = pmui.Alert(
            object="",
            severity="error",
            visible=False,
            sizing_mode="stretch_width",
            margin=(0, 0, 8, 0),
        )
        self._status = pn.pane.HTML("", margin=0, stylesheets=[_STATUS_CSS])
        self._cards = pmui.FlexBox(
            margin=0,
            sizing_mode="stretch_width",
            flex_wrap="wrap",
            sx={"gap": "16px"},
            css_classes=["flowdash-admin-providers"],
        )
        self._models_note = cell("", sizing_mode=None)
        self._add_button = pmui.Button(
            label="Add model", icon="add", variant="contained", size="small", margin=0
        )
        self._add_button.on_click(lambda _event: self.open_model(None))
        toolbar = pn.Row(
            caption("Click a model to change who it is offered to, its settings and its limit."),
            pn.layout.HSpacer(),
            self._add_button,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "padding": "10px 12px",
                "align-items": "center",
                "gap": "12px",
                "border-bottom": "1px solid var(--mui-palette-divider)",
            },
        )
        self._table = Table(
            [
                ("Default", "64px"),
                ("Model", "2.2fr"),
                ("Provider", "1fr"),
                ("Status", "1fr"),
                ("Offered to", "1.3fr"),
                ("Settings", "1.4fr"),
                ("This month", "1.3fr"),
            ],
            toolbar=toolbar,
            footer=caption(
                "The picker shows the models a person may use; with one usable model it shows "
                "it as a label.",
                margin=(12, 12),
            ),
            empty="No models yet. Add one, or start the app with --agent-model.",
            css_classes=["flowdash-admin-models"],
        )
        self._build_drawer()
        self._build_key_dialog()
        self._layout = pn.Column(
            self._alert,
            heading(
                "Providers",
                cell(
                    '<span class="muted small">Keys are environment variables or stored '
                    "encrypted, and never shown again</span>",
                    sizing_mode=None,
                ),
                margin=(8, 0, 8, 0),
            ),
            self._cards,
            heading("Models", self._models_note),
            self._table,
            self._drawer,
            self._key_dialog,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-ai"],
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def ai(self) -> dict[str, t.Any]:
        """Return the ``ai`` section as the file has it now."""
        try:
            return copy.deepcopy(dict(current_section(self.project, "ai") or {}))
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return {}

    def entries(
        self, ai: t.Mapping[str, t.Any] | None = None
    ) -> tuple[list[ModelEntry], str | None]:
        """Return the models, those given in code first, and the default model's id."""
        ai = self.ai() if ai is None else ai
        if self._app is not None:
            return self._app._model_entries(dict(ai))
        entries = model_entries(ai)
        default = ai.get("default")
        return entries, default if default in {e.id for e in entries} else (
            entries[0].id if entries else None
        )

    def _ledger(self):
        if self._app is not None:
            return self._app._usage_ledger()
        return ledger_for(self.project.directory)

    def _default_locked(self) -> str | None:
        if self._app is None:
            return None
        return self._app._code_setting_source("agent_model") or (
            self._app._code_setting_source("agent") if isinstance(self._app.agent, str) else None
        )

    def _still_admin(self) -> bool:
        return self._app is None or self._app._still_admin()

    def write(self, ai: dict[str, t.Any], message: str = "Saved") -> bool:
        """Write *ai* as the project file's ``ai`` section; show its problems if it is rejected."""
        if not self._still_admin():
            self._show_problems([Problem(NOT_ADMIN)])
            return False
        try:
            self.project.set("ai", {k: v for k, v in ai.items() if v not in (None, {}, [])})
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return False
        except OSError as exc:
            self._show_problems([Problem(f"Could not write the project file: {error_text(exc)}")])
            return False
        self._alert.visible = False
        self._status.object = f'<span class="status">{_SAVED_ICON}All changes saved</span>'
        self.refresh()
        return True

    def _show_problems(self, problems: t.Sequence[Problem]):
        self._alert.param.update(
            object="\n\n".join(p.message for p in problems), visible=bool(problems)
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def providers(self, ai: t.Mapping[str, t.Any], entries: t.Sequence[ModelEntry]) -> list[str]:
        """Return the providers shown: those of the models, then the others the file configures."""
        names = [e.provider for e in entries] + list(ai.get("providers") or {})
        return [n for n in dict.fromkeys(names) if n in PROVIDERS]

    def refresh(self):
        """Draw the cards and the table from the project file."""
        ai = self.ai()
        entries, default = self.entries(ai)
        with pn.io.hold():
            self._cards[:] = [self._card(ai, name) for name in self.providers(ai, entries)] or [
                caption("Add a model to set up its provider's key.")
            ]
            locked = [e.name for e in entries if e.locked]
            if locked:
                sources = self._app.settings_sources if self._app is not None else {}
                where = sources.get("agent_models") or sources.get("agent_model") or "code"
                names = " and ".join(
                    [", ".join(locked[:-1]), locked[-1]] if len(locked) > 1 else locked
                )
                self._models_note.object = (
                    f'<div class="k"><span class="muted small">{esc(names)} '
                    f"{'comes' if len(locked) == 1 else 'come'} from <code>{esc(where)}</code>; "
                    "others were added here</span></div>"
                )
            else:
                self._models_note.object = ""
            ledger = self._ledger()
            self._table.set_rows([(e.id, self._row(ai, e, default, ledger)) for e in entries])

    def _card(self, ai, name: str):
        provider = PROVIDERS[name]
        status = provider_status(ai, name)
        color = _AVATAR_COLORS.get(name, "#455a64")
        head = cell(
            f'<div class="card-head"><span class="avatar" style="background:{color}">'
            f'{esc(_initial(name))}</span><span class="who"><b>{esc(provider.label)}</b>'
            f'<span class="line"><span class="muted small">{esc(status.source)}</span>'
            f"{chip(status.chip, status.tone)}</span></span></div>"
            f'<div class="detail">{status.detail or "&nbsp;"}</div>',
            stylesheets=[_CARD_CSS],
        )
        setup = not provider.local and status.source == "Not set up"
        replace = pmui.Button(
            label="Set up" if setup or provider.local else "Replace key",
            variant="text",
            size="small",
            margin=0,
            disabled=not self.editable,
        )
        replace.on_click(lambda _event, name=name: self.open_key_dialog(name))
        test = pmui.Button(label="Test", variant="text", size="small", margin=0)
        test.on_click(lambda _event, name=name: pn.state.execute(partial(self.test, name)))
        return pmui.Paper(
            head,
            pn.Row(replace, test, margin=(12, 0, 0, -8), styles={"gap": "8px"}),
            variant="outlined",
            margin=0,
            width=None,
            styles={"flex": "1 1 220px", "min-width": "220px", "max-width": "100%"},
            sx={
                "p": 2,
                **({"borderColor": "error.main"} if status.rejected else {}),
            },
            css_classes=["flowdash-admin-provider", f"flowdash-admin-provider-{name}"],
        )

    def _row(self, ai, entry: ModelEntry, default: str | None, ledger) -> list:
        is_default = entry.id == default
        locked = self._default_locked()
        radio = pmui.IconButton(
            icon="radio_button_checked" if is_default else "radio_button_unchecked",
            description=(
                f"The default model, {locked}"
                if is_default and locked
                else "The default model"
                if is_default
                else f"Make {entry.name} the default"
            ),
            color="primary" if is_default else "default",
            size="small",
            margin=0,
            disabled=bool(locked) or not self.editable,
            css_classes=["flowdash-admin-default"],
        )
        radio.on_click(lambda _event, model=entry.id: self.set_default(model))
        name = pmui.Button(
            label=entry.name,
            variant="text",
            size="small",
            margin=(0, 0, 0, -8),
            sx={
                "textTransform": "none",
                "fontSize": "0.875rem",
                "fontWeight": 400,
                "justifyContent": "flex-start",
                "color": "text.primary",
                "minWidth": 0,
                "py": 0,
                "px": 1,
                "minHeight": 0,
                "lineHeight": 1.5,
                "letterSpacing": "0.15px",
            },
            description=f"Change {entry.name}",
            css_classes=["flowdash-admin-model-name"],
        )
        name.on_click(lambda _event, model=entry.id: self.open_model(model))
        provider = PROVIDERS.get(entry.provider or "")
        status = provider_status(ai, entry.provider) if provider else None
        if status is None:
            model_status = chip("Usable", "success")
        elif status.usable:
            model_status = chip("Usable", "success")
        elif status.rejected:
            model_status = chip(status.chip, "error")
        else:
            model_status = chip("Needs a key", "")
        spent, requests = ledger.month(entry.id)
        if entry.monthly_limit:
            share = min(spent / entry.monthly_limit, 1.0) if entry.monthly_limit else 1.0
            tone = "error" if share >= 1 else ("warning" if share >= 0.8 else "")
            spend = (
                f'<div class="spend"><span>{_dollars(spent)}</span><span class="muted">of '
                f'{_dollars(entry.monthly_limit)}</span></div><div class="bar"><i class="{tone}" '
                f'style="width:{share * 100:.0f}%"></i></div>'
            )
        else:
            spend = f'<span title="{requests} requests">{_dollars(spent)}</span>'
        return [
            radio,
            pn.Column(
                name,
                cell(f'<span class="mono">{esc(entry.id)}</span>'),
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "0"},
            ),
            cell(
                f'<span class="main">{esc(provider.label if provider else entry.provider or "")}</span>'
            ),
            cell(model_status),
            cell(f'<span class="main">{esc(offered_summary(entry))}</span>'),
            cell(
                f'<span class="main muted" title="{esc(settings_summary(entry.settings))}">{esc(settings_summary(entry.settings))}</span>'
            ),
            cell(spend),
        ]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def set_default(self, model_id: str) -> bool:
        """Make *model_id* the default model."""
        if self._default_locked():
            return False
        with _FILE_LOCK:
            ai = self.ai()
            ai["default"] = model_id
            return self.write(ai)

    async def test(self, name: str) -> KeyCheck:
        """Test provider *name*'s key as the file has it and show the result on its card."""
        if not self._still_admin():
            self._notify("error", NOT_ADMIN)
            return KeyCheck(False, NOT_ADMIN)
        ai = self.ai()
        provider = PROVIDERS[name]
        try:
            key = None if provider.local else resolve_secret(provider_key(ai, name))
        except SecretError as exc:
            result = KeyCheck(False, str(exc))
        else:
            result = await check_key(name, key, provider_config(ai, name).get("base_url"))
        KEY_CHECKS[name] = result
        self.refresh()
        self._notify("success" if result.ok else "error", result.message)
        return result

    def _notify(self, severity: str, message: str):
        if self._app is not None:
            self._app._notify(severity, message, duration=5000)

    # ------------------------------------------------------------------
    # The model drawer
    # ------------------------------------------------------------------

    def _build_drawer(self):
        self._editing: str | None = None
        self._drawer_title = pmui.Typography("", variant="h6", margin=0, sx={"fontWeight": 400})
        self._drawer_subtitle = caption("")
        close = pmui.IconButton(icon="close", description="Close", size="small", margin=0)
        close.on_click(lambda _event: self.close_model())
        self._label = pmui.TextInput(
            label="Label", sizing_mode="stretch_width", margin=0, size="small"
        )
        self._model_id = pmui.TextInput(
            label="Model id",
            placeholder="provider:model, e.g. openai:gpt-5",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._description = pmui.TextInput(
            label="Description",
            placeholder="e.g. Careful with SQL",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._offered = pmui.CheckButtonGroup(
            options={label: key for key, label in SURFACES.items()},
            value=list(SURFACES),
            size="small",
            margin=0,
            color="primary",
        )
        self._groups = pmui.MultiChoice(
            label="Only for these groups",
            placeholder="Everyone",
            options=[],
            sizing_mode="stretch_width",
            margin=0,
        )
        self._settings_inputs = {
            key: pmui.TextInput(
                label=label + (" (s)" if key == "timeout" else ""),
                placeholder="Default",
                margin=0,
                size="small",
                sizing_mode="stretch_width",
            )
            for key, (label, *_rest) in MODEL_SETTINGS.items()
        }
        self._limit = pmui.TextInput(
            label="Monthly limit ($)",
            placeholder="No limit",
            margin=0,
            size="small",
            sizing_mode="stretch_width",
        )
        self._spend = cell("")
        self._fallback_on = pmui.Switch(
            label="At the limit, switch people to", value=False, margin=0
        )
        self._fallback = pmui.Select(
            label="Fallback", options={}, margin=0, size="small", width=200
        )
        self._fallback_on.param.watch(
            lambda event: setattr(self._fallback, "disabled", not event.new), "value"
        )
        self._drawer_alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._remove = pmui.Button(label="Remove model", variant="text", color="error", margin=0)
        self._remove.on_click(lambda _event: self.remove_model(self._editing))
        cancel = pmui.Button(label="Cancel", variant="outlined", color="default", margin=0)
        cancel.on_click(lambda _event: self.close_model())
        self._save_model = pmui.Button(label="Save", variant="contained", margin=0)
        self._save_model.on_click(lambda _event: self.save_model())
        info = pmui.Alert(
            object=(
                "Settings here are merged over the app's `agent_model_settings` for this "
                "model's requests; ones a model does not know are ignored."
            ),
            severity="info",
            sizing_mode="stretch_width",
            margin=0,
        )
        section = lambda text: pmui.Typography(
            text,
            variant="overline",
            sx={
                "color": "text.secondary",
                "fontSize": "0.75rem",
                "fontWeight": 500,
                "letterSpacing": "1.2px",
                "lineHeight": 2,
            },
            margin=(12, 0, 0, 0),
        )
        head = pn.Row(
            pn.Column(
                self._drawer_title,
                self._drawer_subtitle,
                margin=0,
                sizing_mode="stretch_width",
            ),
            close,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "align-items": "flex-start",
                "padding": "20px 20px 12px 20px",
                "box-sizing": "border-box",
                "flex": "0 0 auto",
            },
        )
        fields = pn.Column(
            self._drawer_alert,
            pn.Row(
                self._label,
                self._model_id,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px"},
            ),
            self._description,
            section("Offered to"),
            self._offered,
            self._groups,
            section("Model settings"),
            pn.Row(
                self._settings_inputs["temperature"],
                self._settings_inputs["max_tokens"],
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px"},
            ),
            pn.Row(
                self._settings_inputs["timeout"],
                pn.Spacer(sizing_mode="stretch_width"),
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px"},
            ),
            info,
            section("Spending"),
            self._limit,
            self._spend,
            pn.Row(
                self._fallback_on,
                self._fallback,
                margin=0,
                styles={"align-items": "center", "gap": "8px", "flex-wrap": "wrap"},
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "gap": "14px",
                "padding": "4px 20px 20px 20px",
                "overflow-y": "auto",
                "flex": "1 1 auto",
                "min-height": "0",
                "box-sizing": "border-box",
            },
        )
        foot = pn.Row(
            self._remove,
            pn.layout.HSpacer(),
            cancel,
            self._save_model,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "gap": "8px",
                "border-top": "1px solid var(--mui-palette-divider)",
                "padding": "16px 20px",
                "box-sizing": "border-box",
                "flex": "0 0 auto",
            },
        )
        body = pn.Column(
            head,
            fields,
            foot,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "height": "100vh",
                "display": "flex",
                "flex-direction": "column",
                "box-sizing": "border-box",
            },
            css_classes=["flowdash-admin-model-drawer"],
        )
        self._drawer = pmui.Drawer(body, anchor="right", variant="temporary", size=460, open=False)

    def open_model(self, model_id: str | None):
        """Open the drawer on model *model_id*, or on a new model for None."""
        entries, default = self.entries()
        by_id = {e.id: e for e in entries}
        entry = by_id.get(model_id) if model_id else None
        self._editing = entry.id if entry else None
        groups = sorted(
            set(self._app._known_groups() if self._app else ())
            | set(entry.groups if entry else ())
        )
        others = {e.name: e.id for e in entries if e.id != (entry.id if entry else None)}
        with pn.io.hold():
            self._drawer_alert.visible = False
            if entry is None:
                self._drawer_title.object = "Add a model"
                self._drawer_subtitle.object = (
                    "A pydantic-ai model id, e.g. openai:gpt-5 or ollama:qwen3:14b"
                )
            else:
                provider = PROVIDERS.get(entry.provider or "")
                bits = [provider.label if provider else (entry.provider or "")]
                bits.append("the default" if entry.id == default else "not the default")
                if entry.locked:
                    bits.append(entry.locked)
                self._drawer_title.object = entry.name
                self._drawer_subtitle.object = " · ".join(b for b in bits if b)
            self._label.param.update(
                value=(entry.label or "") if entry else "", disabled=bool(entry and entry.locked)
            )
            self._description.param.update(
                value=(entry.description or "") if entry else "",
                disabled=bool(entry and entry.locked),
            )
            self._model_id.param.update(
                value=entry.id if entry else "", disabled=entry is not None
            )
            self._offered.value = list(entry.offered_to) if entry else list(SURFACES)
            self._groups.param.update(options=groups, value=list(entry.groups) if entry else [])
            for key, widget in self._settings_inputs.items():
                value = entry.settings.get(key) if entry else None
                widget.value = "" if value is None else f"{value:g}"
            limit = entry.monthly_limit if entry else None
            self._limit.value = "" if limit is None else f"{limit:g}"
            if entry is not None:
                spent, requests = self._ledger().month(entry.id)
                first = dt.date.today().replace(day=1).strftime("%-d %b")
                share = f" · {spent / entry.monthly_limit:.0%}" if entry.monthly_limit else ""
                self._spend.object = (
                    f'<div class="k"><span class="muted small">{_dollars(spent)} spent since {first} · '
                    f"{requests:,} request{'' if requests == 1 else 's'}{share}</span></div>"
                )
            else:
                self._spend.object = ""
            self._fallback.options = others or {"No other model": ""}
            fallback = entry.fallback if entry and entry.fallback in others.values() else None
            self._fallback.value = fallback or next(iter(others.values()), "")
            self._fallback_on.param.update(value=fallback is not None, disabled=not others)
            self._fallback.disabled = fallback is None
            self._remove.visible = entry is not None and not entry.locked
            self._drawer.open = True

    def close_model(self):
        """Close the drawer without saving."""
        self._drawer.open = False
        self._editing = None

    def _drawer_entry(self) -> tuple[dict[str, t.Any] | None, list[str]]:
        problems = []
        model_id = (
            self._editing or self._model_id.value_input or self._model_id.value or ""
        ).strip()
        if ":" not in model_id:
            problems.append("The model id names the provider and the model, e.g. openai:gpt-5.")
        data: dict[str, t.Any] = {"id": model_id}
        label = (
            self._label.value_input if self._label.value_input is not None else self._label.value
        ) or ""
        description = (
            self._description.value_input
            if self._description.value_input is not None
            else self._description.value
        ) or ""
        if label.strip():
            data["label"] = label.strip()
        if description.strip():
            data["description"] = description.strip()
        offered = [s for s in SURFACES if s in self._offered.value]
        if offered != list(SURFACES):
            data["offered_to"] = offered
        if self._groups.value:
            data["groups"] = list(self._groups.value)
        settings = {}
        for key, widget in self._settings_inputs.items():
            label, _low, _high, integer = MODEL_SETTINGS[key]
            value, problem = _number(_text(widget), label, integer)
            if problem:
                problems.append(problem)
            elif value is not None:
                settings[key] = value
        problems += [message for _path, message in check_model_settings(settings)]
        if settings:
            data["settings"] = settings
        limit, problem = _number(_text(self._limit), "The monthly limit", False)
        if problem or (limit is not None and limit < 0):
            problems.append("The monthly limit is an amount in US dollars.")
        elif limit is not None:
            data["monthly_limit"] = limit
        if self._fallback_on.value and self._fallback.value:
            data["fallback"] = self._fallback.value
        return (None if problems else data), problems

    def save_model(self) -> bool:
        """Write the drawer's model to the project file and close the drawer."""
        data, problems = self._drawer_entry()
        if data is None:
            self._drawer_alert.param.update(object="\n\n".join(problems), visible=True)
            return False
        with _FILE_LOCK:
            ai = self.ai()
            entries, _ = self.entries(ai)
            locked = {e.id: e for e in entries if e.locked}
            models = list(ai.get("models") or [])
            if self._editing is None and data["id"] in {e.id for e in entries}:
                self._drawer_alert.param.update(
                    object=f"{data['id']} is listed already.", visible=True
                )
                return False
            if data["id"] in locked:
                # A model given in code keeps its label and description from there.
                data = {k: v for k, v in data.items() if k not in ("label", "description")}
            index = next((i for i, m in enumerate(models) if m.get("id") == data["id"]), None)
            keep = len(data) > 1 or data["id"] not in locked
            if index is None and keep:
                models.append(data)
            elif index is not None:
                if keep:
                    models[index] = data
                else:
                    del models[index]
            ai["models"] = models
            if not self.write(ai):
                self._drawer_alert.param.update(object=self._alert.object, visible=True)
                self._alert.visible = False
                return False
        self.close_model()
        return True

    def remove_model(self, model_id: str | None) -> bool:
        """Remove model *model_id*, which the project file added, and the fallbacks to it."""
        if model_id is None:
            return False
        with _FILE_LOCK:
            ai = self.ai()
            models = [m for m in ai.get("models") or [] if m.get("id") != model_id]
            for model in models:
                if model.get("fallback") == model_id:
                    model.pop("fallback")
            ai["models"] = models
            if ai.get("default") == model_id:
                ai.pop("default")
            ok = self.write(ai)
        if ok:
            self.close_model()
        return ok

    # ------------------------------------------------------------------
    # The key dialog
    # ------------------------------------------------------------------

    def _build_key_dialog(self):
        self._key_provider: str | None = None
        self._key_rejected = False
        self._key_head = cell("", stylesheets=[_CARD_CSS])
        self._use_env = pmui.Switch(
            label="Read the key from an environment variable", value=True, margin=0
        )
        self._env_name = pmui.TextInput(
            label="Variable", sizing_mode="stretch_width", margin=0, size="small"
        )
        self._key = pmui.PasswordInput(label="API key", sizing_mode="stretch_width", margin=0)
        # Panel's debug log records each value a widget receives; never this one's.
        self._key._log = types.MethodType(_redacted_log, self._key)
        self._base_url = pmui.TextInput(
            label="Server URL", sizing_mode="stretch_width", margin=0, size="small"
        )
        self._key_info = pmui.Alert(
            object="", severity="info", sizing_mode="stretch_width", margin=0
        )
        self._key_test = pmui.Button(label="Test", icon="refresh", variant="text", margin=0)
        self._key_test.on_click(lambda _event: pn.state.execute(self.test_typed))
        cancel = pmui.Button(label="Cancel", variant="outlined", color="default", margin=0)
        cancel.on_click(lambda _event: self.close_key_dialog())
        self._key_save = pmui.Button(label="Save key", variant="contained", margin=0)
        self._key_save.on_click(lambda _event: self.save_key())
        self._use_env.param.watch(lambda _event: self._sync_key_dialog(), "value")
        for widget in (self._key, self._env_name):
            widget.param.watch(self._on_key_typed, ["value", "value_input"])
        self._key_dialog = pmui.Dialog(
            pn.Column(
                self._key_head,
                self._use_env,
                self._env_name,
                self._key,
                self._base_url,
                self._key_info,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "16px"},
                css_classes=["flowdash-admin-key-dialog"],
            ),
            actions=[self._key_test, pn.layout.HSpacer(), cancel, self._key_save],
            title="Replace the key",
            title_variant="h5",
            show_close_button=True,
            open=False,
            width_option="sm",
            sizing_mode="stretch_width",
        )
        self._key_dialog.param.watch(self._on_key_dialog_open, "open")

    def _on_key_dialog_open(self, event):
        if not event.new:
            self._clear_typed_key()

    def _clear_typed_key(self):
        clear_text(self._key)
        self._key.param.update(error_state=False, helper_text="")
        self._key_rejected = False

    def _on_key_typed(self, *_events):
        if self._key_rejected:
            self._key_rejected = False
            self._key.param.update(error_state=False, helper_text="")
        self._sync_key_dialog()

    def _typed_key(self) -> str:
        return (self._key.value_input or self._key.value or "").strip()

    def open_key_dialog(self, name: str):
        """Open the key dialog of provider *name*."""
        provider = PROVIDERS[name]
        ai = self.ai()
        configured = provider_config(ai, name)
        current = configured.get("api_key")
        self._key_provider = name
        color = _AVATAR_COLORS.get(name, "#455a64")
        self._key_head.object = (
            f'<div class="k"><div class="card-head"><span class="avatar" style="background:{color}">'
            f'{esc(_initial(name))}</span><span class="who"><b>{esc(provider.label)}</b>'
            f'<span class="muted small">{esc(provider.blurb or "Model provider")}</span></span></div></div>'
        )
        stored = isinstance(current, dict)
        env = current[2:-1] if isinstance(current, str) else provider.env
        self._clear_typed_key()
        with pn.io.hold():
            self._env_name.value = env or ""
            self._use_env.value = not stored
            self._base_url.value = configured.get("base_url") or OLLAMA_URL
            title = (
                f"Set up {provider.label}"
                if provider.local
                else f"Replace the {provider.label} key"
            )
            self._key_dialog.title = title
            self._sync_key_dialog()
            self._key_dialog.open = True

    def _sync_key_dialog(self):
        name = self._key_provider
        if name is None:
            return
        provider = PROVIDERS[name]
        local = provider.local
        can_store = encryption_available()
        use_env = self._use_env.value or not can_store
        self._use_env.visible = not local
        self._use_env.disabled = not can_store
        self._env_name.visible = not local and use_env
        self._key.visible = not local and not use_env
        self._base_url.visible = local
        variable = (
            self._env_name.value_input or self._env_name.value or provider.env or ""
        ).strip()
        if local:
            info = "FlowDash connects to the server at this address for the models it runs."
        elif use_env:
            info = (
                f"The key is read from `{variable or 'the variable'}` in the server's environment "
                "for each request. It is never written to the project file or shown here."
                + ("" if can_store else f" {INSTALL_HINT}")
            )
        else:
            info = (
                f"The key is stored encrypted in the project file and is only sent to "
                f"{provider.label}. Nobody can read it back here, admins included. To keep it "
                f"out of the file, set `{provider.env or 'a variable'}` in the server's "
                "environment instead."
            )
        self._key_info.object = info
        typed = bool(self._typed_key())
        self._key_save.disabled = (
            self._key_rejected
            or (not local and not use_env and not typed)
            or (not local and use_env and not variable)
        )
        self._key_test.label = "Test again" if self._key_rejected else "Test"

    def close_key_dialog(self):
        """Close the key dialog, forgetting a typed key."""
        self._key_dialog.open = False
        self._clear_typed_key()

    def _dialog_key(self) -> tuple[t.Any, str | None]:
        """Return the project file value the dialog describes and the key it resolves to."""
        name = self._key_provider
        provider = PROVIDERS[name]
        if provider.local:
            return None, None
        if self._use_env.value or not encryption_available():
            variable = (self._env_name.value_input or self._env_name.value or "").strip()
            reference = env_reference(variable)
            try:
                return reference, resolve_secret(reference)
            except SecretError:
                return reference, None
        typed = self._typed_key()
        return None, typed or None

    async def test_typed(self) -> KeyCheck | None:
        """Test the key the dialog describes, without saving it."""
        name = self._key_provider
        if name is None:
            return None
        if not self._still_admin():
            result = KeyCheck(False, NOT_ADMIN)
            self._show_key_check(result)
            return result
        try:
            _, key = self._dialog_key()
        except SecretError as exc:
            result = KeyCheck(False, str(exc))
        else:
            base_url = (self._base_url.value or "").strip() or None
            result = await check_key(name, key, base_url)
        self._show_key_check(result)
        return result

    def _show_key_check(self, result: KeyCheck):
        if result.ok:
            self._key.param.update(error_state=False, helper_text="")
            self._notify("success", result.message)
        else:
            self._key_rejected = not self._use_env.value
            if self._key.visible:
                self._key.param.update(error_state=True, helper_text=result.message)
            else:
                self._notify("error", result.message)
        self._sync_key_dialog()

    def save_key(self) -> bool:
        """Write the dialog's key (as a reference, or encrypted) or server URL and close it."""
        name = self._key_provider
        if name is None:
            return False
        provider = PROVIDERS[name]
        try:
            if provider.local:
                url = (self._base_url.value_input or self._base_url.value or "").strip()
                value = {"base_url": url}
                what = "server URL"
            elif self._use_env.value or not encryption_available():
                variable = (self._env_name.value_input or self._env_name.value or "").strip()
                value = {"api_key": env_reference(variable)}
                what = f"key reference to {variable}"
            else:
                typed = self._typed_key()
                stored = encrypt_secret(typed)
                value = {"api_key": stored}
                what = f"key, stored encrypted (key {stored['key_id']})"
        except SecretError as exc:
            self._key.param.update(error_state=True, helper_text=str(exc))
            return False
        with _FILE_LOCK:
            ai = self.ai()
            providers = dict(ai.get("providers") or {})
            providers[name] = {**provider_config(ai, name), **value}
            ai["providers"] = providers
            ok = self.write(ai)
        if not ok:
            return False
        user = self._app._identity.user if self._app is not None else "someone"
        # The audit entry names who changed which key and how it is kept, never the key.
        logger.info("Admin: %s saved the %s %s.", user, provider.label, what)
        KEY_CHECKS.pop(name, None)
        self.close_key_dialog()
        if pn.state.curdoc is not None:
            pn.state.execute(partial(self.test, name))
        return True

    def __panel__(self):
        """Render the section."""
        return self._layout


def ai_settings(project: Project, editable: bool) -> AISettings:
    """Build the AI section of the admin view."""
    return AISettings(project, editable)


def dump_ai(value: t.Any) -> t.Any:
    """Return the ``ai`` section as plain data, without empty parts."""
    if not isinstance(value, dict):
        return value
    return {k: v for k, v in value.items() if v not in (None, {}, [])}


AI = ProjectSection(
    "ai",
    load_ai,
    title="AI",
    icon="smart_toy",
    description=(
        "Model providers, the models people can pick in the assistant and Explore, and what "
        "they may spend."
    ),
    dump=dump_ai,
    settings_view=ai_settings,
    group="App",
)

register_section(AI)

__all__ = ["AI", "AISettings", "provider_name", "provider_status", "settings_summary"]
