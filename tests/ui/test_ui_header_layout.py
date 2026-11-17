"""UI tests for the compact and full header layouts and the placement of the controls."""

import re

import pytest

from panel_flowdash import DashboardItem, MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .test_ui_view_header import COMPONENTS, _store
from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

VIEW_ACTIONS = ["View", "Edit", "Refresh now", "Update automatically", "Share", "More actions"]
EDIT_ACTIONS = [
    "Dataflow",
    "Layout",
    "Split",
    "Settings",
    "More actions",
    "Discard",
    "Save",
    "Done",
]
TOP_BAR = ["Search", "Toggle theme"]


def _serve(page, store, dash, width=1440, edit=False, **params):
    page.set_viewport_size({"width": width, "height": 900})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False, **params
        )

    suffix = f"/dash/{dash}" + ("?edit=true" if edit else "")
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix)
    if not edit:
        page.locator(".flowdash-tile").filter(visible=True).first.wait_for()


def _button(page, name):
    pattern = re.compile(rf"(^|\s){re.escape(name)}")
    return page.get_by_role("button", name=pattern).filter(visible=True).first


def _first_tile_top(page):
    tiles = page.locator(".flowdash-tile").filter(visible=True)
    expect(tiles.first).to_be_visible()
    return min(stable_box(tiles.nth(i), page)["y"] for i in range(tiles.count()))


def _assert_reachable(page, names):
    """Each named button is shown inside the window, and no two of them overlap."""
    width = page.viewport_size["width"]
    boxes = []
    for name in names:
        button = _button(page, name)
        expect(button).to_be_visible()
        box = stable_box(button, page)
        assert box["x"] >= 0
        assert box["x"] + box["width"] <= width + 1, name
        boxes.append((name, box))
    for i, (name, a) in enumerate(boxes):
        for other, b in boxes[i + 1 :]:
            apart = (
                a["x"] + a["width"] <= b["x"] + 1
                or b["x"] + b["width"] <= a["x"] + 1
                or a["y"] + a["height"] <= b["y"] + 1
                or b["y"] + b["height"] <= a["y"] + 1
            )
            assert apart, f"{name} overlaps {other}"


def test_tiles_start_below_the_controls(page):
    store, dash = _store()
    _serve(page, store, dash)

    row = page.locator(".fd-controls-bar")
    expect(row.get_by_text("unit picker control")).to_be_visible()
    box = stable_box(row, page)
    assert box["y"] + box["height"] <= _first_tile_top(page)


@pytest.mark.parametrize(("header_layout", "top"), [("compact", 80), ("full", 160)])
def test_first_tile_offset_without_controls(page, header_layout, top):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [DashboardItem(instance_id="chart", component_id="Test/chart")]
    store.save_dashboard(model)
    _serve(page, store, model.dashboard_id, header_layout=header_layout)

    expect(page.locator(".fd-controls-bar")).to_be_hidden()
    assert _first_tile_top(page) <= top


def test_the_breadcrumb_is_the_only_title(page):
    store, dash = _store()
    _serve(page, store, dash)

    expect(page.get_by_role("button", name="Plant")).to_be_visible()
    expect(page.locator(".fd-dashboard-bar")).to_be_hidden()
    expect(page.get_by_text("Plant", exact=True).filter(visible=True)).to_have_count(1)


@pytest.mark.parametrize("header_layout", ["compact", "full"])
@pytest.mark.parametrize("width", [1440, 1024])
def test_every_view_action_is_reachable(page, width, header_layout):
    store, dash = _store()
    _serve(page, store, dash, width=width, header_layout=header_layout)

    _assert_reachable(page, VIEW_ACTIONS + TOP_BAR)
    edit = _button(page, "Edit")
    assert stable_box(_button(page, "View"), page)["x"] < stable_box(edit, page)["x"]


