"""Built-in text components: Markdown, rich text notes, headings and dividers."""

from __future__ import annotations

import math
import numbers
import re
import typing as t
from collections.abc import Mapping
from html.parser import HTMLParser

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.registry import register
from panel_flowdash.ui.node_card import summarize_value
from panel_flowdash.ui.text import escape_markdown, markdown_pane, text_typography
from panel_flowdash.util import is_failure

_PLACEHOLDER = re.compile(r"\{\{\s*value(?:\.([\w-]+))?\s*(?::([^{}]*?))?\s*\}\}")
# Every ASCII punctuation character can be backslash-escaped in CommonMark, which
# keeps a substituted value from forming emphasis, links, lists or headings.
_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")

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


def format_value(value: t.Any, spec: str = "") -> str:
    """Return *value* as the text a placeholder shows, formatted by *spec* if it applies."""
    if value is None or is_failure(value):
        return ""
    if spec:
        try:
            return format(value, spec)
        except (TypeError, ValueError):
            pass
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, numbers.Integral):
        return f"{value:,}"
    if isinstance(value, numbers.Real) and math.isfinite(value):
        return format(value, ",.10g")
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(format_value(item) for item in value)
    if getattr(value, "shape", None) is not None:
        return summarize_value(value)
    return str(value)


def fill_template(template: str, value: t.Any, escape: t.Callable[[str], str] = str) -> str:
    """Replace the ``{{ value }}`` placeholders of *template*, passing each filled text through *escape*."""

    def replace(match: re.Match) -> str:
        key, spec = match.group(1), (match.group(2) or "").strip()
        return escape(format_value(_lookup(value, key), spec))

    return _PLACEHOLDER.sub(replace, template or "")


def escape_inline_markdown(text: str) -> str:
    """Escape *text* for a Markdown pane that renders raw HTML as text, keeping it on one line."""
    return _PUNCTUATION.sub(r"\\\1", " ".join(text.split()))


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
  color: var(--mui-palette-primary-main, var(--panel-primary-color));
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
        default="## Notes\n\nWrite **Markdown** here.",
        label="Text",
        doc="The Markdown to show." + _TEMPLATE_DOC,
    )

    value = param.Parameter(default=None, doc="The value filled into the text's placeholders.")

    def __init__(self, **params):
        super().__init__(**params)
        self._pane = markdown_pane(
            self._render(),
            renderer_options={"html": False},
            disable_anchors=True,
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[_MARKDOWN_CSS],
        )

    def _render(self) -> str:
        return fill_template(self.text, self.value, escape_inline_markdown)

    @param.depends("text", "value", watch=True)
    def _update(self):
        self._pane.object = self._render()

    def __flowdash_preview__(self) -> str:
        """Return the Markdown text with its placeholders filled in."""
        return self._render()

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
        default="left", objects=["left", "center", "right"], label="Alignment"
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
        self._subtitle.param.update(
            object=escape_markdown(fill_template(self.subtitle, self.value)),
            visible=bool(self.subtitle),
            sx=sx,
        )

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
            margin=(12, 12),
        )

    @param.depends("label", watch=True)
    def _update(self):
        self._label.object = escape_markdown(self.label)
        self._divider.objects = [self._label] if self.label else []

    def __panel__(self):
        """Return the rule."""
        return self._divider


class _TextExtractor(HTMLParser):
    _BLOCKS = frozenset({"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Return the text of *markup* with tags dropped and one line per block."""
    parser = _TextExtractor()
    parser.feed(markup or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


_NOTES_CSS = """
:host {
  --fd-notes-text: var(--mui-palette-text-primary, var(--panel-on-background-color));
  --fd-notes-muted: var(--mui-palette-text-secondary, rgba(127, 127, 127, 0.9));
  --fd-notes-divider: var(--mui-palette-divider, rgba(127, 127, 127, 0.25));
  --fd-notes-primary: var(--mui-palette-primary-main, var(--panel-primary-color));
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
            the inspector. Only paragraphs, headings (h1 to h3), strong, em, u,
            s, ol/ul/li lists, links, blockquote and inline code are kept.""",
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
            value=self.text,
            mode="bubble",
            toolbar=_NOTES_TOOLBAR,
            sizing_mode="stretch_both",
            margin=0,
            stylesheets=[_NOTES_CSS],
        )
        self._editor.param.watch(self._edited, "value")
        from panel_flowdash.editor import current_dashboard

        dashboard = current_dashboard()
        self._dashboard = dashboard
        if dashboard is not None:
            dashboard.param.watch(self._sync_editable, ["editable", "read_only"])
        self._sync_editable()

    @property
    def editing(self) -> bool:
        """Whether the notes can be edited, as they can while the dashboard is."""
        dashboard = self._dashboard
        return dashboard is None or (dashboard.editable and not dashboard.read_only)

    def _sync_editable(self, *events):
        editing = self.editing
        self._editor.param.update(
            disabled=not editing,
            placeholder=self.placeholder if editing else "",
            css_classes=["fd-notes-editing"] if editing else [],
        )

    def _edited(self, event):
        if self.editing:
            self.text = event.new

    @param.depends("text", watch=True)
    def _update_text(self):
        if self._editor.value != self.text:
            self._editor.value = self.text

    @param.depends("placeholder", watch=True)
    def _update_placeholder(self):
        self._sync_editable()

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
