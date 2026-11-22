"""Built-in text components: Markdown, rich text notes, headings and dividers."""

from __future__ import annotations

import decimal
import html
import itertools
import math
import numbers
import re
import secrets
import typing as t
import weakref
from collections.abc import Collection, Mapping
from html.parser import HTMLParser

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.registry import register
from panel_flowdash.ui.node_card import summarize_value
from panel_flowdash.ui.text import escape_markdown, markdown_renderer, text_typography
from panel_flowdash.util import is_failure
from panel_flowdash.value_text import safe_str

_PLACEHOLDER = re.compile(r"\{\{\s*value(?:\.([\w-]+))?\s*(?::([^{}]*?))?\s*\}\}")
# A format spec asking for thousands of characters would make the server build them.
_HUGE_SPEC = re.compile(r"\d{4,}")
_MAX_FILL = 1000
_MAX_ITEMS = 50
_MAX_DEPTH = 3
_MAX_DIGITS = 600
_HUGE_INT = 10**_MAX_DIGITS
# What all the placeholders of one text may add up to.
_MAX_TOTAL = 20 * _MAX_FILL
# Raw HTML is shown as text; math is never typeset since the HTML pane leaves it alone.
# Exports render with plain links; the app's own Markdown opens them in a new tab.
_MARKDOWN = markdown_renderer("gfm-like", new_tab=False)
_APP_MARKDOWN = markdown_renderer("gfm-like")

_TEMPLATE_DOC = """
Placeholders are filled from the connected `value` input: `{{ value }}` shows
the value, `{{ value.key }}` a key of a dict or a pandas Series, and a format
spec after a colon formats a number, e.g. `{{ value:,.0f }}` or
`{{ value.share:.1%}}`."""


def _lookup(value: t.Any, key: str | None) -> t.Any:
    if key is None:
        return value
    if isinstance(value, Mapping) or (hasattr(value, "index") and hasattr(value, "get")):
        try:
            return value.get(key)
        except Exception:
            return None
    return None


def _huge(value: t.Any) -> bool:
    """Whether *value* is a number too large or small to write out in full."""
    if isinstance(value, bool):
        return False
    if isinstance(value, numbers.Integral):
        return abs(value) >= _HUGE_INT
    if isinstance(value, numbers.Rational):
        return abs(value.numerator) >= _HUGE_INT or value.denominator >= _HUGE_INT
    if isinstance(value, decimal.Decimal):
        return value.is_finite() and value != 0 and abs(value.adjusted()) > _MAX_DIGITS
    return False


def _scientific(value: t.Any, spec: str) -> str:
    if isinstance(value, decimal.Decimal):
        if spec and spec[-1] in "eEgG" and not _HUGE_SPEC.search(spec):
            return format(value, spec)
        return format(value, ".6e")
    if isinstance(value, numbers.Integral):
        log = math.log10(abs(value))
    else:
        log = math.log10(abs(value.numerator)) - math.log10(value.denominator)
    exponent = math.floor(log)
    mantissa = f"{10 ** (log - exponent):.6f}"
    if mantissa.startswith("10."):
        mantissa, exponent = f"{1:.6f}", exponent + 1
    return f"{'-' if value < 0 else ''}{mantissa}e{exponent:+d}"


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_FILL else text[: _MAX_FILL - 1] + "\u2026"


def _items(value: t.Any, depth: int, mapping: bool) -> str:
    if depth >= _MAX_DEPTH:
        noun = ("entry", "entries") if mapping else ("item", "items")
        return f"{len(value):,} {noun[len(value) != 1]}"
    parts = []
    for item in itertools.islice(value, _MAX_ITEMS):
        text = _format(item, "", depth + 1)
        if mapping:
            text += f": {_format(value[item], '', depth + 1)}"
        parts.append(_clip(text))
    text = ", ".join(parts)
    if len(value) > _MAX_ITEMS:
        text += f" and {len(value) - _MAX_ITEMS:,} more"
    return text


