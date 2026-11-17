"""UI tests that send raw input must wait for Panel's loading overlay first."""

import ast
from pathlib import Path

UI_TESTS = Path(__file__).parent / "ui"


def _sends_raw_input(tree):
    return any(
        isinstance(node, ast.Attribute) and node.attr in ("mouse", "keyboard")
        for node in ast.walk(tree)
    )


def _serves_without_waiting(tree):
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "panel.tests.util"
        and any(alias.name == "serve_component" for alias in node.names)
        for node in ast.walk(tree)
    )


def test_raw_input_tests_wait_for_the_loading_overlay():
    # page.mouse and page.keyboard skip Playwright's actionability checks, so
    # until the overlay hides they land on it and the app never sees them.
    # tests/ui/util.serve_component waits for it; Panel's does not.
    offenders = []
    for path in sorted(UI_TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _sends_raw_input(tree) and _serves_without_waiting(tree):
            offenders.append(path.name)
    assert offenders == []
