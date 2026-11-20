"""UI tests for the compact node cards, the inspector and the status bar of the wiring canvas."""

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from tests.test_ui_inspector import ROWS, TABLE, TABLE_COMPONENTS
from tests.ui.util import center, serve_component, stable_box

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
    # Type text is no longer printed inline on port rows; it is shown on handle color and hover tooltips.
    expect(node).not_to_contain_text("DataFrame")
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
    expect(page.locator(".fd-inspector-header")).to_be_visible()
    expect(page.get_by_text("Configuration", exact=True)).to_have_count(0)
    # The description and the status show on hover, are named for keyboard and
    # screen reader users, and a status other than up to date is also spelled out.
    page.locator(".fd-inspector-info").hover()
    expect(page.get_by_role("tooltip", name="Shows rows.")).to_be_visible()
    page.locator(".fd-inspector-status").hover()
    expect(page.get_by_role("tooltip", name="Needs Rows")).to_be_visible()
    info = page.get_by_role("img", name="Shows rows.")
    info.focus()
    expect(info).to_be_focused()
    dot = page.get_by_role("img", name="Status: Needs Rows")
    dot.focus()
    expect(dot).to_be_focused()
    expect(page.get_by_role("status").filter(has_text="Needs Rows")).to_be_visible()

    title = page.get_by_role("textbox", name="Title")
    expect(title).to_have_value("Turbines")
    title.fill("Capacity")
    title.press("Enter")
    wait_until(lambda: editor._component_instances[table].title == "Capacity", timeout=8000)
    assert editor._tile_items[1]["config"] == {"title": "Capacity"}
    assert editor.dirty


@register(page=False, component=True, title="Query", config=["table", "limit"])
class _Query(Viewer):
    table = param.String(default="turbines")
    limit = param.Integer(default=None, allow_None=True)

    def __panel__(self):
        return self.table


def test_fields_are_compact_and_a_number_shares_a_row(page):
    editor = FlowDash(
        {"Demo/query": _Query}, notifications=False, include_builtin_components=False
    )
    node = editor.add_component("Demo/query", position=(0, 0))
    serve_component(page, editor)

    _node(page, node).click()
    fields = page.locator(".fd-field-pair .MuiInputBase-root")
    expect(fields).to_have_count(2, timeout=8000)
    expect(fields.first.locator("input")).to_have_value("turbines")
    title_box, size_box = stable_box(fields.first), stable_box(fields.last)
    assert abs(title_box["y"] - size_box["y"]) < 1
    assert title_box["x"] + title_box["width"] < size_box["x"]
    for box in (title_box, size_box):
        assert 34 <= box["height"] <= 38

    page.get_by_role("tab", name="Tile").click()
    for name in ("Width", "Height", "Frame"):
        select = page.get_by_role("combobox", name=name)
        assert 34 <= stable_box(select)["height"] <= 38, name


@pytest.mark.parametrize("width", [1024, 900])
def test_the_header_actions_stay_on_screen_in_a_narrow_window(page, width):
    from panel_flowdash import DashboardItem, MemoryDashboardStore
    from panel_flowdash.app import FlowDashApp
    from panel_flowdash.auth import resolve_identity
    from panel_flowdash.component_library import normalize_components

    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Rows")
    model.items = [DashboardItem(instance_id="table", component_id=TABLE, x=0, y=0)]
    store.save_dashboard(model)

    def factory():
        return FlowDashApp(
            registry=normalize_components(TABLE_COMPONENTS), store=store, notifications=False
        )

    page.set_viewport_size({"width": width, "height": 800})
    serve_component(
        page,
        {"/": factory, "/dash/[^/]+": factory},
        suffix=f"/dash/{model.dashboard_id}?edit=true",
    )
    page.get_by_role("button", name="Done").wait_for()
    _node(page, "table").click()
    close = page.get_by_role("button", name="Hide inspector")
    expect(close).to_be_visible()
    for part in (page.locator(".fd-status-dot"), page.locator(".fd-inspector-menu button"), close):
        box = stable_box(part)
        assert box["x"] + box["width"] <= width, (width, box)
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    page.locator(".fd-inspector-menu button").click()
    expect(page.get_by_role("menuitem", name="Remove from dashboard")).to_be_visible()


