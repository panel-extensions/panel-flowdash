import pytest
from panel_agents import RunState, events as ev

from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.turns import agent_edits
from panel_flowdash.ui.provenance import NOT_RECORDED

from .test_agent_ops import _editor


@pytest.fixture
def editor():
    editor = _editor(MemoryDashboardStore())
    editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="mine")
    with agent_edits(run="run-1", question="Double it"):
        editor.add_component("T/doubler", instance_id="double")
    yield editor
    editor.dispose()


def _run_state() -> RunState:
    return RunState.from_events(
        [
            ev.RunStarted(run_id="run-1", input="Double it", timestamp=0),
            ev.ToolCallStart(tool_call_id="c1", tool_name="add_node", run_id="run-1", timestamp=1),
            ev.ToolCallResult(tool_call_id="c1", content="ok", run_id="run-1", timestamp=2),
            ev.RunFinished(run_id="run-1", timestamp=3),
        ]
    )


def _sheet(editor):
    return editor._inspect_dialog


def test_only_nodes_an_agent_request_created_offer_how_it_was_made(editor):
    assert editor.made_by_agent("double") and not editor.made_by_agent("mine")
    assert editor.origin_turn("double").run == "run-1"
    assert editor.origin_turn("missing") is None
    editor.provenance = False
    assert not editor.made_by_agent("double")


def test_how_it_was_made_shows_the_span_of_the_run(editor):
    editor.agent_state = _run_state()
    editor.show_how_made("double")
    sheet = _sheet(editor)
    assert sheet.open
    span = sheet.objects[1]
    assert "span-view" in span.css_classes
    assert "Double it" in span.objects[0].object
    assert editor._inspected_id == "double"


def test_viewers_see_the_span_redacted(editor):
    editor.agent_state = _run_state()
    editor.read_only = True
    editor.show_how_made("double")
    span = _sheet(editor).objects[1]
    assert "The question is hidden." in span.objects[0].object


def test_a_run_that_is_not_in_the_session_falls_back_to_the_turn(editor):
    editor.show_how_made("double")
    (fallback,) = _sheet(editor).objects[1:]
    texts = [str(getattr(o, "object", "")) for o in fallback.objects]
    assert "Double it" in texts and "Added double." in texts
    assert any("Made by the assistant on" in t for t in texts)
    assert any(NOT_RECORDED in t for t in texts)
    editor.read_only = True
    editor.show_how_made("double")
    (fallback,) = _sheet(editor).objects[1:]
    assert "Double it" not in [str(getattr(o, "object", "")) for o in fallback.objects]


def test_nodes_made_by_a_person_open_nothing(editor):
    editor.show_how_made("mine")
    assert not _sheet(editor).open


def test_the_tile_menu_offers_how_it_was_made(editor):
    editor.editable = False
    tiles = editor._inspect_tiles
    assert {"double", "mine"} <= set(tiles)
    labels = lambda tile: [i["label"] for i in tile.more_menu.items]
    assert "How was this made?" in labels(tiles["double"])
    assert "How was this made?" not in labels(tiles["mine"])
    tiles["double"]._on_menu({"action": "how_made"})
    assert _sheet(editor).open
    editor.provenance = False
    assert "How was this made?" not in labels(tiles["double"])


def test_closing_the_sheet_releases_the_span(editor):
    editor.agent_state = _run_state()
    editor.show_how_made("double")
    span = editor._span_view
    assert span is not None and not span._released
    header = span._header.object
    editor._close_inspection_dialog()
    assert span._released and editor._span_view is None
    editor.agent_state.apply(
        ev.GuardrailFired(name="sql", message="Blocked", severity="block", run_id="run-1")
    )
    assert span._header.object == header


def test_replacing_the_sheet_releases_the_span(editor):
    editor.agent_state = _run_state()
    editor.show_how_made("double")
    first = editor._span_view
    editor.show_how_made("double")
    assert first._released and editor._span_view is not first and not editor._span_view._released