def _format(value: t.Any, spec: str, depth: int) -> str:
    if value is None or is_failure(value):
        return ""
    if _huge(value):
        return _scientific(value, spec)
    if spec and not _HUGE_SPEC.search(spec):
        try:
            return format(value, spec)
        except (TypeError, ValueError, OverflowError):
            pass
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return f"{value:,}"
    if isinstance(value, numbers.Rational):
        try:
            float(value)
        except OverflowError:
            return _scientific(value, spec)
    if isinstance(value, numbers.Real) and math.isfinite(value):
        return format(value, ",.10g")
    if isinstance(value, (str, bytes, bytearray)):
        return str(value[: _MAX_FILL + 1])
    if getattr(value, "shape", None) is not None:
        return summarize_value(value)
    if isinstance(value, Mapping):
        return _items(value, depth, mapping=True)
    if isinstance(value, Collection):
        return _items(value, depth, mapping=False)
    return safe_str(value)


def format_value(value: t.Any, spec: str = "") -> str:
    """Return *value* as the text a placeholder shows, formatted by *spec* if it applies.

    The text is at most a thousand characters: a collection shows its first
    items, and a number too long to write out is shown in scientific notation.
    A value that cannot be shown gives its type's name.
    """
    try:
        return _clip(_format(value, spec, 0))
    except Exception:
        return type(value).__name__


class _Budget:
    """Cut the placeholders of one text short once together they reach ``_MAX_TOTAL``."""

    def __init__(self):
        self.left = _MAX_TOTAL

    def take(self, text: str) -> str:
        if len(text) > self.left:
            text = text[: self.left] + "\u2026" if self.left else ""
        self.left -= min(len(text), self.left)
        return text


def _value_filler(value: t.Any) -> t.Callable[[re.Match], str]:
    def fill(match: re.Match) -> str:
        key, spec = match.group(1), (match.group(2) or "").strip()
        return format_value(_lookup(value, key), spec)

    return fill


def fill_placeholders(
    template: str,
    pattern: re.Pattern,
    fill: t.Callable[[re.Match], str],
    escape: t.Callable[[str], str] = str,
) -> str:
    """Replace each match of *pattern* in *template* by ``escape(fill(match))``, within the length budget."""
    budget = _Budget()

    def replace(match: re.Match) -> str:
        return escape(budget.take(fill(match)))

    return pattern.sub(replace, template or "")


def fill_template(template: str, value: t.Any, escape: t.Callable[[str], str] = str) -> str:
    """Replace the ``{{ value }}`` placeholders of *template*, passing each filled text through *escape*."""
    return fill_placeholders(template, _PLACEHOLDER, _value_filler(value), escape)


# Private-use characters around a filled value, turned into an element after rendering.
_VALUE_OPEN, _VALUE_CLOSE = "\ue000", "\ue001"


def markdown_with_placeholders(
    template: str,
    pattern: re.Pattern,
    fill: t.Callable[[re.Match], str],
    value_class: str | None = None,
) -> str:
    """Render *template* as HTML, replacing each match of *pattern* by the text ``fill(match)`` gives.

    Placeholders are filled after parsing, so a value is always literal text: it
    cannot add markup, and inside a code span or block it keeps its backslashes.
    A placeholder in a link or image URL is dropped rather than filled, so
    showing the text never sends upstream data to another site. With
    *value_class*, each value in the text is a ``<span>`` of that class.
    """
    fills: list[str] = []
    nonce = secrets.token_hex(4)
    budget = _Budget()
    markers = (_VALUE_OPEN, _VALUE_CLOSE)

    def strip(text: str) -> str:
        return text.replace(_VALUE_OPEN, "").replace(_VALUE_CLOSE, "")

    if value_class is not None:
        template = strip(template or "")

    def mark(match: re.Match) -> str:
        fills.append(strip(budget.take(fill(match))) if value_class else budget.take(fill(match)))
        return f"fdph{nonce}n{len(fills) - 1}e"

    marker = re.compile(f"fdph{nonce}n(\\d+)e")

    def filled(text: str, wrap: bool = False) -> str:
        if wrap and value_class is not None:
            return marker.sub(lambda m: markers[0] + fills[int(m.group(1))] + markers[1], text)
        return marker.sub(lambda m: fills[int(m.group(1))], text)

    def walk(tokens):
        for token in tokens:
            token.content = filled(token.content, wrap=True)
            token.info = marker.sub("", token.info)
            for name, attr in list((token.attrs or {}).items()):
                if isinstance(attr, str):
                    token.attrs[name] = (
                        filled(attr) if name in ("alt", "title") else marker.sub("", attr)
                    )
            if token.children:
                walk(token.children)

    tokens = _APP_MARKDOWN.parse(pattern.sub(mark, template or ""))
    walk(tokens)
    rendered = _APP_MARKDOWN.renderer.render(tokens, _APP_MARKDOWN.options, {})
    if value_class is None:
        return rendered
    return rendered.replace(_VALUE_OPEN, f'<span class="{html.escape(value_class)}">').replace(
        _VALUE_CLOSE, "</span>"
    )


