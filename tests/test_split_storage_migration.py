"""Apps not served at the root fall back to the split stored under the old shared key."""

import panel as pn

from panel_flowdash.ui.split_view import STORAGE_KEY
from tests.test_split_preferences_storage import _editor


def test_an_app_with_a_path_reads_the_old_key_as_its_legacy_key(monkeypatch):
    monkeypatch.setattr(type(pn.state), "app_url", property(lambda self: "/plant/"))
    storage = _editor()._split_view.storage
    assert storage.key == f"{STORAGE_KEY}:/plant"
    assert storage.legacy_key == STORAGE_KEY


def test_an_app_at_the_root_has_no_legacy_key():
    storage = _editor()._split_view.storage
    assert storage.key == STORAGE_KEY
    assert storage.legacy_key is None
