"""UI tests for the framed dashboard tiles, their failed and waiting states and the inspect sheet."""

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import serve_component, wait_until
from panel.viewable import Viewer

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# Content swapped into the sheet and the expand dialog reaches Bokeh in click
# events as models it already knows; see `test_edge_transform_editor_applies_changes`.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

FRAME = pd.DataFrame({f"col{i}": range(30) for i in range(10)})


@register(page=False, component=True, title="Turbines", description="Every turbine in the state")
class Turbines(Viewer):
    @param.output(param.DataFrame)
    def frame(self):
        return FRAME

    def __panel__(self):
        return pn.pane.Markdown("turbines tile", height=120)


class Report(Viewer):
    frame = param.DataFrame(
        default=None, allow_None=True, metadata={"flowdash": {"required": True}}
    )

    def __panel__(self):
        return pn.pane.Markdown("report tile")


@register(page=False, component=True, title="Divider")
class Divider(Viewer):
    divisor = param.Number(default=1)

    @param.output(param.Number)
    @param.depends("divisor")
    def ratio(self):
        return 1 / self.divisor

    @param.depends("divisor")
    def __panel__(self):
        return pn.pane.Markdown(f"divisor {self.divisor}", width=360, height=120)


def _editor(**params):
    editor = FlowDash(
        {"Test/turbines": Turbines, "Test/report": Report, "Test/divider": Divider},
        notifications=False,
        include_builtin_components=False,
        **params,
    )
    return editor


def test_tile_has_a_title_bar_with_named_actions(page):
    editor = _editor(inspect_outputs=True)
    editor.add_component("Test/turbines")
    editor.editable = False
    serve_component(page, editor)

    tile = page.locator(".flowdash-tile")
    expect(tile.get_by_text("Turbines", exact=True)).to_be_visible()
    expect(tile.get_by_text("Every turbine in the state")).to_be_visible()
    expect(page.get_by_role("button", name="Inspect data for Turbines")).to_be_visible()
    expect(page.get_by_role("button", name="Expand Turbines")).to_be_visible()
    expect(page.get_by_role("button", name="More for Turbines")).to_be_visible()


def test_arranging_hides_the_title_bar(page):
    editor = _editor(inspect_outputs=True)
    editor.add_component("Test/turbines")
    editor.mode = "dashboard"
    serve_component(page, editor)

    page.get_by_text("turbines tile").wait_for()
    expect(page.get_by_role("button", name="Expand Turbines")).to_be_hidden()


def test_expand_shows_the_tile_full_screen_and_restores_it(page):
    editor = _editor()
    editor.add_component("Test/turbines")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_role("button", name="Expand Turbines").click()

    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_text("turbines tile")).to_be_visible()
    box = dialog.bounding_box()
    assert box["width"] >= page.viewport_size["width"] - 1

    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    expect(page.locator(".flowdash-tile").get_by_text("turbines tile")).to_be_visible()


def test_inspect_sheet_sits_beside_the_tile_on_wide_screens(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    editor = _editor(inspect_outputs=True)
    editor.add_component("Test/turbines")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_role("button", name="Inspect data for Turbines").click()

    sheet = page.locator(".MuiDrawer-paper")
    expect(sheet.get_by_text("30 rows, 10 columns")).to_be_visible()
    expect(sheet.get_by_role("button", name="Download CSV")).to_be_visible()
    expect(sheet.get_by_role("button", name="Copy as Python")).to_be_visible()
    expect(sheet.locator("tbody tr")).to_have_count(10)
    tile = page.locator(".flowdash-tile")

    def beside():
        return tile.bounding_box()["x"] + tile.bounding_box()["width"] <= sheet.bounding_box()["x"]

    wait_until(beside, page=page)

    page.get_by_role("button", name="Close inspector").click()
    expect(sheet).to_be_hidden()


def test_inspect_sheet_covers_the_screen_on_phones(page):
    page.set_viewport_size({"width": 390, "height": 844})
    editor = _editor(inspect_outputs=True)
    editor.add_component("Test/turbines")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_role("button", name="Inspect data for Turbines").click()

    sheet = page.locator(".MuiDrawer-paper")
    expect(sheet.get_by_text("Inspect data")).to_be_visible()
    wait_until(lambda: sheet.bounding_box()["width"] >= 389, page=page)


def test_waiting_tile_says_what_it_needs(page):
    editor = _editor()
    editor.add_component("Test/report")
    editor.editable = False
    serve_component(page, editor)

    tile = page.locator(".flowdash-tile")
    expect(tile.get_by_text("This tile needs Frame")).to_be_visible()
    expect(tile.locator(".flowdash-tile-status")).to_have_text("Waiting")
    expect(tile.get_by_text("report tile")).to_be_hidden()


def test_more_menu_shows_the_component_in_the_editor(page):
    shown = []
    editor = _editor(show_in_editor=shown.append)
    instance_id = editor.add_component("Test/turbines")
    editor.editable = False
    serve_component(page, editor)

    page.get_by_role("button", name="More for Turbines").click()
    page.get_by_role("menuitem", name="Show in editor").click()

    wait_until(lambda: shown == [instance_id], page=page)


@pytest.mark.failures
def test_failed_tile_explains_in_plain_language(page):
    shown = []
    editor = _editor(propagate_failures=True, show_in_editor=shown.append)
    source = editor.add_component("Test/divider")
    target = editor.add_component("Test/divider")
    assert editor.connect(source, "ratio", target, "divisor") is True
    editor.editable = False
    serve_component(page, editor)

    overlay = page.locator(".flowdash-failure").nth(1)
    expect(overlay).to_be_hidden()

    editor.graph.get_state(source).divisor = 0

    expect(overlay).to_be_visible()
    expect(overlay.get_by_text("This tile could not update")).to_be_visible()
    expect(overlay).to_contain_text("Divider stopped the data")
    details = overlay.locator(".flowdash-failure-details")
    expect(details).to_be_hidden()
    overlay.get_by_role("button", name="Details").click()
    expect(details).to_be_visible()
    expect(details).to_contain_text("ZeroDivisionError: division by zero")
    expect(page.locator(".flowdash-tile-status").nth(1)).to_have_text("Failed")

    overlay.get_by_role("button", name="Show in editor").click()
    wait_until(lambda: shown == [target], page=page)
