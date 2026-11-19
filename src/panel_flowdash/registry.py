"""Component registry: the register decorator and metadata model."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import pathlib
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from panel_flowdash.auth import Permission

logger = logging.getLogger(__name__)


def _name_list(value: Any) -> list[str]:
    """Normalize a user/group argument, treating a lone string as one name."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, Iterable) or isinstance(value, dict):
        raise TypeError(f"Expected a list of user or group names, got {value!r}.")
    return list(value)


def _port_aliases(value: Any) -> dict[str, str]:
    """Validate a mapping of former port names to current ones."""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise TypeError(f"port_aliases must map old port names to new ones, got {value!r}.")
    return dict(value)


def _framed(value: Any) -> bool | None:
    """Validate a ``framed`` hint: True, False or None."""
    if value is not None and not isinstance(value, bool):
        raise TypeError(f"framed must be True, False or None, got {value!r}.")
    return value


def _scanned(normalize: Callable[[Any], Any], value: Any, path: Path, name: str) -> Any:
    """Normalize a scanned argument, ignoring a malformed one until the module is imported."""
    try:
        return normalize(value)
    except TypeError as exc:
        logger.warning("%s: ignoring %s: %s", path, name, exc)
        return normalize(None)


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
    extensions: list[str] = field(default_factory=list)
    default_size: dict[str, Any] | None = None
    min_size: dict[str, Any] | None = None
    max_size: dict[str, Any] | None = None
    framed: bool | None = None
    singleton: bool = False
    provides: list[str] = field(default_factory=list)
    requires: list[Any] = field(default_factory=list)
    placeholder: str | None = None
    config_schema: Any = None
    config: list[str] = field(default_factory=list)
    config_editor: Callable | None = None
    check_config: Callable | None = None
    contract: Any = None
    config_choices: Callable | None = None
    config_completions: Callable | None = None
    handles_failures: list[str] = field(default_factory=list)
    # Former component ids and former port names, so dashboards saved before a
    # rename still resolve.
    aliases: list[str] = field(default_factory=list)
    port_aliases: dict[str, str] = field(default_factory=dict)
    allow_users: list[str] = field(default_factory=list)
    allow_groups: list[str] = field(default_factory=list)
    deny_users: list[str] = field(default_factory=list)
    deny_groups: list[str] = field(default_factory=list)
    authorize: Callable | None = None
    # Set by the source scan when the permission arguments are expressions it
    # cannot evaluate; access is refused until an import supplies the real rules.
    auth_pending: bool = False

    @property
    def permission(self) -> Permission:
        """Build a :class:`~panel_flowdash.auth.Permission` from the declared rules."""
        return Permission.from_spec(
            allow_users=self.allow_users,
            allow_groups=self.allow_groups,
            deny_users=self.deny_users,
            deny_groups=self.deny_groups,
        )

    @staticmethod
    def is_registered(app: Any) -> bool:
        """Whether ``@register`` attached metadata to *app*."""
        return _attached_metadata(app) is not None

    @classmethod
    def from_app(cls, app: Any) -> PanelAppMetadata:
        """Extract metadata from an app object."""
        metadata = _attached_metadata(app)
        if metadata is None:
            return cls()
        if isinstance(metadata, cls):
            return metadata
        if isinstance(metadata, dict):
            return cls(**metadata)
        raise TypeError("Unsupported panel app metadata type.")


# Keyed by id() for apps that refuse the attribute; the reference lets a lookup
# reject an id that a collected app has passed on to a new object.
_APP_METADATA_BY_ID: dict[int, tuple[Any, PanelAppMetadata]] = {}


def _attached_metadata(app: Any) -> Any:
    metadata = getattr(app, "__panel_app_metadata__", None)
    if metadata is None:
        ref, metadata = _APP_METADATA_BY_ID.get(id(app), (None, None))
        target = ref() if isinstance(ref, weakref.ref) else ref
        if target is not app:
            metadata = None
    return metadata


