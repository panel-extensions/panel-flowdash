"""The `flowdash serve` subcommand."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path

import panel as pn
from bokeh.command.subcommand import Argument
from bokeh.embed.bundle import extension_dirs
from bokeh.server.views.multi_root_static_handler import MultiRootStaticHandler
from panel.command.serve import Serve as _PanelServe
from panel.io.application import build_applications
from panel.io.server import DocHandler, WSHandler

from panel_flowdash.admin.access import allow_adding_admins, show_admin_link
from panel_flowdash.app import ACCESS_DENIED_ROUTE, FlowDashApp
from panel_flowdash.auth import AuthConfig, make_authorize_callback
from panel_flowdash.component_library import ComponentLibraryError
from panel_flowdash.dashboard_store import BaseDashboardStore, DashboardStore
from panel_flowdash.project import PROJECT_FILE, ProjectError, find_project
from panel_flowdash.registry import PackageResolutionError, build_registry

log = logging.getLogger(__name__)

_EXCLUDED_ARGS = ("files", "--args")

# FlowDashApp parameters a project ``__init__.py`` may set by defining a module
# attribute of the same name. Each value is validated against the parameter.
# ``sidebar`` and ``contextbar`` are left out because module-level Panel objects
# would be shared by every session; ``configure_layout`` sets them per session.
PROJECT_OPTIONS = (
    "admin_link",
    "agent",
    "agent_approval_rules",
    "agent_authorize",
    "agent_model",
    "agent_model_settings",
    "agent_models",
    "agent_sensitive_components",
    "breakpoints",
    "component_libraries",
    "configure_layout",
    "controls_position",
    "editor_class",
    "editor_options",
    "header_layout",
    "home_dashboard",
    "include_builtin_components",
    "nav_variant",
    "notifications",
    "page_options",
    "propagate_failures",
    "settings_class",
    "title",
)

# Options that also have a CLI flag; an explicit flag takes precedence.
_CLI_OPTIONS = ("home_dashboard", "nav_variant", "title")


def _root_route_kwargs(tornado_app, kwargs: dict) -> dict:
    """Bind a handler to the app served at ``/``, as Bokeh binds its own route handlers."""
    prefix = tornado_app.prefix
    return {
        **kwargs,
        "application_context": tornado_app._applications["/"],
        "bokeh_websocket_path": f"{prefix}/ws",
    }


class _NotFoundWSHandler(WSHandler):
    """The websocket of a page served by :class:`_NotFoundHandler`, at ``<url>/ws``."""

    def __init__(self, tornado_app, *args, **kwargs):
        super().__init__(tornado_app, *args, **_root_route_kwargs(tornado_app, kwargs))


class _NotFoundHandler(DocHandler):
    """Tornado's ``default_handler_class``: the app at ``/`` with a 404 status.

    The app reads the browser's location, so for a URL no route matches it
    renders its own "page does not exist" view instead of Tornado's bare 404.
    Tornado uses one class for every unmatched request, so the page's
    websocket, at ``<url>/ws``, is handed to :class:`_NotFoundWSHandler`.
    """

    def __new__(cls, tornado_app, request, **kwargs):
        if request.path.endswith("/ws"):
            return _NotFoundWSHandler(tornado_app, request, **kwargs)
        return super().__new__(cls)

    def __init__(self, tornado_app, *args, **kwargs):
        super().__init__(tornado_app, *args, **_root_route_kwargs(tornado_app, kwargs))

    async def get(self, *args, **kwargs):
        self.set_status(404)
        await super().get(*args, **kwargs)


def _project_admins(project_file: Path | None, project_dir: Path) -> list[str]:
    """Return the admins the project file names, read without loading its other sections."""
    from panel_flowdash.admin.sections import project_admins
    from panel_flowdash.app import _unloaded_project

    path = project_file or project_dir / PROJECT_FILE
    if not path.is_file():
        return []
    return project_admins(_unloaded_project(path, {}))


def _project_auth_config(
    auth_config: AuthConfig, in_code: dict[str, str], project_file: Path | None, project_dir: Path
):
    """Return a callable giving *auth_config* with the project file's permissions layered under it.

    The file is read again when it changes, so the admin view's changes apply
    to the next request. A file that cannot be read keeps the permissions last
    read from it, and with none, page requests are denied.
    """
    from panel_flowdash.permissions import effective_auth_config, project_permissions

    path = project_file or project_dir / PROJECT_FILE

    def current() -> AuthConfig:
        # Raises PermissionsUnavailable for a broken file with nothing read before; the
        # authorize callback then denies.
        return effective_auth_config(auth_config, project_permissions(path), in_code)

    return current


def _admin_groups(current) -> frozenset[str]:
    try:
        return current().admin_groups
    except Exception:
        return frozenset()


def _resolve_store(
    init_mod, project_dir: Path, db_path: str | None
) -> tuple[BaseDashboardStore, str]:
    """Return the project's dashboard store and a description for the log.

    A project ``__init__.py`` selects the backend by defining ``store``;
    otherwise a SQLite database is used, at *db_path* if given.
    """
    store = getattr(init_mod, "store", None)
    if store is None:
        db_path = db_path or str(project_dir / "dashboards.db")
        return DashboardStore(db_path), f"SQLite database {db_path}"
    if not isinstance(store, BaseDashboardStore):
        print(  # noqa: T201
            f"ERROR: `store` in {project_dir / '__init__.py'} must be a BaseDashboardStore, "
            f"not {type(store).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if db_path:
        print(  # noqa: T201
            f"ERROR: --db-path conflicts with the `store` defined in "
            f"{project_dir / '__init__.py'}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return store, type(store).__name__


def _load_project_init(project_dir: Path, package: str | None):
    """Execute the project's ``__init__.py`` and return it as a module, if any.

    With *package* the file is the package's own ``__init__``, so it is imported
    under that name instead of being executed a second time as a separate module.
    """
    if package is not None:
        return importlib.import_module(package)
    init_file = project_dir / "__init__.py"
    if not init_file.exists():
        return None
    spec = importlib.util.spec_from_file_location("__init__", init_file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _project_options(init_mod, project_dir: Path) -> dict:
    """Return the FlowDashApp options defined in the project's ``__init__.py``.

    Names in ``PROJECT_OPTIONS`` that the module leaves undefined or sets to
    ``None`` are omitted. A value the parameter rejects exits with an error.
    """
    options = {}
    for name in PROJECT_OPTIONS:
        value = getattr(init_mod, name, None)
        if value is None:
            continue
        try:
            FlowDashApp.param[name]._validate(value)
        except (TypeError, ValueError) as exc:
            print(  # noqa: T201
                f"ERROR: invalid `{name}` in {project_dir / '__init__.py'}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        options[name] = value
    return options


class Serve(_PanelServe):
    """Serve a flowdash dashboard application from a project directory."""

    name = "serve"
    help = "Launch the FlowDash dashboard server from a project directory."

    args = (
        (
            "directory",
            Argument(
                metavar="DIRECTORY",
                help="Path to the project directory containing page/component modules.",
            ),
        ),
        (
            "--db-path",
            Argument(
                action="store",
                type=str,
                default=None,
                help=(
                    "Path to the SQLite database file. Defaults to <directory>/dashboards.db. "
                    "Ignored by projects whose __init__.py defines `store`."
                ),
            ),
        ),
        (
            "--package",
            Argument(
                action="store",
                type=str,
                default=None,
                help=(
                    "Import the project as this installed or importable package, naming "
                    "modules <package>.<Section>.<name>, instead of adding DIRECTORY to "
                    "sys.path. The package must resolve to DIRECTORY."
                ),
            ),
        ),
        (
            "--project",
            Argument(
                action="store",
                metavar="FILE",
                type=str,
                default=None,
                help=(
                    "The project file whose sections configure the app, relative to the "
                    "working directory. Defaults to flowdash.yaml in DIRECTORY."
                ),
            ),
        ),
        (
            "--component-library",
            Argument(
                action="append",
                metavar="MODULE",
                default=None,
                help=(
                    "Offer the components of an installed library, e.g. my_library, "
                    "or module:attribute for a mapping of component id to component. "
                    "Repeat it for several libraries; they are added to "
                    "`component_libraries` in __init__.py."
                ),
            ),
        ),
        (
            "--agent-model",
            Argument(
                action="append",
                metavar="ID[=LABEL]",
                default=None,
                help=(
                    "A model the assistant may use, as pydantic-ai names it, e.g. "
                    "openai:<model>, optionally with a label: openai:<model>=Fast. "
                    "Repeat it, or separate models with commas, to let people pick "
                    "between several; the first is the default. Overrides `agent_model`, "
                    "`agent_models` and an `agent` model string in __init__.py."
                ),
            ),
        ),
        (
            "--title",
            Argument(
                action="store",
                type=str,
                default=None,
                help=(
                    "Application title shown in the browser tab. Overrides `title` in "
                    "__init__.py; defaults to 'FlowDash'."
                ),
            ),
        ),
        (
            "--home-dashboard",
            Argument(
                action="store",
                type=str,
                default=None,
                help=(
                    "Dashboard (id or title) to show on the homepage. Overrides "
                    "`home_dashboard` in __init__.py. When unset, the homepage shows "
                    "the dashboard grid."
                ),
            ),
        ),
        (
            "--nav-variant",
            Argument(
                action="store",
                type=str,
                default=None,
                choices=("rail", "left", "right", "menubar"),
                help=(
                    "How to render navigation: 'rail' (navigation rail and "
                    "breadcrumb), 'left' or 'right' (docked drawer on that side) "
                    "or 'menubar' (in the page header). Overrides `nav_variant` "
                    "in __init__.py; defaults to 'rail'."
                ),
            ),
        ),
        (
            "--admin-link",
            Argument(
                action="store_true",
                default=False,
                help=(
                    "Print a link that adds one admin even when the project already has "
                    "admins. By default the link is printed, and works, only while it has none."
                ),
            ),
        ),
        *((name, arg) for name, arg in _PanelServe.args if name not in _EXCLUDED_ARGS),
    )

    def invoke(self, args: argparse.Namespace):
        project_dir = Path(args.directory).resolve()
        if not project_dir.is_dir():
            print(f"ERROR: '{args.directory}' is not a directory.", file=sys.stderr)  # noqa: T201
            raise SystemExit(1)

        if args.project is not None:
            project_file = Path(args.project).resolve()
        else:
            project_file = find_project(cwd=project_dir)

        package = args.package
        if package is None:
            sys.path.insert(0, str(project_dir))
        os.chdir(str(project_dir))

        try:
            registry = build_registry(project_dir, package=package)
        except PackageResolutionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
            raise SystemExit(1) from None

        init_mod = _load_project_init(project_dir, package)
        options = _project_options(init_mod, project_dir)
        sources = dict.fromkeys(options, "__init__.py")
        for name in _CLI_OPTIONS:
            value = getattr(args, name)
            if value is not None:
                options[name] = value
                sources[name] = "--" + name.replace("_", "-")
        from panel_flowdash.agent.models import configured_models, parse_models

        try:
            if args.agent_model:
                models = parse_models(args.agent_model, "--agent-model")
                if models:
                    options["agent_model"] = models[0]["id"]
                    options["agent_models"] = models
                    sources["agent_model"] = sources["agent_models"] = "--agent-model"
                    # The flag names the default agent's model, also over a project's `agent = "<model>"`.
                    if isinstance(options.get("agent"), str):
                        del options["agent"]
            configured_models(options.get("agent_models"), options.get("agent_model"))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
            raise SystemExit(1) from None
        if args.component_library:
            libraries = list(options.get("component_libraries", ()))
            libraries += [name for name in args.component_library if name not in libraries]
            options["component_libraries"] = libraries
        if project_file is not None:
            log.info(f"Project file: {project_file}")
            options["project"] = project_file
        title = options.get("title", FlowDashApp.param.title.default)

        auth_config = AuthConfig.from_module(init_mod)
        # The admin view shows the fields the project's __init__.py sets as locked.
        sources["auth_config"] = "__init__.py"
        for name in ("default_allow", "admin_groups", "user_groups"):
            if hasattr(init_mod, name):
                sources[f"auth_config.{name}"] = "__init__.py"
        current_auth_config = _project_auth_config(
            auth_config,
            {k.split(".", 1)[1]: v for k, v in sources.items() if k.startswith("auth_config.")},
            project_file,
            project_dir,
        )

        store, store_description = _resolve_store(init_mod, project_dir, args.db_path)

        # Install the HTTP-boundary authorization callback for page routes.
        # SPA and dashboard routes are gated in-app by FlowDashApp.
        # Refused pages redirect to the app's own access-denied view.
        url_prefix = "/" + args.prefix.strip("/") if getattr(args, "prefix", None) else ""
        pn.config.authorize_callback = make_authorize_callback(
            registry, current_auth_config, denied_route=ACCESS_DENIED_ROUTE, url_prefix=url_prefix
        )

        # Pages whose permissions are not literals in the source are imported now
        # so their rules are known before the first request; one that fails to
        # import stays denied and is not imported again until restart.
        for entry in registry.values():
            if entry.metadata.auth_pending:
                try:
                    entry.load()
                except Exception as exc:
                    log.warning(
                        "Failed to import '%s'; access to it is denied: %s",
                        entry.app_id,
                        exc,
                    )

        if args.warm:
            for entry in registry.values():
                try:
                    entry.load()
                except Exception as exc:
                    log.warning("Failed to import '%s': %s", entry.app_id, exc)

        try:
            routes = FlowDashApp.build_routes(
                project_dir=project_dir,
                package=package,
                store=store,
                registry=registry,
                auth_config=auth_config,
                settings_sources=sources,
                **options,
            )
        except (ComponentLibraryError, ProjectError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
            raise SystemExit(1) from None

        log.info(f"Serving FlowDash from '{project_dir}' on port {args.port}")
        log.info(f"Dashboard store: {store_description}")

        self._apps = build_applications(routes, title=title, location=True)
        if options.get("admin_link", True) and args.admin_link:
            allow_adding_admins()
        if options.get("admin_link", True) and (
            args.admin_link
            or not (
                _admin_groups(current_auth_config) or _project_admins(project_file, project_dir)
            )
        ):
            address = getattr(args, "address", None)
            host = "localhost" if address in (None, "", "0.0.0.0", "::") else address
            show_admin_link(f"http://{host}:{args.port}{url_prefix}")

        args.files = []
        args.args = None
        super().invoke(args)

    def customize_applications(self, args, applications):
        return self._apps

    def customize_kwargs(self, args, server_kwargs):
        kwargs = super().customize_kwargs(args, server_kwargs)
        kwargs["extra_patterns"].append(
            (r"/.+/static/extensions/(.*)", MultiRootStaticHandler, dict(root=extension_dirs))
        )
        # Only requests no route matches reach the default handler, so unlike a
        # catch-all route it cannot shadow static files or login endpoints.
        kwargs.setdefault("default_handler_class", _NotFoundHandler)
        return kwargs