def render_markdown(template: str, value: t.Any) -> str:
    """Render *template* as HTML, filling its ``{{ value }}`` placeholders from *value*.

    See :func:`markdown_with_placeholders` for how values are kept literal.
    """
    return markdown_with_placeholders(template, _PLACEHOLDER, _value_filler(value))


_MARKDOWN_CSS = """
:host {
  overflow: auto;
  font-family: var(--mui-typography-fontFamily, inherit);
  font-size: 14px;
  line-height: 1.5;
  color: var(--mui-palette-text-primary, var(--panel-on-background-color));
}
:host > div {
  box-sizing: border-box;
  padding: 0 12px;
}
:host > div > :first-child {
  margin-top: 0;
}
h1, h2, h3, h4, h5, h6 {
  font-weight: 500;
  line-height: 1.25;
  margin: 0.8em 0 0.4em;
}
h1 { font-size: 1.75em; }
h2 { font-size: 1.4em; }
h3 { font-size: 1.2em; }
h4, h5, h6 { font-size: 1em; }
a {
  color: var(--mui-palette-primary-text, var(--mui-palette-primary-main, var(--panel-primary-color)));
}
table {
  border-collapse: collapse;
  border: none;
}
th, td {
  padding: 4px 12px;
  border: none;
  border-bottom: 1px solid var(--mui-palette-divider, rgba(127, 127, 127, 0.25));
  text-align: left;
}
th {
  font-weight: 500;
}
img {
  max-width: 100%;
}
"""


class MultilineText(param.String):
    """A String edited in a multi-line text area."""

    def schema(self, safe=False, subset=None, mode="json"):
        return self._serializers[mode].param_schema("String", self, safe=safe, subset=subset)


class MultilineInput(pmui.TextAreaInput):
    """The text area a `MultilineText` config param is edited in, growing with its text."""

    def __init__(self, **params):
        params = {"rows": 6, "max_rows": 20, "auto_grow": True, "max_length": 100_000, **params}
        super().__init__(**params)


pn.Param.mapping[MultilineText] = MultilineInput


@register(
    page=False,
    component=True,
    title="Markdown",
    icon="article",
    tags=["view"],
    description=(
        "Text written in Markdown, with headings, emphasis, lists, links, "
        "tables and images; raw HTML is shown as text. Placeholders like {{ value }} "
        "show the value of the optional Value input."
    ),
    config=["text"],
    requires=[{"key": "value", "label": "Value", "required": False, "blocking": False}],
    default_size={"height": 240},
)
class Markdown(Viewer):
    """Markdown text, optionally filled with values from upstream components."""

    text = MultilineText(
        default="Write **Markdown** here.",
        label="Text",
        doc="The Markdown to show." + _TEMPLATE_DOC,
        metadata={"flowdash": {"widget": "code", "language": "markdown"}},
    )

    value = param.Parameter(default=None, doc="The value filled into the text's placeholders.")

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = pn.pane.HTML(
            self._render(),
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[_MARKDOWN_CSS],
        )

    def _render(self) -> str:
        return render_markdown(self.text, self.value)

    @param.depends("text", "value", watch=True)
    def _update(self):
        self._pane.object = self._render()

    def __export__(self, ctx):
        """Return the Markdown, its placeholders filled with literal values, for a report export."""
        from panel_flowdash.export import Text
        from panel_flowdash.export.build import markdown_text

        return Text(fill_template(self.text, self.value, markdown_text))

    def __flowdash_preview__(self) -> str:
        """Return the text of the Markdown with its placeholders filled in, without markup."""
        return html_to_text(self._render())

    def __panel__(self):
        """Return the rendered Markdown."""
        return self._pane


_HEADING_VARIANTS = {"Title": "h3", "Heading": "h4", "Subheading": "h5", "Section": "h6"}


