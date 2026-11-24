"""UI tests of the Report view: the dashboard read as a document over the live graph."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash import FlowDash

from .util import DASHBOARD_LOAD_TIMEOUT, serve_component, stable_box

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

DOC = """\
flowdash: 1
title: Plant
turns:
- {id: t1, actor: agent, at: '2026-11-20T10:00:00Z', run: r1, question: 'How much does the plant make?'}
- {id: t2, status: pending, question: 'Which month was the best?'}
groups:
  output: {label: Plant output}
nodes:
  level: {component: Widgets/Slider, config: {label: Level, default_value: 40}, position: [0, 0]}
  output:
    component: Widgets/KPI
    config: {label: Output, suffix: ' MW'}
    origin: t1
    group: output
    position: [300, 0]
edges:
- {from: level.selected, to: output.value}
layout:
  tiles:
  - {node: level}
  - {node: output}
"""

REPORT = """\
report:
  title: Plant review
  blocks:
  - heading: Summary
  - text: The plant runs at {{ level.selected }} today.
  - turn: t1
  - turn: t2
"""


def _editor(report=True):
    editor = FlowDash({}, notifications=False, editable=False)
    editor.import_dashboard(DOC + (REPORT if report else ""))
    return editor


def _report(page):
    return page.locator(".flowdash-report")


def test_switching_to_the_report_and_back_keeps_the_tiles_working(page):
    editor = _editor(report=False)
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    serve_component(page, editor)
    expect(page.get_by_text("40 MW")).to_be_visible()

    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    expect(report.get_by_text("40 MW")).to_be_visible()
    assert editor.surface == "report" and not editor.explore
    # The slider is in the controls bar above the document, not in its body.
    expect(
        report.locator(".fd-report-controls .noUi-target, .fd-report-controls .MuiSlider-root")
    ).to_have_count(1)

    page.get_by_role("button", name="View", exact=True).click()
    expect(_report(page)).to_have_count(0)
    editor._component_instances["level"].value = 55
    expect(page.get_by_text("55 MW")).to_be_visible()

    page.get_by_role("button", name="Report", exact=True).click()
    expect(_report(page).get_by_text("55 MW")).to_be_visible()
    page.get_by_role("button", name="Explore", exact=True).click()
    expect(_report(page)).to_have_count(0)
    page.get_by_role("button", name="View", exact=True).click()
    editor._component_instances["level"].value = 60
    expect(page.get_by_text("60 MW")).to_be_visible()
    assert errors == []


def test_live_values_follow_a_control(page):
    editor = _editor()
    serve_component(page, editor)
    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    value = report.locator(".fd-report-value")
    expect(value).to_have_text("40")
    editor._component_instances["level"].value = 75
    expect(value).to_have_text("75")
    expect(report.get_by_text("75 MW")).to_be_visible()
    expect(report.locator(".fd-report-meta")).to_contain_text("Level: 75")


def test_derived_and_authored_reports(page):
    editor = _editor(report=False)
    editor.read_only = False
    serve_component(page, editor)
    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    expect(report.get_by_text("Built from this dashboard")).to_be_visible()
    expect(report.get_by_text("Edit the dashboard to customize it.")).to_be_visible()
    expect(report.get_by_role("button", name="Customize")).to_be_hidden()
    # The derived report shows the group as a section, headed by its label.
    expect(report.locator(".fd-report-group").get_by_text("Plant output")).to_be_visible()
    expect(report.locator(".fd-report-title")).to_have_text("Plant")

    blocks = [{"heading": "Summary"}, {"group": "output"}]
    editor.set_report({"title": "Plant review", "blocks": blocks})
    expect(report.locator(".fd-report-title")).to_have_text("Plant review")
    expect(report.get_by_text("Built from this dashboard")).to_be_hidden()
    expect(report.locator(".fd-contents-list").first).to_contain_text("Summary")


def test_done_and_pending_turns(page):
    editor = _editor()
    serve_component(page, editor)
    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    turn = report.locator(".fd-report-turn")
    expect(turn.get_by_text("How much does the plant make?")).to_be_visible()
    expect(turn.get_by_text("40 MW")).to_be_visible()
    pending = report.locator(".fd-report-pending")
    expect(pending).to_contain_text("Which month was the best?")
    expect(pending).to_contain_text("Not answered yet.")
    expect(pending.get_by_role("button", name="Run")).to_be_disabled()
    item = report.locator(".fd-contents-list .fd-contents-item", has_text="Which month")
    expect(item.first).to_contain_text("Not run")


def test_run_answers_a_pending_question_in_place(page):
    from panel_flowdash.turns import agent_edits

    editor = _editor()
    asked = []

    def run_pending(turn_id):
        asked.append(turn_id)
        # As the assistant does: the request's edits answer the question.
        editor.answer_pending(turn_id, "run-answer")
        with agent_edits("run-answer", "Which month was the best?"):
            editor.add_component(
                "Widgets/KPI", {"label": "Best", "suffix": " MW"}, instance_id="best"
            )
            editor.connect("level", "selected", "best", "value")
        editor.end_agent_turns("run-answer")

    editor.run_pending = run_pending
    serve_component(page, editor)
    page.get_by_role("button", name="Report", exact=True).click()
    report = _report(page)
    pending = report.locator(".fd-report-pending")
    run = pending.get_by_role("button", name="Run")
    expect(run).to_be_enabled()
    run.click()
    wait_until(lambda: asked == ["t2"], page)
    expect(pending).to_have_count(0)
    answer = report.locator(".fd-report-turn", has_text="Which month was the best?")
    expect(answer.locator(".fd-report-node")).to_have_count(1)
    expect(answer).to_contain_text("40 MW")
    assert editor.node_origin("best") == "t2"


def test_run_clicked_twice_asks_once_and_shows_it_running(page):
    editor = _editor()
    asked = []

    def run_pending(turn_id):
        asked.append(turn_id)
        editor.running_pending = [*editor.running_pending, turn_id]

    editor.run_pending = run_pending
    serve_component(page, editor)
    editor.surface = "report"
    pending = _report(page).locator(".fd-report-pending")
    run = pending.get_by_role("button", name="Run")
    expect(run).to_be_enabled()
    run.dblclick()
    running = pending.get_by_role("button", name="Running")
    expect(running).to_be_disabled()
    wait_until(lambda: asked == ["t2"], page)
    page.wait_for_timeout(300)
    assert asked == ["t2"]
    editor.running_pending = []
    expect(pending.get_by_role("button", name="Run")).to_be_enabled()


def test_a_done_turn_keeps_its_output_above_the_next_block(page):
    editor = _editor()
    serve_component(page, editor)
    editor.surface = "report"
    report = _report(page)
    node = report.locator(".fd-report-turn .fd-report-node")
    expect(node.get_by_text("40 MW")).to_be_visible()
    pending = report.locator(".fd-report-pending")

    def apart():
        turn = report.locator(".fd-report-turn").bounding_box()
        box = node.bounding_box()
        return (
            box["y"] + box["height"] <= turn["y"] + turn["height"] + 1
            and turn["y"] + turn["height"] <= pending.bounding_box()["y"]
        )

    wait_until(apart, page, timeout=5000)


def test_undo_refreshes_the_report(page):
    editor = _editor()
    editor.history_coalesce = 0
    serve_component(page, editor)
    editor.surface = "report"
    title = _report(page).locator(".fd-report-title")
    expect(title).to_have_text("Plant review")
    editor.set_report({**editor.report, "title": "Renamed"})
    expect(title).to_have_text("Renamed")
    assert editor.undo()
    expect(title).to_have_text("Plant review")


def test_phone_width_is_one_column_with_chips(page):
    page.set_viewport_size({"width": 390, "height": 844})
    editor = _editor()
    serve_component(page, editor)
    editor.surface = "report"
    report = _report(page)
    expect(report.locator(".fd-report-value")).to_have_text("40")
    # The contents list and the controls fold into chips.
    expect(report.locator(".fd-contents-chip > button")).to_be_visible()
    expect(report.locator(".fd-contents-label")).to_be_hidden()
    controls = report.locator(".fd-report-controls")
    expect(controls).to_be_hidden()
    report.get_by_role("button", name="Filters · 1").click()
    expect(controls).to_be_visible()

    article = report.locator(".fd-report-article").bounding_box()
    assert article["x"] >= 0 and article["x"] + article["width"] <= 390
    wait_until(
        lambda: page.evaluate("document.documentElement.scrollWidth") <= 390, page, timeout=5000
    )

    report.locator(".fd-contents-chip > button").click()
    report.locator(".fd-contents-chip .fd-contents-item", has_text="Which month").click()
    pending = report.locator(".fd-report-pending")
    wait_until(lambda: 0 <= pending.bounding_box()["y"] < 844, page, timeout=5000)


def test_a_narrow_window_keeps_report_in_the_switcher_only_while_shown(page):
    page.set_viewport_size({"width": 1000, "height": 800})
    editor = _editor()
    serve_component(page, editor)
    report = page.get_by_role("button", name="Report", exact=True)
    expect(report).to_be_hidden()
    page.get_by_role("button", name="More actions").click()
    page.get_by_role("menuitem", name="Report").click()
    expect(_report(page)).to_be_visible()
    expect(report).to_be_visible()


# --------------------------------------------------------------------------
# Authoring
# --------------------------------------------------------------------------


EXTRA = """\
  extra: {component: Widgets/KPI, config: {label: Extra}, group: spare, position: [600, 0]}
