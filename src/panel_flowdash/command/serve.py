"""The `flowdash serve` subcommand."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

import panel as pn

from panel_flowdash.app import build_app_class
from panel_flowdash.dashboard_store import DashboardStore

log = logging.getLogger(__name__)


class Serve:
    """Serve a flowdash dashboard application from a project directory."""

    name = "serve"
    help = "Launch the FlowDash dashboard server from a project directory."

    def __init__(self, parser: argparse.ArgumentParser):
        parser.add_argument(
            "directory",
            type=str,
            help="Path to the project directory containing page/component modules.",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=5006,
            help="Port to serve on (default: 5006).",
        )
        parser.add_argument(
            "--address",
            type=str,
            default=None,
            help="Address to bind to (default: 0.0.0.0).",
        )
        parser.add_argument(
            "--title",
            type=str,
            default="FlowDash",
            help="Application title shown in the browser tab.",
        )
        parser.add_argument(
            "--db-path",
            type=str,
            default=None,
            help="Path to the SQLite database file. Defaults to <directory>/dashboards.db.",
        )
        parser.add_argument(
            "--dev",
            action="store_true",
            help="Enable autoreload for development.",
        )
        parser.add_argument(
            "--admin",
            action="store_true",
            help="Enable the Panel admin interface.",
        )
        parser.add_argument(
            "--profiler",
            type=str,
            default=None,
            help="Profiler to use (e.g. pyinstrument, snakeviz).",
        )
        parser.add_argument(
            "--num-threads",
            type=int,
            default=None,
            help="Number of threads for the thread pool.",
        )
        parser.add_argument(
            "--allow-websocket-origin",
            type=str,
            nargs="*",
            default=None,
            help="Allowed websocket origins.",
        )

    def invoke(self, args: argparse.Namespace):
        project_dir = Path(args.directory).resolve()
        if not project_dir.is_dir():
            print(f"ERROR: '{args.directory}' is not a directory.")
            raise SystemExit(1)

        db_path = args.db_path or str(project_dir / "dashboards.db")

        sys.path.insert(0, str(project_dir))
        os.chdir(str(project_dir))

        if args.num_threads is not None:
            pn.config.nthreads = args.num_threads

        store = DashboardStore(db_path)
        AppClass = build_app_class(project_dir, store=store, title=args.title)

        serve_kwargs: dict[str, Any] = {
            "port": args.port,
            "address": args.address,
            "title": args.title,
            "location": True,
            "autoreload": args.dev,
        }

        if args.admin:
            serve_kwargs["admin"] = True
        if args.profiler:
            serve_kwargs["profiler"] = args.profiler
        if args.allow_websocket_origin:
            serve_kwargs["allow_websocket_origin"] = args.allow_websocket_origin

        routes = AppClass.build_routes()

        log.info(f"Serving FlowDash from '{project_dir}' on port {args.port}")
        log.info(f"Database: {db_path}")

        pn.serve(routes, **serve_kwargs)
