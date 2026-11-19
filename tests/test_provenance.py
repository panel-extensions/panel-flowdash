import pytest
from panel_agents import RunState, events as ev

from panel_flowdash.dashboard_store import MemoryDashboardStore
from panel_flowdash.turns import agent_edits
from panel_flowdash.ui.provenance import NOT_RECORDED

from .test_agent_ops import _editor


@pytest.fixture
def editor():
    editor = _editor(MemoryDashboardStore(), finance=True)
    editor.new_dashboard("Mine")
    editor.add_component("T/number", instance_id="mine")
    with agent_edits(run="run-1", question="Double it"):
        editor.add_component("T/doubler", instance_id="double")
    yield editor
    editor.dispose()


def _run(run_id="run-1", question="Double it") -> list[ev.Event]:
    return [
        ev.RunStarted(run_id=run_id, input=question, timestamp=0),
        ev.ToolCallStart(
            tool_call_id=f"{run_id}-c", tool_name="add_node", run_id=run_id, timestamp=1
        ),
        ev.ToolCallResult(tool_call_id=f"{run_id}-c", content="ok", run_id=run_id, timestamp=2),
        ev.RunFinished(run_id=run_id, timestamp=3),
    ]


def _sheet(editor):
    return editor._inspect_dialog


def _texts(objects) -> str:
    out = []
    for obj in objects:
        out.append(str(getattr(obj, "object", "") or getattr(obj, "label", "") or ""))
        out.append(_texts(getattr(obj, "objects", [])))
    return "\n".join(out)


@pytest.fixture
def viewing(editor):
    """The editor in view mode, where the action opens the side sheet."""
    editor.editable = False
    return editor


def test_only_nodes_an_agent_request_created_offer_how_it_was_made(editor):
    assert editor.made_by_agent("double") and not editor.made_by_agent("mine")
    assert editor.origin_turn("double").run == "run-1"
    assert editor.origin_turn("missing") is None
    editor.provenance = False
    assert not editor.made_by_agent("double")


