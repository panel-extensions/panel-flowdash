"""UI tests for setting a literal value on an unconnected input in the inspector."""

import pytest
from panel.tests.util import wait_until

from tests.test_input_literals import _editor
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The inspector swaps in new widgets on every selection, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def _row(page, port):
    return page.locator(f".fd-input-{port}")


def test_a_value_typed_for_an_unwired_input_reaches_the_component(page):
    editor = _editor()
    num = editor.add_component("D/num", instance_id="num", position=(0, 0))
    show = editor.add_component("D/show", instance_id="show", position=(380, 0))
    serve_component(page, editor)

    _node(page, show).click()
    row = _row(page, "value")
    expect(row.get_by_text("Not connected; set a value or connect it.")).to_be_visible(
        timeout=8000
    )
    field = row.get_by_role("textbox", name="Value", exact=True)
    field.fill("42")
    field.press("Enter")
    wait_until(lambda: editor.input_values(show) == {"value": 42}, timeout=8000)
    wait_until(lambda: editor._component_instances[show].value == 42, timeout=8000)
    expect(row.get_by_text("Set here; a connection would replace it.")).to_be_visible()

    editor.connect(num, "value", show, "value")
    row = _row(page, "value")
    expect(row.get_by_text("Ignored while connected")).to_be_visible(timeout=8000)
    ignored = row.get_by_role("textbox", name="Value", exact=True)
    expect(ignored).to_be_disabled()
    expect(ignored).to_have_value("42")
    wait_until(lambda: editor._component_instances[show].value == 5, timeout=8000)

    row.get_by_role("button", name="Disconnect to use").click()
    wait_until(lambda: not editor._edge_id_map, timeout=8000)
    expect(_row(page, "value").get_by_role("textbox", name="Value", exact=True)).to_have_value(
        "42", timeout=8000
    )
    wait_until(lambda: editor._component_instances[show].value == 42, timeout=8000)


def test_clearing_and_undo(page):
    editor = _editor()
    show = editor.add_component("D/show", instance_id="show", position=(0, 0))
    serve_component(page, editor)

    _node(page, show).click()
    row = _row(page, "value")
    field = row.get_by_role("textbox", name="Value", exact=True)
    field.fill("7")
    field.press("Enter")
    wait_until(lambda: editor.input_values(show) == {"value": 7}, timeout=8000)

    row.locator(".fd-literal-clear button, button.fd-literal-clear").first.click()
    wait_until(lambda: editor.input_values(show) == {}, timeout=8000)
    expect(field).to_have_value("", timeout=8000)

    editor.undo()
    expect(_row(page, "value").get_by_role("textbox", name="Value", exact=True)).to_have_value(
        "7", timeout=8000
    )
    assert editor._component_instances[show].value == 7


def test_an_input_with_choices_offers_them(page):
    editor = _editor()
    show = editor.add_component("D/show", instance_id="show", position=(0, 0))
    serve_component(page, editor)

    _node(page, show).click()
    row = _row(page, "state")
    select = row.get_by_role("combobox", name="Value", exact=True)
    select.click(timeout=8000)
    page.get_by_role("listbox").get_by_text("CA", exact=True).click()
    wait_until(lambda: editor._component_instances[show].state == "CA", timeout=8000)


def test_text_that_is_no_value_is_explained(page):
    editor = _editor()
    show = editor.add_component("D/show", instance_id="show", position=(0, 0))
    editor.set_input_value(show, "span", (2, 3))
    serve_component(page, editor)

    _node(page, show).click()
    field = _row(page, "span").get_by_role("textbox", name="Value", exact=True)
    expect(field).to_have_value("(2, 3)", timeout=8000)
    field.fill("(2,")
    field.press("Enter")
    expect(
        _row(page, "span").get_by_text("'(2,' is not a value. Write it like (2010, 2020).")
    ).to_be_visible(timeout=8000)
    expect(_row(page, "span").get_by_text("(invalid)")).to_have_count(0)
    assert editor.input_values(show) == {"span": [2, 3]}