@pytest.mark.parametrize("header_layout", ["compact", "full"])
@pytest.mark.parametrize("width", [1440, 1024])
def test_every_edit_action_is_reachable(page, width, header_layout):
    store, dash = _store()
    _serve(page, store, dash, width=width, edit=True, header_layout=header_layout)

    expect(page.get_by_text("All changes saved")).to_be_visible()
    _assert_reachable(page, EDIT_ACTIONS + TOP_BAR)


def test_full_layout_has_its_own_row_and_the_search_field(page):
    store, dash = _store(description="Output per unit, live.")
    _serve(page, store, dash, header_layout="full")

    bar = page.locator(".fd-dashboard-bar")
    expect(bar.get_by_text("Plant", exact=True)).to_be_visible()
    expect(bar.get_by_text("Output per unit, live.")).to_be_visible()
    expect(bar.get_by_text(re.compile(r"^Refreshed \d\d:\d\d$"))).to_be_visible()
    expect(bar.locator(".fd-health-chip")).to_be_visible()
    assert stable_box(page.get_by_role("button", name="Search"), page)["width"] > 200
    top_bar = stable_box(page.locator(".MuiAppBar-root").first, page)
    assert stable_box(bar, page)["y"] >= top_bar["y"] + top_bar["height"] - 1


def test_search_button_opens_the_command_palette(page):
    store, dash = _store()
    _serve(page, store, dash)

    search = page.get_by_role("button", name="Search")
    assert stable_box(search, page)["width"] <= 48
    search.click()
    expect(page.get_by_role("combobox")).to_be_focused()


def test_health_chip_opens_the_pipeline(page):
    store, dash = _store()
    _serve(page, store, dash)

    chip = page.locator(".fd-health-chip").get_by_role("button")
    expect(chip).to_have_text(re.compile("1 waiting"))
    chip.click()
    popover = page.locator(".MuiPopover-paper").last
    expect(popover.get_by_text("Unit Picker")).to_be_visible()
    expect(popover.get_by_role("button", name="Show issues")).to_be_visible()


@pytest.mark.parametrize(("header_layout", "top"), [("compact", 80), ("full", 160)])
def test_controls_on_the_left(page, header_layout, top):
    store, dash = _store()
    _serve(page, store, dash, controls_position="left", header_layout=header_layout)

    panel = page.locator(".fd-controls-panel")
    expect(panel.get_by_text("unit picker control")).to_be_visible()
    expect(page.locator(".fd-controls-bar")).to_be_hidden()
    box = stable_box(panel, page)
    tile = page.locator(".flowdash-tile").filter(visible=True).first
    assert box["x"] + box["width"] <= stable_box(tile, page)["x"]
    assert _first_tile_top(page) <= top

    page.get_by_role("button", name="Hide controls").click()
    expect(panel).to_be_hidden()
    page.get_by_role("button", name="Show controls").click()
    expect(panel.get_by_text("unit picker control")).to_be_visible()


@pytest.mark.parametrize("nav_variant", ["menubar", "left"])
def test_other_navigation_keeps_the_bar_as_a_row(page, nav_variant):
    store, dash = _store()
    _serve(page, store, dash, nav_variant=nav_variant)

    bar = page.locator(".fd-dashboard-bar")
    expect(bar.get_by_text("Plant", exact=True)).to_be_visible()
    _assert_reachable(page, VIEW_ACTIONS)


def test_dashboard_settings_switch_the_header_layout(page):
    store, dash = _store()
    _serve(page, store, dash)

    expect(page.locator(".fd-dashboard-bar")).to_be_hidden()
    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Settings").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Full", exact=True).click()
    dialog.get_by_role("button", name="Save settings").click()

    bar = page.locator(".fd-dashboard-bar")
    expect(bar.get_by_text("Plant", exact=True)).to_be_visible()
    _assert_reachable(page, VIEW_ACTIONS)
    assert stable_box(page.get_by_role("button", name="Search"), page)["width"] > 200
