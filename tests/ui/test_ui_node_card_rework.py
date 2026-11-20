"""UI tests for stacked-port node cards, output switching, folding, collapse and pop-out."""

import logging
import threading

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.test_node_card_rework import COMPONENTS, _card
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

_ALIGN_VISIBLE = """() => {
  const all = (root, selector, acc = []) => {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) all(el.shadowRoot, selector, acc); });
    root.querySelectorAll(selector).forEach(el => acc.push(el));
    return acc;
  };
  const centre = el => { const r = el.getBoundingClientRect(); return r.top + r.height / 2; };
  const results = [];
  for (const node of all(document, '.react-flow__node')) {
    const labels = {};
    for (const port of all(node, '.fd-port[data-port]')) {
      labels[(port.classList.contains('fd-in') ? 'L:' : 'R:') + port.dataset.port] = centre(port);
    }
    for (const h of node.querySelectorAll('.react-flow__handle')) {
      if (h.classList.contains('fd-handle-hidden')) continue;
      const key = (h.classList.contains('react-flow__handle-left') ? 'L:' : 'R:') + h.dataset.handleid;
      if (key in labels) {
        results.push([node.dataset.id, key, centre(h) - labels[key]]);
      }
    }
  }
  return results;
}"""


def _selected(editor):
    return [n["id"] for n in editor._flow.nodes if n.get("selected")]


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _assert_aligned(page):
    def settled():
        rows = page.evaluate(_ALIGN_VISIBLE)
        return bool(rows) and all(abs(diff) < 1.5 for *_, diff in rows)

    wait_until(settled, page, timeout=8000)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_stacked_card_interactions_handles_and_popout(page, scheme, monkeypatch, caplog):
    page.emulate_media(color_scheme=scheme)
    src, sql, join, kpi, vega = (
        "turbines",
        "turbines_by_year",
        "state_join",
        "total_capacity",
        "capacity_chart",
    )
    # Previews are built on worker threads, but the cards and the pop-out only
    # change on the session's event loop.
    threads = set()
    show_popout = FlowDash._show_card_popout

    def record_popout(self, *args):
        threads.add(threading.current_thread().name)
        return show_popout(self, *args)

    monkeypatch.setattr(FlowDash, "_show_card_popout", record_popout)
    editors = []

    # Built in the session, so the editor lays out the session's page dialog.
    def app():
        editor = FlowDash(COMPONENTS, notifications=False, node_previews=True)
        editor.add_component("Demo/src", instance_id=src, position=(40, 40))
        editor.add_component("Demo/sql", instance_id=sql, position=(360, 40))
        editor.add_component("Demo/join", instance_id=join, position=(680, 40))
        editor.add_component("Demo/kpi", instance_id=kpi, position=(360, 340))
        editor.add_component("Demo/vega", instance_id=vega, position=(680, 340))
        editor.connect(src, "table", sql, "source")
        editor.connect(sql, "data", join, "turbines")
        editor.connect(sql, "data", kpi, "data")
        editor.connect(sql, "data", vega, "data")
        for node_id in (src, sql, join, kpi, vega):
            _card(editor, node_id).__panel__().param.watch(
                lambda _e: threads.add(threading.current_thread().name), "object"
            )
        editors.append(editor)
        return pmui.Page(main=[editor], dark_theme=scheme == "dark")

    serve_component(page, app)
    editor = editors[0]
    wait_until(lambda: editor._flow.color_mode == scheme, page, timeout=8000)

    sql_node = _node(page, sql)
    join_node = _node(page, join)
    kpi_node = _node(page, kpi)
    vega_node = _node(page, vega)

    # Stacked ports, table preview, KPI preview, chart SVG thumbnail render
    expect(sql_node).to_contain_text("turbines_by_year")
    expect(sql_node).to_contain_text("Lumen SQL")
    expect(sql_node.locator("table")).to_have_count(1)
    expect(kpi_node).to_contain_text("150.4")
    expect(vega_node.locator("svg path")).to_have_count(2)
    _assert_aligned(page)

    # Missing required input has red ring class; folded optional inputs are hidden
    pop_handle = join_node.locator('.react-flow__handle-left[data-handleid="population"]')
    expect(pop_handle).to_have_class(pytest.importorskip("re").compile(r"fd-handle-missing"))
    opt_handle = join_node.locator('.react-flow__handle-left[data-handleid="opt_a"]')
    expect(opt_handle).to_have_class(pytest.importorskip("re").compile(r"fd-handle-hidden"))

    # Click folded optional inputs row to expand, verify handles align
    join_node.locator('[data-fd-action="toggle_optional"]').click()
    wait_until(lambda: _card(editor, join).show_optional, page, timeout=8000)
    expect(join_node).to_contain_text("Opt a")
    expect(opt_handle).not_to_have_class(pytest.importorskip("re").compile(r"fd-handle-hidden"))
    _assert_aligned(page)

    # Click output row on multi-output SQL node to switch preview_output
    sql_node.locator('[data-fd-action="select_output"][data-fd-port="pipeline"]').click()
    wait_until(lambda: _card(editor, sql).preview_output == "pipeline", page, timeout=8000)
    expect(sql_node.locator("table")).to_have_count(0)

    # Switch back to data output
    sql_node.locator('[data-fd-action="select_output"][data-fd-port="data"]').click()
    wait_until(lambda: _card(editor, sql).preview_output == "data", page, timeout=8000)
    expect(sql_node.locator("table")).to_have_count(1)

    # Collapsing a card is view state: it neither selects the card nor marks the dashboard changed
    editor.dirty = False
    sql_node.click(position={"x": 60, "y": 20})
    wait_until(lambda: _selected(editor) == [sql], page, timeout=8000)
    kpi_node.locator('[data-fd-action="toggle_collapse"]').click()
    wait_until(lambda: _card(editor, kpi).collapsed, page, timeout=8000)
    wait_until(lambda: kpi_node.bounding_box()["height"] <= 48, page, timeout=8000)
    assert _selected(editor) == [sql]
    assert not editor.dirty

    # Collapse all toggle in status bar, whose label follows what it will do
    page.locator("[aria-label='Collapse all cards']").click()
    wait_until(
        lambda: _card(editor, src).collapsed and _card(editor, sql).collapsed, page, timeout=8000
    )
    collapse_all = page.locator("[aria-label='Expand all cards']")
    expect(collapse_all).to_have_count(1)
    collapse_all.click()
    wait_until(
        lambda: not _card(editor, src).collapsed and not _card(editor, sql).collapsed,
        page,
        timeout=8000,
    )
    _assert_aligned(page)

    # Pop-out preview dialog opens from card pop-out button
    sql_node.locator('[data-fd-action="popout"]').click()
    dialog = page.locator(".MuiDialog-root")
    expect(dialog).to_contain_text("turbines_by_year.data")
    expect(dialog.locator(".tabulator-row")).to_have_count(5)
    expect(dialog).to_contain_text("2014")
    paper = dialog.locator(".MuiDialog-paper")
    assert paper.bounding_box()["width"] >= 900
    holder = dialog.locator(".tabulator-tableholder")
    assert holder.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    sorter = dialog.locator(".tabulator-col-sorter").first
    assert sorter.evaluate("el => getComputedStyle(el).position") == "static"
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)

    # The chart pop-out spans the dialog instead of Vega's default width
    vega_node.locator('[data-fd-action="popout"]').click()
    chart = dialog.locator("canvas, svg.marks").first
    expect(chart).to_be_visible(timeout=15000)
    wait_until(lambda: chart.bounding_box()["width"] >= 800, page, timeout=8000)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert threads and not any(name.startswith("flowdash-preview") for name in threads)
    problems = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and "lock" in r.getMessage().lower()
    ]
    assert problems == []


