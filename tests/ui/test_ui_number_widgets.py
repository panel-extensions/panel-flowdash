"""UI tests for the KPI's format picker and thresholds editor."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def test_a_kpi_is_formatted_and_colored_from_the_inspector(page):
    editor = FlowDash(notifications=False)
    node = editor.add_component("Widgets/KPI", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.get_by_role("combobox", name="Format", exact=True).click(timeout=8000)
    page.get_by_role("listbox").get_by_text("Percent, one decimal (12.3%)", exact=True).click()
    wait_until(lambda: editor._component_instances[node].format == ",.1%", timeout=8000)

    page.locator(".fd-thresholds-add").click()
    wait_until(
        lambda: editor._component_instances[node].thresholds == [[100, "error"]], timeout=8000
    )
    row = page.locator(".fd-threshold-row").first
    limit = row.get_by_role("textbox", name="Up to", exact=True)
    limit.fill("0.5")
    limit.press("Enter")
    wait_until(
        lambda: editor._component_instances[node].thresholds == [[0.5, "error"]], timeout=8000
    )
    row.get_by_role("combobox", name="Color", exact=True).click()
    page.get_by_role("listbox").get_by_text("Warning", exact=True).click()
    wait_until(
        lambda: editor._component_instances[node].thresholds == [[0.5, "warning"]], timeout=8000
    )
    page.locator(".fd-threshold-remove").first.click()
    wait_until(lambda: editor._component_instances[node].thresholds == [], timeout=8000)


def test_a_custom_format_is_checked(page):
    editor = FlowDash(notifications=False)
    node = editor.add_component("Widgets/KPI", position=(0, 0))
    serve_component(page, editor)

    page.locator(f".react-flow__node[data-id='{node}']").click()
    page.get_by_role("combobox", name="Format", exact=True).click(timeout=8000)
    page.get_by_role("listbox").get_by_text("Custom", exact=True).click()
    spec = page.get_by_role("textbox", name="Format spec", exact=True)
    spec.fill(".3q")
    spec.press("Enter")
    expect(page.get_by_text("Not a number format", exact=False)).to_be_visible(timeout=8000)
    assert editor._component_instances[node].format == ""
    spec.fill("+,.2f")
    spec.press("Enter")
    expect(page.get_by_text("Shows like +1,234.57")).to_be_visible(timeout=8000)
    wait_until(lambda: editor._component_instances[node].format == "+,.2f", timeout=8000)
