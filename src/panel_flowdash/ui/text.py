r"""Render user- and data-supplied strings as literal text.

``pmui.Typography``, ``pmui.Alert`` and the Markdown panes render their object as
Markdown with raw HTML allowed, so a dashboard title, a column name or an error
message must be escaped before it reaches them. ``pn.pane.HTML`` takes HTML, so
text interpolated into its markup goes through ``html.escape`` instead.

Markdown panes also typeset TeX found between ``\(``, ``\[``, ``$$`` or
``\begin{...}`` with MathJax, whose ``\style`` and ``\href`` commands can load
remote resources or run script. FlowDash's own Markdown never contains math, so its
panes come from :func:`markdown_pane` with math turned off, and escaped text cannot
form a delimiter either.
"""

from __future__ import annotations

import html
import re
import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from markdown_it import MarkdownIt

# Underscores inside a word never emphasise, so names like ``wind_speed`` stay as typed.
# ``:`` starts links (``https:``), emoji (``:smile:``) and definition list items.
_INLINE = re.compile(r"([`*\[\]~|:]|(?<![^\W_])_|_(?![^\W_]))")
# A backslash or dollar sign is wrapped in its own element, so no MathJax delimiter
# (``\(``, ``\[``, ``\begin``, ``$$``) survives in the rendered HTML or in a text node.
_MATH = re.compile(r"[\\$]")
_RAW = re.compile(r"[\\$\r\n]")
_BLOCK = re.compile(r"^([ \t]*)([#+=-])", re.MULTILINE)
_ORDERED = re.compile(r"^([ \t]*\d+)([.)])", re.MULTILINE)
# Indentation would start a code block; as character references it is plain text.
_INDENT = re.compile(r"^[ \t]+", re.MULTILINE)


ACRONYMS = frozenset(
    {"api", "csv", "html", "id", "json", "kpi", "sql", "ui", "uri", "url", "utc", "xml"}
)


def field_label(parameter: param.Parameter) -> str:
    """Return the label a config field is shown under, with acronyms in capitals.

    Param derives ``Sql`` from ``sql``, so a label the parameter does not set
    itself is derived again from its name.
    """
    if parameter._label is not None:
        return parameter._label
    words = (parameter.name or "").replace("_", " ").split()
    words = [w.upper() if w.lower() in ACRONYMS else w for w in words]
    return " ".join(words)[:1].upper() + " ".join(words)[1:]


def _math_char(match: re.Match) -> str:
    return f"<span>&#{ord(match.group(0))};</span>"


def escape_markdown(text: t.Any) -> str:
    """Escape ``text`` so a Markdown pane shows it verbatim, without HTML, formatting or math."""
    text = "" if text is None else str(text)
    text = html.escape(text, quote=False)
    text = _MATH.sub(_math_char, text)
    text = _INLINE.sub(r"\\\1", text)
    text = _BLOCK.sub(r"\1\\\2", text)
    text = _ORDERED.sub(r"\1\\\2", text)
    return _INDENT.sub(lambda m: "".join(f"&#{ord(c)};" for c in m.group(0)), text)


class _NoMathMarkdown(pn.pane.Markdown):
    # Panel's Markdown only applies disable_math to its own parser and never sends it
    # to the browser, where MathJax would still typeset \(...\), \[...\] and $$...$$.
    _rename: t.ClassVar[dict[str, str | None]] = {
        **pn.pane.Markdown._rename,
        "disable_math": "disable_math",
    }

    # Never chosen by pn.panel for arbitrary strings.
    priority = 0


class _NoMathAlert(pn.pane.Alert):
    _rename: t.ClassVar[dict[str, str | None]] = {
        **pn.pane.Alert._rename,
        "disable_math": "disable_math",
    }

    priority = 0


def escape_html(text: t.Any) -> str:
    """Escape ``text`` for raw HTML inside a Markdown pane, such as a ``<pre>`` block.

    Line breaks become character references too: a blank line would end the HTML
    block and hand the rest of the text to Markdown.
    """
    text = html.escape("" if text is None else str(text), quote=False)
    return _RAW.sub(lambda m: f"&#{ord(m.group(0))};", text)


def markdown_pane(object: t.Any = "", **params) -> pn.pane.Markdown:
    """Return a Markdown pane for FlowDash's own markup, with MathJax turned off in the browser.

    Interpolate user or data text into ``object`` only through :func:`escape_markdown`.
    """
    params.setdefault("disable_math", True)
    return _NoMathMarkdown(object, **params)


def markdown_renderer(preset: str = "commonmark") -> MarkdownIt:
    """Return a Markdown renderer that shows raw HTML as typed and opens links in a new tab.

    A link followed in the app's own tab would leave the dashboard and any unsaved
    edits behind; a link to an anchor on the page stays in it.
    """
    md = MarkdownIt(preset, {"html": False})

    def link_open(renderer, tokens, idx, options, env):
        token = tokens[idx]
        if not str(token.attrGet("href") or "").startswith("#"):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
        return renderer.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    return md


def alert_pane(object: t.Any = "", **params) -> pn.pane.Alert:
    """Return a Panel ``Alert`` pane with MathJax turned off in the browser, like :func:`markdown_pane`."""
    params.setdefault("disable_math", True)
    return _NoMathAlert(object, **params)


def text_typography(text: t.Any, **params) -> pmui.Typography:
    """Return a ``pmui.Typography`` showing ``text``, a string or a reactive one, as literal text."""
    if isinstance(text, param.rx):
        text = text.rx.pipe(escape_markdown)
    elif isinstance(text, param.Parameter):
        text = param.bind(escape_markdown, text)
    else:
        text = escape_markdown(text)
    params.setdefault("disable_math", True)
    return pmui.Typography(text, **params)


def text_alert(text: t.Any, **params) -> pmui.Alert:
    """Return a ``pmui.Alert`` whose message is ``text`` shown as literal text.

    The message goes to the alert's own ``object``: passed positionally a string
    becomes a child pane chosen by ``pn.panel``, which renders a string ending in
    ``.png`` as a remote image and other strings as Markdown with math.
    """
    return pmui.Alert(object=escape_markdown(text), **params)


def code_span(text: t.Any) -> str:
    """Return ``text`` as inline Markdown code that it cannot close early.

    Line breaks become spaces, since a blank line would end the paragraph and
    with it the span.
    """
    text = re.sub(r"[\r\n]", " ", "" if text is None else str(text))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    # Markdown strips one space from each end of a span that starts and ends with one.
    pad = (
        text.startswith("`")
        or text.endswith("`")
        or (text[:1] == text[-1:] == " " and bool(text.strip()))
    )
    space = " " if pad else ""
    return f"{fence}{space}{text}{space}{fence}"


def code_block(text: t.Any) -> str:
    """Return ``text`` as a fenced Markdown code block that it cannot close early."""
    text = "" if text is None else str(text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"