@register(
    page=False,
    component=True,
    title="Heading",
    icon="title",
    tags=["view"],
    description=(
        "A heading with an optional subtitle, for titling a group of tiles. "
        "Placeholders like {{ value }} show the value of the optional Value input."
    ),
    config=["text", "subtitle", "size", "alignment"],
    requires=[{"key": "value", "label": "Value", "required": False, "blocking": False}],
    default_size={"height": 96},
    framed=False,
)
class Heading(Viewer):
    """A heading for a section of the dashboard."""

    text = param.String(default="Heading", label="Text", doc="The heading text." + _TEMPLATE_DOC)

    subtitle = param.String(default="", label="Subtitle", doc="Smaller text below the heading.")

    size = param.Selector(
        default="Heading",
        objects=list(_HEADING_VARIANTS),
        label="Size",
        doc="How large the heading is, from 'Title' (largest) to 'Section'.",
    )

    alignment = param.Selector(
        default="left",
        objects=["left", "center", "right"],
        label="Alignment",
        doc="How the heading and subtitle are aligned in the tile.",
    )

    value = param.Parameter(default=None, doc="The value filled into the text's placeholders.")

    def __init__(self, **params):
        super().__init__(**params)
        self._title = text_typography("", margin=0)
        self._subtitle = text_typography("", variant="body2", color="text.secondary", margin=0)
        self._layout = pn.Column(
            self._title, self._subtitle, sizing_mode="stretch_width", margin=(4, 12)
        )
        self._update()

    @param.depends("text", "subtitle", "size", "alignment", "value", watch=True)
    def _update(self):
        sx = {"textAlign": self.alignment, "width": "100%"}
        self._title.param.update(
            object=escape_markdown(fill_template(self.text, self.value)),
            variant=_HEADING_VARIANTS[self.size],
            sx={**sx, "fontWeight": 500},
        )
        subtitle = fill_template(self.subtitle, self.value)
        self._subtitle.param.update(
            object=escape_markdown(subtitle),
            visible=bool(subtitle.strip()),
            sx=sx,
        )

    def __export__(self, ctx):
        """Return the heading, and its subtitle as text, for a report export."""
        from panel_flowdash.export import Heading as HeadingBlock, Text
        from panel_flowdash.export.build import markdown_text

        level = {"Title": 1, "Heading": 1, "Subheading": 2, "Section": 3}[self.size]
        blocks = [HeadingBlock(fill_template(self.text, self.value), level)]
        subtitle = fill_template(self.subtitle, self.value)
        if subtitle.strip():
            blocks.append(Text(markdown_text(subtitle)))
        return blocks

    def __flowdash_preview__(self) -> str:
        """Return the heading text with its placeholders filled in."""
        return fill_template(self.text, self.value)

    def __panel__(self):
        """Return the heading."""
        return self._layout


@register(
    page=False,
    component=True,
    title="Divider",
    icon="horizontal_rule",
    tags=["view"],
    description=(
        "A horizontal rule separating groups of tiles, with an optional label in the middle."
    ),
    config=["label"],
    default_size={"height": 56},
    framed=False,
)
class Divider(Viewer):
    """A horizontal rule with an optional label."""

    label = param.String(default="", label="Label", doc="Text shown in the middle of the rule.")

    def __init__(self, **params):
        super().__init__(**params)
        self._label = text_typography(self.label, variant="body2", color="text.secondary")
        self._divider = pmui.Divider(
            objects=[self._label] if self.label else [],
            sizing_mode="stretch_width",
            margin=(12, 0),
        )

    @param.depends("label", watch=True)
    def _update(self):
        self._label.object = escape_markdown(self.label)
        self._divider.objects = [self._label] if self.label else []

    def __export__(self, ctx):
        """Return the label as a heading of a report export; a rule without one exports nothing."""
        from panel_flowdash.export import Heading as HeadingBlock

        return [HeadingBlock(self.label, 3)] if self.label.strip() else []

    def __panel__(self):
        """Return the rule."""
        return self._divider


# Elements whose content is never text to show.
_HIDDEN = frozenset({"script", "style", "template", "iframe", "object", "noscript", "svg", "math"})


