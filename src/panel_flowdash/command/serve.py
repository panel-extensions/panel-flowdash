"""The `flowdash serve` subcommand."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from bokeh.command.subcommand import Argument
from bokeh.embed.bundle import extension_dirs
from bokeh.server.views.multi_root_static_handler import MultiRootStaticHandler
from panel.command.serve import Serve as _PanelServe
from panel.io.application import build_applications

from panel_flowdash.app import build_app_class
from panel_flowdash.dashboard_store import DashboardStore

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

        store = DashboardStore(db_path)
        AppClass = build_app_class(project_dir, store=store, title=args.title)
        routes = AppClass.build_routes()

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
