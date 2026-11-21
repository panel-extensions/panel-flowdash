"""UI tests of the Explore view: the dashboard's turns as a feed with live outputs."""

import re

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
pytest.importorskip("pydantic_ai")
expect = pytest.importorskip("playwright.sync_api").expect

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel

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
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent, explore_agent=agent)
    page.get_by_text("note tile").first.wait_for()

    page.get_by_role("button", name="Explore", exact=True).click()
    # The outputs the dashboard was made with wait, collapsed, above the questions asked here.
    expect(page.get_by_placeholder(QUESTION)).to_be_visible()
    expect(page.locator(".artifact-cell")).to_have_count(1)
    expect(page.get_by_text("note tile")).to_have_count(0)
    page.get_by_role("button", name="Show earlier requests and edits").click()
    page.locator(".explore-earlier-toggle").first.click()
    expect(page.locator(".artifact-cell").get_by_text("note tile")).to_be_visible()
    # Idle, the composer offers Send, not Stop.
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)

    composer = _ask(page, apps)
    page.get_by_text("Add another note").first.wait_for()
    card = page.locator(".artifact-cell", has_text="memo").last
    expect(card).to_be_visible(timeout=20000)
    expect(card.get_by_text("note tile")).to_be_visible()
    # The answer stays off the dashboard until it is added.
    expect(card.get_by_role("button", name="Add to dashboard")).to_be_visible()
    explore = page.locator(".flowdash-explore")
    explore.get_by_text(re.compile(r"Done: Added node 'memo'\.")).wait_for(timeout=20000)
    expect(page.get_by_role("button", name="Stop")).to_have_count(0)
    editor = apps[0]._editor
    assert editor.explore and not editor.dirty
    assert [i["instance_id"] for i in editor._tile_items] == ["n1"]
    assert not apps[0]._agent_session.drawer.open

    # The composer sits below the feed, never over its last row.
    last = card.bounding_box()
    assert last["y"] + last["height"] <= composer.bounding_box()["y"]

    # Back on the dashboard, only its own tile is there.
    page.get_by_role("button", name="View", exact=True).click()
    expect(page.locator(".artifact-cell")).to_have_count(0)
    expect(page.get_by_text("note tile")).to_have_count(1, timeout=20000)
    assert errors == []


