"""UI tests of the Explore view: the dashboard's turns as a feed with live outputs."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent

from panel_flowdash import FlowDash
from panel_flowdash.agent import FlowDashDeps, dashboard_toolset
from panel_flowdash.turns import agent_edits
from panel_flowdash.ui import tile_layout

from ..test_agent_tools import scripted
from .test_ui_selection import CHART, FRAME, ROWS, SelectionChart, SelectionRows, _drag
from .test_ui_shell import _serve_app, _store_with
from .util import serve_component

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

QUESTION = "Ask a question about this dashboard…"


def _ask(page, apps, text="Add another note"):
    page.get_by_role("button", name="Explore", exact=True).click()
    composer = page.get_by_placeholder(QUESTION)
    composer.fill(text)
    composer.press("Enter")
    return composer


def test_a_question_asked_in_explore_shows_its_answer_and_output(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("add_node", {"component": "Test/note", "name": "memo"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps, errors = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)
    page.get_by_text("note tile").first.wait_for()

    page.get_by_role("button", name="Explore", exact=True).click()
    expect(page.get_by_text("made before its requests were recorded")).to_be_visible()
    # Idle, the composer offers Send, not Stop.
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)

    composer = _ask(page, apps)
    page.get_by_text("Add another note").first.wait_for()
    card = page.locator(".artifact-cell", has_text="memo")
    expect(card).to_be_visible(timeout=20000)
    expect(card.get_by_text("note tile")).to_be_visible()
    expect(card.locator(".artifact-cell-done")).to_have_text(re.compile("On dashboard"))
    explore = page.locator(".flowdash-explore")
    explore.get_by_text(re.compile(r"Done: Added node 'memo'\.")).wait_for(timeout=20000)
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)
    editor = apps[0]._editor
    assert editor.editable and editor.explore
    assert not apps[0]._agent_session.drawer.open

    # The composer sits below the feed, never over its last row.
    last = card.bounding_box()
    assert last["y"] + last["height"] <= composer.bounding_box()["y"]

    # Back on the dashboard, the tile shows the view the card showed.
    page.get_by_role("button", name="Layout", exact=True).click()
    expect(page.locator(".artifact-cell")).to_have_count(0)
    expect(page.get_by_text("note tile")).to_have_count(2)
    assert errors == []


def test_add_to_dashboard_shows_a_hidden_output(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(
            ("add_node", {"component": "Test/note", "name": "memo"}),
            ("place_tile", {"name": "memo", "visible": False}),
        ),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)
    page.get_by_text("note tile").first.wait_for()
    _ask(page, apps)
    card = page.locator(".artifact-cell", has_text="memo")
    button = card.get_by_role("button", name="Add to dashboard")
    expect(button).to_be_visible(timeout=20000)
    button.click()
    expect(card.locator(".artifact-cell-done")).to_have_text(re.compile("On dashboard"))
    editor = apps[0]._editor
    memo = next(i for i in editor._tile_items if i["instance_id"].startswith("memo"))
    assert memo in tile_layout.grid_tiles(editor)


def test_an_approval_left_pending_in_explore_opens_the_drawer_on_return(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("save_dashboard", {})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)
    page.get_by_text("note tile").first.wait_for()
    _ask(page, apps, "Save it")
    explore = page.locator(".flowdash-explore")
    expect(explore.get_by_role("button", name=re.compile("^Approve"))).to_be_visible(timeout=20000)
    session = apps[0]._agent_session
    assert not session.drawer.open
    page.get_by_role("button", name="View", exact=True).click()
    wait_until(lambda: session.drawer.open, page, timeout=10000)
    expect(page.locator(".flowdash-explore")).to_have_count(0)


def test_charts_and_selection_links_survive_moving_between_explore_and_the_grid(page):
    editor = FlowDash(
        {CHART: SelectionChart, ROWS: SelectionRows}, notifications=False, editable=False
    )
    editor.new_dashboard("Sales")
    with agent_edits(run="run-1", question="Revenue by category, filtered by a brush"):
        chart_id = editor.add_component(CHART)
        rows_id = editor.add_component(ROWS)
        filter_id = editor.add_component("Data/FilterBySelection")
        kpi_id = editor.add_component("Widgets/KPI", config={"label": "Revenue"})
    assert editor.connect(rows_id, "data", filter_id, "data") is True
    assert editor.connect(chart_id, "selection", filter_id, "selection") is True
    steps = [{"op": "column", "column": "price"}, {"op": "aggregate", "how": "sum"}]
    assert editor.connect(filter_id, "filtered", kpi_id, "value", steps) is True
    kpi = editor._component_instances[kpi_id]
    chart = editor._component_instances[chart_id]
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.set_viewport_size({"width": 1400, "height": 1000})
    serve_component(page, editor)
    canvas = page.locator(".vega-embed canvas, .vega-embed svg").first
    canvas.wait_for(state="visible", timeout=15000)

    # Switched back and forth before anything settles, then into Explore.
    for explore in (True, False, True, False, True):
        editor.explore = explore
    expect(page.locator(".flowdash-explore .vega-embed").first).to_be_visible(timeout=15000)
    page.wait_for_timeout(500)
    plot = page.locator(
        ".flowdash-explore .vega-embed canvas, .flowdash-explore .vega-embed svg"
    ).first.bounding_box()
    _drag(page, plot, 0.05, 0.45)
    wait_until(lambda: not chart.selection.empty, page, timeout=8000)
    selected = FRAME[FRAME.category.isin(chart.selection.predicates[0].values)]
    wait_until(lambda: kpi.value == selected.price.sum(), page, timeout=8000)
    expect(
        page.locator(".flowdash-explore").get_by_text(f"{int(selected.price.sum()):,}").first
    ).to_be_visible()

    # Back on the grid the chart re-attaches and still reports its brush.
    editor.explore = False
    expect(page.locator(".flowdash-explore")).to_have_count(0)
    canvas = page.locator(".vega-embed canvas, .vega-embed svg").first
    canvas.wait_for(state="visible", timeout=15000)
    page.wait_for_timeout(500)
    left, y = _drag(page, canvas.bounding_box(), 0.05, 0.25)
    page.mouse.click(left + 300 * 0.9, y)
    wait_until(lambda: kpi.value == FRAME.price.sum(), page, timeout=8000)
    assert errors == []


@pytest.mark.parametrize("width", [390, 640])
def test_cards_stack_within_a_narrow_window(page, width):
    store, (dash,) = _store_with("Plant")
    apps = []
    page.set_viewport_size({"width": width, "height": 900})
    _serve_app(page, store, f"/dash/{dash}", apps)
    page.get_by_text("note tile").first.wait_for()
    editor = apps[0]._editor
    with agent_edits(run="run-1", question="Two notes"):
        editor.add_component("Test/note", instance_id="a")
        editor.add_component("Test/note", instance_id="b")
    page.get_by_role("button", name="Explore", exact=True).click()
    cards = page.locator(".artifact-cell").filter(has=page.get_by_text("note tile"))
    expect(cards).to_have_count(2)
    first, second = cards.first.bounding_box(), cards.last.bounding_box()
    assert second["y"] >= first["y"] + first["height"]
    assert first["x"] >= 0 and first["x"] + first["width"] <= width


def test_a_manual_edit_while_exploring_keeps_the_feed_in_place(page):
    store, (dash,) = _store_with("Plant")
    apps, errors = [], []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps)
    page.get_by_role("button", name="Explore", exact=True).click()
    editor = apps[0]._editor
    with agent_edits(run="run-1", question="A note"):
        editor.add_component("Test/note", instance_id="memo")
    tile_layout.update_tile(editor, "memo", visible=False)
    card = page.locator(".artifact-cell", has_text="memo")
    expect(card.get_by_role("button", name="Add to dashboard")).to_be_visible(timeout=15000)
    resets = []
    editor.explore_feed.state.param.watch(
        lambda e: resets.append(e) if not e.new else None, "runs"
    )
    card.get_by_role("button", name="Add to dashboard").click()
    expect(card.locator(".artifact-cell-done")).to_have_text(re.compile("On dashboard"))
    page.get_by_text("Changed the tile layout").first.wait_for(timeout=10000)
    assert resets == []
    assert errors == []
