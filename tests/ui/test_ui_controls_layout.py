"""UI tests for the dashboard controls in the left panel and in the Split tab."""

import panel_material_ui as pmui
import pytest
from panel.tests.util import wait_until

from panel_flowdash import register
from panel_flowdash.editor import FlowDash

from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# Entering the split moves the canvas into a new pane, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


# Views are laid out for a tile, which most fill with stretch_both.
@register(page=False, component=True, sidebar=True, title="State")
def state_select(config):
    return pmui.Select(label="State", options=["TX", "CA"], sizing_mode="stretch_both")


@register(page=False, component=True, sidebar=True, title="Project")
def project_select(config):
    return pmui.Select(label="Project", options=["Alpha", "Beta"], sizing_mode="stretch_both")


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/state": state_select, "Test/project": project_select, "Test/note": note}


def _editor(**params):
    editor = FlowDash(
        COMPONENTS,
        notifications=False,
        include_builtin_components=False,
        controls_position="left",
        **params,
    )
    for component in COMPONENTS:
        editor.add_component(component)
    return editor


def _control_gap(page) -> float:
    state = page.get_by_role("combobox").nth(0).bounding_box()
    project = page.get_by_role("combobox").nth(1).bounding_box()
    return project["y"] - (state["y"] + state["height"])


def test_left_panel_stacks_its_controls(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    editor = _editor(editable=False)
    serve_component(page, editor)
    expect(page.locator(".fd-controls-panel")).to_be_visible()
    expect(page.get_by_role("combobox")).to_have_count(2)
    # Each control is about 40 px tall; spread out, they sat about 340 px apart.
    wait_until(lambda: 0 <= _control_gap(page) < 80, page, timeout=8000)

    editor.fullscreen = True
    expect(page.locator(".fd-presenting")).to_be_visible()
    page.wait_for_timeout(500)
    wait_until(lambda: 0 <= _control_gap(page) < 80, page, timeout=8000)


def test_split_puts_left_controls_above_the_grid(page):
    page.set_viewport_size({"width": 1024, "height": 860})
    editor = _editor()
    serve_component(page, editor)
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    expect(page.locator(".gutter")).to_be_visible()

    expect(page.locator(".fd-controls-panel")).to_be_hidden()
    expect(page.locator(".fd-controls-bar")).to_be_visible()
    tile = page.get_by_text("note tile")
    expect(tile).to_be_visible()
    gutter = page.locator(".gutter").bounding_box()
    box = tile.bounding_box()
    assert gutter["x"] < box["x"] < 1024
    bar = page.locator(".fd-controls-bar").bounding_box()
    assert bar["x"] > gutter["x"] and bar["y"] + bar["height"] <= box["y"] + 1


def test_switching_to_left_in_the_split_keeps_the_controls(page):
    page.set_viewport_size({"width": 1440, "height": 860})
    editor = _editor()
    editor.settings.controls_position = "top"
    serve_component(page, editor)
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    expect(page.locator(".fd-controls-bar")).to_be_visible()

    editor.settings.controls_position = "left"
    page.wait_for_timeout(300)
    expect(page.locator(".fd-controls-bar")).to_be_visible()
    expect(page.get_by_role("combobox")).to_have_count(2)
    expect(page.get_by_role("combobox").first).to_be_visible()
