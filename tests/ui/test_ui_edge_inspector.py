"""UI tests for editing edge transforms in the inspector."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.test_ui_edge_inspector import COMPONENTS

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The editor swaps in new widgets on every change, so a click can reach Bokeh
# as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _editor(transform=None):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    src = editor.add_component("Test/turbines", position=(0, 0))
    dst = editor.add_component("Test/chart", position=(360, 0))
    assert editor.connect(src, "turbines", dst, "rows", transform) is True
    return editor, (src, "turbines", dst, "rows")


# The middle of the first edge's line, which is where a user clicks.
_EDGE_MIDDLE = """path => {
  const point = path.getPointAtLength(path.getTotalLength() / 2);
  const m = path.getScreenCTM();
  return [point.x * m.a + point.y * m.c + m.e, point.x * m.b + point.y * m.d + m.f];
}"""


def _select_edge(page):
    # serve_component waits for Panel's loading overlay, which would take a
    # raw click. The nodes still grow and the view fits them after that, so
    # click the middle only once it stops moving.
    path = page.locator(".react-flow__edge-path").first
    path.wait_for(state="attached")
    points = []

    def settled():
        points.append(path.evaluate(_EDGE_MIDDLE))
        return len(points) >= 3 and points[-1] == points[-2] == points[-3]

    wait_until(settled, page, interval=100)
    page.mouse.click(*points[-1])


def test_selecting_an_edge_edits_its_steps_in_the_inspector(page):
    editor, key = _editor([{"op": "head", "n": 3}])
    serve_component(page, editor)

    chip = page.locator(".react-flow__edge-text")
    expect(chip).to_have_text("fx first 3")
    _select_edge(page)
    expect(page.get_by_text("Turbines to Chart")).to_be_visible()
    expect(page.get_by_text("turbines (DataFrame) into Rows (DataFrame)")).to_be_visible()
    expect(page.get_by_text("In: 4 rows, 3 columns")).to_be_visible()
    expect(page.get_by_text("Out: 3 rows")).to_be_visible()
    # The steps are only edited in the inspector, never in a panel over the canvas.
    expect(page.locator(".react-flow__panel").get_by_text("Add step")).to_have_count(0)

    page.get_by_role("button", name="Add step").click()
    page.get_by_role("button", name="Sort").click()
    expect(page.get_by_text("Choose a column to apply this step.")).to_be_visible()
    page.get_by_role("combobox", name="Column").click()
    page.get_by_text("t_cap", exact=True).last.click()
    wait_until(lambda: len(editor.graph.get_edge_transform(*key)) == 2, page)
    expect(chip).to_have_text("fx 2 steps")
    # Every change renders the steps anew, so wait for them before clicking one.
    expect(page.get_by_text("Out: 3 rows")).to_have_count(2)
    expect(page.get_by_text("Choose a column to apply this step.")).to_have_count(0)

    page.get_by_role("button", name="Move step up").nth(1).click()
    wait_until(lambda: editor.graph.get_edge_transform(*key)[0]["op"] == "sort", page)
    expect(page.get_by_text("Out: 4 rows")).to_be_visible()

    page.get_by_role("button", name="Remove step").first.click()
    wait_until(lambda: editor.graph.get_edge_transform(*key) == [{"op": "head", "n": 3}], page)
    expect(chip).to_have_text("fx first 3")


def test_new_filter_is_applied_once_complete(page):
    editor, key = _editor()
    serve_component(page, editor)
    _select_edge(page)
    expect(page.get_by_text("Turbines to Chart")).to_be_visible()

    page.get_by_role("button", name="Add step").click()
    page.get_by_role("button", name="Filter rows").click()
    expect(page.get_by_text("Choose a column and a value to apply this step.")).to_be_visible()
    assert editor.graph.get_edge_transform(*key) == []
    expect(page.locator(".react-flow__edge-text")).to_have_count(0)

    page.get_by_role("combobox", name="Column").click()
    page.get_by_text("state", exact=True).last.click()
    expect(page.get_by_text("Choose a value to apply this step.")).to_be_visible()
    assert editor.graph.get_edge_transform(*key) == []

    value = page.get_by_role("textbox", name="Value")
    value.fill("TX")
    value.press("Enter")
    wait_until(lambda: len(editor.graph.get_state(key[2]).rows) == 2, page)
    expect(page.get_by_text("Out: 2 rows")).to_be_visible()
    expect(page.locator(".react-flow__edge-text")).to_have_text("fx state == 'TX'")


def test_add_step_disables_operations_with_the_reason(page):
    editor, _ = _editor()
    serve_component(page, editor)
    _select_edge(page)

    page.get_by_role("button", name="Add step").click()
    expect(page.get_by_role("button", name="Aggregate")).to_be_disabled()
    expect(page.get_by_role("button", name="Round")).to_be_disabled()
    expect(page.get_by_role("button", name="Column", exact=True)).to_be_enabled()
    expect(page.get_by_text("Needs a Series or list; add Column first")).to_have_count(2)
    expect(page.get_by_text("Needs a number or Series; add Column first")).to_be_visible()


def test_invalid_step_is_flagged_counted_and_fixed(page):
    editor, key = _editor([{"op": "sort", "column": "t_cap"}])
    serve_component(page, editor)
    _select_edge(page)
    expect(page.get_by_text("Turbines to Chart")).to_be_visible()

    (view,) = editor._transform_editors.values()
    view._set_argument(0, "column", "t_capacity")
    expect(page.get_by_text("No column named t_capacity. Did you mean t_cap?")).to_be_visible()
    expect(
        page.get_by_text("Not applied yet. Chart keeps its last value until this is fixed.")
    ).to_be_visible()
    issues = page.get_by_role("button", name="1 issue: Sort step on Turbines to Chart")
    expect(issues).to_be_visible()
    assert editor.graph.get_edge_transform(*key)[0]["column"] == "t_cap"

    page.locator(".react-flow__pane").click(position={"x": 10, "y": 10})
    wait_until(lambda: not editor._flow.selection["edges"], page)
    expect(issues).to_be_visible()
    page.wait_for_timeout(500)
    issues.click()
    wait_until(lambda: editor._flow.selection["edges"] == list(editor._edge_id_map), page)

    page.get_by_role("button", name="Use t_cap").click()
    expect(page.get_by_role("button", name="All outputs up to date")).to_be_visible()
    assert view.issue is None
