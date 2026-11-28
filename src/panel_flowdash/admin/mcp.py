"""The MCP section of the admin view: MCP servers, credentials, approvals and discovered tools.

Each change is written to the project file's ``mcp`` section as it is made,
and agent runs pick it up on the next request. Secrets (HTTP bearer tokens,
headers and stdio env vars) are write-only: the UI shows where each comes
from and its last four characters for admins, never the value, and clears
typed secrets as soon as they are saved or the drawer closes.

Host apps such as Lumen can embed :class:`MCPSettings` in their own section
(with ``editable=False`` for non-admins, ``show_add=False`` when the host
renders its own header button, ``server_extra`` for per-server host widgets,
and ``FlowDashApp.admin_section_redirects`` to hide/redirect ``/admin/mcp``).
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import shlex
import types
import typing as t
from collections.abc import Callable, Iterable, Mapping, Sequence
from functools import partial
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.admin.form import current_app
from panel_flowdash.admin.kit import (
    KEY_SVG,
    LINK_SVG,
    LOCK_SVG,
    caption,
    cell,
    chip,
    clear_text,
    esc,
    heading,
)
from panel_flowdash.admin.permission_editor import PermissionEditor
from panel_flowdash.auth import Permission, permission_lockout_reason
from panel_flowdash.mcp import (
    DEFAULT_INIT_TIMEOUT,
    DEFAULT_TIMEOUT,
    MCP_SECTION,
    STDIO_COMMANDS_ENV,
    STDIO_ENV_VARS_ENV,
    SURFACES,
    MCPServerCheck,
    MCPServerConfig,
    MCPToolInfo,
    access_to_dict,
    allowed_secret_env_vars,
    allowed_stdio_commands,
    allowed_stdio_env,
    check_mcp_secret_ref,
    check_mcp_server,
    check_stdio_command,
    check_stdio_env,
    clear_mcp_check,
    configured_servers,
    dump_mcp,
    format_stdio_prefix,
    get_mcp_check,
    load_mcp,
    mcp_server_authorized,
    mcp_tool_authorized,
    mcp_tool_name,
    url_origin,
    validate_server_name,
)
from panel_flowdash.project import (
    _FILE_LOCK,
    Problem,
    Project,
    ProjectError,
    ProjectSection,
    current_section,
    register_section,
)
from panel_flowdash.secret_store import (
    INSTALL_HINT,
    SecretError,
    describe_secret,
    encrypt_secret,
    encryption_available,
    env_reference,
)
from panel_flowdash.value_text import error_text, redact_secrets

logger = logging.getLogger(__name__)

NOT_ADMIN = "You are no longer an admin of this project, so this was not done."

_CARD_CSS = """
.k .card-head { display: flex; align-items: flex-start; gap: 12px; }
.k .avatar { width: 36px; height: 36px; border-radius: 8px; color: #fff; font-weight: 500;
  display: flex; align-items: center; justify-content: center; font-size: 0.9rem; flex: 0 0 auto; }
