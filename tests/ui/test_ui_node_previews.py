"""UI tests for the previews of each node's value inside its card on the wiring canvas."""

import time
from functools import partial

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from tests.test_node_previews import FAILURES, card, demo_editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = pytest.mark.ui

# For each handle, how far its centre is from its label's, and for each edge
# how far each end is from the handle it connects.
_LAYOUT = """(edges) => {
  const all = (root, selector, acc = []) => {
    root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) all(el.shadowRoot, selector, acc); });
    root.querySelectorAll(selector).forEach(el => acc.push(el));
    return acc;
  };
  const centre = el => { const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; };
  const nodes = Object.fromEntries(all(document, '.react-flow__node').map(n => [n.dataset.id, n]));
  const offsets = [];
  for (const node of Object.values(nodes)) {
    const labels = {};
    for (const port of all(node, '.fd-port[data-port]')) {
      labels[(port.classList.contains('fd-in') ? 'L:' : 'R:') + port.dataset.port] = centre(port)[1];
    }
    for (const handle of node.querySelectorAll('.react-flow__handle')) {
      if (handle.classList.contains('fd-handle-hidden')) continue;
      const key = (handle.classList.contains('react-flow__handle-left') ? 'L:' : 'R:') + handle.dataset.handleid;
      offsets.push([node.dataset.id, key, centre(handle)[1] - labels[key]]);
    }
  }
  const handle = (id, side, port) => nodes[id]?.querySelector(
    `.react-flow__handle-${side}[data-handleid="${CSS.escape(port)}"]`);
  const ends = [];
  for (const edge of edges) {
    const path = all(document, `[data-testid="rf__edge-${CSS.escape(edge.id)}"] .react-flow__edge-path`)[0];
    const source = handle(edge.source, 'right', edge.sourceHandle);
    const target = handle(edge.target, 'left', edge.targetHandle);
    if (!path || !source || !target) { ends.push([edge.id, Infinity]); continue; }
    const m = path.getScreenCTM();
    const point = p => [m.a * p.x + m.c * p.y + m.e, m.b * p.x + m.d * p.y + m.f];
    const [start, end] = [path.getPointAtLength(0), path.getPointAtLength(path.getTotalLength())].map(point);
    const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    ends.push([edge.id, Math.max(distance(start, centre(source)), distance(end, centre(target)))]);
  }
  return {offsets, ends};
}"""


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _assert_lined_up(page, editor):
    edges = [
        {k: edge[k] for k in ("id", "source", "target", "sourceHandle", "targetHandle")}
        for edge in editor._flow.edges
    ]

    def settled():
        layout = page.evaluate(_LAYOUT, edges)
        return all(abs(offset) < 1 for *_, offset in layout["offsets"]) and all(
            distance < 8 for _, distance in layout["ends"]
        )

    wait_until(settled, page, timeout=8000)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_previews_render_and_handles_stay_beside_their_labels(page, scheme):
    page.emulate_media(color_scheme=scheme)
    editor, ids = demo_editor()
    editor.dirty = False
    serve_component(page, pmui.Page(main=[editor], dark_theme=scheme == "dark"))
    wait_until(lambda: editor._flow.color_mode == scheme, page)

    filtered = _node(page, ids["filter"])
    expect(filtered).to_contain_text("5 rows, 4 columns")
    expect(filtered.locator("table td").first).to_have_text("TX")
    expect(_node(page, ids["trend"]).locator("svg polyline")).to_be_attached()
    expect(_node(page, ids["states"]).locator(".fd-pv-chip")).to_have_count(5)
    expect(_node(page, ids["stats"])).to_contain_text("mean:")
    expect(_node(page, ids["note"])).to_contain_text("<b>this</b>")
    if FAILURES:
        expect(_node(page, ids["inverse"])).to_contain_text("inverse: Failed")
    _assert_lined_up(page, editor)

    # Every icon is one glyph wide, so each title starts right after it.
    for instance_id in (ids["turbines"], ids["filter"], ids["note"]):
        icon = _node(page, instance_id).locator(".fd-icon").bounding_box()
        title = _node(page, instance_id).locator(".fd-title").bounding_box()
        assert abs(icon["width"] - icon["height"]) < 1
        assert 0 < title["x"] - (icon["x"] + icon["width"]) < icon["width"]

    toggle = page.locator(".fd-status-bar .MuiToggleButton-root", has_text="Previews")
    expect(toggle).to_have_attribute("aria-label", "Hide node previews (P)")
    toggle.click()
    wait_until(lambda: not card(editor, ids["filter"]).show_preview, page, timeout=8000)
    assert editor.settings.node_previews and not editor.dirty
    expect(filtered.locator("table")).to_have_count(0)
    _assert_lined_up(page, editor)

    toggle.click()
    expect(filtered.locator("table")).to_have_count(1)
    _assert_lined_up(page, editor)


def test_card_resizes_only_when_the_kind_of_its_value_changes(page):
    editor, ids = demo_editor()
    serve_component(page, editor)

    waiting = _node(page, ids["waiting"])
    expect(waiting).to_contain_text("No input yet")
    before = waiting.bounding_box()["height"]
    editor.connect(ids["filter"], "kept", ids["waiting"], "rows")
    expect(waiting.locator("table")).to_have_count(1)
    wait_until(lambda: waiting.bounding_box()["height"] > before * 1.5, page, timeout=8000)
    _assert_lined_up(page, editor)

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

    values = [step / 10 for step in range(30)]
    moved = []

    def move(i):
        slider.value = values[i]
        moved.append(time.monotonic())
        if i + 1 < len(values):
            # Each step is timed from the one before, so a busy event loop
            # stretches the drag rather than running its steps back to back.
            doc.add_timeout_callback(partial(move, i + 1), 15)

    # Timers are set from the event loop, not from the test's thread.
    doc.add_next_tick_callback(partial(move, 0))

    wait_until(lambda: slider.value == values[-1], page, timeout=8000)
    expect(filtered).to_contain_text("1 row, 4 columns")
    page.wait_for_timeout(500)
    # The drag lasts at least three times the coalescing delay, and the card
    # updates at most once per delay, plus once after the last step.
    windows = (moved[-1] - moved[0]) / card(editor, ids["filter"])._coalescer.delay
    assert 2 <= len(updates) <= windows + 2
    assert "1 row, 4 columns" in pane.object and "2.3" not in pane.object
