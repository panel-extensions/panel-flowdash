"""UI tests for connecting a table to a number input through transform steps."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.test_transform_offer import COMPONENTS
from tests.ui.util import center, serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _drag(page, output, input_handle):
    x, y = center(stable_box(output, page))
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 25, y)
    wait_until(
        lambda: "rf-handle-valid" in (input_handle.get_attribute("class") or ""), timeout=8000
    )
    page.mouse.move(*center(input_handle.bounding_box()))
    page.mouse.up()


def test_dropping_a_table_on_a_number_offers_a_transform(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    rows = editor.add_component("D/rows", instance_id="rows", position=(0, 0))
    total = editor.add_component("D/total", instance_id="total", position=(420, 0))
    serve_component(page, editor)

    output = page.locator(f".react-flow__node[data-id='{rows}'] .react-flow__handle-right")
    value = page.locator(
        f".react-flow__node[data-id='{total}'] .react-flow__handle-left[data-handleid='value']"
    )
    _drag(page, output, value)

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Connect with a transform")).to_be_visible(timeout=8000)
    expect(dialog.get_by_text("Gives 3.5")).to_be_visible()
    dialog.get_by_role("combobox", name="Aggregate").click()
    page.get_by_role("listbox").get_by_text("Maximum", exact=True).click()
    expect(dialog.get_by_text("Gives 2.0")).to_be_visible(timeout=8000)
    dialog.get_by_role("button", name="Connect").click()

    wait_until(lambda: len(editor.graph.edges) == 1, timeout=8000)
    assert editor.graph.edges[0]["transform"] == [
        {"op": "column", "column": "capacity"},
        {"op": "aggregate", "how": "max"},
    ]
    wait_until(lambda: editor._component_instances[total].value == 2.0, timeout=8000)
    expect(page.locator(".react-flow__edge")).to_have_count(1)


def test_cancelling_leaves_no_edge(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    rows = editor.add_component("D/rows", instance_id="rows", position=(0, 0))
    total = editor.add_component("D/total", instance_id="total", position=(420, 0))
    serve_component(page, editor)

    output = page.locator(f".react-flow__node[data-id='{rows}'] .react-flow__handle-right")
    value = page.locator(
        f".react-flow__node[data-id='{total}'] .react-flow__handle-left[data-handleid='value']"
    )
    _drag(page, output, value)
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("Connect with a transform")).to_be_visible(timeout=8000)
    dialog.get_by_role("button", name="Cancel").click()
    expect(dialog).to_be_hidden(timeout=8000)
    assert editor.graph.edges == []
    expect(page.locator(".react-flow__edge")).to_have_count(0)
