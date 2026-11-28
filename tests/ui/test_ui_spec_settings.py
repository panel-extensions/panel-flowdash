"""UI tests for a spec edited through a form of its settings and as JSON, kept in sync."""

import json

import pandas as pd
import panel as pn
import param
import pytest
from panel.tests.util import wait_until
from panel.viewable import Viewer

from panel_flowdash import SpecSetting, register
from panel_flowdash.editor import FlowDash
from panel_flowdash.spec_settings import format_spec
from tests.ui.util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

SETTINGS = [
    SpecSetting(
        "mark",
        label="Mark",
        widget="radio",
        options={"Bar": "bar", "Line": "line", "Point": "point"},
        group="Chart",
    ),
    SpecSetting("encoding.x.field", label="X", widget="select", choices="columns", of="data"),
    SpecSetting("title", label="Title", group="Labels"),
    SpecSetting("encoding.x.title", label="X title", group="Labels"),
]

SPEC = {
    "mark": "bar",
    "encoding": {
        "x": {"field": "year", "type": "ordinal", "axis": {"labelAngle": -45}},
        "y": {"field": "capacity", "type": "quantitative"},
    },
    "transform": [{"filter": "datum.year >= 2015"}],
}

DATA = pd.DataFrame({"year": [2014, 2015, 2016, 2017], "capacity": [5.0, 8.6, 8.2, 7.1]})


@register(
    page=False, component=True, title="Rows", provides=[{"key": "data", "type": "DataFrame"}]
)
class Rows(Viewer):
    data = param.DataFrame(default=DATA)

    def __panel__(self):
        return pn.pane.Str("rows")


@register(page=False, component=True, title="Chart", config=["spec"], extensions=["vega"])
class Chart(Viewer):
    data = param.DataFrame(default=None, allow_None=True)

    spec = param.Dict(
        default=SPEC, metadata={"flowdash": {"widget": "spec", "settings": SETTINGS}}
    )

    def __panel__(self):
        def render(spec, data):
            values = [] if data is None else data.to_dict("records")
            full = {
                "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
                "width": "container",
                "height": "container",
                **spec,
                "data": {"values": values},
            }
            return pn.pane.Vega(full, sizing_mode="stretch_both", min_height=200)

        return pn.panel(
            pn.bind(render, self.param.spec, self.param.data), sizing_mode="stretch_both"
        )


def _open(page):
    editor = FlowDash(
        {"T/rows": Rows, "T/chart": Chart}, notifications=False, include_builtin_components=False
    )
    rows = editor.add_component("T/rows", position=(0, 0))
    chart = editor.add_component("T/chart", position=(320, 0))
    editor.connect(rows, "data", chart, "data")
    page.set_viewport_size({"width": 1440, "height": 900})
    serve_component(page, editor)
    page.get_by_role("button", name="Split").click()
    wait_until(lambda: editor.mode == "split", page, timeout=8000)
    node = page.locator(f".react-flow__node[data-id='{chart}']")
    # The canvas moves into the split's pane and fits its view, so a click
    # made while it settles can miss the node.
    for _ in range(5):
        stable_box(node, page)
        node.click()
        try:
            wait_until(lambda: editor._flow.selection.get("nodes") == [chart], page, timeout=2000)
            break
        except TimeoutError:
            continue
    # The spec is edited in the code panel under the canvas, not in the inspector.
    expect(page.locator(".fd-edit-spec")).to_be_visible(timeout=10000)
    expect(page.locator(".flowdash-inspector .fd-spec-form")).to_have_count(0)
    page.locator(".fd-edit-spec").click()
    expect(page.locator(".fd-code-panel .fd-spec-form")).to_be_visible(timeout=10000)
    return editor, chart


def _show_json(page):
    page.locator(".fd-code-spec-view").get_by_role("button", name="JSON").click()
    expect(page.locator(".fd-spec-editor .ace_content")).to_be_visible(timeout=8000)


def _show_form(page):
    page.locator(".fd-code-spec-view").get_by_role("button", name="Form").click()
    expect(page.locator(".fd-code-panel .fd-spec-form")).to_be_visible(timeout=8000)


# The SVG the chart on the tile draws, which holds its title; Panel draws Vega on a canvas.
CHART_SVG = """async () => {
  const doc = Bokeh.documents[0]
  const out = []
  for (const model of doc.all_models) {
    if (!model.type.endsWith("VegaPlot")) continue
    const view = Bokeh.index.find_one_by_id(model.id)
    if (view && view.vega_view) out.push(await view.vega_view.toSVG())
  }
  return out.join("")
}"""


def _chart_shows(page, text):
    return text in page.evaluate(CHART_SVG)


