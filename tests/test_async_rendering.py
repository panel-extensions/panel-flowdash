"""Tests that async components render on both the page and the tile path.

An ``async def app`` handed straight to ``pn.panel`` becomes a ``Str`` pane
holding the coroutine's repr, the body never runs, and Python emits
``RuntimeWarning: coroutine 'app' was never awaited``. Both entry points must
therefore defer async callables to Panel rather than calling them inline.
"""

import asyncio
import sys

import panel as pn
import pytest

from panel_flowdash.app import FlowDashApp
from panel_flowdash.dashboard_store import DashboardStore

PAGES = {
    "sync_page.py": (
        "@register(page=True, title='Sync')\n"
        "def app():\n"
        "    return pn.pane.Markdown('sync page')\n"
    ),
    "async_page.py": (
        "@register(page=True, title='Async')\n"
        "async def app():\n"
        "    return pn.pane.Markdown('async page')\n"
    ),
    "gen_page.py": (
        "@register(page=True, title='Gen')\n"
        "async def app():\n"
        "    yield pn.pane.Markdown('first')\n"
        "    yield pn.pane.Markdown('second')\n"
    ),
    "viewer_page.py": (
        "@register(page=True, title='Viewer')\n"
        "class app(pn.viewable.Viewer):\n"
        "    async def __panel__(self):\n"
        "        return pn.pane.Markdown('async viewer page')\n"
    ),
}


def _create_project(tmp_path):
    section = tmp_path / "Async"
    section.mkdir()
    (section / "__init__.py").write_text("")
    for filename, body in PAGES.items():
        (section / filename).write_text(
            "import panel as pn\n\nfrom panel_flowdash import register\n\n\n" + body
        )


@pytest.fixture
async def app(tmp_path):
    _create_project(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        store = DashboardStore(tmp_path / "test.db")
        instance = FlowDashApp(project_dir=tmp_path, store=store)
        await instance._ensure_components_loaded()
        yield instance
    finally:
        sys.path.remove(str(tmp_path))


def _content(view):
    """Resolve what a view renders, looking through any deferred pane."""
    return getattr(view, "_pane", view)


class TestPageRendering:
    async def test_sync_page_renders(self, app):
        view = await app._render_page(("Async", "sync_page"))
        assert _content(view).object == "sync page"

    async def test_async_page_is_awaited(self, app, recwarn):
        view = await app._render_page(("Async", "async_page"))
        await asyncio.sleep(0.05)

        assert isinstance(_content(view), pn.pane.Markdown)
        assert _content(view).object == "async page"
        assert not [w for w in recwarn if "never awaited" in str(w.message)]

    async def test_async_generator_page_is_iterated(self, app):
        view = await app._render_page(("Async", "gen_page"))
        await asyncio.sleep(0.05)

        assert _content(view).object == "second"

    async def test_async_viewer_page_is_awaited(self, app):
        view = await app._render_page(("Async", "viewer_page"))
        await asyncio.sleep(0.05)

        assert _content(view).object == "async viewer page"

    @pytest.mark.parametrize("page", ["async_page", "gen_page", "viewer_page"])
    async def test_pages_never_render_a_coroutine_repr(self, app, page):
        """The bug's signature: the repr of an un-awaited coroutine on screen."""
        view = await app._render_page(("Async", page))
        await asyncio.sleep(0.05)

        rendered = str(_content(view).object)
        assert "coroutine" not in rendered
        assert "async_generator" not in rendered
