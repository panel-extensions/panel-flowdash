"""UI tests of node actions a library adds to the node menus, and the provenance chip of a view."""

import pytest
from panel.tests.util import wait_until

from panel_flowdash.editor import FlowDash
from panel_flowdash.node_actions import NodeAction, register_node_action, unregister_node_action
from tests.test_node_actions import Measured
from tests.ui.util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

# The inspector swaps in new widgets on every selection, so a click can reach
# Bokeh as a model it already knows, which only warns; see test_ui.py.
pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]


@pytest.fixture
def ran():
    calls = []
    register_node_action(
        NodeAction(
            "test.save",
            "Save it…",
            run=lambda ctx: calls.append(ctx.node),
            icon="save",
            admin_only=True,
        )
    )
    yield calls
    unregister_node_action("test.save")


def _app(admin: bool, built: dict):
    def app():
        editor = FlowDash(
            {"T/measured": Measured},
            notifications=False,
            authorize_admin=lambda fresh=False: admin,
        )
        editor.add_component("T/measured", instance_id="source", position=(0, 0))
        editor.add_component("Widgets/KPI", instance_id="kpi", position=(320, 0))
        editor.connect("source", "value", "kpi", "value")
        built["editor"] = editor
        return editor

    return app


def _open_menu(page, instance_id):
    page.locator(f".react-flow__node[data-id='{instance_id}']").click()
    expect(page.locator(".fd-inspector-provenance")).to_contain_text("Metric: capacity")
    page.locator(".fd-inspector-menu button, button.fd-inspector-menu").first.click()


def test_an_admin_runs_a_node_action_from_the_sidecar_menu(page, ran):
    built = {}
    serve_component(page, _app(True, built))
    _open_menu(page, "kpi")
    page.get_by_role("menuitem", name="Save it…").click()
    wait_until(lambda: ran == ["kpi"], page, timeout=8000)


def test_others_are_not_offered_an_admin_action(page, ran):
    built = {}
    serve_component(page, _app(False, built))
    _open_menu(page, "kpi")
    expect(page.get_by_role("menuitem", name="Duplicate")).to_be_visible()
    expect(page.get_by_role("menuitem", name="Save it…")).to_have_count(0)


def test_a_view_shows_the_provenance_chip_and_its_detail_while_laid_out(page):
    built = {}
    serve_component(page, _app(False, built))
    page.get_by_role("button", name="Layout").click()
    chip = page.locator(".flowdash-provenance-chip").first
    expect(chip).to_contain_text("Metric: capacity")
    chip.hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Installed capacity")