class _TextExtractor(HTMLParser):
    _BLOCKS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self._hidden += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Return the text of *markup* with tags dropped and one line per block."""
    parser = _TextExtractor()
    parser.feed(markup or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


_NOTES_TAGS = frozenset(
    {
        "p",
        "br",
        "h1",
        "h2",
        "h3",
        "strong",
        "em",
        "u",
        "s",
        "ol",
        "ul",
        "li",
        "a",
        "blockquote",
        "code",
    }
)
_NOTES_CLASS = re.compile(r"^ql-(indent-[1-8]|align-(center|right|justify))$")
_NOTES_LIST = frozenset({"bullet", "ordered"})
_SAFE_HREF = re.compile(r"^(https?:|mailto:)", re.IGNORECASE)


class _NotesSanitizer(HTMLParser):
    """Rebuild notes HTML from the formats the Notes toolbar offers, dropping everything else."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[str | None] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self._hidden += 1
            return
        if self._hidden or tag not in _NOTES_TAGS:
            return
        kept = []
        for name, value in attrs:
            value = value or ""
            if name == "class":
                classes = [c for c in value.split() if _NOTES_CLASS.match(c)]
                if classes:
                    kept.append(("class", " ".join(classes)))
            elif name == "data-list" and tag == "li" and value in _NOTES_LIST:
                kept.append((name, value))
            elif name == "href" and tag == "a" and _SAFE_HREF.match(value.strip()):
                kept += [
                    ("href", value.strip()),
                    ("rel", "noopener noreferrer"),
                    ("target", "_blank"),
                ]
        rendered = "".join(f' {name}="{html.escape(value)}"' for name, value in kept)
        self.parts.append(f"<{tag}{rendered}>")
        if tag != "br":
            self._open.append(tag)

    def handle_endtag(self, tag):
        if tag in _HIDDEN:
            self._hidden = max(0, self._hidden - 1)
        elif not self._hidden and tag in self._open:
            while self._open:
                closed = self._open.pop()
                self.parts.append(f"</{closed}>")
                if closed == tag:
                    break

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        self.parts += [f"</{tag}>" for tag in reversed(self._open)]
        self._open = []


def sanitize_notes(markup: str) -> str:
    """Return *markup* reduced to the elements, links and classes the Notes editor produces."""
    sanitizer = _NotesSanitizer()
    sanitizer.feed(markup or "")
    sanitizer.close()
    return "".join(sanitizer.parts)


_NOTES_LINES = frozenset({"p", "h1", "h2", "h3", "li", "blockquote"})
_NOTES_INLINE = frozenset({"strong", "em", "u", "s", "code"})
_WHITESPACE = re.compile(r"[ \n\r\f]+")
_INDENT = re.compile(r"^ql-indent-(\d)$")


