"""Component registry: the register decorator and metadata model."""

from __future__ import annotations

import ast
import importlib
import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panel_flowdash.auth import Permission


@dataclass(frozen=True)
class PanelAppMetadata:
    """Metadata attached to a component or page by the @register decorator."""

    page: bool = True
    component: bool = False
    sidebar: bool = False
    title: str | None = None
    icon: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    default_size: dict[str, Any] | None = None
    min_size: dict[str, Any] | None = None
    max_size: dict[str, Any] | None = None
    singleton: bool = False
    provides: list[str] = field(default_factory=list)
    requires: list[Any] = field(default_factory=list)
    config_schema: Any = None
    config: list[str] = field(default_factory=list)
    config_editor: Callable | None = None
    allow_users: list[str] = field(default_factory=list)
    allow_groups: list[str] = field(default_factory=list)
    deny_users: list[str] = field(default_factory=list)
    deny_groups: list[str] = field(default_factory=list)
    authorize: Callable | None = None

    @property
    def permission(self) -> Permission:
        """Build a :class:`~panel_flowdash.auth.Permission` from the declared rules."""
        return Permission.from_spec(
            allow_users=self.allow_users,
            allow_groups=self.allow_groups,
            deny_users=self.deny_users,
            deny_groups=self.deny_groups,
        )

    @classmethod
    def from_app(cls, app: Any) -> PanelAppMetadata:
        """Extract metadata from an app object."""
        metadata = getattr(app, "__panel_app_metadata__", None)
        if metadata is None:
            metadata = _APP_METADATA_BY_ID.get(id(app))
        if metadata is None:
            return cls()
        if isinstance(metadata, cls):
            return metadata
        if isinstance(metadata, dict):
            return cls(**metadata)
        raise TypeError("Unsupported panel app metadata type.")


_APP_METADATA_BY_ID: dict[int, PanelAppMetadata] = {}


def register(
    *,
    page: bool = True,
    component: bool = False,
    sidebar: bool = False,
    title: str | None = None,
    icon: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    default_size: dict[str, Any] | None = None,
    min_size: dict[str, Any] | None = None,
    max_size: dict[str, Any] | None = None,
    singleton: bool = False,
    provides: list[str] | None = None,
    requires: list[Any] | None = None,
    config_schema: Any = None,
    config: list[str] | None = None,
    config_editor: Callable | None = None,
    allow_users: list[str] | None = None,
    allow_groups: list[str] | None = None,
    deny_users: list[str] | None = None,
    deny_groups: list[str] | None = None,
    authorize: Callable | None = None,
):
    """Metadata-only decorator for app exports.

    Annotates an app object/callable without altering runtime behavior.

    The ``config_schema``, ``config`` and ``config_editor`` arguments declare
    design-time configuration options that appear in the node editor. Use
    ``config_schema`` (a ``param.Parameterized`` subclass, a Pydantic model, or
    a JSON Schema dict) to define config explicitly, or ``config`` to name which
    of a Viewer's own params are configuration rather than input ports. Pass
    ``config_editor`` to supply a custom editor callable instead of the
    auto-generated form.

    The ``allow_users``, ``allow_groups``, ``deny_users`` and ``deny_groups``
    arguments declare page-level authorization rules. Users are matched against
    either the OAuth login or the system user; groups against the identity's
    resolved group membership. Deny rules always win; when only allow rules are
    present the identity must match at least one; with no rules the project's
    default policy applies. Pass ``authorize`` for a custom callable taking the
    resolved ``Identity`` and returning a bool (resolved on import).
    """
    metadata = PanelAppMetadata(
        page=page,
        component=component,
        sidebar=sidebar,
        title=title,
        icon=icon,
        description=description,
        tags=list(tags or []),
        default_size=default_size,
        min_size=min_size,
        max_size=max_size,
        singleton=singleton,
        provides=list(provides or []),
        requires=list(requires or []),
        config_schema=config_schema,
        config=list(config or []),
        config_editor=config_editor,
        allow_users=list(allow_users or []),
        allow_groups=list(allow_groups or []),
        deny_users=list(deny_users or []),
        deny_groups=list(deny_groups or []),
        authorize=authorize,
    )

    def _decorator(app):
        _APP_METADATA_BY_ID[id(app)] = metadata
        try:
            app.__panel_app_metadata__ = metadata
        except Exception:
            pass
        return app

    return _decorator


