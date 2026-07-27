"""The `flowdash serve` subcommand."""

from __future__ import annotations

import argparse
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

from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, make_authorize_callback
from panel_flowdash.dashboard_store import DashboardStore
from panel_flowdash.registry import build_registry

log = logging.getLogger(__name__)

_EXCLUDED_ARGS = ("files", "--args")


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
                help="Path to the SQLite database file. Defaults to <directory>/dashboards.db.",
            ),
        ),
        (
            "--title",
            Argument(
                action="store",
                type=str,
                default="FlowDash",
                help="Application title shown in the browser tab.",
            ),
        ),
        (
            "--home-dashboard",
            Argument(
                action="store",
                type=str,
                default=None,
                help=(
                    "Dashboard (id or title) to show on the homepage. "
                    "When unset, the homepage shows the dashboard grid."
                ),
            ),
        ),
        (
            "--nav-variant",
            Argument(
                action="store",
                type=str,
                default="drawer",
                choices=("drawer", "menubar"),
                help=(
                    "Where to render the navigation menu: 'drawer' (docked "
                    "right-hand drawer) or 'menubar' (in the page header)."
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

        db_path = args.db_path or str(project_dir / "dashboards.db")

        sys.path.insert(0, str(project_dir))
        os.chdir(str(project_dir))

        configure_layout = None
        init_mod = None
        init_file = project_dir / "__init__.py"
        if init_file.exists():
            spec = importlib.util.spec_from_file_location("__init__", init_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            init_mod = mod
            configure_layout = getattr(mod, "configure_layout", None)

        auth_config = AuthConfig.from_module(init_mod)

        store = DashboardStore(db_path)

        registry = build_registry(project_dir)

        # Install the HTTP-boundary authorization callback for page routes.
        # SPA and dashboard routes are gated in-app by FlowDashApp.
        pn.config.authorize_callback = make_authorize_callback(registry, auth_config)

        if args.warm:
            for entry in registry.values():
                try:
                    entry.load()
                except Exception as exc:
                    log.warning("Failed to import '%s': %s", entry.app_id, exc)

        routes = FlowDashApp.build_routes(
            project_dir=project_dir,
            store=store,
            title=args.title,
            home_dashboard=args.home_dashboard,
            nav_variant=args.nav_variant,
            registry=registry,
            configure_layout=configure_layout,
            auth_config=auth_config,
        )

        log.info(f"Serving FlowDash from '{project_dir}' on port {args.port}")
        log.info(f"Database: {db_path}")

        self._apps = build_applications(routes, title=args.title, location=True)

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
        return kwargs
