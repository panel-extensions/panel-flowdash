"""Full screen leaves the page sidebar as the user had it."""

import pytest

from tests.test_ui_view_header import _open_with_picker, make_app  # noqa: F401


@pytest.mark.parametrize("was_open", [True, False])
async def test_leaving_full_screen_restores_the_page_sidebar(make_app, was_open):  # noqa: F811
    app = await make_app(editor_options={"controls_bar": False})
    _open_with_picker(app)
    views = list(app._editor.sidebar)
    assert app._page.sidebar_open
    app._page.sidebar_open = was_open

    app._editor.fullscreen = True
    assert app._sidebar_container.objects == []
    assert not app._page.sidebar_open

    app._editor.fullscreen = False
    assert app._sidebar_container.objects == views
    assert app._page.sidebar_open is was_open


async def test_new_sidebar_views_keep_a_collapsed_sidebar_collapsed(make_app):  # noqa: F811
    app = await make_app(editor_options={"controls_bar": False})
    _open_with_picker(app)
    app._page.sidebar_open = False
    app._editor.add_component("Plant/picker")
    assert len(app._sidebar_container.objects) == 2
    assert not app._page.sidebar_open
