"""UI tests of the Report view: the dashboard read as a document over the live graph."""

import pytest
from panel.tests.util import wait_until

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

from panel_flowdash import FlowDash

from .util import serve_component

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
    expect(report.get_by_role("button", name="Customize")).to_be_disabled()
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
