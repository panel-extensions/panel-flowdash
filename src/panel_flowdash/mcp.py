"""The project file's ``mcp`` section: MCP servers, credentials, stdio policy and access rules.

```yaml
mcp:
  servers:
    github:
      transport: http
      url: https://api.githubcopilot.com/mcp/
      token: ${FLOWDASH_MCP_GITHUB_TOKEN}
      description: GitHub issues and pull requests
      auto_approve: [list_issues, get_*]
      access:
        allow_groups: [engineering]
    docs:
      transport: stdio
      command: /opt/mcp/docs-server
      args: ["--readonly"]
      env:
        DOCS_API_KEY: ${FLOWDASH_MCP_DOCS_KEY}
```

Every credential (``token``, ``headers`` values and ``env`` values) is stored
through :mod:`panel_flowdash.secret_store`: either a ``${VARIABLE}`` reference
or a Fernet-encrypted value saved from the admin UI. Plaintext secrets in
``flowdash.yaml`` are rejected. ``${VARIABLE}`` references in ``mcp:`` are
restricted to operator-allowlisted names/patterns, defaulting to
``FLOWDASH_MCP_*``.

``stdio`` servers configured in ``flowdash.yaml`` or the UI are denied by
default unless the server operator explicitly allowlists full argv prefixes via
``FlowDashApp.mcp_stdio_commands`` or ``FLOWDASH_MCP_STDIO_COMMANDS``, and
permitted subprocess environment variable names via
``FlowDashApp.mcp_stdio_env`` or ``FLOWDASH_MCP_STDIO_ENV``. Allowlisting a
package launcher or interpreter without a pinned subcommand/script prefix
grants arbitrary code execution to anyone who can edit ``flowdash.yaml`` or is
an app admin.

This module needs nothing beyond panel-flowdash itself; pydantic-ai and the
MCP client library are imported lazily when an agent session or connection
test runs.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
import math
import os
import re
import shlex
import shutil
import typing as t
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from panel_flowdash.auth import Identity, Permission, evaluate_permission
from panel_flowdash.project import (
    ProjectError,
    Section,
    SectionError,
    current_section,
)
from panel_flowdash.secret_store import SecretError, check_secret, describe_secret, resolve_secret
from panel_flowdash.value_text import error_text

logger = logging.getLogger(__name__)

MCP_SECTION = "mcp"

STDIO_COMMANDS_ENV = "FLOWDASH_MCP_STDIO_COMMANDS"

STDIO_ENV_VARS_ENV = "FLOWDASH_MCP_STDIO_ENV"

SECRET_ENV_VARS_ENV = "FLOWDASH_MCP_SECRET_ENV_VARS"

DEFAULT_SECRET_ENV_VARS: tuple[str, ...] = ("FLOWDASH_MCP_*",)

DEFAULT_INIT_TIMEOUT = 10.0

DEFAULT_TIMEOUT = 30.0

MAX_TOOL_NAME_LENGTH = 64

TRANSPORTS = ("http", "stdio")

SURFACES = ("assistant", "explore")

ACCESS_KEYS = ("allow_users", "allow_groups", "deny_users", "deny_groups")

MCP_EXTRA_MISSING = (
    "MCP server support needs the mcp extra: pip install 'panel-flowdash[mcp]' "
    "(or 'pydantic-ai-slim[mcp]')."
)

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]|_(?!_))*$")
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_COMMON_KEYS = (
    "transport",
    "description",
    "enabled",
    "offered_to",
    "init_timeout",
    "timeout",
    "auto_approve",
    "access",
    "tool_access",
)
_HTTP_KEYS = (*_COMMON_KEYS, "url", "token", "headers")
_STDIO_KEYS = (*_COMMON_KEYS, "command", "args", "env", "cwd")


def _positive_number(value: t.Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def validate_server_name(name: t.Any) -> str | None:
    """Return why *name* is not a valid MCP server id, or None."""
    if not isinstance(name, str) or not name:
        return "A server id is a non-empty name."
    if "__" in name:
        return "A server id may not contain `__`."
    if not _SERVER_NAME_RE.match(name):
        return (
            "A server id starts with a letter or digit and uses only letters, digits, "
            "hyphens and single underscores."
        )
    return None


def mcp_tool_name(server: str, tool: str) -> str:
    """Return the model-facing tool name for *tool* on MCP *server*."""
    return f"mcp_{server}__{tool}"


def is_valid_mcp_tool_name(server: str, raw_tool: str) -> bool:
    """Return whether ``mcp_<server>__<raw_tool>`` matches provider tool-name rules."""
    if not isinstance(raw_tool, str) or not raw_tool:
        return False
    prefixed = mcp_tool_name(server, raw_tool)
    return len(prefixed) <= MAX_TOOL_NAME_LENGTH and bool(_TOOL_NAME_RE.match(prefixed))


def parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Split a model-facing MCP tool name into ``(server, tool)``, or None if not an MCP tool."""
    if not isinstance(name, str) or not name.startswith("mcp_"):
        return None
    rest = name[4:]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