def register(
    *,
    page: bool = True,
    component: bool = False,
    sidebar: bool = False,
    title: str | None = None,
    icon: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    extensions: list[str] | None = None,
    default_size: dict[str, Any] | None = None,
    min_size: dict[str, Any] | None = None,
    max_size: dict[str, Any] | None = None,
    framed: bool | None = None,
    singleton: bool = False,
    provides: list[str] | None = None,
    requires: list[Any] | None = None,
    placeholder: str | None = None,
    config_schema: Any = None,
    config: list[str] | None = None,
    config_editor: Callable | None = None,
    check_config: Callable | None = None,
    contract: Any = None,
    config_choices: Callable | None = None,
    config_completions: Callable | None = None,
    handles_failures: list[str] | None = None,
    aliases: list[str] | None = None,
    port_aliases: dict[str, str] | None = None,
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

    The ``check_config`` argument takes a callable, sync or async, that checks
    a node's config before it is applied: ``check(config, inputs)`` gets the
    node's whole config with the change applied and its input values that are
    set and not failed (``{}`` before the node is placed). It returns ``None``,
    a string or an iterable of strings, each non-empty string a problem, or
    raises ``ValueError`` with the problem as its message. A
    :class:`~panel_flowdash.CheckWarning` in place of a string is a warning,
    shown with the config but not refusing it. Any other result or exception,
    or taking longer than 10 seconds, counts as a failed check. The check runs
    again when an input of the node is connected or disconnected.
    A sync check runs in a worker thread and must not change the editor. A
    config with problems is refused in the inspector and by the agent tools,
    and reported by validation and previews.

    The ``contract`` argument takes a
    :class:`~panel_flowdash.ParameterContract`: what the dashboard needs of
    a value bound to a parameter on one of the component's config fields,
    e.g. the columns its downstream nodes read from a data source. Its
    ``expects(context)`` writes the parameter's ``expects`` and its
    ``check(context, expects)`` runs whenever the parameter is rebound,
    see :mod:`panel_flowdash.parameters`.

    The ``config_choices`` argument takes a callable, sync or async, called
    like ``check_config`` as ``choices(config, inputs)``, that returns the
    values each config field may take: ``{field: choices}``, where choices are
    a list of values, a ``{label: value}`` dict or a
    :class:`~panel_flowdash.Choices`. The inspector offers them in a dropdown,
    or as suggestions with ``Choices(restrict=False)``, and asks again when
    the node's inputs or config change. Choices under the name of an input
    are offered for the value that input takes while it is not connected. A
    config param may also declare that
    it names a column of an input, with
    ``metadata={"flowdash": {"choices": "columns", "of": "<input>"}}``.

    The ``config_completions`` argument takes a callable, sync or async,
    called like ``config_choices``, that returns the words the code editor
    of a code field completes while typing: ``{field: words}``, where words
    are a list, a ``{label: word}`` dict or a
    :class:`~panel_flowdash.Choices` whose ``types`` are shown beside each
    word, e.g. the columns of the tables a query reads. Without it a code
    field completes the column names of the node's tabular inputs.

    The ``placeholder`` argument sets the message a dashboard tile shows while a
    blocking input of the component is unset, in place of the default
    "Waiting for <inputs>" text.

    The ``handles_failures`` argument names the inputs that can receive a
    failure. With ``FlowDash(propagate_failures=True)`` an upstream failure
    reaches such an input as a ``param.ReactiveError`` instead of leaving the
    input on its last value; the parameter behind it must accept one, so use an
    untyped ``param.Parameter``.

    The ``aliases`` argument lists ids the component was registered under
    before (e.g. ``["Old/section_name"]``) and ``port_aliases`` maps former
    port or config names to current ones (e.g. ``{"ticker": "symbol"}``), so a
    saved dashboard that references the old names still loads after a rename.

    The ``framed`` argument says whether the component's dashboard tile is
    drawn as a framed card: ``False`` for components that are layout chrome
    themselves, e.g. a divider or a section heading, ``True`` for ones that
    need a frame, and ``None`` (the default) to follow the dashboard's "Frame
    tiles" setting. A tile's own frame option, set in the Layout tab, takes
    precedence over it.

    The ``extensions`` argument names the Panel extensions (e.g.
    ``["tabulator"]``) the app's output needs. Components are imported lazily,
    after the page has loaded its JavaScript, so an editor loads every declared
    extension up front; an undeclared one fails to render in the browser.

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
        extensions=list(extensions or []),
        default_size=default_size,
        min_size=min_size,
        max_size=max_size,
        framed=_framed(framed),
        singleton=singleton,
        provides=list(provides or []),
        requires=list(requires or []),
        placeholder=placeholder,
        config_schema=config_schema,
        config=list(config or []),
        config_editor=config_editor,
        check_config=check_config,
        contract=contract,
        config_choices=config_choices,
        config_completions=config_completions,
        handles_failures=list(handles_failures or []),
        aliases=_name_list(aliases),
        port_aliases=_port_aliases(port_aliases),
        allow_users=_name_list(allow_users),
        allow_groups=_name_list(allow_groups),
        deny_users=_name_list(deny_users),
        deny_groups=_name_list(deny_groups),
        authorize=authorize,
    )

    def _decorator(app):
        try:
            ref = weakref.ref(app)
        except TypeError:
            ref = app
        _APP_METADATA_BY_ID[id(app)] = (ref, metadata)
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
    # The exception from the last failed ``load()``. Authorization checks deny
    # an entry whose rules are still pending once this is set, rather than
    # re-running a failing import on every listing or request.
    load_error: BaseException | None = field(default=None, repr=False, compare=False)

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
        try:
            module = importlib.import_module(self.module_name)
            app = getattr(module, "app", None)
            if app is None:
                raise ImportError(f"Module '{self.module_name}' has no 'app' export.")
        except Exception as exc:
            object.__setattr__(self, "load_error", exc)
            raise
        object.__setattr__(self, "load_error", None)
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
    "placeholder",
    "handles_failures",
    "aliases",
    "port_aliases",
    "config",
    "tags",
    "extensions",
    "default_size",
    "min_size",
    "max_size",
    "framed",
    "allow_users",
    "allow_groups",
    "deny_users",
    "deny_groups",
}

