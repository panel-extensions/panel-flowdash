"""Escaped text cannot form links, raw HTML or MathJax math in any FlowDash pane."""

import html
import re
from pathlib import Path

import panel as pn
import panel_material_ui as pmui
import pytest
from bokeh.document import Document

import panel_flowdash
from panel_flowdash.placeholders import Placeholder, placeholder_view
from panel_flowdash.ui.text import (
    alert_pane,
    escape_html,
    escape_markdown,
    markdown_pane,
    text_alert,
    text_typography,
)

from .untrusted_payloads import PAYLOADS

# What MathJax's TeX input looks for in the HTML it is given.
_TEX = re.compile(r"\$\$|\\\(|\\\[|\\begin\{")
_ALLOWED_TAGS = re.compile(r"</?(p|span|br)\s*/?>")


def _rendered(pane) -> str:
    """The HTML the browser inserts for *pane*, after the one decode its model applies."""
    if isinstance(pane, pmui.Alert):
        sent = pane._process_param_change({"object": pane.object})["object"]
    else:
        sent = pane._transform_object(pane.object)["object"]
    return html.unescape(sent)


def _shown(markup: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", markup)).split())


@pytest.mark.parametrize(
    "pane",
    [
        lambda text: markdown_pane(escape_markdown(text)),
        lambda text: pn.pane.Markdown(escape_markdown(text)),
        text_typography,
        text_alert,
    ],
    ids=["markdown_pane", "markdown", "typography", "alert"],
)
@pytest.mark.parametrize("name", list(PAYLOADS))
def test_escaped_text_is_shown_verbatim_without_markup_or_math(pane, name):
    text = f"{name} {PAYLOADS[name]}"
    markup = _rendered(pane(text))
    assert not re.search(r"<(?!/?(p|span|br)\b)", markup), markup
    assert not _TEX.search(markup), markup
    assert _shown(markup) == " ".join(text.split())


@pytest.mark.parametrize("char", ["\\", "$"])
def test_math_characters_are_split_into_their_own_elements(char):
    assert (
        escape_markdown(f"a{char}{char}b")
        == f"a<span>&#{ord(char)};</span><span>&#{ord(char)};</span>b"
    )


def test_escape_html_leaves_no_tex_delimiter():
    escaped = escape_html("<b> \\(x\\)\n\n$$y$$")
    assert escaped == "&lt;b&gt; &#92;(x&#92;)&#10;&#10;&#36;&#36;y&#36;&#36;"


@pytest.mark.parametrize("factory", [markdown_pane, alert_pane])
def test_panes_turn_math_off_in_the_browser(factory):
    pane = factory(r"\(x\)")
    model = pane.get_root(Document())
    assert model.disable_math is True


def test_typography_turns_math_off():
    assert text_typography("x").disable_math is True


@pytest.mark.parametrize("message", ["https://evil.test/shot.png", "/etc/hosts", r"\(x\)"])
def test_placeholder_message_is_the_alerts_own_text(message):
    view = placeholder_view(Placeholder("missing", "Title", message))
    assert isinstance(view, pmui.Alert)
    assert not view.objects
    assert _shown(_rendered(view)) == message


def test_no_pane_bypasses_the_helpers():
    # Markdown panes built directly would typeset math: Panel never sends their
    # disable_math to the browser. Strings given to pmui.Alert positionally become
    # panes chosen by pn.panel, which renders image URLs and Markdown with math.
    root = Path(panel_flowdash.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "text.py":
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(
            r"pn\.pane\.(Markdown|Alert)\(|pmui\.Alert\(\s*(f?[\"']|escape_markdown)", source
        ):
            line = source.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(root)}:{line}")
    assert offenders == []
