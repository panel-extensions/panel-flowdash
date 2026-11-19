"""UI tests for config fields that offer choices from a node's inputs."""

import param
import pytest
from panel.tests.util import wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash
from tests.test_config_choices import COMPONENTS
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The inspector swaps in new widgets on every selection, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _node(page, instance_id):
    return page.locator(f".react-flow__node[data-id='{instance_id}']")


def test_a_column_field_offers_the_columns_of_its_input(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    frame = editor.add_component("Demo/frame", position=(0, 0))
    pick = editor.add_component("Demo/pick", position=(380, 0))
    editor.connect(frame, "rows", pick, "rows")
    serve_component(page, editor)

    _node(page, pick).click()
    wait_until(lambda: editor._inspector._shown == ("node", pick), timeout=8000)
    column = page.get_by_role("combobox", name="Column", exact=True)
    expect(column).to_have_text("Not set", timeout=8000)
    column.click()
    listbox = page.get_by_role("listbox")
    for label in ("Not set", "state (string)", "capacity (number)"):
        expect(listbox.get_by_text(label, exact=True)).to_be_visible()
    listbox.get_by_text("year (integer)", exact=True).click()
    wait_until(lambda: editor._component_instances[pick].column == "year", timeout=8000)
    expect(page.get_by_role("combobox", name="Column", exact=True)).to_have_text("year (integer)")
    assert editor._tile_items[1]["config"]["column"] == "year"

    page.get_by_role("combobox", name="Column", exact=True).click()
    page.get_by_role("listbox").get_by_text("Not set", exact=True).click()
    wait_until(lambda: editor._component_instances[pick].column == "", timeout=8000)


def test_a_hook_suggests_values_while_accepting_others(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    tables = editor.add_component("Demo/tables", position=(0, 0))
    pick = editor.add_component("Demo/table_pick", position=(380, 0))
    editor.connect(tables, "source", pick, "source")
    serve_component(page, editor)

    _node(page, pick).click()
    wait_until(lambda: editor._inspector._shown == ("node", pick), timeout=8000)
    page.get_by_role("combobox", name="Table", exact=True).click()
    page.get_by_role("listbox").get_by_text("turbines", exact=True).click()
    wait_until(lambda: editor._component_instances[pick].table == "turbines", timeout=8000)

    label = page.get_by_role("combobox", name="Label", exact=True)
    label.click()
    expect(page.get_by_text("Totals", exact=True)).to_be_visible()
    label.fill("My label")
    label.press("Enter")
    wait_until(lambda: editor._component_instances[pick].label == "My label", timeout=8000)


def test_a_value_set_elsewhere_is_kept_and_marked(page):
    editor = FlowDash(COMPONENTS, notifications=False, include_builtin_components=False)
    frame = editor.add_component("Demo/frame", position=(0, 0))
    pick = editor.add_component("Demo/pick", position=(380, 0))
    editor.connect(frame, "rows", pick, "rows")
    serve_component(page, editor)

    _node(page, pick).click()
    column = page.get_by_role("combobox", name="Column", exact=True)
    expect(column).to_have_text("Not set", timeout=8000)
    editor._apply_config_patch(pick, {"column": "missing"})
    expect(page.get_by_role("combobox", name="Column", exact=True)).to_have_text(
        "missing (not available)", timeout=8000
    )
    page.wait_for_timeout(500)
    assert editor._component_instances[pick].column == "missing"
    assert editor._tile_items[1]["config"]["column"] == "missing"


class OptionalNumbers(param.Parameterized):
    ratio = param.Number(default=None, allow_None=True)
    bounded = param.Number(default=None, allow_None=True, bounds=(0, 10))
    count = param.Integer(default=None, allow_None=True)


@register(page=False, component=True, title="Optional", config_schema=OptionalNumbers)
def optional(config):
    return "optional"


def test_optional_numbers_show_empty_until_set(page):
    editor = FlowDash(
        {"Demo/optional": optional}, notifications=False, include_builtin_components=False
    )
    node = editor.add_component("Demo/optional", position=(0, 0))
    serve_component(page, editor)

    _node(page, node).click()
    for name in ("Ratio", "Bounded", "Count"):
        expect(page.get_by_role("textbox", name=name, exact=True)).to_have_value("", timeout=8000)
    ratio = page.get_by_role("textbox", name="Ratio", exact=True)
    ratio.fill("2.5")
    ratio.press("Enter")
    wait_until(lambda: editor.graph.get_config_state(node).ratio == 2.5, timeout=8000)
