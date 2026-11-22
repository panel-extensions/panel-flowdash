"""Tests for the session-scoped page dialog helpers in ``panel_flowdash.util``."""

import asyncio
import gc
import logging
import threading
import weakref

import panel as pn
import panel_material_ui as pmui
import param
import pytest
from bokeh.document import Document
from panel.io.state import set_curdoc
from panel.tests.util import async_wait_until

from panel_flowdash import FlowDash, MemoryDashboardStore
from panel_flowdash.app import FlowDashApp
from panel_flowdash.util import _dialogs, close_dialog, confirm, open_dialog, page_dialog


class _Location(param.Parameterized):
    """Stands in for ``pn.state.location``, which needs a live session."""

    pathname = param.String(default="/")
    search = param.String(default="")


@pytest.fixture
def doc():
    """A document standing in for a served session."""
    document = Document()
    with set_curdoc(document):
        yield document


def _button(dialog, label):
    return next(b for b in dialog.select(pmui.Button) if b.label == label)


def _click(button):
    button.clicks += 1


class TestPageDialog:
    def test_same_dialog_within_a_session(self, doc):
        assert page_dialog() is page_dialog()
        assert isinstance(page_dialog(), pmui.Dialog)

    def test_separate_sessions_get_separate_dialogs(self, doc):
        first = page_dialog()
        with set_curdoc(Document()):
            second = page_dialog()
        assert first is not second
        assert first is page_dialog()

    def test_dialog_starts_closed(self, doc):
        assert page_dialog().open is False

    def test_no_dialog_outside_a_session(self):
        assert page_dialog() is None

    def test_released_when_the_session_is_destroyed(self, doc):
        page_dialog()
        assert doc in _dialogs
        for callback in list(doc.session_destroyed_callbacks):
            callback(None)
        assert doc not in _dialogs

    def test_released_with_an_abandoned_document(self):
        """A document that never reaches ``on_session_destroyed`` is not kept alive."""
        document = Document()
        with set_curdoc(document):
            page_dialog()
        ref = weakref.ref(document)
        del document
        gc.collect()
        assert ref() is None


class TestOpenDialog:
    def test_shows_title_and_content(self, doc):
        content = pn.pane.Markdown("body")
        open_dialog("Details", content)

        dialog = page_dialog()
        assert dialog.open is True
        assert dialog.title == "Details"
        assert dialog.objects == [content]

    def test_replaces_title_and_content_of_an_open_dialog(self, doc):
        open_dialog("First", pn.pane.Markdown("one"))
        second = pn.pane.Markdown("two")
        open_dialog("Second", second)

        dialog = page_dialog()
        assert dialog.open is True
        assert dialog.title == "Second"
        assert dialog.objects == [second]

    def test_is_dismissible_by_the_user(self, doc):
        open_dialog("Details", "body")
        assert page_dialog().close_on_click is True
        assert page_dialog().show_close_button is True

    def test_width(self, doc):
        open_dialog("Details", "body", width="lg")
        assert page_dialog().width_option == "lg"

    def test_close(self, doc):
        open_dialog("Details", "body")
        close_dialog()
        assert page_dialog().open is False

    def test_close_without_a_dialog_does_nothing(self, doc):
        close_dialog()
        assert doc not in _dialogs

    def test_outside_a_session_warns_and_does_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="panel_flowdash"):
            open_dialog("Details", "body")
        assert "outside a served session" in caplog.text
        close_dialog()