"""


def _author(report=True, extra=False):
    """An editor in edit mode, with every gesture its own version."""
    doc = DOC
    if extra:
        doc = (
            doc.replace("groups:\n", "groups:\n  spare: {label: Spare}\n")
            .replace("edges:\n", EXTRA + "edges:\n")
            .replace("  - {node: output}\n", "  - {node: output}\n  - {node: extra}\n")
        )
    editor = FlowDash({}, notifications=False, editable=True, history_coalesce=0)
    editor.import_dashboard(doc + (REPORT if report else ""))
    editor.read_only = False
    return editor


def _open_report(page):
    """Open the Report view from the view switch, as a person would, in the session's own thread."""
    page.get_by_role("button", name="Report", exact=True).click()
    expect(_report(page)).to_be_visible(timeout=DASHBOARD_LOAD_TIMEOUT)


def _kinds(editor):
    return [next(iter(block)) for block in editor.report["blocks"]]


def _shown(page, count, timeout=10000):
    """Wait until the edited report shows *count* blocks, each with its handle."""
    expect(_report(page).locator(".fd-report-handle button")).to_have_count(count, timeout=timeout)


# The first word of each kind's handle label; a node's is its title.
_HANDLE_WORD = {"turn": "question", "node": None}


def _shown_in_order(page, kinds, timeout=10000):
    """Wait until the edited report shows blocks of *kinds*, in order.

    The blocks are re-rendered a moment after the report changes, so the
    page can still show the previous order once the report has the new one.
    """
    words = [_HANDLE_WORD.get(kind, kind) for kind in kinds]
    handles = _report(page).locator(".fd-report-handle button")

    def shown():
        labels = handles.evaluate_all("(els) => els.map((el) => el.getAttribute('aria-label'))")
        shown = [(label or "").removeprefix("Drag to reorder: ").split(" ")[0] for label in labels]
        return len(shown) == len(words) and all(
            w in (None, s) for w, s in zip(words, shown, strict=True)
        )

    wait_until(shown, page, timeout=timeout)