def is_mcp_tool(name: str) -> bool:
    """Return whether *name* is a prefixed MCP tool name."""
    return parse_mcp_tool_name(name) is not None


def url_origin(url: str | None) -> tuple[str, str, int] | None:
    """Return normalized ``(scheme, host, port)`` for an http(s) URL, or None."""
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


def _check_access_mapping(value: t.Any) -> tuple[Permission, list[tuple[tuple, str, bool]]]:
    """Validate an ``access`` mapping and return ``(Permission, problems)``."""
    if value is None:
        return Permission(), []
    if not isinstance(value, Mapping):
        return Permission(), [
            (
                (),
                "`access` is a mapping with `allow_users`, `allow_groups`, `deny_users` "
                "and/or `deny_groups`.",
                False,
            )
        ]
    problems: list[tuple[tuple, str, bool]] = []
    kwargs: dict[str, list[str]] = {}
    for key, item in value.items():
        if key not in ACCESS_KEYS:
            known = ", ".join(f"`{k}`" for k in ACCESS_KEYS)
            problems.append(((key,), f"Unknown access key {key!r}; known: {known}.", True))
            continue
        if item is None:
            continue
        if isinstance(item, str):
            if not item.strip():
                problems.append(((key,), f"`{key}` entries must be non-empty text.", False))
                continue
            kwargs[key] = [item.strip()]
        elif isinstance(item, list) and all(isinstance(v, str) and v.strip() for v in item):
            kwargs[key] = [v.strip() for v in item]
        else:
            problems.append(((key,), f"`{key}` is a list of names.", False))
    return Permission.from_spec(**kwargs), problems


def access_to_dict(permission: Permission | None) -> dict[str, list[str]]:
    """Serialize *permission*'s four access fields to a sparse dict for ``flowdash.yaml``."""
    if permission is None or permission.is_empty:
        return {}
    full = permission.to_dict()
    return {key: full[key] for key in ACCESS_KEYS if full[key]}


def resolve_cwd(cwd: str | None, directory: Path | None) -> Path | None:
    """Resolve *cwd* relative to *directory*, returning None if it escapes *directory*."""
    if not cwd:
        return directory.resolve() if directory is not None else None
    if directory is None:
        return None
    root = directory.resolve()
    candidate = Path(cwd)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    return resolved if resolved.is_relative_to(root) else None


def _has_path_separator(value: str) -> bool:
    return "/" in value or "\\" in value


def split_command(text: str, *, windows: bool | None = None) -> list[str]:
    """Split a command line into argv, keeping Windows paths' backslashes on Windows."""
    if not (os.name == "nt" if windows is None else windows):
        return shlex.split(text)
    # POSIX shlex treats backslash as an escape and would turn C:\tools\x.exe into C:toolsx.exe.
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def parse_stdio_prefix(entry: str | Sequence[str]) -> tuple[str, ...] | None:
    """Parse one stdio allowlist entry (command string or argv sequence) into an argv prefix."""
    if isinstance(entry, str):
        text = entry.strip()
        if not text:
            return None
        try:
            tokens = tuple(split_command(text))
        except ValueError:
            return None
        return tokens or None
    if isinstance(entry, Sequence):
        tokens = tuple(item.strip() for item in entry if isinstance(item, str) and item.strip())
        return tokens or None
    return None


def format_stdio_prefix(entry: str | Sequence[str]) -> str:
    """Format a stdio allowlist prefix for display in messages and UI."""
    tokens = parse_stdio_prefix(entry)
    return shlex.join(tokens) if tokens else str(entry)


