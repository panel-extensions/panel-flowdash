"""UI test: a control feeding several tiles updates them all in one document patch."""

import json

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.tests.util import wait_until

from panel_flowdash import DashboardEdge, DashboardItem, MemoryDashboardStore, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import resolve_identity
from panel_flowdash.component_library import normalize_components

from .util import serve_component

pytest.importorskip("playwright")

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@register(page=False, component=True, title="Amount", provides=[{"key": "value", "type": "int"}])
class Amount(pn.viewable.Viewer):
    value = param.Integer(default=0)

    def __panel__(self):
        return pmui.IntInput.from_param(self.param.value, label="Amount")


@register(
    page=False, component=True, title="Viewer tile", requires=[{"key": "value", "type": "int"}]
)
class ViewerTile(pn.viewable.Viewer):
    value = param.Integer(default=None, allow_None=True)

    def __panel__(self):
        return pn.pane.Markdown(pn.bind(lambda v: f"viewer got {v}", self.param.value))


@register(
    page=False, component=True, title="Function tile", requires=[{"key": "value", "type": "int"}]
)
def function_tile(config):
    return pn.pane.Markdown(pn.bind(lambda v: f"function got {v}", config.param.value))


COMPONENTS = {"T/amount": Amount, "T/viewer": ViewerTile, "T/function": function_tile}
CONSUMERS = ["T/viewer", "T/viewer", "T/viewer", "T/function", "T/function"]


def _content(payload):
    """The events of a server message's content frame, or None for any other frame."""
    if not isinstance(payload, str) or '"events"' not in payload:
        return None
    try:
        return json.loads(payload).get("events")
    except ValueError:
        return None


def test_a_fanned_out_change_arrives_as_one_patch(page):
    store = MemoryDashboardStore()
    model = store.create_dashboard(resolve_identity().user, "Fan-out")
    model.items = [DashboardItem(instance_id="amount", component_id="T/amount", x=0, y=0)] + [
        DashboardItem(instance_id=f"tile{i}", component_id=c, x=0, y=i + 1)
        for i, c in enumerate(CONSUMERS)
    ]
    model.edges = [
        DashboardEdge("amount", "value", f"tile{i}", "value") for i in range(len(CONSUMERS))
    ]
    store.save_dashboard(model)

    patches = []
    page.on(
        "websocket",
        lambda ws: ws.on(
            "framereceived",
            lambda payload: (events := _content(payload)) is not None and patches.append(events),
        ),
    )

    def factory():
        return FlowDashApp(
            registry=normalize_components(COMPONENTS), store=store, notifications=False
        )

    serve_component(page, {"/dash/[^/]+": factory}, suffix=f"/dash/{model.dashboard_id}")
    page.get_by_text("viewer got 0").first.wait_for()

    amount = page.locator("input").filter(visible=True).first
    amount.fill("7")
    amount.press("Enter")

    def updating(events):
        return [e for e in events if "got 7" in json.dumps(e)]

    wait_until(lambda: sum(len(updating(events)) for events in patches) >= len(CONSUMERS), page)
    assert len([events for events in patches if updating(events)]) == 1
