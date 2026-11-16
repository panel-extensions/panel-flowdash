"""UI tests for the compact node cards, the inspector and the status bar of the wiring canvas."""

import pytest
from panel.tests.util import serve_component, wait_until

from panel_flowdash.editor import FlowDash
from tests.test_ui_inspector import ROWS, TABLE, TABLE_COMPONENTS

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The inspector swaps in new widgets on every selection, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# Pairs each port label on a card with the ReactFlow handle of the same port.
_ALIGNMENT = """() => {
  const all = (root, selector, acc = []) => {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) all(el.shadowRoot, selector, acc); });
    root.querySelectorAll(selector).forEach(el => acc.push(el));
    return acc;
  };
  const centre = el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
  const pairs = [];
  for (const node of all(document, '.react-flow__node')) {
    const labels = {};
    for (const port of all(node, '.fd-port')) {
      labels[(port.classList.contains('fd-in') ? 'target:' : 'source:') + port.dataset.port] = centre(port);
    }
    for (const handle of node.querySelectorAll('.react-flow__handle')) {
      const side = handle.classList.contains('react-flow__handle-left') ? 'target:' : 'source:';
      pairs.push([side + handle.dataset.handleid, centre(handle), labels[side + handle.dataset.handleid]]);
    }
  }
  return pairs;
}"""


def _editor():
    editor = FlowDash(TABLE_COMPONENTS, notifications=False, include_builtin_components=False)
    src = editor.add_component(ROWS, position=(0, 0))
    table = editor.add_component(TABLE, position=(320, 0))
    return editor, src, table


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def test_nodes_are_compact_cards_with_labels_beside_their_handles(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    node = _node(page, table)
    expect(node).to_contain_text("Table")
    expect(node).to_contain_text("Rows")
    expect(node).to_contain_text("DataFrame")
    expect(node).to_contain_text("Showing 3 rows, 2 columns")
    # The live view is only in the tile grid.
    expect(node).not_to_contain_text("Turbines")
    expect(_node(page, src)).to_contain_text("3 rows, 2 columns")

    # Rows has a `size` input and a `rows` output, Table a `rows` input.
    wait_until(lambda: len(page.evaluate(_ALIGNMENT)) == 3, timeout=8000)
    for port, handle, label in page.evaluate(_ALIGNMENT):
        assert label is not None, port
        assert abs(handle - label) < 1, port


def test_selecting_a_node_shows_its_config_and_editing_it_updates_the_component(page):
    editor, _src, table = _editor()
    serve_component(page, editor)

    expect(page.get_by_text("Issues (1)")).to_be_visible()
    _node(page, table).click()
    wait_until(lambda: editor._inspector._shown == ("node", table), timeout=8000)
    expect(page.get_by_text("Shows rows.")).to_be_visible()
    expect(page.get_by_text("Needs Rows").first).to_be_visible()

    title = page.get_by_role("textbox", name="Title")
    expect(title).to_have_value("Turbines")
    title.fill("Capacity")
    title.press("Enter")
    wait_until(lambda: editor._component_instances[table].title == "Capacity", timeout=8000)
    assert editor._tile_items[1]["config"] == {"title": "Capacity"}
    assert editor.dirty


def test_remove_from_dashboard(page):
    editor, _src, table = _editor()
    serve_component(page, editor)

    _node(page, table).click()
    page.get_by_role("button", name="Remove from dashboard").click()
    wait_until(lambda: [i["instance_id"] for i in editor._tile_items] == [_src], timeout=8000)
    expect(_node(page, table)).to_have_count(0)
    expect(page.get_by_text("1 component, 0 connections").first).to_be_visible()


def test_status_bar_counts_issues_and_selects_the_first(page):
    editor = FlowDash(TABLE_COMPONENTS, notifications=False, include_builtin_components=False)
    src = editor.add_component(ROWS, position=(0, 0))
    serve_component(page, editor)

    expect(page.get_by_role("button", name="All outputs up to date")).to_be_visible()
    page.locator(".MuiListItemButton-root").filter(has_text="Table").click()
    wait_until(lambda: len(editor._tile_items) == 2, timeout=8000)
    table = editor._tile_items[1]["instance_id"]
    issues = page.get_by_role("button", name="1 issue")
    expect(issues).to_be_visible()
    expect(page.get_by_text("2 components, 0 connections").first).to_be_visible()

    # Selecting from Python once the page has settled used to be undone by the
    # page reporting its previous, empty selection.
    page.locator(".react-flow__pane").click(position={"x": 10, "y": 10})
    page.wait_for_timeout(1000)
    issues.click()
    page.wait_for_timeout(1000)
    wait_until(
        lambda: "selected" in (_node(page, table).get_attribute("class") or ""), timeout=8000
    )
    wait_until(lambda: editor._flow.selection["nodes"] == [table], timeout=8000)
    wait_until(lambda: editor._inspector._shown == ("node", table), timeout=8000)

    editor.connect(src, "rows", table, "rows")
    expect(page.get_by_role("button", name="All outputs up to date")).to_be_visible()
    expect(page.get_by_text("2 components, 1 connection").first).to_be_visible()


def test_port_value_opens_on_click_without_covering_the_inspector(page):
    editor = FlowDash(TABLE_COMPONENTS, notifications=False, include_builtin_components=False)
    table = editor.add_component(TABLE, position=(0, 0))
    # The source sits at the right edge of the fitted graph, next to the inspector.
    src = editor.add_component(ROWS, position=(320, 0))
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    handle = _node(page, src).locator(".react-flow__handle-right")
    handle.hover()
    page.wait_for_timeout(1000)
    popup = page.locator(".rf-value-popup")
    expect(popup).to_have_count(0)

    handle.click()
    expect(popup).to_be_visible()
    expect(popup).to_contain_text("3 rows")
    box = popup.bounding_box()
    canvas = page.locator(".react-flow").first.bounding_box()
    assert box["x"] + box["width"] <= canvas["x"] + canvas["width"] + 1


def test_minimap_stays_off_the_fitted_nodes_until_switched_on(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    expect(_node(page, table)).to_be_visible()
    expect(page.locator(".react-flow__minimap")).to_have_count(0)
    editor._status_bar.minimap_toggle.value = True
    expect(page.locator(".react-flow__minimap")).to_have_count(1)
