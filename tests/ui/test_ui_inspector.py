"""UI tests for the compact node cards, the inspector and the status bar of the wiring canvas."""

import re

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
    page.mouse.move(1, 1)
    expect(page.get_by_role("tooltip")).to_have_count(0)
    info = page.locator(".fd-inspector-info button")
    page.locator(".fd-inspector-title").click()
    page.keyboard.press("Tab")
    expect(info).to_be_focused()
    expect(page.get_by_role("tooltip", name="Shows rows.")).to_be_visible()
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


@pytest.mark.parametrize("width", [1024, 900, 880])
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
    done = page.get_by_role("button", name="Done")
    done.wait_for()
    # Down to 900 the eye icon keeps its label; below, its tooltip names it.
    labelled = done.evaluate("b => getComputedStyle(b).fontSize") != "0px"
    assert labelled == (width >= 900)
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


# The vertical centres of the status bar's texts and icons.
_BAR_CENTRES = """() => {
  const all = (root, acc = []) => {
    root.querySelectorAll('*').forEach(el => { acc.push(el); if (el.shadowRoot) all(el.shadowRoot, acc); });
    return acc;
  };
  const bar = all(document).find(el => el.classList && el.classList.contains('fd-status-bar'));
  const box = bar.getBoundingClientRect();
  const centre = el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
  const inside = el => { const r = el.getBoundingClientRect();
    return r.height > 0 && r.top >= box.top && r.bottom <= box.bottom; };
  const items = all(bar.shadowRoot).filter(el => el.matches && inside(el) && (
    el.matches('.MuiIcon-root, .MuiTypography-root, .MuiButton-root, .MuiToggleButton-root')));
  return {bar: centre(bar), items: items.map(el => [el.className, centre(el)])};
}"""


# Narrower than the editor lets itself become, so the status bar is at its narrowest.
_MIN_WIDTH = 600


def _toggle_width(page, text):
    return page.locator(".fd-status-bar .MuiToggleButton-root", has_text=text).bounding_box()[
        "width"
    ]


def test_status_bar_items_share_one_centred_line(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    expect(page.locator(".fd-status-bar").get_by_text("All outputs up to date")).to_be_visible()
    centres = page.evaluate(_BAR_CENTRES)
    assert len(centres["items"]) >= 6
    for name, centre in centres["items"]:
        assert abs(centre - centres["bar"]) <= 1.5, name


def test_view_controls_show_labels_only_when_the_bar_has_room(page):
    page.set_viewport_size({"width": 1500, "height": 800})
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    previews = page.locator(".fd-status-bar .MuiToggleButton-root", has_text="Previews")
    expect(previews).to_be_visible()
    expect(page.locator(".fd-status-bar").get_by_text("Collapse all")).to_be_visible()
    assert _toggle_width(page, "Previews") > 60

    page.set_viewport_size({"width": 820, "height": 800})
    wait_until(lambda: _toggle_width(page, "Previews") < 40, timeout=8000)
    assert _toggle_width(page, "Collapse all") < 40


def test_view_controls_stay_inside_the_bar_at_the_minimum_width(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    page.set_viewport_size({"width": 1200, "height": 800})
    serve_component(page, editor)

    page.set_viewport_size({"width": _MIN_WIDTH, "height": 800})
    bar = page.locator(".fd-status-bar")
    zoom = bar.get_by_text("Zoom ")
    expect(page.locator(".fd-keys")).to_be_hidden()

    def inside():
        outer, inner = bar.bounding_box(), zoom.bounding_box()
        return inner["width"] > 0 and inner["x"] + inner["width"] <= outer["x"] + outer["width"]

    wait_until(inside, timeout=8000)


def test_view_controls_follow_the_cards(page):
    editor, src, table = _editor()
    editor.connect(src, "rows", table, "rows")
    serve_component(page, editor)

    collapse = page.locator(".fd-status-bar .MuiToggleButton-root", has_text="Collapse all")
    collapse.click()
    wait_until(lambda: all(card.collapsed for card in editor._node_cards().values()), timeout=8000)
    # Drawn pressed while every card is collapsed, which its ARIA state says too.
    expect(collapse).to_have_attribute("aria-pressed", "true")
    # The name stays the visible label (WCAG 2.5.3); aria-pressed carries the state.
    expect(collapse).to_have_attribute("aria-label", "Collapse all nodes (C)")
    expect(collapse).to_have_class(re.compile(r"\bMui-selected\b"))

    previews = page.locator(".fd-status-bar .MuiToggleButton-root", has_text="Previews")
    shown = editor._status_bar.preview_toggle.value
    expect(previews).to_have_attribute("aria-pressed", str(shown).lower())
    editor._status_bar.toggle_previews()
    expect(previews).to_have_attribute("aria-pressed", str(not shown).lower())