_AUTH_KEYS = {"allow_users", "allow_groups", "deny_users", "deny_groups", "authorize"}

# Key under which _extract_register_kwargs flags rules it could not evaluate.
_AUTH_PENDING = "auth_pending"
# Key listing permission arguments whose literal value is not a list of names.
_AUTH_INVALID = "auth_invalid"


def _is_name_list(value: Any) -> bool:
    """Whether a literal permission value is a name or a collection of names."""
    if isinstance(value, str):
        return True
    return isinstance(value, (list, tuple, set, frozenset)) and all(
        isinstance(v, str) for v in value
    )


def _eval_literal(node: ast.expr) -> Any:
    """Safely evaluate an AST literal node; return None on failure."""
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _extract_register_kwargs(source: str) -> dict[str, Any] | None:
    """Parse *source* and return the kwargs of the first @register/@panel_app call.

    Only literal-evaluable arguments are captured; runtime expressions (e.g.
    ``config_schema=MyParamClass``) are skipped and picked up later when the
    module is actually imported. A permission argument that cannot be
    evaluated, or any ``authorize`` callable, sets ``auth_pending`` instead, so
    the page fails closed until it is imported rather than appearing open.

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
                if kw.arg is None:
                    # **kwargs may carry permission arguments the scan cannot see.
                    kwargs[_AUTH_PENDING] = True
                    continue
                if kw.arg == "authorize":
                    if not (isinstance(kw.value, ast.Constant) and kw.value.value is None):
                        kwargs[_AUTH_PENDING] = True
                    continue
                if kw.arg in _LITERAL_KEYS:
                    val = _eval_literal(kw.value)
                    if kw.arg in _AUTH_KEYS and val is not None and not _is_name_list(val):
                        # The decorator refuses this value at import, so the page
                        # stays pending and is denied rather than aborting the scan.
                        kwargs.setdefault(_AUTH_INVALID, []).append(kw.arg)
                        kwargs[_AUTH_PENDING] = True
                        continue
                    if val is not None or isinstance(kw.value, ast.Constant):
                        kwargs[kw.arg] = val
                    elif kw.arg in _AUTH_KEYS:
                        kwargs[_AUTH_PENDING] = True
            return kwargs

    return None


class PackageResolutionError(ValueError):
    """The package passed to ``build_registry`` does not resolve to the project directory."""


def _check_package(package: str, project_dir: Path) -> None:
    """Raise unless importing *package* resolves to *project_dir*.

    ``find_spec`` on a dotted name imports its parent packages, so validating
    ``a.b`` executes ``a/__init__.py``.
    """
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError) as exc:
        raise PackageResolutionError(f"Cannot resolve package '{package}': {exc}") from exc
    if spec is None or spec.submodule_search_locations is None:
        raise PackageResolutionError(
            f"Package '{package}' is not importable as a package. Install it or put its "
            f"parent directory on sys.path so that it resolves to '{project_dir}'."
        )
    locations = [Path(location).resolve() for location in spec.submodule_search_locations]
    if project_dir.resolve() not in locations:
        found = ", ".join(f"'{location}'" for location in locations)
        raise PackageResolutionError(
            f"Package '{package}' resolves to {found}, not to the project directory "
            f"'{project_dir.resolve()}'."
        )


def build_registry(project_dir: Path, *, package: str | None = None) -> dict[str, RegistryEntry]:
    """Scan *project_dir* for page/component modules without importing them.

    Reads each ``.py`` file with the AST to extract ``@register`` metadata.
    Modules are **not** imported at this stage; each ``RegistryEntry.app`` is
    ``None`` until ``RegistryEntry.load()`` is called.

    By default modules are named relative to *project_dir*
    (``<Section>.<name>``), so the directory itself must be on ``sys.path``.
    Pass ``package`` when *project_dir* is an importable package: modules are
    then named ``<package>.<Section>.<name>``, so the project's own imports and
    flowdash's lazy imports share one module object. Raises
    ``PackageResolutionError`` (a ``ValueError``) if ``package`` does not
    resolve to *project_dir*.
    """
    if package is not None:
        _check_package(package, project_dir)
    registry: dict[str, RegistryEntry] = {}

    for section_dir in sorted(project_dir.glob("*")):
        if not section_dir.is_dir() or section_dir.name.startswith(("_", ".")):
            continue
        section = section_dir.name
        for module_path in sorted(section_dir.glob("*.py")):
            if module_path.name.startswith("_"):
                continue

            try:
                source = module_path.read_text(encoding="utf-8")
                kwargs = _extract_register_kwargs(source)
            except (OSError, ValueError, RecursionError, MemoryError) as exc:
                # One unreadable module must not keep the rest of the project from serving.
                logger.warning(
                    "Skipping %s: it could not be read or parsed (%s)", module_path, exc
                )
                continue
            if kwargs is None:
                # No @register call found — skip silently (same as before).
                continue

            if kwargs.get(_AUTH_INVALID):
                logger.error(
                    "%s: %s must be a list of names; access to this module is denied.",
                    module_path,
                    ", ".join(kwargs[_AUTH_INVALID]),
                )

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
                extensions=list(kwargs.get("extensions") or []),
                default_size=kwargs.get("default_size"),
                min_size=kwargs.get("min_size"),
                max_size=kwargs.get("max_size"),
                framed=_scanned(_framed, kwargs.get("framed"), module_path, "framed"),
                singleton=bool(kwargs.get("singleton", False)),
                provides=list(kwargs.get("provides") or []),
                requires=list(kwargs.get("requires") or []),
                placeholder=kwargs.get("placeholder"),
                handles_failures=list(kwargs.get("handles_failures") or []),
                aliases=_scanned(_name_list, kwargs.get("aliases"), module_path, "aliases"),
                port_aliases=_scanned(
                    _port_aliases, kwargs.get("port_aliases"), module_path, "port_aliases"
                ),
                config=list(kwargs.get("config") or []),
                allow_users=_name_list(kwargs.get("allow_users")),
                allow_groups=_name_list(kwargs.get("allow_groups")),
                deny_users=_name_list(kwargs.get("deny_users")),
                deny_groups=_name_list(kwargs.get("deny_groups")),
                auth_pending=bool(kwargs.get(_AUTH_PENDING, False)),
            )

            parts = module_path.relative_to(project_dir).with_suffix("").parts
            module_name = ".".join((package, *parts) if package is not None else parts)
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