def _undo_redo(page, editor, before, after):
    """Undo and redo the last gesture, checking the blocks each time."""
    assert editor.undo()
    wait_until(lambda: _kinds(editor) == before, page)
    _shown_in_order(page, before)
    assert editor.redo()
    wait_until(lambda: _kinds(editor) == after, page)
    _shown_in_order(page, after)


def _pick(page, index, label, menu=None):
    """Pick *label* from the menu of block *index*, or from *menu*, once the menu lists it."""
    if menu is None:
        menu = _report(page).locator(".fd-report-block-menu").nth(index)
    item = page.get_by_role("menuitem", name=label)

    def picked():
        # The menu closes if its entries change as it opens, e.g. just after a move.
        menu.click()
        try:
            item.click(timeout=1500)
        except Exception:
            page.keyboard.press("Escape")
            return False
        return True

    wait_until(picked, page, timeout=15000)


def test_customize_turns_the_derived_report_into_the_dashboards_own(page):
    editor = _author(report=False)
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    expect(report.get_by_text("Built from this dashboard")).to_be_visible()
    # Outside edit mode an editor reads the report, and is told where to customize it.
    editor.editable = False
    expect(report.get_by_text("Edit the dashboard to customize it.")).to_be_visible()
    expect(report.get_by_role("button", name="Customize")).to_be_hidden()
    editor.editable = True
    editor.surface = "report"

    report.get_by_role("button", name="Customize").click()
    wait_until(lambda: editor.report == {"blocks": [{"group": "output"}]}, page)
    expect(report.get_by_text("Built from this dashboard")).to_be_hidden()
    expect(report.locator(".fd-report-handle")).to_have_count(1)
    expect(report.locator(".fd-report-group").get_by_text("40 MW")).to_be_visible()

    assert editor.undo()
    wait_until(lambda: editor.report is None, page)
    expect(report.get_by_text("Built from this dashboard")).to_be_visible()
    assert editor.redo()
    wait_until(lambda: editor.report is not None, page)
    expect(report.locator(".fd-report-handle")).to_have_count(1)


