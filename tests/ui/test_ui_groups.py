"""UI tests of groups on the wiring canvas: grouping a selection, collapsing and ungrouping."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.groups import group_node_id
from tests.test_ui_inspector import ROWS, TABLE, TABLE_COMPONENTS
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _editor():
    editor = FlowDash(TABLE_COMPONENTS, notifications=False, include_builtin_components=False)
    src = editor.add_component(ROWS, position=(0, 0))
    first = editor.add_component(TABLE, position=(360, 0))
    second = editor.add_component(TABLE, position=(360, 260))
    editor.connect(src, "rows", first, "rows")
    editor.connect(src, "rows", second, "rows")
    return editor, src, first, second


def _node(page, node_id):
    return page.locator(f".react-flow__node[data-id='{node_id}']")


def _box(page, node_id):
    return _node(page, node_id).bounding_box()


def test_grouping_the_selection_frames_it_and_keeps_the_nodes_in_place(page):
    editor, src, first, second = _editor()
    serve_component(page, editor)
    expect(_node(page, second)).to_be_visible()
    before = {n: _box(page, n) for n in (first, second)}

    _node(page, first).click()
    _node(page, second).click(modifiers=["Shift"])
    wait_until(lambda: editor._inspector._shown == ("nodes", (first, second)), timeout=8000)
    expect(page.get_by_text("2 components selected")).to_be_visible()
    page.get_by_role("button", name="Group", exact=True).click()

    wait_until(lambda: len(editor.groups) == 1, timeout=8000)
    (group_id,) = editor.groups
    frame = _node(page, group_node_id(group_id))
    expect(frame).to_be_visible()
    wait_until(lambda: editor._inspector._shown == ("group", group_id), timeout=8000)
    expect(page.get_by_text("Ungroup")).to_be_visible()
    frame_box = frame.bounding_box()
    for node_id, box in before.items():
        after = _box(page, node_id)
        assert abs(after["x"] - box["x"]) < 2 and abs(after["y"] - box["y"]) < 2, node_id
        assert (
            frame_box["x"] < after["x"]
            and after["x"] + after["width"] < frame_box["x"] + frame_box["width"]
        )
    assert editor.dirty

    _node(page, src).click()
    wait_until(lambda: editor._inspector._shown == ("node", src), timeout=8000)


def test_collapsing_draws_the_group_as_one_node_with_its_ports(page):
    editor, _src, first, second = _editor()
    group_id = editor.group_nodes([first, second], "Tables")
    serve_component(page, editor)
    frame = _node(page, group_node_id(group_id))
    expect(frame).to_contain_text("Tables")

    frame.get_by_role("button", name="Collapse group").click()
    wait_until(lambda: editor.groups[group_id].collapsed, timeout=8000)
    expect(_node(page, first)).to_be_hidden()
    expect(_node(page, second)).to_be_hidden()
    # Both connections from the source now end at the group's one input.
    expect(page.locator(".rf-edge--rerouted")).to_have_count(2)
    expect(frame.locator(".react-flow__handle[data-handleid='in:rows']")).to_have_count(1)

    frame.get_by_role("button", name="Expand group").click()
    wait_until(lambda: not editor.groups[group_id].collapsed, timeout=8000)
    expect(_node(page, first)).to_be_visible()
    expect(page.locator(".rf-edge--rerouted")).to_have_count(0)


def test_ungroup_from_the_inspector_keeps_the_nodes(page):
    editor, _src, first, second = _editor()
    group_id = editor.group_nodes([first, second], "Tables")
    serve_component(page, editor)
    frame = _node(page, group_node_id(group_id))
    frame.locator(".rf-group-header").click()
    wait_until(lambda: editor._inspector._shown == ("group", group_id), timeout=8000)
    before = _box(page, first)

    page.get_by_role("button", name="Ungroup").click()
    wait_until(lambda: editor.groups == {}, timeout=8000)
    expect(frame).to_have_count(0)
    expect(_node(page, first)).to_be_visible()
    after = _box(page, first)
    assert abs(after["x"] - before["x"]) < 2 and abs(after["y"] - before["y"]) < 2
    assert len(editor._tile_items) == 3