.k .who { flex: 1 1 auto; min-width: 0; }
.k .who b { font-weight: 500; font-size: 1rem; display: block; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.k .who .line { display: flex; align-items: center; flex-wrap: wrap; column-gap: 6px; row-gap: 2px;
  margin-top: 2px; }
.k .who .chip { margin: 0; }
.k .desc { margin-top: 8px; color: var(--mui-palette-text-secondary); font-size: 0.8125rem; }
.k .detail { display: flex; align-items: center; flex-wrap: wrap; column-gap: 6px; row-gap: 2px;
  margin-top: 10px; color: var(--mui-palette-text-secondary); min-width: 0; font-size: 0.8125rem; }
.k .detail code { overflow-wrap: anywhere; color: var(--mui-palette-text-primary); }
.k .tools { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.k .tools-label { width: 100%; font-size: 0.75rem; color: var(--mui-palette-text-secondary);
  margin-bottom: 2px; }
"""

_TRANSPORT_COLORS = {"http": "#0072B5", "stdio": "#455a64"}


def _redacted_log(widget, msg: str, *args, level: str = "debug") -> None:
    type(widget)._log(widget, msg, *("<redacted>" for _ in args), level=level)


def _text(widget) -> str:
    value = widget.value_input if widget.value_input is not None else widget.value
    return (value or "").strip()


def _ago(when: dt.datetime) -> str:
    seconds = (dt.datetime.now(dt.UTC) - when).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return when.strftime("%-d %b")


def _positive_float(text: str, label: str, default: float) -> tuple[float | None, str | None]:
    if not text:
        return default, None
    try:
        value = float(text)
    except ValueError:
        return None, f"{label} is a number of seconds."
    if value <= 0:
        return None, f"{label} must be greater than zero."
    return value, None


def _secret_summary(value: t.Any) -> str:
    info = describe_secret(value)
    if info.source == "env":
        suffix = f" (ends in {info.last4})" if info.last4 else ""
        if info.error:
            return f"${{{info.env}}} (not set)"
        return f"${{{info.env}}}{suffix}"
    if info.source == "stored":
        suffix = f" (ends in {info.last4})" if info.last4 else ""
        if info.error:
            return "stored encrypted (needs re-entry)"
        return f"stored encrypted{suffix}"
    return "not set"


class ServerStatus(t.NamedTuple):
    """Status chip and tone for an MCP server card."""

    chip: str
    tone: str
    detail: str
    error: bool = False


def server_status(
    config: MCPServerConfig,
    *,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    project: t.Any = None,
    project_key: str | None = None,
) -> ServerStatus:
    """Return the status chip, tone and detail message for *config*."""
    if not config.enabled:
        return ServerStatus("Disabled", "outlined", "")
    if config.transport == "stdio" and not config.locked and config.toolset is None:
        problem = check_stdio_command(config.command or "", allowed_stdio, args=config.args)
        if problem is None and config.env:
            problem = check_stdio_env(config.env, allowed_stdio_env)
        if problem is not None:
            return ServerStatus("Blocked by policy", "warning", problem, error=True)
    secret_issues = config.secret_problems(allowed_secret_env=allowed_secret_env)
    if secret_issues:
        return ServerStatus("Needs secret", "error", secret_issues[0], error=True)
    check = get_mcp_check(config.name, project=project, project_key=project_key)
    if check is None:
        return ServerStatus("Not tested", "", "")
    if check.ok:
        return ServerStatus("Connected", "success", f"{check.message}, {_ago(check.when)}")
    return ServerStatus("Unreachable", "error", f"{check.message}, {_ago(check.when)}", error=True)


class MCPSettings(Viewer):
    """MCP server configuration cards, discovered tools and admin edit drawer.

    Parameters
    ----------
    project
        The loaded :class:`~panel_flowdash.project.Project` backing ``flowdash.yaml``.
    editable
        True for administrators (shows Add/Edit/Test/Delete/Enable controls);
        False for read-only viewers (hides secrets, stdio commands and edit controls).
    app
        The live :class:`~panel_flowdash.app.FlowDashApp`. Defaults to
        :func:`~panel_flowdash.admin.form.current_app` when built inside ``AdminPage``.
    server_filter
        Optional predicate ``(MCPServerConfig) -> bool`` deciding which server
        cards to show. Defaults to all servers when ``editable=True``, and
        servers allowed by :func:`mcp_server_authorized` when ``editable=False``.
    tool_filter
        Optional predicate ``(server_name, tool_name) -> bool`` deciding which
        discovered tools appear on a card. Defaults to all tools when
        ``editable=True``, and tools allowed by ``mcp_tool_authorized`` plus
        ``app.tool_authorize`` when ``editable=False``.
    server_extra
        Optional callable ``(MCPServerConfig, editable) -> Viewable | None``
        whose returned Panel object is appended inside each server card (used
        by host apps such as Lumen to render custom access rule controls).
    show_access
        Whether to show built-in ``access:`` fields in the edit drawer. Set
        False when a host app manages access rules via *server_extra*.
    show_add
        Whether to render the section's top ``Add server`` button. Set False
        when a host page places the primary button in its own header and
        calls :meth:`open_server`.
    on_change
        Optional callback invoked with no arguments after a server is saved,
        toggled or removed.
    """

    editable = param.Boolean(default=True)

    def __init__(
        self,
        project: Project,
        editable: bool = True,
        *,
        app: t.Any = None,
        server_filter: Callable[[MCPServerConfig], bool] | None = None,
        tool_filter: Callable[[str, str], bool] | None = None,
        server_extra: Callable[[MCPServerConfig, bool], t.Any] | None = None,
        show_access: bool = True,
        show_add: bool = True,
        on_change: Callable[[], None] | None = None,
        **params,
    ):
        super().__init__(editable=editable, **params)
        self.project = project
        self._app = app if app is not None else current_app()
        self._server_filter = server_filter
        self._tool_filter = tool_filter
        self._server_extra = server_extra
        self._show_access = show_access
        self._show_add = show_add
        self._on_change = on_change

        self._alert = pmui.Alert(
            object="",
            severity="error",
            visible=False,
            sizing_mode="stretch_width",
            margin=(0, 0, 8, 0),
        )
        self._add_button = pmui.Button(
            label="Add server",
            icon="add",
            variant="contained",
            size="small",
            margin=0,
            visible=bool(self.editable and self._show_add),
            css_classes=["flowdash-mcp-add"],
        )
        self._add_button.on_click(lambda _event: self.open_server(None))
        self._cards = pmui.FlexBox(
            margin=0,
            sizing_mode="stretch_width",
            flex_wrap="wrap",
            sx={"gap": "16px"},
            css_classes=["flowdash-admin-mcp-servers"],
        )
        self._header_row = heading(
            "MCP servers",
            self._add_button,
            margin=(8, 0, 12, 0),
        )
        self._policy_note = caption(
            "",
            margin=(0, 0, 12, 0),
            visible=False,
        )

        self._drawer: pmui.Drawer | None = None
        if self.editable:
            self._build_drawer()

        items: list[t.Any] = [self._alert]
        if self.editable and self._show_add:
            items.append(self._header_row)
        items.extend([self._policy_note, self._cards])
        if self._drawer is not None:
            items.append(self._drawer)

        self._layout = pn.Column(
            *items,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["flowdash-admin-mcp"],
        )
        self.refresh()

    # ------------------------------------------------------------------
    # Data and permissions
    # ------------------------------------------------------------------

    def _allowed_stdio(self) -> tuple[tuple[str, ...], ...]:
        if self._app is not None and hasattr(self._app, "_allowed_stdio_commands"):
            return tuple(self._app._allowed_stdio_commands())
        return allowed_stdio_commands(None)

    def _allowed_stdio_env(self) -> frozenset[str]:
        if self._app is not None and hasattr(self._app, "_allowed_stdio_env"):
            return frozenset(self._app._allowed_stdio_env())
        return allowed_stdio_env(None)

    def _allowed_secret_env_vars(self) -> tuple[str, ...]:
        if self._app is not None and hasattr(self._app, "_allowed_secret_env_vars"):
            return tuple(self._app._allowed_secret_env_vars())
        ctx = getattr(self.project, "context", None) or {}
        if "mcp_secret_env_vars" in ctx and ctx["mcp_secret_env_vars"] is not None:
            return allowed_secret_env_vars(ctx["mcp_secret_env_vars"])
        return allowed_secret_env_vars(None)

    def _project_directory(self) -> Path | None:
        if self.project.directory is not None:
            return self.project.directory
        if self._app is not None and getattr(self._app, "project_dir", None) is not None:
            return Path(self._app.project_dir)
        return None

    def _still_admin(self) -> bool:
        if not self.editable:
            return False
        if self._app is None:
            return True
        if hasattr(self._app, "still_admin"):
            return bool(self._app.still_admin())
        return bool(self._app._still_admin())

    def _identity(self):
        return getattr(self._app, "_identity", None) if self._app is not None else None

    def _is_admin(self) -> bool:
        if self._app is None:
            return bool(self.editable)
        if hasattr(self._app, "_authorize_admin"):
            return bool(self._app._authorize_admin(fresh=True))
        return bool(getattr(self._app, "is_admin", False))

    def _access_lockout_reason(self, permission: Permission) -> str | None:
        identity = self._identity()
        if identity is None:
            return None
        return permission_lockout_reason(permission, identity, is_admin=True)

    def mcp_data(self) -> dict[str, t.Any]:
        """Return the ``mcp`` section as the project file has it now."""
        try:
            return copy.deepcopy(dict(current_section(self.project, MCP_SECTION) or {}))
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return {}

    def servers(self) -> list[MCPServerConfig]:
        """Return configured MCP servers, merging code-defined and project file servers."""
        return list(configured_servers(self.project, self._app).values())

    def _visible_servers(self) -> list[MCPServerConfig]:
        all_servers = self.servers()
        if self._server_filter is not None:
            return [s for s in all_servers if self._server_filter(s)]
        if self.editable:
            return all_servers
        identity = self._identity()
        is_admin = self._is_admin()
        return [
            s
            for s in all_servers
            if s.enabled and mcp_server_authorized(s, identity, is_admin=is_admin)
        ]

    def _visible_tools(
        self, server: MCPServerConfig, tools: t.Sequence[MCPToolInfo]
    ) -> list[MCPToolInfo]:
        if self._tool_filter is not None:
            return [tool for tool in tools if self._tool_filter(server.name, tool.name)]
        if self.editable:
            return list(tools)
        identity = self._identity()
        is_admin = self._is_admin()
        result: list[MCPToolInfo] = []
        for tool in tools:
            if not mcp_tool_authorized(server, tool.name, identity, is_admin=is_admin):
                continue
            if self._app is not None and hasattr(self._app, "_tool_authorized"):
                if not self._app._tool_authorized(mcp_tool_name(server.name, tool.name)):
                    continue
            result.append(tool)
        return result

    def write(self, data: dict[str, t.Any]) -> bool:
        """Write *data* as the project file's ``mcp`` section after re-checking admin rights."""
        if not self._still_admin():
            self._show_problems([Problem(NOT_ADMIN)])
            return False
        try:
            self.project.set(MCP_SECTION, dump_mcp(data))
        except ProjectError as exc:
            self._show_problems(exc.problems)
            return False
        except OSError as exc:
            self._show_problems([Problem(f"Could not write the project file: {error_text(exc)}")])
            return False
        self._alert.visible = False
        self.refresh()
        if self._on_change is not None:
            self._on_change()
        return True

    def _show_problems(self, problems: t.Sequence[Problem]) -> None:
        self._alert.param.update(
            object="\n\n".join(p.message for p in problems),
            visible=bool(problems),
        )

    def _notify(self, severity: str, message: str) -> None:
        if self._app is not None and hasattr(self._app, "_notify"):
            self._app._notify(severity, message, duration=5000)

    # ------------------------------------------------------------------
    # Rendering cards
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Redraw the server cards from the project file and cached status checks."""
        allowed = self._allowed_stdio()
        allowed_env = self._allowed_stdio_env()
        allowed_secret_env = self._allowed_secret_env_vars()
        visible = self._visible_servers()
        with pn.io.hold():
            if self.editable:
                if allowed:
                    cmds = ", ".join(format_stdio_prefix(p) for p in allowed)
                    self._policy_note.object = (
                        f"MCP tools require user approval by default unless auto-approved per server. "
                        f"Allowed stdio prefixes: {cmds}."
                    )
                else:
                    self._policy_note.object = (
                        "MCP tools require user approval by default unless auto-approved per server. "
                        "Stdio commands are disabled until allowlisted by the server operator."
                    )
                self._policy_note.visible = True
            else:
                self._policy_note.visible = False

            if visible:
                self._cards[:] = [
                    self._card(server, allowed, allowed_env, allowed_secret_env)
                    for server in visible
                ]
            else:
                empty = (
                    "No MCP servers configured yet. Click Add server to connect one."
                    if self.editable
                    else "No MCP servers are available."
                )
                self._cards[:] = [caption(empty, margin=(4, 0))]

    def _card(
        self,
        server: MCPServerConfig,
        allowed: tuple[tuple[str, ...], ...],
        allowed_env: frozenset[str],
        allowed_secret_env: tuple[str, ...],
    ):
        status = server_status(
            server,
            allowed_stdio=allowed,
            allowed_stdio_env=allowed_env,
            allowed_secret_env=allowed_secret_env,
            project=self.project,
        )
        color = _TRANSPORT_COLORS.get(server.transport, "#455a64")
        badge_text = "HTTP" if server.transport == "http" else "STDIO"
        chips_html = chip(badge_text, "outlined") + chip(status.chip, status.tone)
        if server.locked:
            chips_html += chip("In code", "outlined")
        if self.editable and self._show_access and not server.access.is_empty:
            chips_html += chip("Restricted", "warning")

        desc_html = (
            f'<div class="desc">{esc(server.description)}</div>' if server.description else ""
        )

        detail_html = ""
        if self.editable:
            if server.transport == "http" and server.url:
                parts = [f"{LINK_SVG}<code>{esc(redact_secrets(server.url))}</code>"]
                if server.token is not None:
                    parts.append(
                        f"{KEY_SVG}<span>token: {esc(_secret_summary(server.token))}</span>"
                    )
                if server.headers:
                    parts.append(
                        f"{LOCK_SVG}<span>{len(server.headers)} header"
                        f"{'' if len(server.headers) == 1 else 's'}</span>"
                    )
                detail_html = f'<div class="detail">{" · ".join(parts)}</div>'
            elif server.transport == "stdio" and server.command:
                cmd_str = " ".join([server.command, *server.args])
                parts = [f"<code>{esc(cmd_str)}</code>"]
                if server.env:
                    parts.append(
                        f"{LOCK_SVG}<span>{len(server.env)} env var"
                        f"{'' if len(server.env) == 1 else 's'}</span>"
                    )
                detail_html = f'<div class="detail">{" · ".join(parts)}</div>'
        if status.detail and (self.editable or not status.error):
            detail_html += (
                f'<div class="detail"><span class="small muted">{esc(status.detail)}</span></div>'
            )

        check = get_mcp_check(server.name, project=self.project)
        tools_html = ""
        if check is not None and check.ok:
            visible_tools = self._visible_tools(server, check.tools)
            if visible_tools:
                tool_chips = []
                for tool in visible_tools:
                    auto = server.is_auto_approved(tool.name)
                    title = tool.description or tool.name
                    if auto:
                        title = f"{title} (auto-approved)"
                    else:
                        title = f"{title} (requires approval)"
                    tone = "primary" if auto else ""
                    label = f"{tool.name} · auto" if auto else tool.name
                    tool_chips.append(
                        f'<span class="chip {tone}" title="{esc(title)}">{esc(label)}</span>'
                    )
                tools_html = (
                    f'<div class="tools">'
                    f'<span class="tools-label">Tools ({len(visible_tools)})</span>'
                    f"{''.join(tool_chips)}</div>"
                )
            else:
                tools_html = (
                    '<div class="tools"><span class="tools-label">No tools available</span></div>'
                )
        elif self.editable:
            tools_html = (
                '<div class="tools"><span class="tools-label">'
                "No tools discovered yet. Test the connection to list tools."
                "</span></div>"
            )

        body = cell(
            f'<div class="card-head">'
            f'<span class="avatar" style="background:{color}">{esc(server.name[0].upper())}</span>'
            f'<span class="who"><b>{esc(server.name)}</b>'
            f'<span class="line">{chips_html}</span></span></div>'
            f"{desc_html}{detail_html}{tools_html}",
            stylesheets=[_CARD_CSS],
        )

        card_items: list[t.Any] = [body]
        if self._server_extra is not None:
            try:
                extra = self._server_extra(server, self.editable)
            except Exception:
                logger.exception("server_extra failed for MCP server %r", server.name)
                extra = None
            if extra is not None:
                card_items.append(extra)

        if self.editable:
            enabled_switch = pmui.Switch(
                label="Enabled",
                value=server.enabled,
                margin=0,
                disabled=bool(server.locked),
                css_classes=["flowdash-mcp-toggle"],
            )
            enabled_switch.param.watch(
                lambda event, name=server.name: self.toggle_server(name, bool(event.new)),
                "value",
            )
            edit_btn = pmui.Button(
                label="Details" if server.locked else "Edit",
                variant="text",
                size="small",
                margin=0,
                css_classes=["flowdash-mcp-edit"],
            )
            edit_btn.on_click(lambda _event, name=server.name: self.open_server(name))
            test_btn = pmui.Button(
                label="Test",
                variant="text",
                size="small",
                margin=0,
                css_classes=["flowdash-mcp-test"],
            )
            test_btn.on_click(
                lambda _event, name=server.name: pn.state.execute(partial(self.test_server, name))
            )
            card_items.append(
                pn.Row(
                    enabled_switch,
                    pn.layout.HSpacer(),
                    edit_btn,
                    test_btn,
                    margin=(12, 0, 0, 0),
                    sizing_mode="stretch_width",
                    styles={"align-items": "center", "gap": "4px"},
                )
            )

        return pmui.Paper(
            *card_items,
            variant="outlined",
            margin=0,
            width=None,
            styles={"flex": "1 1 320px", "min-width": "280px", "max-width": "480px"},
            sx={
                "p": 2,
                **({"borderColor": "error.main"} if status.error and self.editable else {}),
            },
            css_classes=["flowdash-mcp-card", f"flowdash-mcp-card-{server.name}"],
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def toggle_server(self, name: str, enabled: bool) -> bool:
        """Enable or disable file-configured server *name*."""
        if not self._still_admin():
            self._show_problems([Problem(NOT_ADMIN)])
            self.refresh()
            return False
        with _FILE_LOCK:
            data = self.mcp_data()
            servers = dict(data.get("servers") or {})
            if name not in servers:
                self.refresh()
                return False
            entry = dict(servers[name])
            if enabled:
                entry.pop("enabled", None)
            else:
                entry["enabled"] = False
            servers[name] = entry
            data["servers"] = servers
            return self.write(data)

    async def test_server(self, name: str) -> MCPServerCheck:
        """Test connection to server *name*, cache its tools and refresh the card."""
        if not self._still_admin():
            self._notify("error", NOT_ADMIN)
            return MCPServerCheck(False, NOT_ADMIN)
        server = next((s for s in self.servers() if s.name == name), None)
        if server is None:
            return MCPServerCheck(False, f"MCP server {name!r} was not found.")
        result = await check_mcp_server(
            server,
            allowed_stdio=self._allowed_stdio(),
            allowed_stdio_env=self._allowed_stdio_env(),
            allowed_secret_env=self._allowed_secret_env_vars(),
            directory=self._project_directory(),
            project=self.project,
        )
        self.refresh()
        self._notify("success" if result.ok else "error", f"{name}: {result.message}")
        return result

    # ------------------------------------------------------------------
    # Edit / Add drawer
    # ------------------------------------------------------------------

    def _build_drawer(self) -> None:
        self._editing: str | None = None
        self._editing_locked: bool = False
        self._editing_url: str | None = None
        self._loading_drawer: bool = False
        self._reentered_token: bool = False
        self._reentered_headers: set[str] = set()
        self._existing_token: t.Any = None
        self._existing_headers: dict[str, t.Any] = {}
        self._existing_env: dict[str, t.Any] = {}

        self._drawer_title = pmui.Typography("", variant="h6", margin=0, sx={"fontWeight": 400})
        self._drawer_subtitle = caption("")
        close = pmui.IconButton(icon="close", description="Close", size="small", margin=0)
        close.on_click(lambda _event: self.close_server())

        self._drawer_alert = pmui.Alert(
            object="", severity="error", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._name_input = pmui.TextInput(
            label="Server id",
            placeholder="e.g. github or docs",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._transport = pmui.RadioButtonGroup(
            options={"Streamable HTTP": "http", "Stdio (local)": "stdio"},
            value="http",
            button_style="contained",
            color="primary",
            size="small",
            margin=0,
        )
        self._transport.param.watch(lambda _e: self._sync_drawer(), "value")
        self._description = pmui.TextInput(
            label="Description",
            placeholder="Short summary of what this server provides",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._enabled = pmui.Switch(label="Enabled", value=True, margin=0)
        self._offered = pmui.CheckButtonGroup(
            options={"Assistant": "assistant", "Explore": "explore"},
            value=list(SURFACES),
            size="small",
            margin=0,
            color="primary",
        )

        # HTTP fields
        self._url = pmui.TextInput(
            label="Server URL",
            placeholder="https://example.com/mcp",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._url.param.watch(lambda _e: self._sync_origin_warning(), "value")
        self._url.param.watch(lambda _e: self._sync_origin_warning(), "value_input")
        self._url_origin_warn = pmui.Alert(
            object="",
            severity="warning",
            visible=False,
            sizing_mode="stretch_width",
            margin=0,
        )
        self._token_status = caption("", visible=False)
        self._token_mode = pmui.RadioButtonGroup(
            options={"None": "none", "Environment var": "env", "Encrypted secret": "encrypted"},
            value="none",
            button_style="outlined",
            color="primary",
            size="small",
            margin=0,
        )
        self._token_mode.param.watch(lambda _e: self._sync_drawer(), "value")
        self._token_env = pmui.TextInput(
            label="Token environment variable",
            placeholder="e.g. FLOWDASH_MCP_GITHUB_TOKEN",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._token_secret = pmui.PasswordInput(
            label="Bearer token",
            sizing_mode="stretch_width",
            margin=0,
        )
        self._token_secret._log = types.MethodType(_redacted_log, self._token_secret)

        def _mark_token_reentered(event) -> None:
            if self._loading_drawer:
                return
            if (event.new or "").strip():
                self._reentered_token = True
            self._sync_origin_warning()

        self._token_env.param.watch(_mark_token_reentered, "value")
        self._token_env.param.watch(_mark_token_reentered, "value_input")
        self._token_secret.param.watch(_mark_token_reentered, "value")
        self._token_secret.param.watch(_mark_token_reentered, "value_input")
        self._token_hint = caption("", visible=False)

        # Custom HTTP headers (secret_store backed)
        self._headers_list = pn.Column(margin=0, sizing_mode="stretch_width")
        self._header_name = pmui.TextInput(
            label="Header name",
            placeholder="e.g. X-API-Key",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._header_use_env = pmui.Switch(label="Env var", value=True, margin=0)
        self._header_use_env.param.watch(lambda _e: self._sync_drawer(), "value")
        self._header_env = pmui.TextInput(
            label="Variable",
            placeholder="e.g. FLOWDASH_MCP_API_KEY",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._header_secret = pmui.PasswordInput(
            label="Secret value", sizing_mode="stretch_width", margin=0
        )
        self._header_secret._log = types.MethodType(_redacted_log, self._header_secret)
        self._add_header_btn = pmui.Button(
            label="Add header", icon="add", variant="text", size="small", margin=0
        )
        self._add_header_btn.on_click(lambda _e: self._add_secret_entry("headers"))

        self._http_box = pn.Column(
            self._url,
            self._url_origin_warn,
            self._token_status,
            self._token_mode,
            self._token_env,
            self._token_secret,
            self._token_hint,
            caption("Additional secret headers (optional)", margin=(6, 0, 0, 0)),
            self._headers_list,
            pn.Row(
                self._header_name,
                self._header_use_env,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "8px", "align-items": "center"},
            ),
            pn.Row(
                self._header_env,
                self._header_secret,
                self._add_header_btn,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "8px", "align-items": "center"},
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": "10px"},
        )

        # Stdio fields
        self._stdio_policy_alert = pmui.Alert(
            object="", severity="warning", visible=False, sizing_mode="stretch_width", margin=0
        )
        self._command = pmui.AutocompleteInput(
            label="Command",
            placeholder="e.g. /opt/mcp/docs-server",
            options=[],
            restrict=False,
            min_characters=0,
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._args = pmui.TextInput(
            label="Arguments",
            placeholder="Space-separated, e.g. --readonly --timeout 10",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._cwd = pmui.TextInput(
            label="Working directory (optional)",
            placeholder="Relative path inside the project directory",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._env_list = pn.Column(margin=0, sizing_mode="stretch_width")
        self._env_name = pmui.TextInput(
            label="Env var name",
            placeholder="e.g. DOCS_API_KEY",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._env_use_ref = pmui.Switch(label="Env ref", value=True, margin=0)
        self._env_use_ref.param.watch(lambda _e: self._sync_drawer(), "value")
        self._env_ref = pmui.TextInput(
            label="Source variable",
            placeholder="e.g. FLOWDASH_MCP_DOCS_KEY",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._env_secret = pmui.PasswordInput(
            label="Secret value", sizing_mode="stretch_width", margin=0
        )
        self._env_secret._log = types.MethodType(_redacted_log, self._env_secret)
        self._add_env_btn = pmui.Button(
            label="Add env var", icon="add", variant="text", size="small", margin=0
        )
        self._add_env_btn.on_click(lambda _e: self._add_secret_entry("env"))

        self._stdio_box = pn.Column(
            self._stdio_policy_alert,
            self._command,
            self._args,
            self._cwd,
            caption("Secret environment variables (optional)", margin=(6, 0, 0, 0)),
            self._env_list,
            pn.Row(
                self._env_name,
                self._env_use_ref,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "8px", "align-items": "center"},
            ),
            pn.Row(
                self._env_ref,
                self._env_secret,
                self._add_env_btn,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "8px", "align-items": "center"},
            ),
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": "10px"},
        )

        # Timeouts
        self._init_timeout = pmui.TextInput(
            label="Connect timeout (s)",
            placeholder=str(int(DEFAULT_INIT_TIMEOUT)),
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._timeout = pmui.TextInput(
            label="Call timeout (s)",
            placeholder=str(int(DEFAULT_TIMEOUT)),
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )

        # Auto-approve
        self._auto_approve = pmui.MultiChoice(
            label="Auto-approved tools",
            placeholder="None (all tools require approval)",
            options=[],
            value=[],
            sizing_mode="stretch_width",
            margin=0,
        )
        self._auto_pattern = pmui.TextInput(
            placeholder="Add tool name or glob, e.g. list_*",
            sizing_mode="stretch_width",
            margin=0,
            size="small",
        )
        self._add_auto_btn = pmui.Button(
            label="Add pattern", icon="add", variant="text", size="small", margin=0
        )
        self._add_auto_btn.on_click(lambda _e: self._add_auto_pattern())

        # Access rules (shared PermissionEditor; hidden when host manages access)
        self._permission_editor = PermissionEditor(
            framed=False,
            show_title=False,
            show_actions=False,
            caption_text="Leave empty to allow everyone who has access to the assistant.",
            app=self._app,
            project=self.project,
            lockout_check=self._access_lockout_reason,
        )
        self._allow_groups = self._permission_editor.allow_groups_input
        self._allow_users = self._permission_editor.allow_users_input
        self._deny_groups = self._permission_editor.deny_groups_input
        self._deny_users = self._permission_editor.deny_users_input
        self._access_box = pn.Column(
            pmui.Typography(
                "Access rules",
                variant="overline",
                sx={"color": "text.secondary", "fontSize": "0.75rem", "letterSpacing": "1.2px"},
                margin=(8, 0, 0, 0),
            ),
            self._permission_editor,
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": "8px"},
            visible=self._show_access,
        )

        self._remove_btn = pmui.Button(
            label="Remove server",
            variant="text",
            color="error",
            margin=0,
            css_classes=["flowdash-mcp-remove"],
        )
        self._remove_btn.on_click(lambda _e: self.remove_server(self._editing))
        cancel = pmui.Button(label="Cancel", variant="outlined", color="default", margin=0)
        cancel.on_click(lambda _e: self.close_server())
        self._save_btn = pmui.Button(
            label="Save", variant="contained", margin=0, css_classes=["flowdash-mcp-save"]
        )
        self._save_btn.on_click(lambda _e: self.save_server())

        def section_label(text: str):
            return pmui.Typography(
                text,
                variant="overline",
                sx={
                    "color": "text.secondary",
                    "fontSize": "0.75rem",
                    "fontWeight": 500,
                    "letterSpacing": "1.2px",
                    "lineHeight": 2,
                },
                margin=(8, 0, 0, 0),
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
            self._name_input,
            self._transport,
            self._description,
            pn.Row(
                self._enabled,
                pn.layout.HSpacer(),
                self._offered,
                margin=0,
                sizing_mode="stretch_width",
                styles={"align-items": "center", "flex-wrap": "wrap", "gap": "8px"},
            ),
            section_label("Connection"),
            self._http_box,
            self._stdio_box,
            section_label("Timeouts"),
            pn.Row(
                self._init_timeout,
                self._timeout,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px"},
            ),
            section_label("Approvals"),
            pmui.Alert(
                object=(
                    "MCP tools require user approval before running by default. "
                    "Only tools matching the list below run without asking."
                ),
                severity="info",
                sizing_mode="stretch_width",
                margin=0,
            ),
            self._auto_approve,
            pn.Row(
                self._auto_pattern,
                self._add_auto_btn,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "8px", "align-items": "center"},
            ),
            self._access_box,
            margin=0,
            sizing_mode="stretch_width",
            styles={
                "gap": "12px",
                "padding": "4px 20px 20px 20px",
                "overflow-y": "auto",
                "flex": "1 1 auto",
                "min-height": "0",
                "box-sizing": "border-box",
            },
        )
        foot = pn.Row(
            self._remove_btn,
            pn.layout.HSpacer(),
            cancel,
            self._save_btn,
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
            css_classes=["flowdash-admin-mcp-drawer"],
        )
        self._drawer = pmui.Drawer(body, anchor="right", variant="temporary", size=480, open=False)
        self._drawer.param.watch(self._on_drawer_open, "open")

    def _on_drawer_open(self, event) -> None:
        if not event.new:
            self._clear_secret_inputs()

    def _clear_secret_inputs(self) -> None:
        if self._drawer is None:
            return
        for widget in (self._token_secret, self._header_secret, self._env_secret):
            clear_text(widget)

    def _origin_changed(self, url: str | None = None) -> bool:
        if self._editing is None or self._editing_url is None:
            return False
        current = _text(self._url) if url is None else url
        return url_origin(self._editing_url) != url_origin(current)

    def _sync_origin_warning(self) -> None:
        if self._drawer is None or self._loading_drawer:
            return
        if self._transport.value != "http" or self._editing_locked:
            self._url_origin_warn.visible = False
            return
        has_unreentered_token = self._existing_token is not None and not self._reentered_token
        has_unreentered_headers = any(
            k not in self._reentered_headers for k in self._existing_headers
        )
        if self._origin_changed() and (has_unreentered_token or has_unreentered_headers):
            self._url_origin_warn.param.update(
                object=(
                    "Changing the URL scheme, host or port will drop stored token and headers "
                    "unless you re-enter them before saving."
                ),
                visible=True,
            )
        else:
            self._url_origin_warn.visible = False

    def _render_secret_entries(self, kind: str) -> None:
        entries = self._existing_headers if kind == "headers" else self._existing_env
        container = self._headers_list if kind == "headers" else self._env_list
        rows = []
        for key, value in entries.items():
            remove = pmui.IconButton(
                icon="delete_outline",
                description=f"Remove {key}",
                size="small",
                margin=0,
                disabled=self._editing_locked,
            )
            remove.on_click(lambda _e, k=key, kind=kind: self._remove_secret_entry(kind, k))
            rows.append(
                pn.Row(
                    cell(
                        f"<code>{esc(key)}</code> <span class='muted small'>{esc(_secret_summary(value))}</span>"
                    ),
                    remove,
                    margin=0,
                    sizing_mode="stretch_width",
                    styles={"align-items": "center", "gap": "6px"},
                )
            )
        container[:] = rows

    def _remove_secret_entry(self, kind: str, key: str) -> None:
        target = self._existing_headers if kind == "headers" else self._existing_env
        target.pop(key, None)
        if kind == "headers":
            self._reentered_headers.discard(key)
            self._sync_origin_warning()
        self._render_secret_entries(kind)

    def _add_secret_entry(self, kind: str) -> None:
        if self._editing_locked:
            return
        if kind == "headers":
            key = _text(self._header_name)
            use_env = self._header_use_env.value or not encryption_available()
            var_name = _text(self._header_env)
            raw_secret = _text(self._header_secret)
            target = self._existing_headers
        else:
            key = _text(self._env_name)
            use_env = self._env_use_ref.value or not encryption_available()
            var_name = _text(self._env_ref)
            raw_secret = _text(self._env_secret)
            target = self._existing_env
        if not key:
            self._drawer_alert.param.update(
                object=f"Enter a {'header' if kind == 'headers' else 'variable'} name first.",
                visible=True,
            )
            return
        if kind == "env":
            env_err = check_stdio_env([key], self._allowed_stdio_env())
            if env_err:
                self._drawer_alert.param.update(object=env_err, visible=True)
                return
        try:
            stored: t.Any = env_reference(var_name) if use_env else encrypt_secret(raw_secret)
        except SecretError as exc:
            self._drawer_alert.param.update(object=str(exc), visible=True)
            return
        ref_err = check_mcp_secret_ref(stored, f"{kind}.{key}", self._allowed_secret_env_vars())
        if ref_err:
            self._drawer_alert.param.update(object=ref_err, visible=True)
            return
        self._drawer_alert.visible = False
        target[key] = stored
        if kind == "headers":
            self._reentered_headers.add(key)
            clear_text(self._header_name)
            clear_text(self._header_env)
            clear_text(self._header_secret)
            self._sync_origin_warning()
        else:
            clear_text(self._env_name)
            clear_text(self._env_ref)
            clear_text(self._env_secret)
        self._render_secret_entries(kind)

    def _add_auto_pattern(self) -> None:
        pattern = _text(self._auto_pattern)
        if not pattern:
            return
        opts = list(dict.fromkeys([*self._auto_approve.options, pattern]))
        vals = list(dict.fromkeys([*self._auto_approve.value, pattern]))
        self._auto_approve.param.update(options=opts, value=vals)
        clear_text(self._auto_pattern)

    def _sync_drawer(self) -> None:
        if self._drawer is None:
            return
        is_http = self._transport.value == "http"
        can_encrypt = encryption_available()
        allowed = self._allowed_stdio()
        allowed_secret_env = self._allowed_secret_env_vars()
        locked = self._editing_locked

        self._http_box.visible = is_http
        self._stdio_box.visible = not is_http

        mode = self._token_mode.value
        if mode == "encrypted" and not can_encrypt:
            mode = "env"
        self._token_env.visible = is_http and mode == "env"
        self._token_secret.visible = is_http and mode == "encrypted"
        if is_http and mode == "encrypted":
            self._token_hint.object = (
                "Stored encrypted in flowdash.yaml. Leave blank while editing to keep the current "
                "token (dropped if the URL scheme, host or port changes)."
                if self._existing_token is not None
                else "Stored encrypted in flowdash.yaml and never shown again."
            )
            self._token_hint.visible = True
        elif is_http and mode == "env":
            patterns = ", ".join(allowed_secret_env) if allowed_secret_env else "none"
            self._token_hint.object = (
                f"Read from the server environment at runtime (allowed: {patterns}); "
                "never written to flowdash.yaml." + ("" if can_encrypt else f" {INSTALL_HINT}")
            )
            self._token_hint.visible = True
        else:
            self._token_hint.visible = False

        header_use_env = self._header_use_env.value or not can_encrypt
        self._header_use_env.disabled = locked or not can_encrypt
        self._header_env.visible = header_use_env
        self._header_secret.visible = not header_use_env

        env_use_ref = self._env_use_ref.value or not can_encrypt
        self._env_use_ref.disabled = locked or not can_encrypt
        self._env_ref.visible = env_use_ref
        self._env_secret.visible = not env_use_ref

        if not is_http and not locked:
            self._command.options = list(dict.fromkeys(prefix[0] for prefix in allowed))
            if not allowed:
                self._stdio_policy_alert.object = (
                    "Stdio MCP servers are disabled by server policy. Allowlist command prefixes "
                    f"with `FlowDashApp(mcp_stdio_commands=[...])` or `{STDIO_COMMANDS_ENV}`. "
                    f"Allowlist subprocess env vars with `FlowDashApp(mcp_stdio_env=[...])` or "
                    f"`{STDIO_ENV_VARS_ENV}`."
                )
                self._stdio_policy_alert.visible = True
            else:
                self._stdio_policy_alert.visible = False
        else:
            self._stdio_policy_alert.visible = False

        self._sync_origin_warning()
        self._save_btn.visible = not locked
        self._save_btn.disabled = locked or (not is_http and not allowed)

    def open_server(self, name: str | None = None) -> None:
        """Open the configuration drawer for server *name*, or a new server when None."""
        if not self.editable or self._drawer is None:
            return
        server = next((s for s in self.servers() if s.name == name), None) if name else None
        self._editing = server.name if server is not None else None
        self._editing_locked = bool(server and server.locked)
        self._editing_url = server.url if server is not None else None
        self._reentered_token = False
        self._reentered_headers = set()
        self._existing_token = server.token if server is not None else None
        self._existing_headers = dict(server.headers) if server is not None else {}
        self._existing_env = dict(server.env) if server is not None else {}
        self._clear_secret_inputs()

        check = get_mcp_check(server.name, project=self.project) if server is not None else None
        discovered = [t.name for t in check.tools] if check and check.ok else []
        auto_values = list(server.auto_approve) if server is not None else []
        auto_options = list(dict.fromkeys([*discovered, *auto_values]))

        self._loading_drawer = True
        try:
            with pn.io.hold():
                self._drawer_alert.visible = False
                self._url_origin_warn.visible = False
                if server is None:
                    self._drawer_title.object = "Add an MCP server"
                    self._drawer_subtitle.object = (
                        "Connect a streamable HTTP or allowlisted stdio MCP server"
                    )
                else:
                    self._drawer_title.object = server.name
                    self._drawer_subtitle.object = (
                        f"Configured in code ({server.locked}); read-only here."
                        if server.locked
                        else f"{'Streamable HTTP' if server.transport == 'http' else 'Stdio'} MCP server"
                    )

                locked = self._editing_locked
                self._name_input.param.update(
                    value=server.name if server else "",
                    disabled=server is not None,
                )
                self._transport.param.update(
                    value=server.transport if server else "http",
                    disabled=locked,
                )
                self._description.param.update(
                    value=server.description if server else "",
                    disabled=locked,
                )
                self._enabled.param.update(
                    value=server.enabled if server else True,
                    disabled=locked,
                )
                self._offered.param.update(
                    value=list(server.offered_to) if server else list(SURFACES),
                    disabled=locked,
                )
                self._url.param.update(
                    value=(server.url or "") if server else "",
                    disabled=locked,
                )
                if server is not None and server.token is not None:
                    self._token_status.object = f"Current token: {_secret_summary(server.token)}"
                    self._token_status.visible = True
                    if isinstance(server.token, str) and server.token.startswith("${"):
                        self._token_mode.value = "env"
                        self._token_env.value = server.token[2:-1]
                    else:
                        self._token_mode.value = "encrypted"
                        self._token_env.value = ""
                else:
                    self._token_status.visible = False
                    self._token_mode.value = "none"
                    self._token_env.value = ""
                self._token_mode.disabled = locked
                self._token_env.disabled = locked
                self._token_secret.disabled = locked

                self._command.param.update(
                    value=(server.command or "") if server else "",
                    disabled=locked,
                )
                self._args.param.update(
                    value=shlex.join(server.args) if (server and server.args) else "",
                    disabled=locked,
                )
                self._cwd.param.update(
                    value=(server.cwd or "") if server else "",
                    disabled=locked,
                )
                self._init_timeout.param.update(
                    value=f"{server.init_timeout:g}" if server else f"{DEFAULT_INIT_TIMEOUT:g}",
                    disabled=locked,
                )
                self._timeout.param.update(
                    value=f"{server.timeout:g}" if server else f"{DEFAULT_TIMEOUT:g}",
                    disabled=locked,
                )
                self._auto_approve.param.update(
                    options=auto_options,
                    value=auto_values,
                    disabled=locked,
                )
                self._auto_pattern.disabled = locked
                self._add_auto_btn.disabled = locked

                perm = server.access if server is not None else Permission()
                self._permission_editor.load(
                    perm,
                    app=self._app,
                    project=self.project,
                    disabled=locked,
                )

                self._render_secret_entries("headers")
                self._render_secret_entries("env")
                self._remove_btn.visible = server is not None and not locked
                self._sync_drawer()
                self._drawer.open = True
        finally:
            self._loading_drawer = False

    def close_server(self) -> None:
        """Close the drawer without saving and clear typed secrets."""
        if self._drawer is not None:
            self._drawer.open = False
        self._editing = None
        self._editing_locked = False
        self._editing_url = None
        self._reentered_token = False
        self._reentered_headers = set()
        self._clear_secret_inputs()

    def _drawer_payload(self) -> tuple[str | None, dict[str, t.Any] | None, list[str]]:
        problems: list[str] = []
        name = self._editing or _text(self._name_input)
        name_err = validate_server_name(name)
        if name_err:
            problems.append(name_err)

        transport = self._transport.value
        entry: dict[str, t.Any] = {"transport": transport}
        allowed_secret_env = self._allowed_secret_env_vars()

        if transport == "http":
            url = _text(self._url)
            if url_origin(url) is None:
                problems.append("Server URL must be an http:// or https:// URL with a host.")
            else:
                entry["url"] = url
            origin_changed = self._origin_changed(url)
            mode = self._token_mode.value
            if mode == "env":
                if not origin_changed or self._reentered_token:
                    var_name = _text(self._token_env)
                    try:
                        ref = env_reference(var_name)
                    except SecretError as exc:
                        problems.append(str(exc))
                    else:
                        ref_err = check_mcp_secret_ref(ref, "token", allowed_secret_env)
                        if ref_err:
                            problems.append(ref_err)
                        else:
                            entry["token"] = ref
            elif mode == "encrypted":
                typed = _text(self._token_secret)
                if typed:
                    try:
                        entry["token"] = encrypt_secret(typed)
                    except SecretError as exc:
                        problems.append(str(exc))
                elif origin_changed:
                    # Drop the stored token when the URL scheme, host or port changed unless re-entered.
                    pass
                elif isinstance(self._existing_token, Mapping):
                    entry["token"] = dict(self._existing_token)
                else:
                    problems.append(
                        "Enter a bearer token or switch to environment variable / None."
                    )
            kept_headers = (
                {k: v for k, v in self._existing_headers.items() if k in self._reentered_headers}
                if origin_changed
                else dict(self._existing_headers)
            )
            for key, val in kept_headers.items():
                ref_err = check_mcp_secret_ref(val, f"headers.{key}", allowed_secret_env)
                if ref_err:
                    problems.append(ref_err)
            if kept_headers:
                entry["headers"] = kept_headers
        else:
            command = _text(self._command)
            parsed_args: list[str] = []
            raw_args = _text(self._args)
            if raw_args:
                try:
                    parsed_args = shlex.split(raw_args)
                except ValueError as exc:
                    problems.append(f"Arguments could not be parsed: {exc}")
            cmd_err = check_stdio_command(command, self._allowed_stdio(), args=parsed_args)
            if cmd_err:
                problems.append(cmd_err)
            else:
                entry["command"] = command
                if parsed_args:
                    entry["args"] = parsed_args
            cwd = _text(self._cwd)
            if cwd:
                entry["cwd"] = cwd
            if self._existing_env:
                env_err = check_stdio_env(self._existing_env, self._allowed_stdio_env())
                if env_err:
                    problems.append(env_err)
                for key, val in self._existing_env.items():
                    ref_err = check_mcp_secret_ref(val, f"env.{key}", allowed_secret_env)
                    if ref_err:
                        problems.append(ref_err)
                entry["env"] = dict(self._existing_env)

        desc = _text(self._description)
        if desc:
            entry["description"] = desc
        if not self._enabled.value:
            entry["enabled"] = False
        offered = [s for s in SURFACES if s in self._offered.value]
        if offered != list(SURFACES):
            entry["offered_to"] = offered

        init_t, err = _positive_float(
            _text(self._init_timeout), "Connect timeout", DEFAULT_INIT_TIMEOUT
        )
        if err:
            problems.append(err)
        elif init_t is not None and init_t != DEFAULT_INIT_TIMEOUT:
            entry["init_timeout"] = init_t

        call_t, err = _positive_float(_text(self._timeout), "Call timeout", DEFAULT_TIMEOUT)
        if err:
            problems.append(err)
        elif call_t is not None and call_t != DEFAULT_TIMEOUT:
            entry["timeout"] = call_t

        if self._auto_approve.value:
            entry["auto_approve"] = list(self._auto_approve.value)

        if self._show_access:
            acc = access_to_dict(self._permission_editor.permission())
            if acc:
                entry["access"] = acc
        elif self._editing is not None:
            # Preserve existing access / tool_access managed by a host app.
            existing = (self.mcp_data().get("servers") or {}).get(self._editing) or {}
            for key in ("access", "tool_access"):
                if key in existing:
                    entry[key] = copy.deepcopy(existing[key])

        if self._editing is not None:
            existing = (self.mcp_data().get("servers") or {}).get(self._editing) or {}
            if "tool_access" in existing and "tool_access" not in entry:
                entry["tool_access"] = copy.deepcopy(existing["tool_access"])

        return (None if problems else name), (None if problems else entry), problems

    def save_server(self) -> bool:
        """Validate and save the drawer's MCP server to ``flowdash.yaml``."""
        if not self.editable or self._editing_locked:
            return False
        if not self._still_admin():
            self._drawer_alert.param.update(object=NOT_ADMIN, visible=True)
            return False
        name, entry, problems = self._drawer_payload()
        if name is None or entry is None:
            self._drawer_alert.param.update(object="\n\n".join(problems), visible=True)
            return False
        with _FILE_LOCK:
            data = self.mcp_data()
            servers = dict(data.get("servers") or {})
            if self._editing is None and name in {s.name for s in self.servers()}:
                self._drawer_alert.param.update(
                    object=f"An MCP server named {name!r} already exists.", visible=True
                )
                return False
            servers[name] = entry
            data["servers"] = servers
            if not self.write(data):
                self._drawer_alert.param.update(object=self._alert.object, visible=True)
                self._alert.visible = False
                return False
        user = self._app._identity.user if self._app is not None else "someone"
        logger.info("Admin: %s saved MCP server %r (%s).", user, name, entry["transport"])
        clear_mcp_check(name, project=self.project)
        self.close_server()
        return True

    def remove_server(self, name: str | None) -> bool:
        """Remove file-configured MCP server *name* from ``flowdash.yaml``."""
        if not self.editable or not name:
            return False
        if not self._still_admin():
            self._drawer_alert.param.update(object=NOT_ADMIN, visible=True)
            return False
        with _FILE_LOCK:
            data = self.mcp_data()
            servers = dict(data.get("servers") or {})
            if name not in servers:
                return False
            del servers[name]
            data["servers"] = servers
            ok = self.write(data)
        if ok:
            user = self._app._identity.user if self._app is not None else "someone"
            logger.info("Admin: %s removed MCP server %r.", user, name)
            clear_mcp_check(name, project=self.project)
            self.close_server()
        return ok

    def __panel__(self):
        """Render the MCP settings view."""
        return self._layout


def mcp_settings(project: Project, editable: bool) -> MCPSettings:
    """Build the MCP section of the admin view."""
    return MCPSettings(project, editable)


MCP = ProjectSection(
    MCP_SECTION,
    load_mcp,
    title="MCP",
    icon="hub",
    description=(
        "Connect Model Context Protocol (MCP) servers to give the assistant and Explore "
        "extra tools. MCP tools require user approval by default."
    ),
    dump=dump_mcp,
    settings_view=mcp_settings,
    group="App",
)

register_section(MCP)

__all__ = ["MCP", "MCPSettings", "ServerStatus", "mcp_settings", "server_status"]
