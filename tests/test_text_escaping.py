"""User- and data-supplied text is shown as text, never rendered as HTML or Markdown."""

import html
import re

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
import pytest
from panel.io.notifications import NotificationArea
from panel.viewable import Viewer

from panel_flowdash import MemoryDashboardStore, Permission, register
from panel_flowdash.app import FlowDashApp
from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.dashboard_store import DashboardModel
from panel_flowdash.editor import FlowDash
from panel_flowdash.failure_overlay import FailureMessage, FailureOverlay
from panel_flowdash.placeholders import Placeholder, broken_edges_panel, placeholder_view
from panel_flowdash.ui.inspect_sheet import dataframe_preview, facts, sheet_header
from panel_flowdash.ui.pipeline import Stage, _stage_chip
from panel_flowdash.ui.states import access_denied, dashboard_not_found, page_not_found
from panel_flowdash.ui.text import code_block, escape_markdown, text_typography
from panel_flowdash.ui.tile_layout import OffGrid
from panel_flowdash.ui.waiting import WaitingPlaceholder
from panel_flowdash.util import notify

PAYLOAD = "Q3 <img src=x onerror=\"document.title='pwned'\"> *draft*"
ENTITY_PAYLOAD = "&lt;img src=x onerror=alert(1)&gt;"


def _inner_html(pane) -> str:
    """The HTML the browser inserts for *pane*, after the one decode its model applies."""
    if isinstance(pane, pmui.Alert):
        sent = pane._process_param_change({"object": pane.object})["object"]
    else:
        sent = pane._transform_object(pane.object)["object"]
    return html.unescape(sent)


def _panes(viewable):
    return pn.panel(viewable).select(
        lambda obj: isinstance(obj, (pn.pane.Markdown, pn.pane.HTML, pmui.Alert))
    )


def _assert_shown_as_text(viewable, text=PAYLOAD):
    rendered = [_inner_html(pane) for pane in _panes(viewable)]
    for markup in rendered:
        assert "<img" not in markup, markup
        assert "<em>draft</em>" not in markup, markup
    shown = [html.unescape(re.sub(r"<[^>]+>", "", markup)) for markup in rendered]
    assert any(text in s for s in shown), shown


@register(page=False, component=True, title="Titled", config=["title"])
class Titled(Viewer):
    title = param.String(default="")

    @param.output(param.DataFrame)
    def frame(self):
        return pd.DataFrame({PAYLOAD: [1]})

    def __panel__(self):
        return pn.pane.Markdown("tile")


@pytest.fixture
def editor():
    editor = FlowDash(
        {"Test/titled": Titled},
        notifications=False,
        include_builtin_components=False,
        inspect_outputs=True,
    )
    editor.load_model(DashboardModel(dashboard_id="d", user_id="u", title=PAYLOAD))
    return editor


class TestEscapeMarkdown:
    @pytest.mark.parametrize(
        "text",
        [PAYLOAD, ENTITY_PAYLOAD, "# Heading", "- item", "1. one", "[x](javascript:alert(1))"],
    )
    def test_round_trips_through_typography_as_text(self, text):
        markup = _inner_html(text_typography(text))
        assert re.fullmatch(r"<p>(.*)</p>\n", markup, re.S)
        assert html.unescape(markup[3:-5]) == text

    def test_keeps_underscores_inside_words(self):
        assert escape_markdown("wind_speed_avg") == "wind_speed_avg"
        assert escape_markdown("_draft_") == r"\_draft\_"

    def test_follows_reactive_text(self):
        class Holder(param.Parameterized):
            value = param.String(default="a")

        holder = Holder()
        typography = text_typography(holder.param.value.rx.pipe(str.upper))
        holder.value = "<b>x</b>"
        assert typography.object == "&lt;B&gt;X&lt;/B&gt;"

    def test_code_block_cannot_be_closed_by_its_text(self):
        pane = pn.pane.Markdown(code_block(f"```\n{PAYLOAD}\n````"))
        assert "<img" not in _inner_html(pane)


