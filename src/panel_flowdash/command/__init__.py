"""Command-line interface for panel-flowdash."""

import argparse
import sys

from panel_flowdash import __version__
from panel_flowdash.command.serve import Serve


def main(args: list[str] | None = None):
    """Entry point for the panel-flowdash CLI."""
    parser = argparse.ArgumentParser(
        prog="flowdash",
        description="Panel FlowDash: visual dataflow dashboard builder.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("-v", "--version", action="version", version=__version__)
    subs = parser.add_subparsers(help="Sub-commands")

    serve_parser = subs.add_parser(Serve.name, help=Serve.help)
    serve_cmd = Serve(parser=serve_parser)
    serve_parser.set_defaults(invoke=serve_cmd.invoke)

    if args is None:
        args = sys.argv[1:]

    if not args:
        parser.print_help()
        sys.exit(1)

    parsed = parser.parse_args(args)
    if not hasattr(parsed, "invoke"):
        parser.print_help()
        sys.exit(1)

    parsed.invoke(parsed)


if __name__ == "__main__":
    main()