def test_quick_structural_gestures_undo_one_at_a_time(page):
    editor = _author(report=False)
    editor.history_coalesce = 3
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    report.get_by_role("button", name="Customize").click()
    _shown(page, 1)
    report.get_by_role("button", name="Insert a block at the start").click()
    report.locator(".fd-report-insert-bar").get_by_role("button", name="Heading").click()
    _shown(page, 2)
    report.locator(".fd-report-handle button").first.focus()
    page.keyboard.press("ArrowDown")
    wait_until(lambda: _kinds(editor) == ["group", "heading"], page)

    assert editor.undo()
    wait_until(lambda: _kinds(editor) == ["heading", "group"], page)
    assert editor.undo()
    wait_until(lambda: _kinds(editor) == ["group"], page)
    assert editor.undo()
    wait_until(lambda: editor.report is None, page)


def test_insert_each_kind_of_block(page):
    editor = _author(extra=True)
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    expect(report.locator(".fd-report-handle")).to_have_count(4)
    bar = report.locator(".fd-report-insert-bar")
    start = ["heading", "text", "turn", "turn"]

    report.get_by_role("button", name="Insert a block after heading Summary").click()
    bar.get_by_role("button", name="Heading").click()
    wait_until(lambda: _kinds(editor) == ["heading", "heading", *start[1:]], page)
    expect(report.locator(".fd-report-heading-input input").nth(1)).to_have_value("New section")
    _undo_redo(page, editor, start, ["heading", "heading", *start[1:]])
    editor.undo()
    _shown(page, 4)

    report.locator(".fd-report-insert-last .fd-report-insert-button").click()
    bar.get_by_role("button", name="Text").click()
    wait_until(lambda: _kinds(editor) == [*start, "text"], page)
    # The new text block opens for writing.
    expect(report.locator(".fd-md-area").last).to_be_focused()
    page.keyboard.press("Escape")
    editor.undo()
    _shown(page, 4)

    report.locator(".fd-report-insert-last .fd-report-insert-button").click()
    bar.get_by_role("button", name="Node").click()
    bar.get_by_label("Find a node").fill("ext")
    item = bar.get_by_role("button", name="Extra")
    expect(item).to_contain_text("KPI · on the dashboard")
    item.click()
    wait_until(lambda: editor.report["blocks"][-1] == {"node": "extra"}, page)
    expect(report.locator(".fd-report-block-node").last).to_contain_text("also on the dashboard")
    _undo_redo(page, editor, start, [*start, "node"])
    editor.undo()
    _shown(page, 4)

    report.locator(".fd-report-insert-last .fd-report-insert-button").click()
    bar.get_by_role("button", name="Group").click()
    bar.get_by_role("button", name="Spare").click()
    wait_until(lambda: editor.report["blocks"][-1] == {"group": "spare"}, page)
    _undo_redo(page, editor, start, [*start, "group"])
    editor.undo()
    _shown(page, 4)

    report.locator(".fd-report-insert-last .fd-report-insert-button").click()
    bar.get_by_role("button", name="Ask a question").click()
    bar.get_by_label("Question").fill("Where is the most wind?")
    bar.get_by_role("button", name="Add question").click()
    wait_until(lambda: len(editor.report["blocks"]) == 5, page)
    turn_id = editor.report["blocks"][-1]["turn"]
    asked = next(t for t in editor.turns if t.id == turn_id)
    assert (asked.status, asked.question) == ("pending", "Where is the most wind?")
    expect(report.locator(".fd-report-pending").last).to_contain_text("Where is the most wind?")
    _undo_redo(page, editor, start, [*start, "turn"])
    editor.undo()
    assert all(t.id != turn_id for t in editor.turns)


