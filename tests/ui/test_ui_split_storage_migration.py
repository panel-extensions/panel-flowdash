"""UI test: an app not served at the root picks up the split it stored under the old shared key."""

import json

import pytest
from panel.tests.util import wait_until

from panel_flowdash.ui.split_view import STORAGE_KEY

from .test_ui_editor_split_mode import _editor
from .util import serve_component

pytest.importorskip("playwright")
expect = pytest.importorskip("playwright.sync_api").expect

pytestmark = [pytest.mark.ui, pytest.mark.filterwarnings("ignore:reference already known")]

NEW_KEY = f"{STORAGE_KEY}:/plant"


def _serve(page):
    editors = []

    def factory():
        editor, _ids = _editor()
        editors.append(editor)
        return editor

    serve_component(page, {"/plant": factory}, suffix="/plant")
    wait_until(lambda: len(editors) == 1, page)
    return editors


def _stored(page, key):
    raw = page.evaluate(f"localStorage.getItem({key!r})")
    return None if raw is None else json.loads(raw)


def test_the_old_key_is_read_once_and_copied(page):
    editors = _serve(page)
    assert editors[0]._split_view.storage.key == NEW_KEY
    page.evaluate(
        f"localStorage.setItem({STORAGE_KEY!r}, JSON.stringify({{split: true, sizes: [70, 30]}}))"
    )
    page.reload()
    wait_until(lambda: len(editors) == 2 and editors[1].mode == "split", page, timeout=8000)
    expect(page.locator(".gutter")).to_be_visible()
    assert editors[1]._split_view.split.sizes == (70.0, 30.0)
    stored = _stored(page, NEW_KEY)
    assert {k: stored[k] for k in ("split", "sizes")} == {"split": True, "sizes": [70, 30]}
    # Other apps on the origin may still read the old key.
    assert _stored(page, STORAGE_KEY) == {"split": True, "sizes": [70, 30]}


def test_the_new_key_wins_over_the_old_one(page):
    editors = _serve(page)
    page.evaluate(
        f"""() => {{
          localStorage.setItem({STORAGE_KEY!r}, JSON.stringify({{split: true, sizes: [70, 30]}}))
          localStorage.setItem({NEW_KEY!r}, JSON.stringify({{split: false, sizes: [40, 60]}}))
        }}"""
    )
    page.reload()
    wait_until(lambda: len(editors) == 2, page, timeout=8000)
    wait_until(lambda: editors[1]._split_view.preferences.sizes == (40.0, 60.0), page)
    assert editors[1].mode == "wiring"