def test_add_to_dashboard_puts_an_answer_on_the_dashboard(page):
    store, (dash,) = _store_with("Plant")
    # A note like the answer's would be reused rather than added again.
    model = store._load_any(dash)
    model.items = []
    store.save_dashboard(model)
    agent = Agent(
        scripted(("add_node", {"component": "Test/note", "name": "memo"})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent, explore_agent=agent)
    _ask(page, apps)
    card = page.locator(".artifact-cell", has_text="memo")
    button = card.get_by_role("button", name="Add to dashboard")
    expect(button).to_be_visible(timeout=20000)
    button.click()
    expect(card.locator(".artifact-cell-done")).to_have_text(re.compile("On dashboard as memo$"))
    editor = apps[0]._editor
    memo = next(i for i in editor._tile_items if i["instance_id"] == "memo")
    assert memo in tile_layout.grid_tiles(editor)
    # Show leaves Explore for the dashboard, where the answer's tile is.
    card.get_by_role("button", name="Show").click()
    expect(page.locator(".flowdash-explore")).to_have_count(0)
    expect(page.get_by_text("note tile")).to_have_count(1, timeout=20000)


def _ask_drawer(page, text):
    page.get_by_role("button", name="Assistant", exact=True).click()
    chat = page.get_by_placeholder(re.compile("^Ask the agent"))
    chat.fill(text)
    chat.press("Enter")


def test_an_approval_waiting_in_the_drawer_shows_a_notice_while_exploring(page):
    store, (dash,) = _store_with("Plant")
    agent = Agent(
        scripted(("save_dashboard", {})),
        deps_type=FlowDashDeps,
        toolsets=[dashboard_toolset()],
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=agent)
    page.get_by_text("note tile").first.wait_for()
    _ask_drawer(page, "Save it")
    session = apps[0]._agent_session
    wait_until(lambda: bool(session.runner.state.pending_approvals), page, timeout=20000)
    page.get_by_role("button", name="Explore", exact=True).click()
    wait_until(lambda: not session.drawer.open, page, timeout=10000)
    # The drawer's request is not the Explore view's: its approval stays out of the feed.
    explore = page.locator(".flowdash-explore")
    expect(explore.get_by_text("Save it")).to_have_count(0)
    expect(explore.get_by_role("button", name=re.compile("^Approve"))).to_have_count(0)
    # A notice says the approval waits, and its Review button goes back to the drawer.
    notice = page.get_by_text(re.compile("^The assistant is waiting for your approval"))
    expect(notice.first).to_be_visible()
    page.locator("button:visible", has_text=re.compile("^Review$")).first.click()
    wait_until(
        lambda: session.drawer.open and not session.chrome.notice.visible, page, timeout=10000
    )
    expect(page.locator(".flowdash-explore")).to_have_count(0)


def answering(text):
    async def stream(messages, info):
        yield text

    return FunctionModel(stream_function=stream, model_name="answering")


def test_each_question_shows_only_where_it_was_asked(page):
    store, (dash,) = _store_with("Plant")
    drawer = Agent(
        answering("Drawer answer."), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]
    )
    explorer = Agent(
        answering("Explore answer."), deps_type=FlowDashDeps, toolsets=[dashboard_toolset()]
    )
    apps = []
    _serve_app(page, store, f"/dash/{dash}", apps, agent=drawer, explore_agent=explorer)
    page.get_by_text("note tile").first.wait_for()

    _ask(page, apps, "Which plant is largest?")
    explore = page.locator(".flowdash-explore")
    expect(explore.get_by_text("Explore answer.")).to_be_visible(timeout=20000)
    expect(explore.get_by_text("Which plant is largest?")).to_be_visible()

    page.get_by_role("button", name="View", exact=True).click()
    expect(page.locator(".flowdash-explore")).to_have_count(0)
    _ask_drawer(page, "Make the title bold")
    session = apps[0]._agent_session
    expect(page.get_by_text("Drawer answer.")).to_be_visible(timeout=20000)
    expect(page.get_by_text("Explore answer.")).to_have_count(0)
    expect(page.get_by_text("Which plant is largest?")).to_have_count(0)

    page.get_by_role("button", name="Explore", exact=True).click()
    expect(explore.get_by_text("Explore answer.")).to_be_visible(timeout=20000)
    expect(explore.get_by_text("Drawer answer.")).to_have_count(0)
    expect(explore.get_by_text("Make the title bold")).to_have_count(0)
    assert [
        m.content for m in session.runner.state.items if getattr(m, "role", None) == "user"
    ] == ["Make the title bold"]


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
    if width < 600:
        # On a phone the View/Explore/Edit toggle is in the overflow menu.
        page.get_by_role("button", name="More actions").filter(visible=True).click()
        page.get_by_role("menuitem", name="Explore").click()
    else:
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
    page.set_viewport_size({"width": 1400, "height": 1000})
    _serve_app(page, store, f"/dash/{dash}?edit=true", apps)
    page.get_by_role("button", name="Explore", exact=True).wait_for()
    editor = apps[0]._editor
    # A long feed: a full page shown and older entries behind it.
    for i in range(25):
        editor.end_turn()
        with agent_edits(run=f"run-{i}", question=f"Note number {i}"):
            editor.add_component("Test/note", instance_id=f"memo{i}")
    tile_layout.update_tile(editor, "memo24", visible=False)
    page.get_by_role("button", name="Explore", exact=True).click()
    card = page.locator(".artifact-cell", has_text="memo24")
    button = card.get_by_role("button", name="Add to dashboard")
    expect(button).to_be_visible(timeout=90000)
    button.scroll_into_view_if_needed()
    page.wait_for_timeout(1500)
    before = card.bounding_box()["y"]
    resets = []
    editor.explore_feed.state.param.watch(
        lambda e: resets.append(e) if not e.new else None, "runs"
    )
    button.click()
    expect(card.locator(".artifact-cell-done")).to_have_text(re.compile("On dashboard"))
    page.get_by_text("Changed the tile layout").first.wait_for(timeout=10000)
    page.wait_for_timeout(2000)
    after = card.bounding_box()["y"]
    assert abs(after - before) < 200, (before, after)
    assert resets == []
    assert errors == []


