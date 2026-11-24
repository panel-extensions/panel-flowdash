"""UI tests for edit mode on a phone: the palette, inspector and layout panel as bottom sheets."""

import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .test_ui_header_overflow import _button
from .test_ui_phone import _OFF_SCREEN, PHONE, _menu_entries
from .util import DASHBOARD_LOAD_TIMEOUT, ask_from_palette, serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Phone Label", config=["title"])
class PhoneLabel(Viewer):
    """A tile showing a title."""

    title = param.String(default="Turbines")

    def __panel__(self):
        return self.title


COMPONENTS = {"Phone/label": PhoneLabel}


def _serve(page, width=PHONE, agent=False):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [DashboardItem(instance_id="first", component_id="Phone/label", x=0, y=0)]
    store.save_dashboard(model)
    page.set_viewport_size({"width": width, "height": 844})
    apps = []

    def factory():
        extra = {}
        if agent:
            from pydantic_ai import Agent

            from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset

            from ..test_agent_tools import scripted

            extra["agent"] = Agent(
                scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]
            )
        app = FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False, **extra
        )
        apps.append(app)
        return app

    serve_component(
        page,
        {"/": factory, "/dash/[^/]+": factory},
        suffix=f"/dash/{model.dashboard_id}?edit=true",
    )
    expect(page.locator(".react-flow__node").first).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    wait_until(lambda: apps and apps[-1]._editor is not None, timeout=8000)
    return apps[-1]._editor


def _sheet(page, name):
    return page.locator(f".{name} .MuiDrawer-paper")


def _box(locator):
    box = locator.bounding_box()
    assert box is not None
    return box


def _tap_header(node):
    # The card's header, not its embedded view, which is not a tap on the node.
    node.click(position={"x": 40, "y": 10})


def _menu(page, entry):
    _button(page, "More actions").click()
    page.get_by_role("menuitem", name=entry).click()


def test_the_canvas_fills_a_phone_screen_with_the_panels_in_sheets(page):
    _serve(page)

    flow = page.locator(".react-flow").first
    # The window's width reaches the server after the first render.
    wait_until(lambda: _box(flow)["x"] <= 1 and _box(flow)["width"] >= PHONE - 2, page)
    expect(page.get_by_placeholder("Search components")).not_to_be_visible()
    expect(_sheet(page, "fd-inspector-sheet")).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") == PHONE
    assert page.evaluate(_OFF_SCREEN) == []
    add = page.get_by_role("button", name="Add component")
    box = _box(add)
    assert box["width"] >= 44 and box["height"] >= 44
    assert box["x"] + box["width"] <= PHONE