def _spec_text(page):
    return page.locator(".fd-spec-editor .ace_content")


def test_a_title_set_in_the_form_updates_the_spec_and_the_chart(page):
    editor, chart = _open(page)
    title = page.get_by_role("textbox", name="Title", exact=True)
    title.fill("Capacity by year")
    # "Run on change" is on for a spec, so the form edit runs without Run.
    title.press("Enter")
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "Capacity by year",
        page,
        timeout=8000,
    )
    _show_json(page)
    expect(_spec_text(page)).to_contain_text('"title": "Capacity by year"', timeout=8000)
    changed = page.locator(".fd-spec-editor .ace_gutter-cell.fd-spec-changed-row")
    expect(changed).to_have_count(1, timeout=8000)
    row = format_spec(editor._component_instances[chart].spec)[1][("title",)][0]
    expect(changed).to_have_text(str(row + 1))
    # The changed line is highlighted as well as marked in the gutter.
    highlight = page.locator(".fd-spec-editor .ace_marker-layer .fd-spec-changed")
    expect(highlight).to_have_count(1)
    assert highlight.bounding_box()["height"] > 0
    wait_until(lambda: _chart_shows(page, "Capacity by year"), page, timeout=10000)


def test_editing_the_spec_text_updates_the_form(page):
    editor, chart = _open(page)
    expect(page.locator(".fd-spec-mark-type, .fd-spec-mark").first).to_be_visible()
    _show_json(page)
    text = json.dumps({**SPEC, "mark": "point", "title": "Typed"}, indent=2)
    page.locator(".fd-spec-editor .ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text(text)
    page.keyboard.press("ControlOrMeta+Enter")
    wait_until(
        lambda: editor._component_instances[chart].spec.get("title") == "Typed", page, timeout=8000
    )
    _show_form(page)
    expect(page.locator(".fd-spec-title input")).to_have_value("Typed", timeout=8000)
    selected = page.locator(".fd-spec-mark button[aria-pressed='true']")
    expect(selected).to_have_text("Point", timeout=8000)
    assert editor._component_instances[chart].spec["mark"] == "point"


def test_a_key_without_a_control_is_flagged_and_kept(page):
    editor, chart = _open(page)
    banner = page.locator(".fd-spec-unshown")
    expect(banner).to_contain_text("encoding.x.axis", timeout=8000)
    expect(banner).to_contain_text("transform")
    x_title = page.locator(".fd-spec-encoding-x-title input")
    x_title.fill("Year")
    x_title.press("Enter")
    wait_until(
        lambda: editor._component_instances[chart].spec["encoding"]["x"].get("title") == "Year",
        page,
        timeout=8000,
    )
    spec = editor._component_instances[chart].spec
    assert spec["transform"] == SPEC["transform"]
    assert spec["encoding"]["x"]["axis"] == {"labelAngle": -45}
    expect(banner).to_contain_text("transform")
    _show_json(page)
    flagged = page.locator(".fd-spec-editor .ace_gutter-cell.fd-spec-only")
    expect(flagged.first).to_be_visible(timeout=8000)


def test_controls_are_named_by_their_labels(page):
    _open(page)
    expect(page.get_by_role("combobox", name="X", exact=True)).to_contain_text("year")
    expect(page.get_by_role("textbox", name="X title", exact=True)).to_have_value("")


def test_invalid_text_disables_the_form_until_it_parses(page):
    editor, chart = _open(page)
    _show_json(page)
    page.locator(".fd-spec-editor .ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text('{"mark": "line",')
    page.keyboard.press("ControlOrMeta+Enter")
    expect(page.locator(".fd-spec-status")).to_contain_text("Not applied", timeout=8000)
    _show_form(page)
    expect(page.locator(".fd-spec-title input")).to_be_disabled()
    assert editor._component_instances[chart].spec == SPEC


def test_the_form_says_why_it_is_disabled_and_run_reports_it(page):
    editor, chart = _open(page)
    _show_json(page)
    page.locator(".fd-spec-editor .ace_content").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text('{"mark": ')
    page.locator(".fd-code-spec-view").get_by_role("button", name="Form").click()
    error = page.locator(".fd-code-json-error")
    expect(error).to_contain_text("not a valid spec", timeout=8000)
    page.locator(".fd-code-run").click()
    expect(page.locator(".fd-code-footer-bar")).to_contain_text(
        "the JSON is not valid", timeout=8000
    )
    page.locator(".fd-code-fix-json").click()
    expect(page.locator(".fd-spec-editor .ace_content")).to_be_visible(timeout=8000)
    expect(error).to_be_hidden()
    assert editor._component_instances[chart].spec == SPEC
