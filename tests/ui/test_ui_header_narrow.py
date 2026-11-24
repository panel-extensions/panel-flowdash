"""UI tests that the top bar's actions, search, assistant and theme toggle never overlap."""

from itertools import pairwise

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash.agent.tools import FlowDashDeps, dashboard_toolset
from panel_flowdash.app import FlowDashApp
from panel_flowdash.component_library import normalize_components

from ..test_agent_tools import scripted
from .test_ui_header_layout import EDIT_ACTIONS, TOP_BAR, VIEW_ACTIONS
from .test_ui_header_overflow import _HITS_ITSELF, _button, _store
from .test_ui_view_header import COMPONENTS
from .util import DASHBOARD_LOAD_TIMEOUT, serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

# The boxes of the visible buttons, chips and texts in the top bar: the
# actions, crumbs, health chip and save state. A text inside a button counts
# with its button.
_TOP_BAR_ITEMS = """() => {
  const roots = [document]
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  const bar = roots.map((r) => r.querySelector("header.MuiAppBar-root")).find(Boolean).getBoundingClientRect()
  const items = roots.flatMap((r) => [
    ...r.querySelectorAll("button"),
    ...[...r.querySelectorAll(".MuiTypography-root")].filter((t) => !t.closest("button") && t.textContent.trim()),
  ])
  return items.flatMap((el) => {
    const box = el.getBoundingClientRect()
    if (box.width === 0 || box.top < bar.top || box.bottom > bar.bottom + 1) return []
    const name = el.getAttribute("aria-label") || el.textContent.trim()
    return [{name, left: box.left, right: box.right, height: box.height}]
  })
}"""

# The content of the tooltip shown below a control while it is hovered.
_TOOLTIP = "(el) => getComputedStyle(el, '::after').content"

# The Ask toggle's name, which its tooltip shows below the width its label needs.
ASK = "Ask about this dashboard"

# The outlined controls, which share one height, and the text buttons, which share another.
_OUTLINED = {
    False: ["Dashboard", "Ask", "Edit", "Refresh now", "Update automatically"],
    True: ["Dashboard", "Dataflow", "Layout", "Split", "Ask", "Discard", "Done"],
}
_TEXT = {
    False: ["Share", "Full screen", "More actions"],
    True: ["Settings", "More actions"],
}


def _agent():
    return Agent(scripted(), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()])


def _serve(page, width, edit):
    store, dash = _store()
    page.set_viewport_size({"width": width, "height": 900})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            agent=_agent(),
        )

    suffix = f"/dash/{dash}" + ("?edit=true" if edit else "")
    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=suffix)
    # With an assistant, edit mode has Ask too, which opens it.
    names = [*([*EDIT_ACTIONS, "Ask"] if edit else VIEW_ACTIONS), *TOP_BAR]
    for name in names:
        expect(_button(page, name)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
        stable_box(_button(page, name), page)
    if edit and width >= 800:
        expect(page.get_by_text("All changes saved")).to_be_visible()
    return names


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
@pytest.mark.parametrize("width", [640, 800, 900, 1023, 1024])
def test_top_bar_items_never_overlap(page, width, edit):
    names = _serve(page, width, edit)

    items = sorted(page.evaluate(_TOP_BAR_ITEMS), key=lambda item: item["left"])
    if edit and width >= 800:
        assert "All changes saved" in [item["name"] for item in items]
    assert items[0]["left"] >= 0
    assert items[-1]["right"] <= width
    for before, after in pairwise(items):
        assert before["right"] <= after["left"] + 1, f"{before['name']} overlaps {after['name']}"
    for name in names:
        assert _button(page, name).evaluate(_HITS_ITSELF), f"{name} is covered"


@pytest.mark.parametrize("edit", [False, True], ids=["view", "edit"])
@pytest.mark.parametrize("width", [800, 1440])
def test_top_bar_buttons_keep_their_height_without_labels(page, width, edit):
    _serve(page, width, edit)

    for group in (_OUTLINED[edit], _TEXT[edit]):
        heights = {name: stable_box(_button(page, name), page)["height"] for name in group}
        assert max(heights.values()) - min(heights.values()) <= 1, heights
    outlined = stable_box(_button(page, _OUTLINED[edit][0]), page)["height"]
    text = stable_box(_button(page, _TEXT[edit][0]), page)["height"]
    assert outlined == pytest.approx(36.75, abs=1)
    assert text == pytest.approx(34.75, abs=1)


@pytest.mark.parametrize(
    ("edit", "names"),
    [
        (False, {"Dashboard": "Dashboard", "Ask": ASK}),
        (
            True,
            {
                "Dashboard": "Dashboard",
                "Dataflow": "Dataflow",
                "Layout": "Layout",
                "Split": "Split",
                "Ask": ASK,
            },
        ),
    ],
    ids=["view", "edit"],
)
def test_icon_only_controls_show_their_name_on_hover(page, edit, names):
    _serve(page, 800, edit)

    for name, tooltip in names.items():
        button = _button(page, name)
        page.mouse.move(1, 400)
        wait_until(lambda b=button: b.evaluate(_TOOLTIP) == "none", page)
        button.hover()
        wait_until(lambda b=button, t=tooltip: b.evaluate(_TOOLTIP) == f'"{t}"', page)
    page.mouse.move(1, 400)

    # Edit is a button rather than a toggle, with a tooltip of its own.
    if not edit:
        _button(page, "Edit").hover()
        expect(page.get_by_role("tooltip").filter(has_text="Edit")).to_be_visible()
        page.mouse.move(1, 400)

    # A disabled button takes no pointer events, so its host shows its name.
    for name in ["Discard", "Save"] if edit else []:
        button = _button(page, name)
        expect(button).to_be_disabled()
        box = stable_box(button, page)
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        wait_until(lambda b=button, n=name: b.evaluate(_TOOLTIP) == f'"{n}"', page)


@pytest.mark.parametrize("width", [1024, 1100])
def test_crumbs_give_way_before_the_title_truncates(page, width):
    store, dash = _store(title="Fleet Overview")
    page.set_viewport_size({"width": width, "height": 900})

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )

    serve_component(page, {"/": factory, "/dash/[^/]+": factory}, suffix=f"/dash/{dash}")
    title = page.get_by_role("button", name="Fleet Overview")
    expect(title).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)
    stable_box(title, page)

    crumb = page.locator(".MuiAppBar-root").first.get_by_role(
        "button", name="Dashboards", exact=True
    )
    expect(crumb).to_be_hidden()
    assert title.evaluate("el => el.scrollWidth <= el.clientWidth")


def test_tapping_an_icon_only_control_leaves_no_tooltip_open(browser):
    context = browser.new_context(
        viewport={"width": 800, "height": 900}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    try:
        for edit, name in ((False, "Dashboard"), (True, "Dataflow")):
            _serve(page, 800, edit)
            button = _button(page, name)
            button.tap()
            page.wait_for_timeout(800)
            assert button.evaluate(_TOOLTIP) == "none"
    finally:
        context.close()