class TestConfirm:
    def test_opening_does_not_run_anything(self, doc):
        calls = []
        confirm("Save?", "diff", on_confirm=calls.append, on_cancel=calls.append)

        dialog = page_dialog()
        assert dialog.open is True
        assert dialog.title == "Save?"
        assert calls == []

    def test_is_only_dismissed_by_its_buttons(self, doc):
        confirm("Save?", on_confirm=lambda _event: None)
        dialog = page_dialog()
        assert dialog.close_on_click is False
        assert dialog.show_close_button is False
        assert dialog.close_on_escape is False

    def test_a_later_dialog_is_dismissible_with_escape_again(self, doc):
        confirm("Save?", on_confirm=lambda _event: None)
        open_dialog("Details", "body")
        dialog = page_dialog()
        assert dialog.close_on_click is True
        assert dialog.close_on_escape is True
        assert dialog.actions == []

    def test_closing_without_an_answer_counts_as_cancel(self, doc):
        seen = []
        confirm("Save?", on_confirm=lambda _event: seen.append("confirm"), on_cancel=seen.append)

        close_dialog()

        assert len(seen) == 1
        assert (seen[0].name, seen[0].new) == ("open", False)

    def test_closing_after_an_answer_does_not_cancel(self, doc):
        seen = []
        confirm("Save?", on_confirm=lambda _event: seen.append("confirm"), on_cancel=seen.append)
        dialog = page_dialog()

        _click(_button(dialog, "Confirm"))
        dialog.open = True
        close_dialog()

        assert seen == ["confirm"]

    def test_content_is_optional(self, doc):
        confirm("Save?", on_confirm=lambda _event: None)
        assert page_dialog().objects == []
        assert [b.label for b in page_dialog().actions] == ["Cancel", "Confirm"]

    def test_custom_labels(self, doc):
        confirm("Save?", on_confirm=lambda _event: None, confirm_label="Save", cancel_label="Back")
        assert {b.label for b in page_dialog().select(pmui.Button)} == {"Save", "Back"}

    def test_confirm_closes_before_calling_the_handler(self, doc):
        seen = []
        dialog = page_dialog()
        confirm("Save?", on_confirm=lambda event: seen.append((dialog.open, event)))

        _click(_button(dialog, "Confirm"))

        assert len(seen) == 1
        assert seen[0][0] is False
        assert seen[0][1].name == "clicks"

    def test_cancel_closes_and_calls_on_cancel(self, doc):
        seen = []
        dialog = page_dialog()
        confirm(
            "Save?",
            on_confirm=lambda _event: seen.append("confirm"),
            on_cancel=lambda _event: seen.append(("cancel", dialog.open)),
        )

        _click(_button(dialog, "Cancel"))

        assert seen == [("cancel", False)]

    def test_cancel_without_a_handler_just_closes(self, doc):
        seen = []
        confirm("Save?", on_confirm=seen.append)
        dialog = page_dialog()

        _click(_button(dialog, "Cancel"))

        assert dialog.open is False
        assert seen == []

    def test_a_double_click_runs_the_handler_once(self, doc):
        seen = []
        confirm("Save?", on_confirm=seen.append)
        button = _button(page_dialog(), "Confirm")

        _click(button)
        _click(button)

        assert len(seen) == 1

    def test_answering_once_disarms_the_other_button(self, doc):
        seen = []
        confirm("Save?", on_confirm=lambda _event: seen.append("confirm"), on_cancel=seen.append)
        dialog = page_dialog()
        confirm_button, cancel_button = _button(dialog, "Confirm"), _button(dialog, "Cancel")

        _click(confirm_button)
        _click(cancel_button)

        assert seen == ["confirm"]

    def test_concurrent_clicks_run_one_handler(self, doc):
        """Under threaded Panel a second click may be dispatched while the first is handled."""
        seen = []
        confirm("Save?", on_confirm=seen.append)
        dialog = page_dialog()
        button = _button(dialog, "Confirm")
        other = threading.Thread(target=_click, args=(button,))

        def click_again_while_closing(event):
            if other.ident is None:
                other.start()
                other.join()

        dialog.param.watch(click_again_while_closing, "open")
        _click(button)

        assert len(seen) == 1

    def test_replacing_an_open_confirmation_drops_the_question(self, doc):
        seen = []
        confirm("Save?", on_confirm=seen.append, on_cancel=seen.append)
        open_dialog("Details", "body")

        assert page_dialog().title == "Details"
        close_dialog()
        assert seen == []

    def test_a_replaced_confirmation_cannot_cancel_its_successor(self, doc):
        seen = []
        confirm("Save?", on_confirm=seen.append, on_cancel=lambda _event: seen.append("first"))
        confirm("Apply?", on_confirm=seen.append, on_cancel=lambda _event: seen.append("second"))

        close_dialog()

        assert seen == ["second"]

    def test_a_handler_may_open_the_next_dialog(self, doc):
        confirm("Save?", on_confirm=lambda _event: open_dialog("Saved", "done"))

        _click(_button(page_dialog(), "Confirm"))

        assert page_dialog().open is True
        assert page_dialog().title == "Saved"

    async def test_coroutine_handlers_are_scheduled(self, doc):
        seen = []
        dialog = page_dialog()

        async def on_confirm(event):
            await asyncio.sleep(0)
            seen.append(("confirm", dialog.open))

        async def on_cancel(event):
            seen.append(("cancel", dialog.open))

        confirm("Save?", on_confirm=on_confirm, on_cancel=on_cancel)
        _click(_button(dialog, "Confirm"))
        await async_wait_until(lambda: bool(seen))
        assert seen == [("confirm", False)]

        confirm("Save?", on_confirm=on_confirm, on_cancel=on_cancel)
        _click(_button(dialog, "Cancel"))
        await async_wait_until(lambda: len(seen) == 2)
        assert seen[1] == ("cancel", False)

        confirm("Save?", on_confirm=on_confirm, on_cancel=on_cancel)
        close_dialog()
        await async_wait_until(lambda: len(seen) == 3)
        assert seen[2] == ("cancel", False)

    def test_outside_a_session_warns_and_runs_no_handler(self, caplog):
        calls = []
        with caplog.at_level(logging.WARNING, logger="panel_flowdash"):
            confirm("Save?", on_confirm=calls.append, on_cancel=calls.append)
        assert "outside a served session" in caplog.text
        assert calls == []