def test_a_phone_adds_a_node_from_the_palette_sheet_and_edits_it_in_the_inspector_sheet(page):
    editor = _serve(page)

    page.get_by_role("button", name="Add component").click()
    palette = _sheet(page, "fd-palette-sheet")
    expect(palette).to_be_visible()
    expect(page.get_by_role("dialog", name="Add component")).to_be_visible()
    entry = palette.get_by_text("Phone Label", exact=True)
    height = entry.evaluate("(el) => el.closest('.MuiListItemButton-root').offsetHeight")
    assert height >= 44
    # The hint about dragging does not apply to a tap.
    expect(palette.get_by_text("Drag onto the canvas", exact=False)).not_to_be_visible()
    entry.click()

    expect(palette).not_to_be_visible()
    expect(page.locator(".react-flow__node")).to_have_count(2)
    wait_until(lambda: len(editor._tile_items) == 2, timeout=8000)
    added = editor._tile_items[1]["instance_id"]
    node = page.locator(f".react-flow__node[data-id='{added}']")
    wait_until(lambda: (editor._flow.selection or {}).get("nodes") == [added], timeout=8000)
    # The new node is shown above the sheet.
    sheet = _sheet(page, "fd-inspector-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".fd-inspector-header")).to_be_visible()
    wait_until(lambda: editor._inspector._shown == ("node", added), timeout=8000)
    assert _box(node)["y"] + _box(node)["height"] <= _box(sheet)["y"]
    # The Add component button moves above the sheet, and the sheet is named.
    add = page.get_by_role("button", name="Add component")
    wait_until(lambda: _box(add)["y"] + _box(add)["height"] <= _box(sheet)["y"], page)
    expect(page.get_by_role("region", name="Inspector")).to_be_visible()

    title = sheet.get_by_role("textbox", name="Title")
    title.fill("Capacity")
    title.press("Enter")
    wait_until(lambda: editor._tile_items[1]["config"] == {"title": "Capacity"}, timeout=8000)

    # Dismissing the sheet keeps the selection; a tap on the node opens it again.
    close = sheet.get_by_role("button", name="Hide inspector")
    box = _box(close)
    assert box["width"] >= 44 and box["height"] >= 44
    close.click()
    expect(sheet).not_to_be_visible()
    assert (editor._flow.selection or {}).get("nodes") == [added]
    _tap_header(node)
    expect(sheet).to_be_visible()


def test_a_phone_switches_workspace_tabs_with_the_sheet_buttons_following(page):
    _serve(page)

    _tap_header(page.locator(".react-flow__node[data-id='first']"))
    expect(_sheet(page, "fd-inspector-sheet")).to_be_visible()

    _menu(page, "Layout")
    expect(page.locator(".flowdash-tile").filter(visible=True).first).to_be_visible()
    expect(_sheet(page, "fd-inspector-sheet")).not_to_be_visible()
    expect(page.get_by_role("button", name="Add component")).not_to_be_visible()
    page.get_by_role("button", name="Arrange tiles").click()
    layout = _sheet(page, "fd-layout-sheet")
    expect(layout).to_be_visible()
    expect(layout.get_by_text("Arrange for")).to_be_visible()
    layout.get_by_role("button", name="Close").click()
    expect(layout).not_to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth") == PHONE

    _menu(page, "Dataflow")
    expect(page.get_by_role("button", name="Add component")).to_be_visible()
    expect(page.get_by_role("button", name="Arrange tiles")).not_to_be_visible()
    # The selection is kept, and so is its sheet.
    expect(_sheet(page, "fd-inspector-sheet")).to_be_visible()


def test_wider_screens_keep_the_palette_and_inspector_beside_the_canvas(page):
    _serve(page, 1280)

    expect(page.get_by_placeholder("Search components")).to_be_visible()
    expect(page.get_by_role("button", name="Add component")).to_have_count(0)
    _tap_header(page.locator(".react-flow__node[data-id='first']"))
    header = page.locator(".fd-inspector-header")
    expect(header).to_be_visible()
    assert _box(header)["x"] > _box(page.locator(".react-flow").first)["x"]
    expect(_sheet(page, "fd-inspector-sheet")).not_to_be_visible()


def test_widening_a_phone_with_the_assistant_open_keeps_the_inspector_folded(page):
    pytest.importorskip("pydantic_ai")
    editor = _serve(page, agent=True)
    wait_until(lambda: editor._phone, page)

    # Crossing the phone breakpoint moves the palette, the inspector and their
    # sheets between containers in one patch, which a Windows runner renders
    # about ten times slower than a desktop (5.5 s against 0.55 s), so the
    # browser gets the load timeout to show the result.
    _tap_header(page.locator(".react-flow__node[data-id='first']"))
    expect(_sheet(page, "fd-inspector-sheet")).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    # A phone's dashboard has no Assistant or Ask button for the drawer; the palette opens it.
    ask_from_palette(page, "Hello")
    wait_until(lambda: editor._inspector.collapsed, page)

    page.set_viewport_size({"width": 1280, "height": 844})
    wait_until(lambda: not editor._phone, page)
    expect(page.get_by_placeholder("Search components")).to_be_visible(
        timeout=DASHBOARD_LOAD_TIMEOUT
    )
    wait_until(lambda: editor._inspector.collapsed, page)
    flow = page.locator(".react-flow").first
    wait_until(lambda: _box(flow)["width"] >= 300, page)


def test_a_phone_opens_the_assistant_from_the_menu_in_edit_mode(page):
    pytest.importorskip("pydantic_ai")
    editor = _serve(page, agent=True)
    wait_until(lambda: editor._phone, page)

    _menu(page, "Assistant")
    wait_until(lambda: editor.ask, page)
    new_conversation = page.get_by_role("button", name="New conversation")
    expect(new_conversation).to_be_visible()
    # On a phone the drawer covers the bar, so its own button closes it.
    page.get_by_role("button", name="Close", exact=True).click()
    wait_until(lambda: not editor.ask, page)
    expect(new_conversation).not_to_be_visible()
    assert "Assistant" in _menu_entries(page)
