"""A hidden transform editor keeps the issue it reports on the canvas up to date."""

from panel_flowdash.transforms import parse_transform
from panel_flowdash.ui.status_bar import select_on_canvas
from tests.test_transform_editor_previews import FRAME, _edges, checks  # noqa: F401


def _draft(view, steps):
    view._steps = parse_transform(steps)
    view._commit()


async def test_a_hidden_draft_is_rechecked_when_the_source_changes(checks):  # noqa: F811
    editor, source, (edge_id,) = _edges(1)
    select_on_canvas(editor._flow, edge_id=edge_id)
    view = editor._transform_editors[edge_id]
    _draft(view, [{"op": "column", "column": "extra"}])
    assert view.issue == "Column step"
    select_on_canvas(editor._flow)

    source.frame = FRAME.assign(extra=1)
    assert view.issue is None

    source.frame = FRAME
    assert view.issue == "Column step"


async def test_the_canvas_chip_follows_the_hidden_issue(checks):  # noqa: F811
    editor, source, (edge_id,) = _edges(1)
    select_on_canvas(editor._flow, edge_id=edge_id)
    view = editor._transform_editors[edge_id]
    _draft(view, [{"op": "column", "column": "extra"}])
    mapping = editor._edge_id_map[edge_id]
    flagged = editor._edge_chip(mapping)["style"]
    select_on_canvas(editor._flow)

    source.frame = FRAME.assign(extra=1)
    assert editor._edge_chip(mapping)["style"] != flagged


async def test_a_hidden_editor_without_a_draft_still_skips_the_check(checks):  # noqa: F811
    editor, source, (edge_id,) = _edges(1)
    select_on_canvas(editor._flow, edge_id=edge_id)
    select_on_canvas(editor._flow)

    checks.clear()
    source.frame = FRAME.head(1)
    assert checks == []