class TestLayout:
    def test_editor_lays_out_the_session_dialog(self, doc):
        editor = FlowDash({}, notifications=False)
        assert editor._dialog_host.objects.count(page_dialog()) == 1

    def test_editor_keeps_the_dialog_across_mode_changes(self, doc):
        editor = FlowDash({}, notifications=False)
        editor.mode = "dashboard"
        assert editor._dialog_host.objects.count(page_dialog()) == 1
        editor.editable = False
        assert editor._dialog_host.objects.count(page_dialog()) == 1

    def test_editor_can_leave_the_dialog_to_the_embedder(self, doc):
        editor = FlowDash({}, notifications=False, include_page_dialog=False)
        assert page_dialog() not in editor._dialog_host.objects

        editor.include_page_dialog = True
        assert editor._dialog_host.objects.count(page_dialog()) == 1

    def test_editor_outside_a_session_has_no_page_dialog(self):
        editor = FlowDash({}, notifications=False)
        dialogs = [o for o in editor._dialog_host.objects if isinstance(o, pmui.Dialog)]
        assert dialogs == [
            editor._clear_dialog,
            editor._import_dialog,
            editor._settings_dialog,
            editor._transform_offer.dialog,
        ]

    @pytest.mark.parametrize("pathname", ["/", "/dash/{dashboard_id}", "/nowhere"])
    async def test_app_lays_out_the_dialog_once_on_every_route(self, doc, tmp_path, pathname):
        store = MemoryDashboardStore()
        app = FlowDashApp(project_dir=tmp_path, store=store)
        dashboard = store.create_dashboard(app._user_id, "Dash")
        previous, pn.state._location = pn.state._location, _Location()
        try:
            pn.state._location.pathname = pathname.format(dashboard_id=dashboard.dashboard_id)
            await app._load_page_layout()
        finally:
            pn.state._location = previous

        dialog = page_dialog()
        assert app._page.main.count(dialog) == 1
        assert dialog not in app._editor._dialog_host.objects
        assert dialog not in app._page.header

    async def test_app_with_an_editor_class_lays_out_the_dialog_once(self, doc, tmp_path):
        class CustomFlowDash(FlowDash):
            include_page_dialog = param.Boolean(default=True)

        app = FlowDashApp(
            project_dir=tmp_path, store=MemoryDashboardStore(), editor_class=CustomFlowDash
        )
        previous, pn.state._location = pn.state._location, _Location()
        try:
            await app._load_page_layout()
        finally:
            pn.state._location = previous

        assert app._editor.include_page_dialog is False
        assert app._page.main.count(page_dialog()) == 1
        assert page_dialog() not in app._editor._dialog_host.objects

    async def test_app_navigation_closes_the_dialog(self, doc, tmp_path):
        app = FlowDashApp(project_dir=tmp_path, store=MemoryDashboardStore())
        confirm("Leave?", on_confirm=lambda _event: None)
        previous, pn.state._location = pn.state._location, _Location()
        try:
            pn.state._location.pathname = "/nowhere"
            await app._load_page_layout()
        finally:
            pn.state._location = previous

        assert page_dialog().open is False