def allowed_stdio_commands(
    configured: Sequence[str | Sequence[str]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Return allowlisted argv prefixes for file/UI-configured stdio MCP servers.

    A non-None *configured* wins, even when empty; otherwise
    ``FLOWDASH_MCP_STDIO_COMMANDS`` is read as a comma-separated list of
    command prefixes.
    """
    raw_items: Sequence[str | Sequence[str]]
    if configured is not None:
        raw_items = configured
    else:
        env = os.environ if environ is None else environ
        raw = env.get(STDIO_COMMANDS_ENV, "")
        raw_items = [item.strip() for item in raw.split(",") if item.strip()]
    prefixes: list[tuple[str, ...]] = []
    for item in raw_items:
        parsed = parse_stdio_prefix(item)
        if parsed is None:
            continue
        if _has_path_separator(parsed[0]) and not Path(parsed[0]).is_absolute():
            logger.warning(
                "Ignoring relative stdio allowlist entry %r; path entries must be absolute.",
                item,
            )
            continue
        prefixes.append(parsed)
    return tuple(prefixes)


def check_stdio_command(
    command: str,
    allowed: Sequence[str | Sequence[str]],
    args: Sequence[str] = (),
) -> str | None:
    """Return why ``(command, args)`` is not allowed under *allowed* argv prefixes, or None."""
    if not isinstance(command, str) or not command.strip():
        return "A stdio server needs a command."
    prefixes: list[tuple[str, ...]] = []
    for entry in allowed:
        parsed = parse_stdio_prefix(entry)
        if parsed is None:
            continue
        if _has_path_separator(parsed[0]) and not Path(parsed[0]).is_absolute():
            continue
        prefixes.append(parsed)
    if not prefixes:
        return (
            "Stdio MCP servers are disabled by server policy. Allowlist command prefixes with "
            f"`FlowDashApp(mcp_stdio_commands=[...])` or `{STDIO_COMMANDS_ENV}`."
        )
    cmd = command.strip()
    if _has_path_separator(cmd) and not Path(cmd).is_absolute():
        return f"Stdio command {cmd!r} must be an absolute path or a bare executable name."
    argv_args = tuple(args)
    for prefix in prefixes:
        prefix_cmd, *prefix_args = prefix
        if _has_path_separator(prefix_cmd):
            if not _has_path_separator(cmd):
                continue
            try:
                if Path(cmd).resolve() != Path(prefix_cmd).resolve():
                    continue
            except OSError:
                continue
        else:
            if _has_path_separator(cmd) or cmd != prefix_cmd:
                continue
        if len(argv_args) >= len(prefix_args) and argv_args[: len(prefix_args)] == tuple(
            prefix_args
        ):
            return None
    listed = ", ".join(f"`{shlex.join(p)}`" for p in prefixes)
    full = shlex.join([cmd, *argv_args])
    return f"Command `{full}` does not match an allowed stdio prefix ({listed})."


def resolve_stdio_executable(
    command: str,
    allowed: Sequence[str | Sequence[str]],
    args: Sequence[str] = (),
) -> str:
    """Validate ``(command, args)`` against *allowed* and return the resolved executable path."""
    problem = check_stdio_command(command, allowed, args=args)
    if problem is not None:
        raise PermissionError(problem)
    cmd = command.strip()
    if _has_path_separator(cmd):
        resolved = Path(cmd).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Stdio MCP command {cmd!r} does not exist.")
        return str(resolved)
    found = shutil.which(cmd)
    if found is None:
        raise FileNotFoundError(f"Stdio MCP command {cmd!r} was not found on PATH.")
    return found


def allowed_stdio_env(
    configured: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the allowlisted subprocess environment variable names for file/UI stdio servers.

    A non-None *configured* wins, even when empty; otherwise
    ``FLOWDASH_MCP_STDIO_ENV`` is read as a comma-separated list.
    """
    if configured is not None:
        return frozenset(
            item.strip() for item in configured if isinstance(item, str) and item.strip()
        )
    env = os.environ if environ is None else environ
    raw = env.get(STDIO_ENV_VARS_ENV, "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def check_stdio_env(env_names: Iterable[str], allowed: Iterable[str]) -> str | None:
    """Return why *env_names* are not allowed under *allowed* stdio env names, or None."""
    names = [name for name in env_names if name]
    if not names:
        return None
    allowed_set = frozenset(
        item.strip() for item in allowed if isinstance(item, str) and item.strip()
    )
    if not allowed_set:
        return (
            "Stdio MCP environment variables are disabled by server policy. Allowlist variable "
            f"names with `FlowDashApp(mcp_stdio_env=[...])` or `{STDIO_ENV_VARS_ENV}`."
        )
    disallowed = sorted(name for name in names if name not in allowed_set)
    if disallowed:
        bad = ", ".join(f"`{n}`" for n in disallowed)
        permitted = ", ".join(f"`{n}`" for n in sorted(allowed_set))
        return f"Stdio environment variable(s) {bad} are not in the allowed list ({permitted})."
    return None


def allowed_secret_env_vars(
    configured: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return glob patterns/names of env vars allowed in ``mcp:`` ``${VAR}`` references.

    A non-None *configured* wins, even when empty; otherwise
    ``FLOWDASH_MCP_SECRET_ENV_VARS`` is read (comma-separated), defaulting to
    ``("FLOWDASH_MCP_*",)``.
    """
    if configured is not None:
        return tuple(item.strip() for item in configured if isinstance(item, str) and item.strip())
    env = os.environ if environ is None else environ
    if SECRET_ENV_VARS_ENV in env:
        raw = env[SECRET_ENV_VARS_ENV]
        return tuple(item.strip() for item in raw.split(",") if item.strip())
    return DEFAULT_SECRET_ENV_VARS


def check_mcp_secret_ref(
    value: t.Any,
    what: str,
    allowed_env_vars: Sequence[str] | None = None,
) -> str | None:
    """Validate a secret in ``mcp:`` and enforce allowed ``${VAR}`` variable names."""
    msg = check_secret(value, what)
    if msg is not None:
        return msg
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        patterns = (
            allowed_secret_env_vars(None) if allowed_env_vars is None else tuple(allowed_env_vars)
        )
        if not any(fnmatch.fnmatchcase(var_name, pat) for pat in patterns):
            if patterns:
                listed = ", ".join(f"`{p}`" for p in patterns)
                return (
                    f"`{what}` references `${{{var_name}}}`, which is not allowed in `mcp:` "
                    f"(allowed: {listed})."
                )
            return (
                f"`{what}` references `${{{var_name}}}`, but environment variable references "
                "are disabled for `mcp:`."
            )
    return None


@dataclass(frozen=True)
class MCPToolInfo:
    """Metadata about one tool discovered from an MCP server."""

    name: str
    description: str = ""
    annotations: Mapping[str, t.Any] | None = None


@dataclass(frozen=True)
class MCPServerCheck:
    """Result of the last connection check or tool discovery for an MCP server."""

    ok: bool
    message: str
    tools: tuple[MCPToolInfo, ...] = ()
    when: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


# Process-level cache of last test / discovery result keyed by (project_key, server_name).
MCP_CHECKS: dict[tuple[str, str], MCPServerCheck] = {}


def project_check_key(project: t.Any = None, directory: Path | None = None) -> str:
    """Return a stable cache scope key for *project* or *directory*."""
    if project is not None:
        path = getattr(project, "path", None)
        if path:
            try:
                return str(Path(path).resolve())
            except OSError:
                return str(path)
        proj_dir = getattr(project, "directory", None)
        if proj_dir:
            try:
                return str(Path(proj_dir).resolve())
            except OSError:
                return str(proj_dir)
        return f"project:{id(project)}"
    if directory is not None:
        try:
            return str(Path(directory).resolve())
        except OSError:
            return str(directory)
    return ""


def mcp_check_key(
    server_name: str,
    *,
    project: t.Any = None,
    directory: Path | None = None,
    project_key: str | None = None,
) -> tuple[str, str]:
    """Return the :data:`MCP_CHECKS` key for *server_name* scoped to *project*."""
    scope = project_key if project_key is not None else project_check_key(project, directory)
    return (scope, server_name)


def get_mcp_check(
    server_name: str,
    *,
    project: t.Any = None,
    directory: Path | None = None,
    project_key: str | None = None,
) -> MCPServerCheck | None:
    """Return the cached :class:`MCPServerCheck` for *server_name* in *project*, or None."""
    return MCP_CHECKS.get(
        mcp_check_key(server_name, project=project, directory=directory, project_key=project_key)
    )


def set_mcp_check(
    server_name: str,
    check: MCPServerCheck,
    *,
    project: t.Any = None,
    directory: Path | None = None,
    project_key: str | None = None,
) -> None:
    """Store *check* for *server_name* scoped to *project*."""
    MCP_CHECKS[
        mcp_check_key(server_name, project=project, directory=directory, project_key=project_key)
    ] = check


def clear_mcp_check(
    server_name: str,
    *,
    project: t.Any = None,
    directory: Path | None = None,
    project_key: str | None = None,
) -> None:
    """Remove any cached :class:`MCPServerCheck` for *server_name* in *project*."""
    MCP_CHECKS.pop(
        mcp_check_key(server_name, project=project, directory=directory, project_key=project_key),
        None,
    )


@dataclass(frozen=True)
class MCPServerConfig:
    """Configuration of one MCP server, from ``flowdash.yaml`` or code."""

    name: str
    transport: t.Literal["http", "stdio"] = "http"
    url: str | None = None
    token: t.Any = None
    headers: Mapping[str, t.Any] = field(default_factory=dict)
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, t.Any] = field(default_factory=dict)
    cwd: str | None = None
    description: str = ""
    enabled: bool = True
    offered_to: tuple[str, ...] = SURFACES
    init_timeout: float = DEFAULT_INIT_TIMEOUT
    timeout: float = DEFAULT_TIMEOUT
    auto_approve: tuple[str, ...] = ()
    access: Permission = field(default_factory=Permission)
    tool_access: Mapping[str, Permission] = field(default_factory=dict)
    locked: str | None = None
    toolset: t.Any = None

    def is_auto_approved(self, raw_tool: str) -> bool:
        """Whether *raw_tool* on this server skips human approval."""
        return any(fnmatch.fnmatchcase(raw_tool, pattern) for pattern in self.auto_approve)

    def offered(self, surface: str) -> bool:
        """Whether this server is offered on *surface* (``assistant`` or ``explore``)."""
        return self.enabled and surface in self.offered_to

    def secret_problems(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        allowed_secret_env: Sequence[str] | None = None,
    ) -> list[str]:
        """Return problems resolving this server's configured secrets, without leaking values."""
        problems: list[str] = []
        check_refs = not self.locked
        if self.token is not None:
            ref_err = (
                check_mcp_secret_ref(self.token, "token", allowed_secret_env)
                if check_refs
                else None
            )
            if ref_err:
                problems.append(ref_err)
            else:
                info = describe_secret(self.token, environ)
                if info.error:
                    problems.append(f"token: {info.error}")
        for key, value in self.headers.items():
            ref_err = (
                check_mcp_secret_ref(value, f"headers.{key}", allowed_secret_env)
                if check_refs
                else None
            )
            if ref_err:
                problems.append(ref_err)
            else:
                info = describe_secret(value, environ)
                if info.error:
                    problems.append(f"header {key}: {info.error}")
        for key, value in self.env.items():
            ref_err = (
                check_mcp_secret_ref(value, f"env.{key}", allowed_secret_env)
                if check_refs
                else None
            )
            if ref_err:
                problems.append(ref_err)
            else:
                info = describe_secret(value, environ)
                if info.error:
                    problems.append(f"env {key}: {info.error}")
        return problems

    def to_data(self) -> dict[str, t.Any]:
        """Return the plain YAML representation of this server for ``flowdash.yaml``."""
        data: dict[str, t.Any] = {"transport": self.transport}
        if self.transport == "http":
            if self.url:
                data["url"] = self.url
            if self.token is not None:
                data["token"] = self.token
            if self.headers:
                data["headers"] = dict(self.headers)
        else:
            if self.command:
                data["command"] = self.command
            if self.args:
                data["args"] = list(self.args)
            if self.cwd:
                data["cwd"] = self.cwd
            if self.env:
                data["env"] = dict(self.env)
        if self.description:
            data["description"] = self.description
        if not self.enabled:
            data["enabled"] = False
        if tuple(self.offered_to) != SURFACES:
            data["offered_to"] = list(self.offered_to)
        if self.init_timeout != DEFAULT_INIT_TIMEOUT:
            data["init_timeout"] = self.init_timeout
        if self.timeout != DEFAULT_TIMEOUT:
            data["timeout"] = self.timeout
        if self.auto_approve:
            data["auto_approve"] = list(self.auto_approve)
        access = access_to_dict(self.access)
        if access:
            data["access"] = access
        if self.tool_access:
            tool_access = {
                pattern: rule
                for pattern, perm in self.tool_access.items()
                if (rule := access_to_dict(perm))
            }
            if tool_access:
                data["tool_access"] = tool_access
        return data


def _resolve_mcp_secret(
    value: t.Any,
    what: str,
    environ: Mapping[str, str] | None,
    *,
    locked: str | None,
    allowed_secret_env: Sequence[str] | None,
) -> str | None:
    if value is None:
        return None
    if not locked:
        err = check_mcp_secret_ref(value, what, allowed_secret_env)
        if err is not None:
            raise SecretError(err)
    return resolve_secret(value, environ)


def resolve_http_headers(
    config: MCPServerConfig,
    environ: Mapping[str, str] | None = None,
    *,
    allowed_secret_env: Sequence[str] | None = None,
) -> dict[str, str]:
    """Resolve HTTP headers and optional Bearer token for *config* lazily."""
    headers: dict[str, str] = {}
    for key, raw in config.headers.items():
        resolved = _resolve_mcp_secret(
            raw,
            f"headers.{key}",
            environ,
            locked=config.locked,
            allowed_secret_env=allowed_secret_env,
        )
        if resolved is not None:
            headers[key] = resolved
    if config.token is not None and not any(k.lower() == "authorization" for k in headers):
        resolved_token = _resolve_mcp_secret(
            config.token,
            "token",
            environ,
            locked=config.locked,
            allowed_secret_env=allowed_secret_env,
        )
        if resolved_token:
            headers["Authorization"] = f"Bearer {resolved_token}"
    return headers


def resolve_stdio_env(
    config: MCPServerConfig,
    environ: Mapping[str, str] | None = None,
    *,
    allowed_env: Iterable[str] | None = None,
    allowed_secret_env: Sequence[str] | None = None,
) -> dict[str, str] | None:
    """Resolve stdio environment variables for *config* lazily, returning None if none set."""
    if not config.env:
        return None
    if not config.locked and allowed_env is not None:
        err = check_stdio_env(config.env, allowed_env)
        if err is not None:
            raise PermissionError(err)
    resolved: dict[str, str] = {}
    for key, raw in config.env.items():
        value = _resolve_mcp_secret(
            raw,
            f"env.{key}",
            environ,
            locked=config.locked,
            allowed_secret_env=allowed_secret_env,
        )
        if value is not None:
            resolved[key] = value
    return resolved or None


def mcp_server_authorized(
    server: MCPServerConfig,
    identity: Identity | None,
    *,
    is_admin: bool = False,
) -> bool:
    """Whether *identity* may access *server* under its server-level ``access`` rule."""
    return evaluate_permission(server.access, identity, is_admin=is_admin)


def mcp_tool_authorized(
    server: MCPServerConfig,
    raw_tool: str,
    identity: Identity | None,
    *,
    is_admin: bool = False,
) -> bool:
    """Whether *identity* may access *raw_tool* on *server* (server + matching tool rules)."""
    if not mcp_server_authorized(server, identity, is_admin=is_admin):
        return False
    for pattern, perm in server.tool_access.items():
        if fnmatch.fnmatchcase(raw_tool, pattern):
            if not evaluate_permission(perm, identity, is_admin=is_admin):
                return False
    return True


def _project_directory(section: Section) -> Path | None:
    project = section.project
    if project.directory is not None:
        return project.directory
    project_dir = project.context.get("project_dir") if project.context else None
    return Path(project_dir) if project_dir is not None else None


def _section_allowed_secret_env(section: Section) -> tuple[str, ...]:
    ctx = section.context or {}
    if "mcp_secret_env_vars" in ctx and ctx["mcp_secret_env_vars"] is not None:
        return allowed_secret_env_vars(ctx["mcp_secret_env_vars"])
    return allowed_secret_env_vars(None)


def _check_secret_mapping(
    section: Section,
    base_path: tuple[str | int, ...],
    value: t.Any,
    label: str,
    *,
    env_names: bool = False,
    allowed_env_vars: Sequence[str] | None = None,
) -> list:
    problems = []
    if value is None:
        return problems
    if not isinstance(value, Mapping):
        return [section.problem(base_path, f"`{label}` is a mapping of names to secrets.")]
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            problems.append(
                section.problem((*base_path, key), f"`{label}` keys must be text.", True)
            )
            continue
        if env_names and not _ENV_NAME_RE.match(key):
            problems.append(
                section.problem(
                    (*base_path, key),
                    f"{key!r} is not a valid environment variable name.",
                    True,
                )
            )
            continue
        msg = check_mcp_secret_ref(item, f"{label}.{key}", allowed_env_vars)
        if msg:
            problems.append(section.problem((*base_path, key), msg))
    return problems


def _server_problems(section: Section, name: str, config: t.Any) -> list:
    path: tuple[str | int, ...] = ("servers", name)
    name_err = validate_server_name(name)
    if name_err is not None:
        return [section.problem(path, name_err, True)]
    if not isinstance(config, Mapping):
        return [section.problem(path, "An MCP server is a mapping with `transport`.")]
    problems = []
    allowed_env_vars = _section_allowed_secret_env(section)
    transport = config.get("transport")
    if transport not in TRANSPORTS:
        known = ", ".join(f"`{tr}`" for tr in TRANSPORTS)
        problems.append(section.problem((*path, "transport"), f"`transport` is one of {known}."))
        allowed_keys = set(_HTTP_KEYS) | set(_STDIO_KEYS)
    else:
        allowed_keys = set(_HTTP_KEYS if transport == "http" else _STDIO_KEYS)

    for key in config:
        if key not in allowed_keys:
            known = ", ".join(f"`{k}`" for k in sorted(allowed_keys))
            problems.append(
                section.problem((*path, key), f"Unknown key {key!r}; known: {known}.", True)
            )

    if transport == "http":
        url = config.get("url")
        if url_origin(url) is None:
            problems.append(section.problem((*path, "url"), "`url` is an http(s) URL."))
        if "token" in config:
            msg = check_mcp_secret_ref(config.get("token"), "token", allowed_env_vars)
            if msg:
                problems.append(section.problem((*path, "token"), msg))
        problems.extend(
            _check_secret_mapping(
                section,
                (*path, "headers"),
                config.get("headers"),
                "headers",
                allowed_env_vars=allowed_env_vars,
            )
        )
    elif transport == "stdio":
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            problems.append(section.problem((*path, "command"), "`command` names an executable."))
        elif _has_path_separator(command.strip()) and not Path(command.strip()).is_absolute():
            problems.append(
                section.problem(
                    (*path, "command"),
                    "`command` must be an absolute path or a bare executable name.",
                )
            )
        args = config.get("args")
        if args is not None and (
            not isinstance(args, list) or not all(isinstance(a, str) for a in args)
        ):
            problems.append(
                section.problem((*path, "args"), "`args` is a list of text arguments.")
            )
        cwd = config.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd.strip():
                problems.append(
                    section.problem(
                        (*path, "cwd"), "`cwd` is a relative path inside the project directory."
                    )
                )
            else:
                directory = _project_directory(section)
                if Path(cwd).is_absolute() or (
                    directory is not None and resolve_cwd(cwd, directory) is None
                ):
                    problems.append(
                        section.problem(
                            (*path, "cwd"),
                            "`cwd` must stay inside the project directory.",
                        )
                    )
        problems.extend(
            _check_secret_mapping(
                section,
                (*path, "env"),
                config.get("env"),
                "env",
                env_names=True,
                allowed_env_vars=allowed_env_vars,
            )
        )

    if config.get("description") is not None and not isinstance(config["description"], str):
        problems.append(section.problem((*path, "description"), "`description` is text."))
    if "enabled" in config and not isinstance(config["enabled"], bool):
        problems.append(section.problem((*path, "enabled"), "`enabled` is true or false."))
    offered = config.get("offered_to", list(SURFACES))
    if not isinstance(offered, list) or any(s not in SURFACES for s in offered):
        problems.append(
            section.problem(
                (*path, "offered_to"), "`offered_to` lists `assistant` and/or `explore`."
            )
        )
    for timeout_key in ("init_timeout", "timeout"):
        if timeout_key in config and not _positive_number(config[timeout_key]):
            problems.append(
                section.problem(
                    (*path, timeout_key), f"`{timeout_key}` is a positive number of seconds."
                )
            )
    auto_approve = config.get("auto_approve")
    if auto_approve is not None and (
        not isinstance(auto_approve, list)
        or not all(isinstance(p, str) and p.strip() for p in auto_approve)
    ):
        problems.append(
            section.problem(
                (*path, "auto_approve"), "`auto_approve` is a list of tool names or patterns."
            )
        )
    if "access" in config:
        _, acc_problems = _check_access_mapping(config["access"])
        for sub, message, is_key in acc_problems:
            problems.append(section.problem((*path, "access", *sub), message, is_key))
    tool_access = config.get("tool_access")
    if tool_access is not None:
        if not isinstance(tool_access, Mapping):
            problems.append(
                section.problem(
                    (*path, "tool_access"),
                    "`tool_access` maps tool names or patterns to access rules.",
                )
            )
        else:
            for pattern, rule in tool_access.items():
                if not isinstance(pattern, str) or not pattern.strip():
                    problems.append(
                        section.problem(
                            (*path, "tool_access", pattern),
                            "Tool patterns in `tool_access` must be non-empty text.",
                            True,
                        )
                    )
                    continue
                _, rule_problems = _check_access_mapping(rule)
                for sub, message, is_key in rule_problems:
                    problems.append(
                        section.problem((*path, "tool_access", pattern, *sub), message, is_key)
                    )
    return problems


def load_mcp(section: Section) -> dict[str, t.Any]:
    """Validate the ``mcp`` section of ``flowdash.yaml`` and return it as plain data."""
    value = section.value
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SectionError("`mcp` is a mapping with `servers`.")
    problems = []
    for key in value:
        if key != "servers":
            problems.append(
                section.problem((key,), f"Unknown key {key!r}; known: `servers`.", True)
            )
    servers = value.get("servers")
    if servers is not None:
        if not isinstance(servers, Mapping):
            problems.append(section.problem(("servers",), "`servers` is a mapping by server id."))
        else:
            for name, config in servers.items():
                problems.extend(_server_problems(section, name, config))
    if problems:
        raise ProjectError(problems)
    return {"servers": dict(servers)} if servers else {}


def dump_mcp(value: t.Any) -> t.Any:
    """Return the ``mcp`` section as plain data, omitting empty parts."""
    if not isinstance(value, Mapping):
        return value
    servers = value.get("servers")
    if not servers:
        return {}
    cleaned = {}
    for name, cfg in servers.items():
        if isinstance(cfg, MCPServerConfig):
            cleaned[name] = cfg.to_data()
        elif isinstance(cfg, Mapping):
            cleaned[name] = dict(cfg)
    return {"servers": cleaned} if cleaned else {}


def _config_from_mapping(
    name: str, data: Mapping[str, t.Any], *, locked: str | None = None
) -> MCPServerConfig:
    access, _ = _check_access_mapping(data.get("access"))
    tool_access: dict[str, Permission] = {}
    for pattern, rule in (data.get("tool_access") or {}).items():
        perm, _ = _check_access_mapping(rule)
        if not perm.is_empty:
            tool_access[str(pattern)] = perm
    return MCPServerConfig(
        name=name,
        transport=data.get("transport", "http"),
        url=data.get("url"),
        token=data.get("token"),
        headers=dict(data.get("headers") or {}),
        command=data.get("command"),
        args=tuple(data.get("args") or ()),
        env=dict(data.get("env") or {}),
        cwd=data.get("cwd"),
        description=data.get("description") or "",
        enabled=bool(data.get("enabled", True)),
        offered_to=tuple(data.get("offered_to", SURFACES)),
        init_timeout=float(data.get("init_timeout", DEFAULT_INIT_TIMEOUT)),
        timeout=float(data.get("timeout", DEFAULT_TIMEOUT)),
        auto_approve=tuple(data.get("auto_approve") or ()),
        access=access,
        tool_access=tool_access,
        locked=locked,
    )


def server_configs(
    mcp_data: Mapping[str, t.Any] | None,
    *,
    code_servers: Mapping[str, t.Any] | None = None,
    locked: str | None = None,
) -> list[MCPServerConfig]:
    """Return all configured MCP servers: code-defined first (locked), then project file entries."""
    configs: list[MCPServerConfig] = []
    seen: set[str] = set()
    lock_label = locked or "set in code"
    for name, entry in (code_servers or {}).items():
        if validate_server_name(name) is not None:
            logger.warning("Skipping code MCP server with invalid id %r.", name)
            continue
        seen.add(name)
        if isinstance(entry, MCPServerConfig):
            configs.append(replace(entry, name=name, locked=entry.locked or lock_label))
        elif isinstance(entry, Mapping):
            configs.append(_config_from_mapping(name, entry, locked=lock_label))
        else:
            # Pre-built toolset (e.g. pydantic_ai.mcp.MCPToolset or FastMCP-backed toolset).
            configs.append(
                MCPServerConfig(
                    name=name,
                    transport="http",
                    description="Configured in code",
                    locked=lock_label,
                    toolset=entry,
                )
            )
    for name, data in ((mcp_data or {}).get("servers") or {}).items():
        if name in seen or not isinstance(data, Mapping):
            continue
        if validate_server_name(name) is not None:
            continue
        configs.append(_config_from_mapping(name, data))
    return configs


def configured_servers(project: t.Any, app: t.Any = None) -> dict[str, MCPServerConfig]:
    """Return configured MCP servers by id, merging ``app.mcp_servers`` and ``project``'s ``mcp`` section.

    Reads the ``mcp`` section fresh from *project* when it is backed by a file,
    so edits saved from another session or tab are reflected immediately.
    """
    mcp_data: Mapping[str, t.Any] = {}
    if project is not None:
        try:
            mcp_data = current_section(project, MCP_SECTION) or {}
        except ProjectError as exc:
            logger.warning("Could not read the project file's mcp section: %s", exc)
            mcp_data = {}
    code_servers = getattr(app, "mcp_servers", None) if app is not None else None
    locked = (
        app._code_setting_source("mcp_servers")
        if app is not None and hasattr(app, "_code_setting_source")
        else None
    )
    return {
        cfg.name: cfg for cfg in server_configs(mcp_data, code_servers=code_servers, locked=locked)
    }


async def check_mcp_server(
    config: MCPServerConfig,
    *,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    directory: Path | None = None,
    project: t.Any = None,
    project_key: str | None = None,
) -> MCPServerCheck:
    """Connect to *config*, list its tools, cache and return the :class:`MCPServerCheck`."""
    scope = project_key if project_key is not None else project_check_key(project, directory)
    if config.transport == "stdio" and not config.locked and config.toolset is None:
        problem = check_stdio_command(config.command or "", allowed_stdio, args=config.args)
        if problem is None and config.env:
            problem = check_stdio_env(config.env, allowed_stdio_env)
        if problem is not None:
            result = MCPServerCheck(False, problem)
            set_mcp_check(config.name, result, project_key=scope)
            return result
    try:
        from panel_flowdash.agent.mcp import probe_mcp_server
    except ImportError:
        result = MCPServerCheck(False, MCP_EXTRA_MISSING)
        set_mcp_check(config.name, result, project_key=scope)
        return result
    try:
        result = await probe_mcp_server(
            config,
            allowed_stdio=allowed_stdio,
            allowed_stdio_env=allowed_stdio_env,
            allowed_secret_env=allowed_secret_env,
            directory=directory,
        )
    except (SecretError, PermissionError) as exc:
        result = MCPServerCheck(False, str(exc))
    except Exception as exc:
        result = MCPServerCheck(False, f"{type(exc).__name__}: {error_text(exc)}")
    set_mcp_check(config.name, result, project_key=scope)
    return result


__all__ = [
    "ACCESS_KEYS",
    "DEFAULT_INIT_TIMEOUT",
    "DEFAULT_SECRET_ENV_VARS",
    "DEFAULT_TIMEOUT",
    "MAX_TOOL_NAME_LENGTH",
    "MCP_CHECKS",
    "MCP_EXTRA_MISSING",
    "MCP_SECTION",
    "SECRET_ENV_VARS_ENV",
    "STDIO_COMMANDS_ENV",
    "STDIO_ENV_VARS_ENV",
    "SURFACES",
    "TRANSPORTS",
    "MCPServerCheck",
    "MCPServerConfig",
    "MCPToolInfo",
    "access_to_dict",
    "allowed_secret_env_vars",
    "allowed_stdio_commands",
    "allowed_stdio_env",
    "check_mcp_secret_ref",
    "check_mcp_server",
    "check_stdio_command",
    "check_stdio_env",
    "clear_mcp_check",
    "configured_servers",
    "dump_mcp",
    "evaluate_permission",
    "format_stdio_prefix",
    "get_mcp_check",
    "is_mcp_tool",
    "is_valid_mcp_tool_name",
    "load_mcp",
    "mcp_check_key",
    "mcp_server_authorized",
    "mcp_tool_authorized",
    "mcp_tool_name",
    "parse_mcp_tool_name",
    "parse_stdio_prefix",
    "project_check_key",
    "resolve_cwd",
    "resolve_http_headers",
    "resolve_stdio_env",
    "resolve_stdio_executable",
    "server_configs",
    "set_mcp_check",
    "url_origin",
    "validate_server_name",
]