_EDGE_ENDS = """() => {
  const all = (root, selector, acc = []) => {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) all(el.shadowRoot, selector, acc); });
    root.querySelectorAll(selector).forEach(el => acc.push(el));
    return acc;
  };
  const ends = {};
  for (const edge of all(document, '.react-flow__edge')) {
    const path = edge.querySelector('path.react-flow__edge-path');
    const point = (length) => {
      const p = path.getPointAtLength(length);
      return new DOMPoint(p.x, p.y).matrixTransform(path.getScreenCTM()).y;
    };
    ends[edge.dataset.id] = [point(0), point(path.getTotalLength())];
  }
  const handles = {};
  for (const node of all(document, '.react-flow__node')) {
    for (const h of node.querySelectorAll('.react-flow__handle')) {
      const r = h.getBoundingClientRect();
      const side = h.classList.contains('react-flow__handle-left') ? 'in' : 'out';
      handles[`${node.dataset.id}:${side}:${h.dataset.handleid}`] = r.top + r.height / 2;
    }
  }
  return [ends, handles];
}"""


def _detached_edges(page, editor):
    """Return the edges whose ends are not on their handles, by how far off they are."""
    ends, handles = page.evaluate(_EDGE_ENDS)
    off = {}
    for edge in editor._flow.edges:
        if edge["id"] not in ends:
            return {edge["id"]: "not drawn"}
        start, end = ends[edge["id"]]
        source = handles[f"{edge['source']}:out:{edge['sourceHandle']}"]
        target = handles[f"{edge['target']}:in:{edge['targetHandle']}"]
        if abs(start - source) > 1.5 or abs(end - target) > 1.5:
            off[edge["id"]] = (round(start - source, 1), round(end - target, 1))
    return off


