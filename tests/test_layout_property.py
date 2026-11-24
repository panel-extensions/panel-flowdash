"""The layout the agent's tools report is the layout the editor shows, saves and restores.

Random requests of add_node, remove_node, place_tile and arrange_tiles, with
titles among the tiles, run through the agent's tools. After every call the
reported layout must match the tile grid's, after the request ends it must
match the saved dashboard's, and undo and redo must bring it back.
"""

import os
import random

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from panel_flowdash import FlowDash, auto_layout
from panel_flowdash.agent import DashboardOps
from panel_flowdash.agent.ops import _tile_text
from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.text_components import TEXT_COMPONENTS

from .test_agent_ops import COMPONENTS
from .test_editor_auto_layout import EXTRA

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

KINDS = {"chart": "V/chart", "kpi": "V/headline", "table": "V/table"}


def _editor():
    editor = FlowDash(
        components={**COMPONENTS, **EXTRA, **TEXT_COMPONENTS},
        include_builtin_components=False,
        notifications=False,
        user="alice",
    )
    editor.new_dashboard("Mine")
    # The tile grid is built while it is on screen.
    editor.mode = "dashboard"
    return editor


def _rows(text):
    return [line for line in text.splitlines() if line.startswith("Row")]


def _grid_rows(ops):
    """Describe the rows of the tile grid as it is given to the browser, packed as it packs them."""
    editor = ops.editor
    grid = editor._tile_grid
    names = ops._names()
    shown = [
        {**entry, "instance_id": iid}
        for iid, entry in zip(editor._grid_ids, grid.layout, strict=False)
        if iid is not None and entry.get("visible", True) is not False
    ]
    # A tile without an index yet, e.g. one waiting for the request to end, follows the others.
    position = {id(e): n for n, e in enumerate(shown)}
    shown.sort(key=lambda e: (e.get("index") is None, e.get("index") or 0, position[id(e)]))
    lines = []
    for number, row in enumerate(auto_layout.pack_rows(shown), start=1):
        tiles = []
        for entry, under in row:
            entry = {k: v for k, v in entry.items() if k in ("instance_id", "width", "height")}
            if under is not None:
                entry["under"] = shown[under]["instance_id"]
            tiles.append(_tile_text(names, entry))
        lines.append(f"Row {number}: {' | '.join(tiles)}")
    return lines


def _request_calls(rng, names, counter):
    calls = []
    for _ in range(rng.randint(1, 4)):
        op = rng.choice(["add", "add", "title", "remove", "place", "place", "place", "arrange"])
        if op == "add" or (op in ("remove", "place") and not names):
            kind = rng.choice(list(KINDS))
            counter[0] += 1
            name = f"{kind}{counter[0]}"
            names.append(name)
            calls.append(("add_node", {"component": KINDS[kind], "name": name}))
        elif op == "title":
            counter[0] += 1
            name = f"title{counter[0]}"
            names.append(name)
            config = {"text": "Fleet", "size": "Title"}
            calls.append(
                ("add_node", {"component": "Text/Heading", "name": name, "config": config})
            )
        elif op == "remove":
            name = names.pop(rng.randrange(len(names)))
            calls.append(("remove_node", {"name": name}))
        elif op == "place":
            name = rng.choice(names)
            args = {"name": name}
            for key, values in (
                ("width", [25, 33, 40, 50, 60, 65, 100, 0]),
                ("height", [130, 240, 400, 600]),
                ("index", [0, 1, 2, 5]),
            ):
                if rng.random() < 0.4:
                    args[key] = rng.choice(values)
            if rng.random() < 0.25:
                args["area"] = rng.choice(["top", "top", "grid"])
            elif rng.random() < 0.15 and len(names) > 1 and "index" not in args:
                args["before"] = rng.choice([n for n in names if n != name])
            calls.append(("place_tile", args))
        else:
            calls.append(("arrange_tiles", {}))
    return calls


async def _run(ops, calls, checks):
    def model(messages, info):
        done = sum(
            isinstance(p, (ToolReturnPart, RetryPromptPart))
            for m in messages
            for p in getattr(m, "parts", [])
        )
        if done:
            report = _rows(ops.describe_layout())
            checks.append((calls[done - 1], report, _grid_rows(ops)))
        if done < len(calls):
            name, args = calls[done]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"call{done}")])
        return ModelResponse(parts=[TextPart("Done.")])

    agent = Agent(
        FunctionModel(model), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()], retries=10
    )
    deps = FlowDashDeps(ops)
    deps.start_request("Lay it out")
    try:
        await agent.run("Lay it out", deps=deps)
    finally:
        deps.end_request()


def _saved_rows(ops):
    editor = _editor()
    try:
        editor.load_model(ops.editor.to_model())
        return _rows(DashboardOps(editor).describe_layout())
    finally:
        editor.dispose()


@pytest.mark.parametrize("seed", range(12))
async def test_the_reported_layout_is_the_layout(seed):
    rng = random.Random(seed)
    editor = _editor()
    ops = DashboardOps(editor)
    names: list[str] = []
    counter = [0]
    try:
        for _ in range(8):
            calls = _request_calls(rng, names, counter)
            checks = []
            await _run(ops, calls, checks)
            for call, report, grid in checks:
                assert report == grid, f"after {call}"
            editor.edit_history.flush()
            final = _rows(ops.describe_layout())
            assert final == _grid_rows(ops), f"after the request {calls}"
            assert final == _saved_rows(ops), f"saved after {calls}"
            editor.undo()
            editor.redo()
            assert _rows(ops.describe_layout()) == final, f"undo and redo of {calls}"
            names[:] = [n for n in names if n in ops._names().values()]
    finally:
        editor.dispose()