def test_reorder_by_drag_and_by_menu(page):
    editor = _author()
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    # The first page a worker opens can take a while to render the edited blocks.
    _shown(page, 4, timeout=DASHBOARD_LOAD_TIMEOUT)

    # Drag the heading below the text, once the blocks have stopped moving.
    handle = stable_box(report.locator(".fd-report-handle button").first, page)
    target = stable_box(report.locator(".fd-report-insert").nth(2), page)
    x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x, y + 5)
    page.mouse.move(x, target["y"] + target["height"] / 2, steps=10)
    expect(page.locator(".fd-report-drop-line")).to_be_visible()
    page.mouse.up()
    wait_until(lambda: _kinds(editor) == ["text", "heading", "turn", "turn"], page, timeout=10000)
    _shown_in_order(page, ["text", "heading", "turn", "turn"])
    _undo_redo(
        page, editor, ["heading", "text", "turn", "turn"], ["text", "heading", "turn", "turn"]
    )

    # Move it back up with its menu, and down again with the keyboard.
    _pick(page, 1, "Move up")
    wait_until(lambda: _kinds(editor) == ["heading", "text", "turn", "turn"], page)
    _undo_redo(
        page, editor, ["text", "heading", "turn", "turn"], ["heading", "text", "turn", "turn"]
    )
    report.locator(".fd-report-handle button").first.focus()
    page.keyboard.press("ArrowDown")
    wait_until(lambda: _kinds(editor) == ["text", "heading", "turn", "turn"], page)
    _shown_in_order(page, ["text", "heading", "turn", "turn"])
    expect(report.locator(".fd-report-handle button").nth(1)).to_be_focused()