def _assert_edges_attached(page, editor):
    def attached():
        return not _detached_edges(page, editor)

    try:
        wait_until(attached, page, timeout=8000)
    except TimeoutError:
        raise AssertionError(f"Edges off their handles: {_detached_edges(page, editor)}") from None


def _wired_editor(**params):
    editor = FlowDash(COMPONENTS, notifications=False, history_coalesce=0, **params)
    src = editor.add_component("Demo/src", instance_id="turbines", position=(40, 40))
    sql = editor.add_component("Demo/sql", instance_id="turbines_by_year", position=(360, 40))
    join = editor.add_component("Demo/join", instance_id="state_join", position=(700, 40))
    kpi = editor.add_component("Demo/kpi", instance_id="total_capacity", position=(700, 360))
    editor.connect(src, "table", sql, "source")
    editor.connect(sql, "data", join, "turbines")
    editor.connect(sql, "data", kpi, "data")
    return editor


def _collapse_and_expand(page, editor, instance_id):
    toggle = _node(page, instance_id).locator('[data-fd-action="toggle_collapse"]')
    toggle.click()
    wait_until(lambda: _card(editor, instance_id).collapsed, page, timeout=8000)
    wait_until(lambda: _node(page, instance_id).bounding_box()["height"] <= 48, page, timeout=8000)
    _assert_edges_attached(page, editor)
    toggle.click()
    wait_until(lambda: not _card(editor, instance_id).collapsed, page, timeout=8000)
    wait_until(lambda: _node(page, instance_id).bounding_box()["height"] > 48, page, timeout=8000)
    _assert_edges_attached(page, editor)


@pytest.mark.parametrize("previews", [False, True])
def test_edges_stay_on_the_handles_of_a_loaded_dashboard(page, previews):
    model = _wired_editor().to_model()
    editor = FlowDash(COMPONENTS, notifications=False, node_previews=previews)
    editor.load_model(model)
    serve_component(page, editor)
    expect(page.locator(".react-flow__edge")).to_have_count(3)
    _assert_edges_attached(page, editor)

    _collapse_and_expand(page, editor, "turbines_by_year")
    _collapse_and_expand(page, editor, "state_join")


def test_edges_stay_on_the_handles_after_undo(page):
    editor = _wired_editor(node_previews=True)
    serve_component(page, editor)
    expect(page.locator(".react-flow__edge")).to_have_count(3)

    editor.connect("turbines_by_year", "data", "state_join", "population")
    expect(page.locator(".react-flow__edge")).to_have_count(4)
    assert editor.undo()
    expect(page.locator(".react-flow__edge")).to_have_count(3)
    _assert_edges_attached(page, editor)

    _collapse_and_expand(page, editor, "turbines_by_year")