panel_app = register


_DEFAULT_SECTION = "Components"

_UNINFORMATIVE_MODULES = {"main", "builtins", "abc"}


def _module_parts(app: Any) -> list[str]:
    """Meaningful dotted parts of an app's defining module, outermost first."""
    module = getattr(app, "__module__", "") or ""
    return [
        part
        for part in module.split(".")
        if part and not part.startswith("_") and part not in _UNINFORMATIVE_MODULES
    ]


def _app_name(app: Any) -> str:
    """Derive a component name from a live app object.

    Modules following the project-directory convention export their component as
    ``app``, which makes a useless id, so in that case the module stem names the
    component instead (matching how :func:`build_registry` ids it).
    """
    for attr in ("__name__", "__qualname__"):
        value = getattr(app, attr, None)
        if isinstance(value, str) and value and value != "app":
            return value
    parts = _module_parts(app)
    if parts:
        return parts[-1]
    return type(app).__name__


def _app_section(app: Any) -> str:
    """Derive a section from an app object's defining module.

    A component defined in ``myproject.analytics`` lands in an "analytics"
    section. When the module stem already names the component (the ``app``
    convention), the parent package supplies the section instead.
    """
    parts = _module_parts(app)
    if getattr(app, "__name__", None) == "app":
        parts = parts[:-1]
    return parts[-1] if parts else _DEFAULT_SECTION


@dataclass
class RegistryEntry:
    """A registered component/page with its metadata."""

    app_id: str
    section: str
    name: str
    page_path: str
    module_name: str
    metadata: PanelAppMetadata
    module_path: pathlib.Path | None = None
    app: Any = None
    # Cache for the entry's ComponentSpec, populated by build_component_spec.
    # Registry entries are shared across sessions, so a spec is introspected
    # once per process rather than once per session. Untyped to avoid a circular
    # import with component_spec.
    spec: Any = field(default=None, repr=False, compare=False)

    @property
    def title(self) -> str:
        """Human-readable title."""
        return self.metadata.title or self.name.replace("_", " ")

    @classmethod
    def from_app(
        cls,
        app: Any,
        *,
        app_id: str | None = None,
        section: str | None = None,
        name: str | None = None,
    ) -> RegistryEntry:
        """Build an entry from an already-imported app object.

        Unlike :func:`build_registry`, which discovers modules on disk and defers
        importing them, this wraps a live object so ``load()`` is a no-op. Used
        by the programmatic API where components are passed in directly.

        Objects without ``@register`` metadata are treated as components, since
        a bare ``Viewer`` subclass handed to the editor is only ever meant to be
        one.
        """
        metadata = PanelAppMetadata.from_app(app)
        if metadata == PanelAppMetadata():
            metadata = PanelAppMetadata(page=False, component=True)

        name = name or _app_name(app)
        if app_id is not None:
            section, _, derived = app_id.rpartition("/")
            section = section or "Components"
            name = derived or name
        else:
            section = section or _app_section(app)
            app_id = f"{section}/{name}"

        return cls(
            app_id=app_id,
            section=section,
            name=name,
            page_path=f"/{app_id}",
            module_name=getattr(app, "__module__", "") or "",
            metadata=metadata,
            module_path=None,
            app=app,
        )

    def load(self) -> Any:
        """Import the module and return the app object.

        Caches the result on ``self.app``.  Raises on import failure.
        """
        if self.app is not None:
            return self.app
        module = importlib.import_module(self.module_name)
        app = getattr(module, "app", None)
        if app is None:
            raise ImportError(f"Module '{self.module_name}' has no 'app' export.")
        # Refresh metadata from the live object (decorators may carry richer info
        # e.g. config_schema / config_editor that AST cannot capture).
        object.__setattr__(self, "app", app)
        live_metadata = PanelAppMetadata.from_app(app)
        # Only replace if the live decorator actually produced a non-default result
        # (guards against bare Viewer classes with no @register decorator).
        if live_metadata != PanelAppMetadata():
            object.__setattr__(self, "metadata", live_metadata)
        return app