def test_how_it_was_made_shows_the_span_of_the_run(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.show_how_made("double")
    sheet = _sheet(viewing)
    assert sheet.open
    span = sheet.objects[1]
    assert "span-view" in span.css_classes
    assert "Double it" in span.objects[0].object
    assert viewing._inspected_id == "double"


def test_on_the_canvas_it_replaces_the_inspectors_content(editor):
    editor.agent_state = RunState.from_events(_run())
    editor.show_how_made("double")
    assert not _sheet(editor).open
    body = editor._inspector._body.objects
    assert "flowdash-inspector-extra" in body[0].css_classes and "span-view" in body[1].css_classes
    span = editor._inspector._releases[0].__self__
    editor._inspector.refresh(force=True)
    assert span._released


def test_viewers_see_someone_elses_request_redacted_and_their_own_in_full(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.read_only = True
    viewing.show_how_made("double")
    assert "The question is hidden." in _sheet(viewing).objects[1].objects[0].object
    assert viewing.run_redacted("run-1")
    viewing.note_own_run("run-1")
    assert not viewing.run_redacted("run-1")
    viewing.show_how_made("double")
    assert "Double it" in _sheet(viewing).objects[1].objects[0].object


def test_a_request_that_made_a_restricted_node_is_redacted():
    editor = _editor(MemoryDashboardStore(), finance=True)
    editor.new_dashboard("Pay")
    with agent_edits(run="run-s", question="Show salaries"):
        editor.add_component("T/secret", instance_id="secret")
    editor.agent_state = RunState.from_events(_run("run-s", "Show salaries"))
    assert not editor.run_redacted("run-s")
    editor.authorize_component = lambda entry: entry.app_id != "T/secret"
    assert editor.run_redacted("run-s")
    editor.editable = False
    editor.show_how_made("secret")
    assert "Show salaries" not in _texts(_sheet(editor).objects)
    editor.dispose()


def test_a_run_without_recorded_steps_falls_back_to_the_turn(viewing):
    # A stored history restores the run and its versions, but not its steps.
    viewing.agent_state = RunState.from_events(
        [ev.RunStarted(run_id="run-1", input="Double it"), ev.RunFinished(run_id="run-1")]
    )
    viewing.show_how_made("double")
    text = _texts(_sheet(viewing).objects)
    assert "Double it" in text and "Added double." in text
    assert "Made by the assistant on" in text and NOT_RECORDED in text
    assert "Model requests" not in text
    viewing.read_only = True
    text = _texts(_sheet(viewing).objects)
    assert "Double it" not in text and "Added double." not in text


def test_later_changes_are_listed_and_open_their_request(viewing):
    viewing.editable = True
    with agent_edits(run="run-2", question="Rewire it"):
        viewing.connect("mine", "value", "double", "value")
    viewing.add_component("T/number", instance_id="other")
    viewing.end_turn()
    viewing.editable = False
    assert [t.run for t in viewing.later_turns("double")] == ["run-2"]
    viewing.agent_state = RunState.from_events([*_run(), *_run("run-2", "Rewire it")])
    viewing.show_how_made("double")
    later = _sheet(viewing).objects[-1]
    assert "flowdash-provenance-later" in later.css_classes
    assert "Changed later by 1 request" in later.objects[0].object
    later.objects[1].clicks += 1
    sheet = _sheet(viewing)
    assert "How this was changed" in _texts(sheet.objects) and "Rewire it" in _texts(sheet.objects)


def test_nodes_made_by_a_person_open_nothing(viewing):
    viewing.show_how_made("mine")
    assert not _sheet(viewing).open


def test_the_tile_menu_offers_how_it_was_made(viewing):
    tiles = viewing._inspect_tiles
    assert {"double", "mine"} <= set(tiles)

    def labels(tile):
        return [i["label"] for i in tile.more_menu.items]

    assert "How was this made?" in labels(tiles["double"])
    assert "How was this made?" not in labels(tiles["mine"])
    tiles["double"]._on_menu({"action": "how_made"})
    assert _sheet(viewing).open
    viewing.provenance = False
    assert "How was this made?" not in labels(tiles["double"])


def test_closing_the_sheet_releases_the_span(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.show_how_made("double")
    span = viewing._span_view
    assert span is not None and not span._released
    header = span._header.object
    viewing._close_inspection_dialog()
    assert span._released and viewing._span_view is None
    viewing.agent_state.apply(
        ev.GuardrailFired(name="sql", message="Blocked", severity="block", run_id="run-1")
    )
    assert span._header.object == header


def test_replacing_the_sheet_releases_the_span(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.show_how_made("double")
    first = viewing._span_view
    viewing.show_how_made("double")
    assert first._released and viewing._span_view is not first
    assert not viewing._span_view._released


def test_the_sheet_follows_a_change_of_read_only(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.show_how_made("double")
    viewing.read_only = True
    assert "The question is hidden." in _sheet(viewing).objects[1].objects[0].object


def test_a_request_that_changed_a_restricted_node_is_redacted():
    editor = _editor(MemoryDashboardStore(), finance=True)
    editor.new_dashboard("Pay")
    editor.add_component("T/secret", instance_id="secret")
    editor.add_component("T/doubler", instance_id="double")
    with agent_edits(run="run-c", question="Double the salaries"):
        editor.connect("secret", "value", "double", "value")
    assert editor.origin_turn("double").run is None
    assert not editor.run_redacted("run-c")
    editor.authorize_component = lambda entry: entry.app_id != "T/secret"
    assert editor.run_redacted("run-c")
    editor.dispose()


def test_a_closed_sheet_stays_closed_when_read_only_changes(viewing):
    viewing.agent_state = RunState.from_events(_run())
    viewing.show_how_made("double")
    viewing._close_inspection_dialog()
    viewing.read_only = True
    assert not _sheet(viewing).open and _sheet(viewing).objects == []
    assert viewing._span_view is None