def test_remove_from_dashboard(page):
    editor, _src, table = _editor()
    serve_component(page, editor)

    _node(page, table).click()
    wait_until(lambda: editor._inspector._shown == ("node", table), timeout=8000)
    # Remove lives only in the overflow menu, not as a separate button.
    expect(page.get_by_role("button", name="Remove from dashboard")).to_have_count(0)
    page.locator(".fd-inspector-menu button, button.fd-inspector-menu").first.click()
    page.get_by_role("menuitem", name="Remove from dashboard").click()
    wait_until(lambda: [i["instance_id"] for i in editor._tile_items] == [_src], timeout=8000)
    expect(_node(page, table)).to_have_count(0)
    expect(page.get_by_text("1 component, 0 connections").first).to_be_visible()


def test_wired_input_shows_live_data_summary(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    _node(page, table).click()
    wait_until(lambda: editor._inspector._shown == ("node", table), timeout=8000)
    summary = page.locator(".fd-input-rows .fd-input-summary")
    expect(summary).to_contain_text("3 rows \u00d7 2")
    expect(summary).to_contain_text("a")
    expect(summary).to_contain_text("b")


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


def test_a_config_its_check_refuses_is_held_back_with_an_alert(page):
    from tests.test_config_check import COMPONENTS, QUERY

    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    query = editor.add_component(QUERY, position=(0, 0))
    serve_component(page, editor)

    _node(page, query).click()
    wait_until(lambda: editor._inspector._shown == ("node", query), timeout=8000)
    sql = page.get_by_role("textbox", name="Sql")
    expect(sql).to_have_value("SELECT 1")
    sql.fill("DROP t")
    sql.press("Enter")
    alert = page.locator(".fd-config-check-alert")
    expect(alert).to_contain_text("Only SELECT statements are allowed.")
    expect(sql).to_have_value("DROP t")
    assert editor._component_instances[query].sql == "SELECT 1"
    assert "config" not in editor._tile_items[0] or not editor._tile_items[0]["config"]

    sql.fill("SELECT 2")
    sql.press("Enter")
    wait_until(lambda: editor._component_instances[query].sql == "SELECT 2", timeout=8000)
    expect(alert).to_have_count(0)
    assert editor._tile_items[0]["config"] == {"sql": "SELECT 2"}


def test_connecting_an_input_shows_the_alert_its_check_raises(page):
    from tests.test_config_check import COMPONENTS, LIMIT, QUERY

    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    query = editor.add_component(QUERY, position=(320, 0))
    limit = editor.add_component(LIMIT, position=(0, 0))
    serve_component(page, editor)

    _node(page, query).click()
    wait_until(lambda: editor._inspector._shown == ("node", query), timeout=8000)
    expect(page.get_by_role("textbox", name="Sql")).to_have_value("SELECT 1")
    alert = page.locator(".fd-config-check-alert")
    expect(alert).to_have_count(0)

    output = _node(page, limit).locator(".react-flow__handle-right[data-handleid='limit']")
    input_handle = _node(page, query).locator(".react-flow__handle-left[data-handleid='limit']")
    x, y = center(stable_box(output, page))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 25, y)
    page.mouse.move(*center(input_handle.bounding_box()))
    page.mouse.up()
    wait_until(lambda: len(editor.graph.edges) == 1, timeout=8000)

    expect(alert).to_contain_text("rows must be at most 5")
    assert editor._inspector._shown == ("node", query)
    assert editor.graph.get_config_state(query).rows == 10

    editor.disconnect(limit, "limit", query, "limit")
    expect(alert).to_have_count(0)
