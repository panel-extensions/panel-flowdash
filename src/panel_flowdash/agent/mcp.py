"""Connecting MCP servers to FlowDash's pydantic-ai agents, with approvals, auth and isolation.

Needs the ``panel-flowdash[mcp]`` extra (pydantic-ai and fastmcp client) for
transport creation; `MCPApprovalRule` and `ToolAuthorizeCapability` need only
``panel-flowdash[agent]``.

MCP connections are scoped to a single agent run: when a tool call pauses for
user approval, the approved call runs on a fresh connection in the resumed run,
so stateful MCP servers do not preserve session state across an approval.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import typing as t
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from panel_agents import ApprovalRule
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool
from pydantic_ai.toolsets.wrapper import WrapperToolset

from panel_flowdash.auth import Identity
from panel_flowdash.mcp import (
    MCP_EXTRA_MISSING,
    MCPServerCheck,
    MCPServerConfig,
    MCPToolInfo,
    check_stdio_command,
    check_stdio_env,
    is_valid_mcp_tool_name,
    mcp_server_authorized,
    mcp_tool_authorized,
    mcp_tool_name,
    parse_mcp_tool_name,
    project_check_key,
    resolve_cwd,
    resolve_http_headers,
    resolve_stdio_env,
    resolve_stdio_executable,
    set_mcp_check,
)
from panel_flowdash.secret_store import SecretError
from panel_flowdash.value_text import error_text

logger = logging.getLogger(__name__)


def _check_message(valid_count: int, skipped: Sequence[str]) -> str:
    base = f"Connected ({valid_count} tool{'s' if valid_count != 1 else ''})"
    if not skipped:
        return base
    names = ", ".join(skipped[:5]) + (f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else "")
    return f"{base}; skipped {len(skipped)} incompatible: {names}"


@dataclass(frozen=True)
class MCPApprovalRule(ApprovalRule):
    """Require user approval for MCP tools unless explicitly opted out via ``auto_approve``.

    ``auto_approve`` maps each server name to its tuple of allowed raw tool
    names or ``fnmatch`` patterns. Every other MCP tool call requires approval.
    """

    tool: str = "mcp_*"
    auto_approve: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def matches(self, tool_name: str, args: dict[str, t.Any] | None = None) -> bool:
        """Whether calling *tool_name* requires user approval."""
        if not super().matches(tool_name, args):
            return False
        parsed = parse_mcp_tool_name(tool_name)
        if parsed is None:
            return True
        server, raw_tool = parsed
        for name, patterns in self.auto_approve:
            if name == server and any(fnmatch.fnmatchcase(raw_tool, pat) for pat in patterns):
                return False
        return True


def mcp_approval_rules(configs: Sequence[MCPServerConfig]) -> list[ApprovalRule]:
    """Return the default MCP approval rule covering *configs*."""
    allow_pairs = tuple(
        (cfg.name, tuple(cfg.auto_approve)) for cfg in configs if cfg.enabled and cfg.auto_approve
    )
    return [MCPApprovalRule(tool="mcp_*", auto_approve=allow_pairs)]


def _safe_authorize(authorize: Callable[[str], bool], tool_name: str) -> bool:
    try:
        return bool(authorize(tool_name))
    except Exception:
        logger.exception("tool_authorize failed for %r; denying tool", tool_name)
        return False


@dataclass
class _AuthorizedWrapperToolset(WrapperToolset[t.Any]):
    """Hides and refuses execution of tools rejected by ``authorize(tool_name)``."""

    authorize: Callable[[str], bool]

    async def get_tools(self, ctx: RunContext[t.Any]) -> dict[str, ToolsetTool[t.Any]]:
        tools = await super().get_tools(ctx)
        return {
            name: replace(tool, toolset=self)
            for name, tool in tools.items()
            if _safe_authorize(self.authorize, name)
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, t.Any],
        ctx: RunContext[t.Any],
        tool: ToolsetTool[t.Any],
    ) -> t.Any:
        if not _safe_authorize(self.authorize, name):
            return f"Refused: you are not authorized to use tool {name!r}."
        inner_tool = replace(tool, toolset=self.wrapped)
        return await self.wrapped.call_tool(name, tool_args, ctx, inner_tool)


@dataclass
class ToolAuthorizeCapability(AbstractCapability[t.Any]):
    """Run-scoped capability enforcing ``FlowDashApp.tool_authorize`` across all tools."""

    authorize: Callable[[str], bool]

    async def prepare_tools(
        self, ctx: RunContext[t.Any], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [td for td in tool_defs if _safe_authorize(self.authorize, td.name)]

    def get_wrapper_toolset(
        self, toolset: AbstractToolset[t.Any]
    ) -> AbstractToolset[t.Any] | None:
        return _AuthorizedWrapperToolset(toolset, self.authorize)


def _create_raw_toolset(
    config: MCPServerConfig,
    *,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    directory: Path | None = None,
) -> AbstractToolset[t.Any]:
    """Instantiate the underlying pydantic-ai ``MCPToolset`` for *config*."""
    if config.toolset is not None:
        if isinstance(config.toolset, AbstractToolset):
            return config.toolset
        try:
            from pydantic_ai.mcp import MCPToolset
        except ImportError as exc:
            raise ImportError(MCP_EXTRA_MISSING) from exc
        return MCPToolset(
            config.toolset,
            id=f"mcp:{config.name}",
            init_timeout=config.init_timeout,
            read_timeout=config.timeout,
            include_instructions=False,
        )

    try:
        from pydantic_ai.mcp import MCPToolset, StdioTransport, StreamableHttpTransport
    except ImportError as exc:
        raise ImportError(MCP_EXTRA_MISSING) from exc

    if config.transport == "http":
        if not config.url:
            raise ValueError(f"MCP server {config.name!r} has no URL.")
        headers = resolve_http_headers(config, allowed_secret_env=allowed_secret_env)
        transport = StreamableHttpTransport(config.url, headers=headers or None)
        return MCPToolset(
            transport,
            id=f"mcp:{config.name}",
            init_timeout=config.init_timeout,
            read_timeout=config.timeout,
            include_instructions=False,
        )

    if config.transport == "stdio":
        if config.locked:
            executable = config.command or ""
            if not executable:
                raise ValueError(f"MCP server {config.name!r} has no command.")
        else:
            executable = resolve_stdio_executable(
                config.command or "", allowed_stdio, args=config.args
            )
        cwd_path = resolve_cwd(config.cwd, directory)
        if config.cwd and cwd_path is None:
            raise PermissionError(
                f"MCP server {config.name!r} cwd {config.cwd!r} escapes the project directory."
            )
        env = resolve_stdio_env(
            config,
            allowed_env=None if config.locked else allowed_stdio_env,
            allowed_secret_env=allowed_secret_env,
        )
        transport = StdioTransport(
            command=executable,
            args=list(config.args),
            env=env,
            cwd=str(cwd_path) if cwd_path is not None else None,
        )
        return MCPToolset(
            transport,
            id=f"mcp:{config.name}",
            init_timeout=config.init_timeout,
            read_timeout=config.timeout,
            include_instructions=False,
        )

    raise ValueError(f"Unsupported MCP transport {config.transport!r}.")


class ResilientMCPToolset(AbstractToolset[t.Any]):
    """Wraps an MCP server toolset with prefixing, timeouts, auth and failure isolation.

    An unreachable, slow, or misconfigured server degrades to an empty tool
    list and records its error in ``MCP_CHECKS`` rather than breaking the
    agent run.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        wrapped: AbstractToolset[t.Any] | None = None,
        init_error: str | None = None,
        authorize: Callable[[str], bool] | None = None,
        record_check: bool = True,
        project_key: str = "",
    ):
        self.config = config
        self._wrapped = wrapped
        self._init_error = init_error
        self._authorize = authorize
        self._record_check = record_check
        self._project_key = project_key
        self._entered = False
        self._unavailable: str | None = init_error

    @property
    def id(self) -> str | None:
        return f"mcp:{self.config.name}"

    @property
    def unavailable(self) -> str | None:
        """Why this server is unavailable for the current run, if it failed."""
        return self._unavailable

    async def get_instructions(self, ctx: RunContext[t.Any]) -> None:
        # Never forward remote MCP server instructions into the agent's system prompt.
        return None

    def _note_failure(self, message: str) -> None:
        self._unavailable = message
        logger.warning("MCP server %r unavailable: %s", self.config.name, message)
        if self._record_check:
            set_mcp_check(
                self.config.name,
                MCPServerCheck(False, message),
                project_key=self._project_key,
            )

    async def __aenter__(self) -> ResilientMCPToolset:  # noqa: D105
        if self._init_error is not None or self._wrapped is None:
            self._note_failure(self._init_error or "MCP server could not be initialized.")
            return self
        try:
            # Explicit wait_for guards custom or in-process toolsets that ignore init_timeout.
            await asyncio.wait_for(self._wrapped.__aenter__(), timeout=self.config.init_timeout)
            self._entered = True
            self._unavailable = None
        except TimeoutError:
            self._note_failure(f"Timed out connecting after {self.config.init_timeout:g}s.")
        except Exception as exc:
            self._note_failure(f"{type(exc).__name__}: {error_text(exc)}")
        return self

    async def __aexit__(self, *args: t.Any) -> bool | None:  # noqa: D105
        if self._entered and self._wrapped is not None:
            try:
                await asyncio.wait_for(self._wrapped.__aexit__(*args), timeout=5.0)
            except Exception as exc:
                logger.warning(
                    "Ignoring error closing MCP server %r: %s", self.config.name, error_text(exc)
                )
            finally:
                self._entered = False
        return None

    async def get_tools(self, ctx: RunContext[t.Any]) -> dict[str, ToolsetTool[t.Any]]:
        if self._unavailable is not None or self._wrapped is None:
            return {}
        try:
            # Explicit wait_for guards custom or in-process toolsets that ignore read_timeout.
            raw_tools = await asyncio.wait_for(
                self._wrapped.get_tools(ctx), timeout=self.config.timeout
            )
        except TimeoutError:
            self._note_failure(f"Timed out listing tools after {self.config.timeout:g}s.")
            return {}
        except Exception as exc:
            self._note_failure(f"{type(exc).__name__}: {error_text(exc)}")
            return {}

        valid_tools: list[tuple[str, ToolsetTool[t.Any]]] = []
        skipped: list[str] = []
        for raw_name, tool in raw_tools.items():
            if is_valid_mcp_tool_name(self.config.name, raw_name):
                valid_tools.append((raw_name, tool))
            else:
                skipped.append(raw_name)
        if skipped:
            logger.warning(
                "MCP server %r exposed incompatible tool name(s) skipped to protect model calls: %s",
                self.config.name,
                ", ".join(skipped),
            )

        infos = tuple(
            MCPToolInfo(
                name=raw_name,
                description=tool.tool_def.description or "",
                annotations=(tool.tool_def.metadata or {}).get("annotations"),
            )
            for raw_name, tool in valid_tools
        )
        if self._record_check:
            set_mcp_check(
                self.config.name,
                MCPServerCheck(True, _check_message(len(infos), skipped), tools=infos),
                project_key=self._project_key,
            )

        result: dict[str, ToolsetTool[t.Any]] = {}
        for raw_name, tool in valid_tools:
            prefixed = mcp_tool_name(self.config.name, raw_name)
            if self._authorize is not None and not _safe_authorize(self._authorize, prefixed):
                continue
            result[prefixed] = replace(
                tool,
                toolset=self,
                tool_def=replace(tool.tool_def, name=prefixed),
            )
        return result

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, t.Any],
        ctx: RunContext[t.Any],
        tool: ToolsetTool[t.Any],
    ) -> t.Any:
        if self._authorize is not None and not _safe_authorize(self._authorize, name):
            return f"Refused: you are not authorized to use tool {name!r}."
        if self._unavailable is not None or self._wrapped is None:
            return f"MCP server {self.config.name!r} is unavailable: {self._unavailable}"
        parsed = parse_mcp_tool_name(name)
        raw_name = parsed[1] if parsed is not None else name
        raw_tool = replace(
            tool,
            toolset=self._wrapped,
            tool_def=replace(tool.tool_def, name=raw_name),
        )
        raw_ctx = replace(ctx, tool_name=raw_name)
        try:
            return await asyncio.wait_for(
                self._wrapped.call_tool(raw_name, tool_args, raw_ctx, raw_tool),
                timeout=self.config.timeout,
            )
        except (ModelRetry, ApprovalRequired, CallDeferred):
            raise
        except TimeoutError:
            msg = f"MCP server {self.config.name!r} timed out calling {raw_name!r} after {self.config.timeout:g}s."
            logger.warning("%s", msg)
            return msg
        except Exception as exc:
            msg = f"MCP server {self.config.name!r} error calling {raw_name!r}: {error_text(exc)}"
            logger.warning("%s", msg)
            return msg


