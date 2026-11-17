"""Render user- and data-supplied strings as literal text.

``pmui.Typography``, ``pmui.Alert`` and the Markdown panes render their object as
Markdown with raw HTML allowed, so a dashboard title, a column name or an error
message must be escaped before it reaches them. ``pn.pane.HTML`` takes HTML, so
text interpolated into its markup goes through ``html.escape`` instead.
"""

from __future__ import annotations

import html
import re
import typing as t

import panel_material_ui as pmui
import param

# Underscores inside a word never emphasise, so names like ``wind_speed`` stay as typed.
_INLINE = re.compile(r"([\\`*\[\]~|]|(?<![^\W_])_|_(?![^\W_]))")
_BLOCK = re.compile(r"^([ \t]*)([#+=-])", re.MULTILINE)
_ORDERED = re.compile(r"^([ \t]*\d+)([.)])", re.MULTILINE)


def escape_markdown(text: t.Any) -> str:
    """Escape ``text`` so a Markdown pane shows it verbatim, without HTML or formatting."""
    text = "" if text is None else str(text)
    text = _INLINE.sub(r"\\\1", text)
    text = _BLOCK.sub(r"\1\\\2", text)
    text = _ORDERED.sub(r"\1\\\2", text)
    return html.escape(text, quote=False)


def text_typography(text: t.Any, **params) -> pmui.Typography:
    """Return a ``pmui.Typography`` showing ``text``, a string or a reactive one, as literal text."""
    if isinstance(text, param.rx):
        text = text.rx.pipe(escape_markdown)
    elif isinstance(text, param.Parameter):
        text = param.bind(escape_markdown, text)
    else:
        text = escape_markdown(text)
    return pmui.Typography(text, **params)


def code_block(text: t.Any) -> str:
    """Return ``text`` as a fenced Markdown code block that it cannot close early."""
    text = "" if text is None else str(text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"