_DEEP_COUNT = """() => {
  const roots = [document]
  let n = 0
  for (let i = 0; i < roots.length; i++) {
    for (const el of roots[i].querySelectorAll("*")) {
      n++
      if (el.shadowRoot) roots.push(el.shadowRoot)
    }
  }
  return n
}"""


def test_a_long_history_shows_its_latest_page_and_loads_earlier_ones(page):
    import time

    store, (dash,) = _store_with("Plant")
    apps = []
    page.set_viewport_size({"width": 1400, "height": 1000})
    _serve_app(page, store, f"/dash/{dash}", apps)
    page.get_by_role("button", name="Explore", exact=True).wait_for()
    editor = apps[0]._editor
    for i in range(60):
        editor.end_turn()
        with agent_edits(run=f"run-{i}", question=f"Note number {i}"):
            editor.add_component("Test/note", instance_id=f"memo{i}")
    page.wait_for_timeout(1000)
    before = page.evaluate(_DEEP_COUNT)
    start = time.monotonic()
    page.get_by_role("button", name="Explore", exact=True).click()
    page.locator(".artifact-cell", has_text="memo59").wait_for(timeout=120000)
    switched = time.monotonic() - start
    cells = page.locator(".artifact-cell")
    expect(cells).to_have_count(20)
    # The feed holds one page, so the page grows by a bounded amount whatever the history.
    assert page.evaluate(_DEEP_COUNT) - before < 20000, page.evaluate(_DEEP_COUNT) - before
    # A loose budget for slow runners; the node count above is what bounds the feed.
    assert switched < 120, switched

    # The entry on screen stays where it is while older ones go in above it.
    latest = page.get_by_text("Note number 59", exact=True)
    page.wait_for_timeout(1000)
    before = latest.bounding_box()["y"]
    feed = editor.explore_feed
    page.get_by_role("button", name=re.compile(r"Show earlier requests \(41 more\)")).click()
    # The server adds the page at once; a slow browser then takes a while to draw it.
    wait_until(lambda: len(feed.state.runs) == 40, page, timeout=60000)
    expect(cells).to_have_count(40, timeout=120000)
    page.get_by_text("Note number 20", exact=True).wait_for(state="attached")
    page.wait_for_timeout(1500)
    assert abs(latest.bounding_box()["y"] - before) < 50, (before, latest.bounding_box())
    expect(
        page.get_by_role("button", name=re.compile(r"Show earlier requests \(21 more\)"))
    ).to_be_visible()

    # A reader at the top of the feed stays at the entry they were reading.
    page.mouse.move(700, 500)
    for _ in range(40):
        page.mouse.wheel(0, -2000)
    page.wait_for_timeout(1000)
    top = page.get_by_text("Note number 20", exact=True)
    before = top.bounding_box()["y"]
    assert 0 <= before <= 1000, before
    page.get_by_role("button", name=re.compile(r"Show earlier requests \(21 more\)")).click()
    wait_until(lambda: len(feed.state.runs) == 60, page, timeout=60000)
    expect(cells).to_have_count(60, timeout=120000)
    page.wait_for_timeout(1500)
    assert abs(top.bounding_box()["y"] - before) < 50, (before, top.bounding_box())
