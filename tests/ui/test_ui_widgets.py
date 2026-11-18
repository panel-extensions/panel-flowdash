"""UI tests: the built-in widgets render in dashboard tiles and send their values downstream."""

import datetime as dt
import re

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


def _tile(page, editor, instance_id):
    index = [item["instance_id"] for item in editor._tile_items].index(instance_id)
    return page.locator(".flowdash-tile").nth(index)


def test_input_widgets_render_and_send_their_values(page):
    editor = FlowDash({}, notifications=False)
    toggle = editor.add_component("Widgets/Toggle", config={"label": "Show outliers"})
    text = editor.add_component("Widgets/TextInput", config={"label": "Query"})
    radio = editor.add_component(
        "Widgets/RadioGroup", config={"label": "Period", "default_options": ["Day", "Week"]}
    )
    buttons = editor.add_component(
        "Widgets/RadioGroup",
        config={"label": "Unit", "default_options": ["MW", "GW"], "style": "buttons"},
    )
    number = editor.add_component("Widgets/NumberInput", config={"label": "Limit"})
    dates = editor.add_component("Widgets/DateRange", config={"default_period": "last_7_days"})
    editor.add_component("Widgets/RangeSlider", config={"label": "Capacity"})
    editor.mode = "dashboard"
    serve_component(page, editor)
    state = editor.graph.get_state

    page.get_by_role("switch", name="Show outliers").click()
    wait_until(lambda: state(toggle).checked is True, page)

    field = page.get_by_role("textbox", name="Query")
    field.fill("wind")
    assert state(text).text == ""
    field.press("Enter")
    wait_until(lambda: state(text).text == "wind", page)

    page.get_by_role("radio", name="Week").check()
    wait_until(lambda: state(radio).selected == "Week", page)
    page.get_by_role("button", name="GW").click()
    wait_until(lambda: state(buttons).selected == "GW", page)

    limit = page.get_by_role("textbox", name="Limit")
    limit.fill("12")
    limit.press("Enter")
    wait_until(lambda: state(number).selected == 12, page)

    today = dt.date.today()
    start = today - dt.timedelta(days=6)
    expect(_tile(page, editor, dates).locator("input").first).to_have_value(
        re.compile(f"{start.isoformat()}.*{today.isoformat()}")
    )
    assert state(dates).selected == (start, today)
    expect(page.get_by_text("Capacity:")).to_be_visible()


def test_kpi_and_image_render_wired_values(page):
    editor = FlowDash({}, notifications=False)
    slider = editor.add_component("Widgets/Slider", config={"default_value": 40})
    reference = editor.add_component("Widgets/NumberInput", config={"default_value": 50})
    kpi = editor.add_component("Widgets/KPI", config={"label": "Output", "suffix": " MW"})
    image = editor.add_component(
        "Widgets/Image", config={"default_url": "javascript:alert(1)", "alt": "Logo"}
    )
    editor.connect(slider, "selected", kpi, "value")
    editor.connect(reference, "selected", kpi, "reference")
    editor.mode = "dashboard"
    serve_component(page, editor)

    card = _tile(page, editor, kpi)
    expect(card).to_contain_text("Output")
    expect(card).to_contain_text("40 MW")
    expect(card).to_contain_text("▼ 20.0%")

    editor._component_instances[slider].value = 75
    expect(card).to_contain_text("75 MW")
    expect(card).to_contain_text("▲ 50.0%")

    tile = _tile(page, editor, image)
    expect(tile).to_contain_text("No image")
    assert tile.locator("img").count() == 0


def test_range_slider_and_date_range_send_browser_values(page):
    editor = FlowDash({}, notifications=False)
    rng = editor.add_component("Widgets/RangeSlider", config={"label": "Capacity"})
    dates = editor.add_component(
        "Widgets/DateRange",
        config={
            "default_period": "custom",
            "custom_start": "2024-03-04",
            "custom_end": "2024-03-08",
        },
    )
    editor.mode = "dashboard"
    serve_component(page, editor)
    state = editor.graph.get_state

    page.get_by_role("slider").first.press("ArrowRight")
    wait_until(lambda: state(rng).selected == (1, 100), page)
    assert all(type(v) is int for v in state(rng).selected)

    _tile(page, editor, dates).locator("input").first.click()
    march = page.get_by_role("grid").first
    march.get_by_text("11", exact=True).click()
    march.get_by_text("15", exact=True).click()
    wait_until(lambda: state(dates).selected == (dt.date(2024, 3, 11), dt.date(2024, 3, 15)), page)