def build_mcp_toolset(
    config: MCPServerConfig,
    *,
    identity: Identity | None = None,
    is_admin: bool = False,
    tool_authorize: Callable[[Identity | None, str], bool] | None = None,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    directory: Path | None = None,
    project: t.Any = None,
    project_key: str | None = None,
    record_check: bool = True,
) -> ResilientMCPToolset:
    """Build a :class:`ResilientMCPToolset` for *config* with auth, secrets and stdio policy."""
    scope = project_key if project_key is not None else project_check_key(project, directory)

    def authorize(prefixed_name: str) -> bool:
        parsed = parse_mcp_tool_name(prefixed_name)
        raw_tool = parsed[1] if parsed is not None else prefixed_name
        if not mcp_tool_authorized(config, raw_tool, identity, is_admin=is_admin):
            return False
        if tool_authorize is not None:
            try:
                return bool(tool_authorize(identity, prefixed_name))
            except Exception:
                logger.exception("tool_authorize failed for %r; denying tool", prefixed_name)
                return False
        return True

    try:
        raw = _create_raw_toolset(
            config,
            allowed_stdio=allowed_stdio,
            allowed_stdio_env=allowed_stdio_env,
            allowed_secret_env=allowed_secret_env,
            directory=directory,
        )
    except (ImportError, SecretError, PermissionError, FileNotFoundError, ValueError) as exc:
        return ResilientMCPToolset(
            config,
            wrapped=None,
            init_error=error_text(exc),
            authorize=authorize,
            record_check=record_check,
            project_key=scope,
        )
    except Exception as exc:
        return ResilientMCPToolset(
            config,
            wrapped=None,
            init_error=f"{type(exc).__name__}: {error_text(exc)}",
            authorize=authorize,
            record_check=record_check,
            project_key=scope,
        )

    return ResilientMCPToolset(
        config,
        wrapped=raw,
        authorize=authorize,
        record_check=record_check,
        project_key=scope,
    )


