"""UI tests for the size, look and alignment of the controls in the row above the tiles."""

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.viewable import Viewer

from panel_flowdash import DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import serve_component, stable_box

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

LONG_STATE = "TX (18,315 turbines)"


# A control's view is often wrapped in a column sized like the widget, e.g. by
# the failure overlay, which then keeps the widget's default 300px as its minimum.
def _wrapped(widget):
    return pn.Column(widget, width=widget.width, sizing_mode="stretch_width", margin=0)


@register(page=False, component=True, sidebar=True, title="State")
class StatePicker(Viewer):
    state = param.Selector(default=LONG_STATE, objects=["All states", LONG_STATE])

    def __panel__(self):
        return _wrapped(
            pmui.Select.from_param(
                self.param.state, label="State", searchable=True, sizing_mode="stretch_width"
            )
        )


@register(page=False, component=True, sidebar=True, title="Project")
class ProjectPicker(Viewer):
    project = param.String(default=None, allow_None=True)

    def __panel__(self):
        return _wrapped(
            pmui.AutocompleteInput.from_param(
                self.param.project,
                options=["Alpha", "Beta"],
                label="Project",
                placeholder="Type a project name",
                sizing_mode="stretch_width",
            )
        )


@register(page=False, component=True, title="Note")
def note(config):
    return "note tile"


COMPONENTS = {"Test/state": StatePicker, "Test/project": ProjectPicker, "Test/note": note}


def _serve(page, width, header_layout, copies=1):
    page.set_viewport_size({"width": width, "height": 900})
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Plant")
    model.items = [
        *(
            DashboardItem(instance_id=f"{name}{i}", component_id=f"Test/{name}")
            for i in range(copies)
            for name in ("state", "project")
        ),
        DashboardItem(instance_id="note", component_id="Test/note"),
    ]
    store.save_dashboard(model)

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS),
            store=store,
            notifications=False,
            header_layout=header_layout,
        )

    serve_component(
        page, {"/": factory, "/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}"
    )
    page.locator(".flowdash-tile").filter(visible=True).first.wait_for()


@pytest.mark.parametrize("header_layout", ["compact", "full"])
@pytest.mark.parametrize("width", [1440, 1024])
def test_controls_row_is_even_and_lines_up_with_the_tiles(page, width, header_layout):
    _serve(page, width, header_layout)

    row = page.locator(".fd-controls-bar")
    outlines = row.locator(".MuiInputBase-root")
    expect(outlines).to_have_count(2)
    expect(row.get_by_role("combobox").first).to_have_text(LONG_STATE)
    boxes = [stable_box(outlines.nth(i), page) for i in range(2)]
    state, project = boxes

    assert not _overlap(state, project)
    assert abs(state["height"] - project["height"]) <= 1
    assert abs(state["y"] - project["y"]) <= 1

    # Both labels sit in the outline's notch, so it is open for each of them.
    for i, label in enumerate(["State", "Project"]):
        notch = (
            outlines.nth(i).locator("legend").evaluate("(el) => el.getBoundingClientRect().width")
        )
        assert notch > 10, label
        label_box = stable_box(row.locator("label").filter(has_text=label), page)
        assert label_box["y"] < boxes[i]["y"] + 2, label

    first_tile = stable_box(page.locator(".flowdash-tile").filter(visible=True).first, page)
    assert abs(state["x"] - first_tile["x"]) <= 2


def _overlap(a, b):
    return not (
        a["x"] + a["width"] <= b["x"]
        or b["x"] + b["width"] <= a["x"]
        or a["y"] + a["height"] <= b["y"]
        or b["y"] + b["height"] <= a["y"]
    )


def test_controls_wrap_instead_of_overlapping(page):
    # Six controls are wider than the row at 1024 px, so they take two lines.
    _serve(page, 1024, "compact", copies=3)

    row = page.locator(".fd-controls-bar")
    outlines = row.locator(".MuiInputBase-root")
    expect(outlines).to_have_count(6)
    boxes = [stable_box(outlines.nth(i), page) for i in range(6)]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not _overlap(a, b)
    assert len({round(box["y"]) for box in boxes}) == 2
    row_box = stable_box(row, page)
    assert all(box["x"] + box["width"] <= row_box["x"] + row_box["width"] for box in boxes)