# ---------------------------------------------------------------------------
# AST-based metadata extraction
# ---------------------------------------------------------------------------

_REGISTER_NAMES = {"register", "panel_app"}

_LITERAL_KEYS = {
    "page",
    "component",
    "sidebar",
    "title",
    "icon",
    "description",
    "singleton",
    "provides",
    "requires",
    "config",
    "tags",
    "default_size",
    "min_size",
    "max_size",
    "allow_users",
    "allow_groups",
    "deny_users",
    "deny_groups",
}


def _eval_literal(node: ast.expr) -> Any:
    """Safely evaluate an AST literal node; return None on failure."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _extract_register_kwargs(source: str) -> dict[str, Any] | None:
    """Parse *source* and return the kwargs of the first @register/@panel_app call.

    Only literal-evaluable arguments are captured; runtime expressions (e.g.
    ``config_schema=MyParamClass``) are silently skipped — they are picked up
    later when the module is actually imported.

    Returns ``None`` if no @register call is found.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            if call is None:
                continue
            func = call.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name not in _REGISTER_NAMES:
                continue
            kwargs: dict[str, Any] = {}
            for kw in call.keywords:
                if kw.arg in _LITERAL_KEYS:
                    val = _eval_literal(kw.value)
                    if val is not None or isinstance(kw.value, ast.Constant):
                        kwargs[kw.arg] = val
            return kwargs

    return None


def build_registry(project_dir: Path) -> dict[str, RegistryEntry]:
    """Scan *project_dir* for page/component modules without importing them.

    Reads each ``.py`` file with the AST to extract ``@register`` metadata.
    Modules are **not** imported at this stage; each ``RegistryEntry.app`` is
    ``None`` until ``RegistryEntry.load()`` is called.
    """
    registry: dict[str, RegistryEntry] = {}

    for section_dir in sorted(project_dir.glob("*")):
        if not section_dir.is_dir() or section_dir.name.startswith(("_", ".")):
            continue
        section = section_dir.name
        for module_path in sorted(section_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue

            source = module_path.read_text(encoding="utf-8")
            kwargs = _extract_register_kwargs(source)
            if kwargs is None:
                # No @register call found — skip silently (same as before).
                continue

            # Defaults that match PanelAppMetadata
            page = kwargs.get("page", True)
            component = kwargs.get("component", False)
            if not page and not component:
                continue

            metadata = PanelAppMetadata(
                page=bool(page),
                component=bool(component),
                sidebar=bool(kwargs.get("sidebar", False)),
                title=kwargs.get("title"),
                icon=kwargs.get("icon"),
                description=kwargs.get("description"),
                tags=list(kwargs.get("tags") or []),
                default_size=kwargs.get("default_size"),
                min_size=kwargs.get("min_size"),
                max_size=kwargs.get("max_size"),
                singleton=bool(kwargs.get("singleton", False)),
                provides=list(kwargs.get("provides") or []),
                requires=list(kwargs.get("requires") or []),
                config=list(kwargs.get("config") or []),
                allow_users=list(kwargs.get("allow_users") or []),
                allow_groups=list(kwargs.get("allow_groups") or []),
                deny_users=list(kwargs.get("deny_users") or []),
                deny_groups=list(kwargs.get("deny_groups") or []),
            )

            module_name = ".".join(module_path.relative_to(project_dir).with_suffix("").parts)
            app_id = f"{section}/{module_path.stem}"
            registry[app_id] = RegistryEntry(
                app_id=app_id,
                section=section,
                name=module_path.stem,
                page_path=f"/{app_id}",
                module_name=module_name,
                metadata=metadata,
                module_path=module_path,
                app=None,
            )

    return registry