def build_mcp_toolsets(
    configs: Sequence[MCPServerConfig],
    *,
    surface: str = "assistant",
    identity: Identity | None = None,
    is_admin: bool = False,
    tool_authorize: Callable[[Identity | None, str], bool] | None = None,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    directory: Path | None = None,
    project: t.Any = None,
    project_key: str | None = None,
) -> list[ResilientMCPToolset]:
    """Build resilient MCP toolsets for every server offered on *surface* and allowed for *identity*."""
    scope = project_key if project_key is not None else project_check_key(project, directory)
    toolsets: list[ResilientMCPToolset] = []
    for config in configs:
        if not config.offered(surface):
            continue
        if not mcp_server_authorized(config, identity, is_admin=is_admin):
            continue
        toolsets.append(
            build_mcp_toolset(
                config,
                identity=identity,
                is_admin=is_admin,
                tool_authorize=tool_authorize,
                allowed_stdio=allowed_stdio,
                allowed_stdio_env=allowed_stdio_env,
                allowed_secret_env=allowed_secret_env,
                directory=directory,
                project_key=scope,
            )
        )
    return toolsets


async def probe_mcp_server(
    config: MCPServerConfig,
    *,
    allowed_stdio: Sequence[str | Sequence[str]] = (),
    allowed_stdio_env: Iterable[str] = (),
    allowed_secret_env: Sequence[str] | None = None,
    directory: Path | None = None,
) -> MCPServerCheck:
    """Connect to *config*, list its tools and close the connection cleanly."""
    if config.transport == "stdio" and not config.locked and config.toolset is None:
        problem = check_stdio_command(config.command or "", allowed_stdio, args=config.args)
        if problem is None and config.env:
            problem = check_stdio_env(config.env, allowed_stdio_env)
        if problem is not None:
            return MCPServerCheck(False, problem)
    raw = _create_raw_toolset(
        config,
        allowed_stdio=allowed_stdio,
        allowed_stdio_env=allowed_stdio_env,
        allowed_secret_env=allowed_secret_env,
        directory=directory,
    )
    entered = False
    try:
        await asyncio.wait_for(raw.__aenter__(), timeout=config.init_timeout)
        entered = True
        infos: list[MCPToolInfo] = []
        skipped: list[str] = []
        list_tools = getattr(raw, "list_tools", None)
        if callable(list_tools):
            mcp_tools = await asyncio.wait_for(list_tools(), timeout=config.timeout)
            for item in mcp_tools:
                if not is_valid_mcp_tool_name(config.name, item.name):
                    skipped.append(item.name)
                    continue
                ann = getattr(item, "annotations", None)
                ann_dict = (
                    ann.model_dump(by_alias=True)
                    if ann is not None and hasattr(ann, "model_dump")
                    else (dict(ann) if isinstance(ann, dict) else None)
                )
                infos.append(
                    MCPToolInfo(
                        name=item.name,
                        description=getattr(item, "description", None) or "",
                        annotations=ann_dict,
                    )
                )
        else:
            tools_map = await asyncio.wait_for(raw.get_tools(None), timeout=config.timeout)  # type: ignore[arg-type]
            for name, tool in tools_map.items():
                if not is_valid_mcp_tool_name(config.name, name):
                    skipped.append(name)
                    continue
                infos.append(
                    MCPToolInfo(
                        name=name,
                        description=tool.tool_def.description or "",
                        annotations=(tool.tool_def.metadata or {}).get("annotations"),
                    )
                )
        return MCPServerCheck(
            True,
            _check_message(len(infos), skipped),
            tools=tuple(infos),
        )
    except TimeoutError:
        return MCPServerCheck(
            False,
            f"Timed out after {config.timeout if entered else config.init_timeout:g}s.",
        )
    except (SecretError, PermissionError) as exc:
        return MCPServerCheck(False, str(exc))
    except Exception as exc:
        return MCPServerCheck(False, f"{type(exc).__name__}: {error_text(exc)}")
    finally:
        if entered:
            try:
                await asyncio.wait_for(raw.__aexit__(None, None, None), timeout=5.0)
            except Exception as exc:
                logger.debug(
                    "Error closing probed MCP server %r: %s", config.name, error_text(exc)
                )


__all__ = [
    "MCPApprovalRule",
    "ResilientMCPToolset",
    "ToolAuthorizeCapability",
    "build_mcp_toolset",
    "build_mcp_toolsets",
    "mcp_approval_rules",
    "probe_mcp_server",
]