class TestEditorSurfaces:
    def test_dashboard_bar_title_and_description(self, editor):
        editor.settings.description = PAYLOAD
        editor.editable = False
        bar = editor._dashboard_bar
        bar.sync()
        _assert_shown_as_text(bar.title)
        _assert_shown_as_text(bar.description)

    def test_tile_title(self, editor):
        editor.add_component("Test/titled", config={"title": PAYLOAD})
        editor.settings.tile_titles = True
        editor.editable = False
        (tile,) = editor._tile_grid.objects
        _assert_shown_as_text(tile.title)

    def test_inspector_summary_and_component_header(self, editor):
        _assert_shown_as_text(pn.Column(*editor._inspector._summary_view()))
        instance_id = editor.add_component("Test/titled", config={"title": PAYLOAD})
        header = editor._inspector._header("widgets", editor._instance_title(instance_id), None)
        _assert_shown_as_text(header)

    def test_inspect_sheet_title_facts_and_column_chips(self, editor):
        instance_id = editor.add_component("Test/titled", config={"title": PAYLOAD})
        _assert_shown_as_text(sheet_header(editor._instance_title(instance_id), lambda: None))
        _assert_shown_as_text(facts([("Comes from", PAYLOAD)]))
        _assert_shown_as_text(dataframe_preview(pd.DataFrame({PAYLOAD: [1]}), "out"))

    def test_value_popup_title(self, editor):
        _assert_shown_as_text(editor._build_value_popup(PAYLOAD, PAYLOAD, None))

    def test_import_confirmation_names_the_file(self, editor):
        editor.add_component("Test/titled")
        editor._on_files_dropped([{"name": f"{PAYLOAD}.json", "content": '{"items": []}'}])
        _assert_shown_as_text(editor._import_message, f"{PAYLOAD}.json")

    def test_pipeline_stage_chip(self):
        _assert_shown_as_text(_stage_chip(Stage(PAYLOAD, "ok", ("a",))))

    def test_layout_panel_off_grid_row(self, editor):
        row = editor._layout_panel._off_grid_row(OffGrid("a", PAYLOAD, "hidden"))
        _assert_shown_as_text(row)


class TestStatesAndPlaceholders:
    def test_not_found_and_denied_pages(self):
        _assert_shown_as_text(
            dashboard_not_found(PAYLOAD, [], on_open=lambda _: None, on_all=lambda: None)
        )
        _assert_shown_as_text(page_not_found(f"/{PAYLOAD}", on_home=lambda: None))
        _assert_shown_as_text(access_denied(user=PAYLOAD, owner="alice"))

    def test_placeholder_and_broken_edges(self):
        placeholder = Placeholder("missing", PAYLOAD, f"Component '{PAYLOAD}' is not available.")
        _assert_shown_as_text(placeholder_view(placeholder))
        _assert_shown_as_text(broken_edges_panel([PAYLOAD], 0, lambda: None))

    def test_failure_overlay_sources_and_details(self):
        class Failures(param.Parameterized):
            failed = param.Dict(default={"a": object()})

        overlay = FailureOverlay(
            pn.pane.Markdown("view"),
            Failures(),
            lambda _failed: FailureMessage(sources=[PAYLOAD], details=PAYLOAD),
        )
        _assert_shown_as_text(overlay.summary)
        _assert_shown_as_text(overlay.details)

    def test_waiting_message(self):
        placeholder = WaitingPlaceholder()
        placeholder.show(["Company"], PAYLOAD)
        _assert_shown_as_text(placeholder.headline)


class TestApp:
    async def test_share_entry_names_and_dialog_subtitle(self, monkeypatch):
        store = MemoryDashboardStore()
        dashboard = store.create_dashboard("alice", PAYLOAD)
        store.set_permission(dashboard.dashboard_id, Permission.from_spec(allow_users=[PAYLOAD]))
        identity = Identity(user="alice", oauth_user="alice")
        monkeypatch.setattr("panel_flowdash.app.resolve_identity", lambda _config: identity)
        app = FlowDashApp(registry={}, store=store, auth_config=AuthConfig(), notifications=False)
        app._editor.load_model(store._load_any(dashboard.dashboard_id))
        app._open_share_dialog(dashboard.dashboard_id, PAYLOAD)
        view = app._editor._settings_view
        _assert_shown_as_text(view._people)
        _assert_shown_as_text(view._subtitle)

    async def test_unknown_page_names_its_path_as_text(self):
        app = FlowDashApp(registry={}, store=MemoryDashboardStore(), notifications=False)
        _assert_shown_as_text(pn.panel(await app._render_page(("x", PAYLOAD))))


def test_notifications_escape_html_for_panels_own_area(monkeypatch):
    area = NotificationArea()
    monkeypatch.setattr(pn.state, "_notifications", {pn.state.curdoc: area}, raising=False)
    monkeypatch.setattr(type(pn.state), "notifications", property(lambda _self: area))
    notify("info", PAYLOAD)
    assert area.notifications[-1].message == html.escape(PAYLOAD)
