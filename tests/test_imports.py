import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "modules",
    [
        ["panel_flowdash"],
        [
            "panel_flowdash",
            "panel_flowdash.app",
            "panel_flowdash.agent",
            "panel_flowdash.agent.actions",
        ],
    ],
)
def test_importing_flowdash_does_not_import_pydantic_ai(modules):
    code = (
        "import importlib, json, sys\n"
        f"for name in {modules!r}: importlib.import_module(name)\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'pydantic_ai' or m.startswith('pydantic_ai.'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
