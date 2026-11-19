"""UI tests of groups on the wiring canvas: grouping a selection, collapsing and ungrouping."""

import panel as pn
import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from panel_flowdash.groups import group_node_id
from tests.test_ui_inspector import ROWS, TABLE, TABLE_COMPONENTS
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Items", provides=[{"key": "items", "type": "list"}])
def items(config):
    config.items = [1, 2]
    return pn.pane.Str("items")


@register(page=False, component=True, title="Merge", requires=[{"key": "items", "type": "list"}])
def merge(config):
    return pn.pane.Str("merge")


COMPONENTS = {**TABLE_COMPONENTS, "G/items": items, "G/merge": merge}


def _editor():
    editor = FlowDash(
        COMPONENTS, notifications=False, include_builtin_components=False, history_coalesce=0
    )
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


def _serve(page, editor):
    """Serve *editor* with a button to undo, which a bare editor lacks."""
    undo = pmui.Button(label="Undo for test")
    undo.on_click(lambda _event: editor.undo(whole_runs=False))
    serve_component(page, pn.Column(undo, editor, sizing_mode="stretch_both"))


def test_connecting_to_a_collapsed_port_wires_the_inner_port(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    a = editor.add_component("G/items", position=(0, 0))
    b = editor.add_component("G/items", position=(0, 300))
    m = editor.add_component("G/merge", position=(400, 0))
    editor.connect(a, "items", m, "items")
    group_id = editor.group_nodes([m], "Merged", collapsed=True)
    serve_component(page, editor)
    frame = _node(page, group_node_id(group_id))
    target = frame.locator(".react-flow__handle[data-handleid='in:items']")
    expect(target).to_have_count(1)
    expect(target).to_have_attribute("data-tooltip", "items (list)")

    source = _node(page, b).locator(".react-flow__handle[data-handleid='items']").bounding_box()
    end = target.bounding_box()
    page.mouse.move(source["x"] + source["width"] / 2, source["y"] + source["height"] / 2)
    page.mouse.down()
    page.mouse.move(end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=12)
    page.mouse.up()
    wait_until(
        lambda: (
            (b, "items", m, "items")
            in {
                (e["source"], e["source_port"], e["target"], e["target_port"])
                for e in editor._dataflow_graph.edges
            }
        ),
        timeout=8000,
    )
    assert editor.groups[group_id].collapsed


def test_undo_brings_back_the_expanded_group_and_its_label(page):
    editor, _src, first, second = _editor()
    group_id = editor.group_nodes([first, second], "Tables")
    _serve(page, editor)
    frame = _node(page, group_node_id(group_id))

    frame.get_by_role("button", name="Collapse group").click()
    wait_until(lambda: editor.groups[group_id].collapsed, timeout=8000)
    expect(_node(page, first)).to_be_hidden()
    page.get_by_role("button", name="Undo for test").click()
    wait_until(lambda: not editor.groups[group_id].collapsed, timeout=8000)
    expect(_node(page, first)).to_be_visible()

    frame.locator(".rf-group-header").click()
    wait_until(lambda: editor._inspector._shown == ("group", group_id), timeout=8000)
    label = page.get_by_label("Label")
    label.fill("Renamed")
    label.press("Enter")
    expect(frame.locator(".rf-group-label")).to_have_text("Renamed")
    page.get_by_role("button", name="Undo for test").click()
    wait_until(lambda: editor.groups[group_id].title == "Tables", timeout=8000)
    expect(frame.locator(".rf-group-label")).to_have_text("Tables")


def test_delete_on_a_frame_ungroups_it_and_keeps_the_connections(page):
    editor, _src, first, second = _editor()
    edges = set(editor._edge_id_map.values())
    group_id = editor.group_nodes([first, second], "Tables")
    serve_component(page, editor)
    frame = _node(page, group_node_id(group_id))
    frame.locator(".rf-group-header").click()
    wait_until(lambda: editor._inspector._shown == ("group", group_id), timeout=8000)
    expect(page.get_by_role("button", name="Delete group and its 2 components")).to_be_visible()
    page.keyboard.press("Delete")
    wait_until(lambda: editor.groups == {}, timeout=8000)
    expect(frame).to_have_count(0)
    expect(_node(page, first)).to_be_visible()
    expect(_node(page, second)).to_be_visible()
    assert len(editor._tile_items) == 3
    assert set(editor._edge_id_map.values()) == edges
    expect(page.locator(".react-flow__edge")).to_have_count(len(edges))


def test_the_inspector_deletes_a_group_with_its_components(page):
    editor, src, first, second = _editor()
    group_id = editor.group_nodes([first, second], "Tables")
    serve_component(page, editor)
    frame = _node(page, group_node_id(group_id))
    frame.locator(".rf-group-header").click()
    wait_until(lambda: editor._inspector._shown == ("group", group_id), timeout=8000)
    page.get_by_role("button", name="Delete group and its 2 components").click()
    wait_until(lambda: editor.groups == {}, timeout=8000)
    expect(frame).to_have_count(0)
    expect(_node(page, first)).to_have_count(0)
    expect(_node(page, src)).to_be_visible()
    assert [i["instance_id"] for i in editor._tile_items] == [src]
