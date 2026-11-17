"""UI tests for the previews of each node's value inside its card on the wiring canvas."""

import pytest
from panel.tests.util import wait_until

from tests.test_node_previews import FAILURES, card, demo_editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui

# For each handle, how far its centre is from its label's, and for each edge
# whether both ends sit on a handle.
_LAYOUT = """() => {
  const all = (root, selector, acc = []) => {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) all(el.shadowRoot, selector, acc); });
    root.querySelectorAll(selector).forEach(el => acc.push(el));
    return acc;
  };
  const centre = el => { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  const offsets = [], handles = [], loose = [];
  for (const node of all(document, '.react-flow__node')) {
    const labels = {};
    for (const port of all(node, '.fd-port')) {
      labels[(port.classList.contains('fd-in') ? 'L:' : 'R:') + port.dataset.port] = centre(port)[1];
    }
    for (const handle of node.querySelectorAll('.react-flow__handle')) {
      const key = (handle.classList.contains('react-flow__handle-left') ? 'L:' : 'R:') + handle.dataset.handleid;
      handles.push(centre(handle));
      offsets.push([node.dataset.id, key, centre(handle)[1] - labels[key]]);
    }
  }
  const edges = all(document, '.react-flow__edge-path');
  for (const path of edges) {
    const m = path.getScreenCTM();
    const point = p => [m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f];
    for (const end of [path.getPointAtLength(0), path.getPointAtLength(path.getTotalLength())]) {
      const [x, y] = point(end);
      if (!handles.some(([hx, hy]) => Math.hypot(hx - x, hy - y) < 8)) loose.push([x, y]);
    }
  }
  return {offsets, loose, edges: edges.length};
}"""


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _assert_lined_up(page, edges):
    def settled():
        layout = page.evaluate(_LAYOUT)
        return (
            layout["edges"] == edges
            and not layout["loose"]
            and all(abs(offset) < 1 for *_, offset in layout["offsets"])
        )

    wait_until(settled, page, timeout=8000)


def test_previews_render_and_handles_stay_beside_their_labels(page):
    editor, ids = demo_editor()
    serve_component(page, editor)

    filtered = _node(page, ids["filter"])
    expect(filtered).to_contain_text("5 rows, 4 columns")
    expect(filtered.locator("table td").first).to_have_text("TX")
    expect(_node(page, ids["trend"]).locator("svg polyline")).to_be_attached()
    expect(_node(page, ids["states"]).locator(".fd-pv-chip")).to_have_count(5)
    expect(_node(page, ids["stats"])).to_contain_text("mean:")
    expect(_node(page, ids["note"])).to_contain_text("<b>this</b>")
    if FAILURES:
        expect(_node(page, ids["inverse"])).to_contain_text("inverse: Failed")
    _assert_lined_up(page, 7)

    # Every icon is one glyph wide, so each title starts right after it.
    for instance_id in (ids["turbines"], ids["filter"], ids["note"]):
        icon = _node(page, instance_id).locator(".fd-icon").bounding_box()
        title = _node(page, instance_id).locator(".fd-title").bounding_box()
        assert abs(icon["width"] - icon["height"]) < 1
        assert 0 < title["x"] - (icon["x"] + icon["width"]) < icon["width"]

    toggle = page.locator("[aria-label='Preview values on the canvas']")
    toggle.click()
    wait_until(lambda: not editor.settings.node_previews, page, timeout=8000)
    expect(filtered.locator("table")).to_have_count(0)
    _assert_lined_up(page, 7)

    toggle.click()
    expect(filtered.locator("table")).to_have_count(1)
    _assert_lined_up(page, 7)


def test_card_resizes_only_when_the_kind_of_its_value_changes(page):
    editor, ids = demo_editor()
    serve_component(page, editor)

    waiting = _node(page, ids["waiting"])
    expect(waiting).to_contain_text("No input yet")
    before = waiting.bounding_box()["height"]
    editor.connect(ids["filter"], "kept", ids["waiting"], "rows")
    expect(waiting.locator("table")).to_have_count(1)
    wait_until(lambda: waiting.bounding_box()["height"] > before * 1.5, page, timeout=8000)
    _assert_lined_up(page, 8)

    grown = waiting.bounding_box()["height"]
    editor._component_instances[ids["slider"]].value = 2.9
    expect(waiting).to_contain_text("1 row, 4 columns")
    assert waiting.bounding_box()["height"] == grown


def test_a_burst_of_changes_sends_few_card_updates(page):
    editor, ids = demo_editor()
    serve_component(page, editor)
    filtered = _node(page, ids["filter"])
    expect(filtered).to_contain_text("5 rows, 4 columns")

    pane = card(editor, ids["filter"]).__panel__()
    updates = []
    pane.param.watch(updates.append, "object")
    slider = editor._component_instances[ids["slider"]]
    doc = next(iter(pane._models.values()))[0].document

    def drag():
        for step in range(30):
            slider.value = step / 10

    doc.add_next_tick_callback(drag)
    expect(filtered).to_contain_text("1 row, 4 columns")
    page.wait_for_timeout(500)
    assert 1 <= len(updates) <= 2