class _NotesLines(HTMLParser):
    """Split sanitized notes into the lines the editor shows, with their formats.

    Quill rewrites the HTML it is given into its own form: it joins blocks
    without whitespace, collapses spaces, splits a paragraph at each line break,
    drops trailing empty paragraphs, nests indented list items and wraps
    loose text in a paragraph. Two
    markups that give the same lines show the same notes.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[tuple] = []
        self._blocks: list[tuple[str, tuple]] = []
        self._inline: list[tuple] = []
        self._runs: list[tuple] = []
        self._started = False
        self._break = False

    def _kind(self) -> tuple:
        depth, kind, classes = 0, "p", []
        for tag, attrs in self._blocks:
            if tag in ("ol", "ul"):
                depth += 1
                kind = ("li", "bullet" if tag == "ul" else "ordered", depth)
            elif tag == "li" and not depth:
                pass
            elif tag == "li":
                own = " ".join(value for name, value in attrs if name == "class").split()
                indent = sum(int(m.group(1)) for c in own if (m := _INDENT.match(c)))
                listed = dict(attrs).get("data-list") or kind[1]
                kind = ("li", listed, depth + indent)
                classes += [c for c in own if not _INDENT.match(c)]
                continue
            elif kind == "p" and tag in _NOTES_LINES:
                kind = tag
            classes += [value for name, value in attrs if name == "class"]
        return (kind, tuple(sorted(" ".join(classes).split())))

    def _flush(self, explicit: bool = False):
        runs: list[list] = []
        for formats, text in self._runs:
            text = _WHITESPACE.sub(" ", text)
            if runs and runs[-1][0] == formats:
                runs[-1][1] += text
            else:
                runs.append([formats, text])
        text = "".join(text for _, text in runs)
        if text.strip(" ") or explicit or self._started:
            merged, previous = [], " "
            for formats, chunk in runs:
                if previous.endswith(" "):
                    chunk = chunk.lstrip(" ")
                if not merged:
                    chunk = chunk.lstrip(" \t")
                if chunk:
                    merged.append((formats, chunk))
                    previous = chunk
            if merged:
                formats, chunk = merged[-1]
                merged[-1] = (formats, chunk.rstrip(" \t"))
                if not merged[-1][1]:
                    merged.pop()
            self.lines.append((self._kind(), tuple(merged)))
        self._runs, self._started, self._break = [], False, False

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            if self._break:
                self._flush(explicit=True)
            self._break = True
            self._started = True
        elif tag in _NOTES_INLINE or tag == "a":
            self._inline.append((tag, dict(attrs).get("href")))
        else:
            if self._runs or self._break:
                self._flush()
            self._blocks.append((tag, tuple(attrs)))
            self._started = tag in _NOTES_LINES

    def handle_endtag(self, tag):
        if tag in _NOTES_INLINE or tag == "a":
            for index in range(len(self._inline) - 1, -1, -1):
                if self._inline[index][0] == tag:
                    del self._inline[index]
                    break
        elif self._blocks and self._blocks[-1][0] == tag:
            if self._runs or self._started:
                self._flush(explicit=True)
            self._blocks.pop()

    def handle_data(self, data):
        if self._break:
            self._flush(explicit=True)
            self._started = True
        self._runs.append((frozenset(self._inline), data))

    def close(self):
        super().close()
        if self._runs or self._started:
            self._flush()
        while self.lines and self.lines[-1] == (("p", ()), ()):
            self.lines.pop()


def notes_lines(markup: str) -> list[tuple]:
    """Return the lines *markup* shows in the Notes editor, to compare notes regardless of form."""
    parser = _NotesLines()
    parser.feed(sanitize_notes(markup))
    parser.close()
    return parser.lines


def notes_markdown(markup: str) -> str:
    """Return notes as Markdown: headings, lists, quotes, bold, italic, code and links."""
    from panel_flowdash.export.build import markdown_text

    lines = []
    for (kind, _classes), runs in notes_lines(markup):
        parts = []
        for formats, raw in runs:
            core = raw.strip(" ")
            if not core:
                parts.append(raw)
                continue
            text = markdown_text(core)
            tags = dict(formats)
            if "code" in tags:
                text = f"`{text}`"
            if "em" in tags:
                text = f"*{text}*"
            if "strong" in tags:
                text = f"**{text}**"
            if tags.get("a"):
                text = f"[{text}](<{tags['a'].replace('>', '%3E')}>)"
            lead, trail = raw[: len(raw) - len(raw.lstrip(" "))], raw[len(raw.rstrip(" ")) :]
            parts.append(lead + text + trail)
        text = "".join(parts)
        if isinstance(kind, tuple):
            _, listed, depth = kind
            marker = "1." if listed == "ordered" else "-"
            lines.append("   " * max(depth - 1, 0) + f"{marker} {text}")
        elif kind in ("h1", "h2", "h3"):
            lines.append(f"\n{'#' * (int(kind[1]) + 1)} {text}\n")
        elif kind == "blockquote":
            lines.append(f"> {text}\n")
        else:
            lines.append(f"{text}\n")
    return "\n".join(lines).strip()


_NOTES_CSS = """
:host {
  --fd-notes-text: var(--mui-palette-text-primary, var(--panel-on-background-color));
  --fd-notes-muted: var(--mui-palette-text-secondary, rgba(127, 127, 127, 0.9));
  --fd-notes-divider: var(--mui-palette-divider, rgba(127, 127, 127, 0.25));
  --fd-notes-primary: var(--mui-palette-primary-text, var(--mui-palette-primary-main, var(--panel-primary-color)));
  --fd-notes-paper: var(--mui-palette-background-paper, var(--panel-background-color));
}
.ql-container.ql-bubble,
.ql-container.ql-snow {
  border: none;
  font-family: var(--mui-typography-fontFamily, inherit);
  font-size: 14px;
  color: var(--fd-notes-text);
}
.ql-editor {
  padding: 8px 12px;
  line-height: 1.5;
}
.ql-editor h1, .ql-editor h2, .ql-editor h3 {
  font-weight: 500;
  margin: 0.4em 0 0.2em;
}
.ql-editor a {
  color: var(--fd-notes-primary);
}
.ql-editor ol {
  padding-left: 0;
}
.ql-editor > :first-child {
  margin-top: 0;
}
.ql-editor.ql-blank::before {
  left: 12px;
  right: 12px;
  color: var(--fd-notes-muted);
  font-style: normal;
}
.ql-bubble .ql-tooltip {
  z-index: 10;
  border-radius: 6px;
  background: var(--mui-palette-grey-900, #212121);
  box-shadow: var(--mui-shadows-4, 0 2px 8px rgba(0, 0, 0, 0.3));
}
.ql-bubble .ql-tooltip:not(.ql-flip) .ql-tooltip-arrow {
  border-bottom-color: var(--mui-palette-grey-900, #212121);
}
.ql-bubble .ql-tooltip.ql-flip .ql-tooltip-arrow {
  border-top-color: var(--mui-palette-grey-900, #212121);
}
:host(.fd-notes-editing) .ql-container:focus-within {
  border-radius: 6px;
  outline: 1px solid var(--fd-notes-primary);
  outline-offset: -2px;
}
"""

_NOTES_TOOLBAR = [
    [{"header": [1, 2, 3, False]}],
    ["bold", "italic", "underline", "strike"],
    [{"list": "ordered"}, {"list": "bullet"}],
    ["link", "blockquote", "code"],
    ["clean"],
]


@register(
    page=False,
    component=True,
    title="Notes",
    icon="sticky_note_2",
    tags=["view"],
    description=(
        "Rich text notes typed into the tile while the dashboard is being changed, "
        "with bold, lists, links and headings from a toolbar over selected text. "
        "Read-only in view mode."
    ),
    config=["text", "placeholder"],
    extensions=["texteditor"],
    default_size={"height": 220},
)
class Notes(Viewer):
    """Rich text notes, edited in place while the dashboard is editable."""

    text = MultilineText(
        default="",
        label="Text",
        doc="""
            The notes as HTML, typed into the tile itself rather than edited in
            the inspector. Only p, br, h1 to h3, strong, em, u, s, ol/ul/li,
            blockquote, inline code and http(s) or mailto links are kept;
            other elements, images, styles and attributes are dropped.""",
        precedence=-1,
        metadata={"flowdash": {"two_way": True}},
    )

    placeholder = param.String(
        default="Write notes here",
        label="Placeholder",
        doc="Shown while the notes are empty and the dashboard is being edited.",
    )

    def __init__(self, **params):
        super().__init__(**params)
        self._editor = pn.widgets.TextEditor(
            value=sanitize_notes(self.text),
            mode="bubble",
            toolbar=_NOTES_TOOLBAR,
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[_NOTES_CSS],
        )
        self._editor.param.watch(self._edited, "value")
        from panel_flowdash.editor import current_dashboard

        dashboard = current_dashboard()
        # Weak, so notes kept by the app do not keep a discarded editor alive.
        self._dashboard = None if dashboard is None else weakref.ref(dashboard)
        if dashboard is not None:
            dashboard.param.watch(self._sync_editable, ["editable", "preview", "read_only"])
        self._sync_editable()

    @property
    def editing(self) -> bool:
        """Whether the notes can be edited, as they can while the dashboard is and is not previewed."""
        if self._dashboard is None:
            return True
        dashboard = self._dashboard()
        return (
            dashboard is not None
            and dashboard.editable
            and not dashboard.read_only
            and not dashboard.preview
        )

    def _sync_editable(self, *events):
        editing = self.editing
        self._editor.param.update(
            disabled=not editing,
            placeholder=self.placeholder if editing else "",
            css_classes=["fd-notes-editing"] if editing else [],
        )

    def _edited(self, event):
        # The editor sends back its own form of the notes it is shown, which is no edit.
        if self.editing and notes_lines(event.new) != notes_lines(self.text):
            # The editor keeps showing what was typed; only what is saved is sanitized.
            self._typing = True
            try:
                self.text = sanitize_notes(event.new)
            finally:
                self._typing = False

    @param.depends("text", watch=True)
    def _update_text(self):
        if getattr(self, "_typing", False):
            return
        markup = sanitize_notes(self.text)
        if self._editor.value != markup:
            self._editor.value = markup

    @param.depends("placeholder", watch=True)
    def _update_placeholder(self):
        self._sync_editable()

    def __export__(self, ctx):
        """Return the notes as Markdown for a report export."""
        from panel_flowdash.export import Text

        return Text(notes_markdown(self.text))

    def __flowdash_preview__(self) -> str:
        """Return the text of the notes, without formatting."""
        return html_to_text(self.text)

    def __panel__(self):
        """Return the notes editor."""
        return self._editor


TEXT_COMPONENTS = {
    "Text/Markdown": Markdown,
    "Text/Notes": Notes,
    "Text/Heading": Heading,
    "Text/Divider": Divider,
}