def test_edit_prose_and_insert_a_value(page):
    editor = _author()
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    text = report.locator(".fd-report-block-text")
    expect(text.locator(".fd-report-value")).to_have_text("40")

    text.locator(".fd-md-preview").click()
    area = text.locator(".fd-md-area")
    expect(area).to_be_focused()
    expect(text.get_by_role("button", name="Bold")).to_be_visible()
    area.fill("Output is ")
    area.press("End")
    _pick(page, None, "Level, selected", text.get_by_role("button", name="Insert value"))
    expected = "Output is {{ level.selected }}"
    wait_until(lambda: editor.report["blocks"][1]["text"] == expected, page)
    expect(area).to_be_focused()

    area.press("ControlOrMeta+a")
    text.get_by_role("button", name="Bold").click()
    wait_until(lambda: editor.report["blocks"][1]["text"] == f"**{expected}**", page)
    text.get_by_role("button", name="Done").click()
    expect(text.locator("strong .fd-report-value")).to_have_text("40")
    editor._component_instances["level"].value = 65
    expect(text.locator(".fd-report-value")).to_have_text("65")

    editor.undo()
    wait_until(lambda: editor.report["blocks"][1]["text"] == expected, page)
    editor.redo()
    wait_until(lambda: editor.report["blocks"][1]["text"] == f"**{expected}**", page)

    # The title is edited in place, and an empty one goes back to the dashboard's.
    title = report.locator(".fd-report-title-input input")
    title.fill("Plant notes")
    title.press("Enter")
    wait_until(lambda: editor.report.get("title") == "Plant notes", page)
    editor.undo()
    wait_until(lambda: editor.report.get("title") == "Plant review", page)
    expect(title).to_have_value("Plant review")


def test_remove_caption_table_and_page_break_from_the_menu(page):
    editor = _author()
    editor.set_report({"blocks": [{"heading": "Summary"}, {"node": "output"}]})
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    expect(report.locator(".fd-report-handle")).to_have_count(2)

    _pick(page, 1, "Add caption")
    caption = report.locator(".fd-report-caption-input input")
    caption.fill("Output right now")
    caption.press("Enter")
    wait_until(lambda: editor.report["blocks"][1].get("caption") == "Output right now", page)

    _pick(page, 1, "Page break before")
    wait_until(lambda: editor.report["blocks"][1].get("page_break") is True, page)
    node = report.locator(".fd-report-block-node")
    expect(node.get_by_text("Page break", exact=True)).to_be_visible()

    _pick(page, 0, "Remove from report")
    wait_until(lambda: _kinds(editor) == ["node"], page)
    _undo_redo(page, editor, ["heading", "node"], ["node"])


def test_select_on_canvas_from_the_block_menu(page):
    editor = _author()
    editor.set_report({"blocks": [{"node": "output"}]})
    serve_component(page, editor)
    _open_report(page)
    _pick(page, 0, "Select on canvas")
    wait_until(lambda: editor.surface == "workspace" and editor.mode in ("wiring", "split"), page)
    wait_until(lambda: (editor._flow.selection or {}).get("nodes") == ["output"], page)
    expect(page.locator(".react-flow__node.selected")).to_have_count(1)
    reported = []
    editor._flow.on("selection_changed", reported.append)
    # The inspector opens for the selection before the session closes, and the
    # canvas, back on the page, does not report a selection of its own.
    page.wait_for_timeout(1000)
    assert (editor._flow.selection or {}).get("nodes") == ["output"]
    assert reported == []


def test_viewers_read_the_report(page):
    editor = _author()
    editor.read_only = True
    serve_component(page, editor)
    _open_report(page)
    report = _report(page)
    expect(report.locator(".fd-report-value")).to_have_text("40")
    expect(report.locator(".fd-report-handle")).to_have_count(0)
    expect(report.locator(".fd-report-block-menu")).to_have_count(0)
    expect(report.locator(".fd-report-insert-button")).to_have_count(0)
    expect(report.locator(".fd-report-title-input")).to_be_hidden()
    expect(report.locator(".fd-report-title")).to_have_text("Plant review")
    report.locator(".fd-report-text").click()
    expect(report.locator(".fd-md-area")).to_have_count(0)
