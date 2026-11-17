"""A page waiting for session state names the missing keys as code, shown exactly as typed."""

import html
import re
from types import SimpleNamespace

import pytest

from panel_flowdash.ui.text import code_span, markdown_pane
from tests.test_ui_view_header import make_app  # noqa: F401

TRICKY = ["ticker", "a`b", "``x``", " padded ", "<i>x</i> $$y$$ \\(z\\)", "two\n\nlines *em*"]


def _codes(markup: str) -> list[str]:
    rendered = markdown_pane(markup)._transform_object(markup)["object"]
    rendered = html.unescape(rendered)
    assert "<i>" not in rendered and "<em>" not in rendered
    return [html.unescape(code) for code in re.findall(r"<code>(.*?)</code>", rendered, re.S)]


@pytest.mark.parametrize("text", TRICKY)
def test_code_span_shows_text_as_typed(text):
    assert _codes(code_span(text)) == [text.replace("\n", " ")]


async def test_waiting_page_lists_keys_as_code(make_app):  # noqa: F811
    app = await make_app()
    entry = SimpleNamespace(
        title="Prices *now*",
        metadata=SimpleNamespace(requires=["ticker", "<i>x</i>"]),
    )
    alert = await app._instantiate_entry(entry, context="page")
    assert alert.object.endswith("is waiting for: `ticker`, `<i>x</i>`")
    assert _codes(alert.object) == ["ticker", "<i>x</i>"]
