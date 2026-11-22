"""The Report view: the dashboard read as a document, its blocks rendered live from the graph.

The view shows :func:`~panel_flowdash.report.report_blocks`: the dashboard's
own report, or the one derived from its groups and tile layout. The
dashboard's controls sit in a bar above a centred column of blocks, with a
list of contents beside it on wide screens. A node block shows the view
object the node's tile shows, so the Report view, the Explore view and the
workspace are never on screen together (see ``FlowDash.surface``). Text
blocks fill their ``{{ node.port }}`` placeholders from the current outputs
and follow them as they change. The view is read-only: authoring reports and
running pending turns come later.
"""

from __future__ import annotations

import html
import logging
import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewable, Viewer

from panel_flowdash.port_types import port_types_compatible
from panel_flowdash.report import (
    PLACEHOLDER,
    block_kind,
    group_members,
    placeholder_text,
    render_report_text,
    report_blocks,
)
from panel_flowdash.ui import tile_layout
from panel_flowdash.ui.controls_bar import _FIT_CONTENT, _row_width
from panel_flowdash.ui.text import escape_markdown, text_typography

if t.TYPE_CHECKING:
    from panel_flowdash.turns import DashboardTurn

logger = logging.getLogger(__name__)

#: The width of the document column, in pixels.
COLUMN_WIDTH = 780

#: The width of the contents list beside the column, shown on wide windows only.
CONTENTS_WIDTH = 220

#: Below this window width the contents list is hidden; below the phone width it becomes a chip.
WIDE_BREAKPOINT = 1280
PHONE_BREAKPOINT = 600

#: The height of a chart, table or data block whose tile gives none.
BLOCK_HEIGHT = 320

DERIVED_NOTICE = "Built from this dashboard's groups and tile layout."
CUSTOMIZE_HINT = "Customize it to reorder, write prose or ask questions."
PENDING_NOTE = "Not answered yet. Running it asks the assistant to build the answer here."
EMPTY_TEXT = "This dashboard has nothing to show in a report yet."
RESTRICTED_TEXT = "This output uses a component you may not see."

VALUE_CLASS = "fd-report-value"

_COALESCE_MS = 50

_PHONE = f"(max-width: {PHONE_BREAKPOINT - 0.05}px)"
_NARROW = f"(max-width: {WIDE_BREAKPOINT - 0.05}px)"
_HIDE_ON_PHONE = f"@media {_PHONE} {{ :host {{ display: none !important; }} }}"
_PHONE_ONLY = f"@media not {_PHONE} {{ :host {{ display: none !important; }} }}"
_HIDE_WHEN_NARROW = f"@media {_NARROW} {{ :host {{ display: none !important; }} }}"

# A view that stretches makes its frame stretch too, to the height of the section
# it is in; blocks, sections and frames take the height of their content instead.
_HOST_FITS_CONTENT = ":host { height: auto !important; flex: 0 0 auto !important; }"

_LAYOUT_CSS = """
:host {
  background: var(--mui-palette-background-default, #fafafa);
}
"""

# The controls stay in view while the document scrolls under them. On a phone
# they fold away behind the Filters chip.
_CONTROLS_CSS = f"""
:host {{
  position: sticky;
  top: 0;
  z-index: 3;
  background: var(--mui-palette-background-paper, #fff);
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  box-sizing: border-box;
}}
@media {_PHONE} {{
  :host(:not(.fd-report-filters-open)) {{ display: none !important; }}
}}
"""

_PHONE_ROW_CSS = f"""
:host {{
  border-bottom: 1px solid var(--mui-palette-divider, rgba(0, 0, 0, 0.12));
  background: var(--mui-palette-background-paper, #fff);
  overflow-x: auto;
}}
{_PHONE_ONLY}
"""

_BODY_CSS = f"""
:host {{
  box-sizing: border-box;
  padding: 32px 32px 64px;
}}
@media {_PHONE} {{
  :host {{ padding: 0 0 40px; }}
}}
"""

_ARTICLE_CSS = f"""
:host {{
  flex: 1 1 auto;
  min-width: 0;
  max-width: {COLUMN_WIDTH}px;
}}
@media {_PHONE} {{
  :host {{ max-width: 100%; }}
}}
"""

_SIDE_CSS = f"""
:host {{ flex: 0 0 {CONTENTS_WIDTH}px; }}
{_HIDE_WHEN_NARROW}
"""

# Prose: the body text of a document, with live values as small tinted chips.
_PROSE_CSS = f"""
:host {{
  font-family: var(--mui-typography-fontFamily, Roboto, sans-serif);
  font-size: 16px;
  line-height: 1.7;
  letter-spacing: 0.15px;
  color: var(--mui-palette-text-primary, #212121);
}}
:host > div > :first-child {{ margin-top: 0; }}
:host > div > :last-child {{ margin-bottom: 0; }}
p {{ margin: 0 0 0.75em; }}
a {{ color: var(--mui-palette-primary-main, #0072b5); }}
h1, h2, h3, h4, h5, h6 {{ font-weight: 500; line-height: 1.3; margin: 1em 0 0.4em; }}
code {{ font-family: "Roboto Mono", monospace; font-size: 0.9em; }}
/* The primary colour drawn towards the text colour: darker on light paper, lighter on dark,
   so the value keeps at least 4.5:1 against its tint in both themes. */
.{VALUE_CLASS} {{
  background: rgba(var(--mui-palette-primary-mainChannel, 0 114 181) / 0.12);
  color: color-mix(
    in srgb,
    var(--mui-palette-primary-main, #0072b5) 70%,
    var(--mui-palette-text-primary, #212121)
  );
  border-radius: 3px;
  padding: 1px 5px;
  font-weight: 500;
  white-space: nowrap;
}}
"""

_META_CSS = """
:host {
  font-family: var(--mui-typography-fontFamily, Roboto, sans-serif);
  font-size: 13px;
  color: var(--mui-palette-text-secondary, #666);
}
.fd-report-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
.fd-report-live {
  display: inline-flex; align-items: center; gap: 6px; font-weight: 500;
  color: var(--mui-palette-success-main, #2e7d32);
}
.fd-report-live::before {
  content: ""; width: 8px; height: 8px; border-radius: 50%;
  background: var(--mui-palette-success-main, #2e7d32);
}
"""

_ICON_CSS = """
:host { flex: 0 0 auto; }
.fd-report-icon {
  font-family: "Material Icons";
  font-size: 22px;
  line-height: 24px;
  display: block;
}
"""


class ReportContents(JSComponent):
    """The report's contents: a list of its sections, or on phones a chip opening that list.

    A click scrolls the section's heading into view; the list marks the
    section being read. Headings are found by their ``fd-report-anchor-<key>``
    CSS class through the page's shadow roots.
    """

    items = param.List(default=[], doc="The sections, each a dict with label, key and badge.")

    variant = param.Selector(default="list", objects=["list", "chip"])

    _esm = """
    function find(root, selector) {
      const found = root.querySelector(selector)
      if (found) { return found }
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) {
          const inner = find(el.shadowRoot, selector)
          if (inner) { return inner }
        }
      }
      return null
    }

    function anchor(key) { return find(document, `.fd-report-anchor-${key}`) }

    export function render({ model, el }) {
      const root = document.createElement("nav")
      root.setAttribute("aria-label", "Contents")
      root.className = `fd-contents fd-contents-${model.variant}`
      el.appendChild(root)
      let links = []
      let open = false

      const go = (key) => {
        const target = anchor(key)
        if (target) { target.scrollIntoView({ behavior: "smooth", block: "start" }) }
        if (model.variant === "chip") { toggle(false) }
      }

      const list = document.createElement("div")
      list.className = "fd-contents-list"
      const toggle = (value) => {
        open = value
        list.hidden = !open
        if (chip) { chip.setAttribute("aria-expanded", String(open)) }
      }
      let chip = null
      if (model.variant === "chip") {
        chip = document.createElement("button")
        chip.type = "button"
        chip.className = "fd-contents-chip"
        chip.textContent = "Contents"
        chip.setAttribute("aria-haspopup", "true")
        chip.addEventListener("click", () => toggle(!open))
        root.appendChild(chip)
        toggle(false)
      } else {
        const label = document.createElement("span")
        label.className = "fd-contents-label"
        label.textContent = "Contents"
        root.appendChild(label)
      }
      root.appendChild(list)

      const draw = () => {
        list.replaceChildren()
        links = model.items.map((item, i) => {
          const link = document.createElement("a")
          link.href = "#"
          link.className = "fd-contents-item" + (i === 0 ? " fd-active" : "")
          link.textContent = item.label
          if (item.badge) {
            const badge = document.createElement("span")
            badge.className = "fd-contents-badge"
            badge.textContent = item.badge
            link.appendChild(badge)
          }
          link.addEventListener("click", (event) => { event.preventDefault(); go(item.key) })
          list.appendChild(link)
          return link
        })
        root.hidden = !model.items.length
      }
      draw()
      model.on("items", () => { anchors = null; draw() })

      let frame = null
      // The headings, found once per set of items, and again only while one is missing
      // from the page, e.g. before it has rendered or after a rebuild.
      let anchors = null
      const follow = () => {
        frame = null
        if (model.variant !== "list" || !links.length || !el.isConnected) { return }
        if (anchors === null || anchors.some((a) => a === null || !a.isConnected)) {
          anchors = model.items.map((item) => anchor(item.key))
        }
        let active = 0
        anchors.forEach((target, i) => {
          if (target && target.getBoundingClientRect().top < 160) { active = i }
        })
        links.forEach((link, i) => link.classList.toggle("fd-active", i === active))
      }
      const onScroll = () => { if (frame === null) { frame = requestAnimationFrame(follow) } }
      document.addEventListener("scroll", onScroll, true)
      model.on("remove", () => document.removeEventListener("scroll", onScroll, true))
    }
    """

    _stylesheets: t.ClassVar[list[str]] = [
        """
    :host { font-family: var(--mui-typography-fontFamily, Roboto, sans-serif); }
    .fd-contents-list { display: flex; flex-direction: column; font-size: 14px; }
    .fd-contents-label {
      display: block; font-size: 12px; padding: 0 16px 8px;
      color: var(--mui-palette-text-secondary, #666);
    }
    .fd-contents-item {
      display: flex; align-items: center; gap: 8px;
      padding: 8px 16px 8px 13px; border-left: 3px solid transparent;
      color: var(--mui-palette-text-secondary, #424242); text-decoration: none;
      line-height: 1.4;
    }
    .fd-contents-item:hover { background: var(--mui-palette-action-hover, rgba(0, 0, 0, 0.04)); }
    .fd-contents-item:focus-visible { outline: 2px solid var(--mui-palette-primary-main, #0072b5); }
    .fd-contents-list .fd-active {
      border-left-color: var(--mui-palette-primary-main, #0072b5);
      background: rgba(var(--mui-palette-primary-mainChannel, 0 114 181) / 0.1);
      color: var(--mui-palette-text-primary, #212121); font-weight: 500;
    }
    .fd-contents-badge {
      font-size: 11px; font-weight: 400; border-radius: 10px; padding: 0 7px; white-space: nowrap;
      color: var(--mui-palette-Alert-warningColor, #663c00);
      background: var(--mui-palette-Alert-warningStandardBg, #fff4e5);
      border: 1px solid rgba(var(--mui-palette-warning-mainChannel, 237 108 2) / 0.4);
    }
    .fd-contents-chip .fd-contents-list {
      position: absolute; z-index: 10; margin-top: 6px; min-width: 240px; max-width: calc(100vw - 32px);
      background: var(--mui-palette-background-paper, #fff); border-radius: 4px; padding: 4px 0;
      box-shadow: var(--mui-shadows-8, 0 5px 5px -3px rgba(0,0,0,.2), 0 8px 10px 1px rgba(0,0,0,.14));
    }
    .fd-contents-chip .fd-contents-item { border-left: 0; padding: 10px 16px; }
    .fd-contents-chip .fd-contents-list[hidden] { display: none; }
    .fd-contents-chip > button {
      height: 32px; padding: 0 12px; border-radius: 16px; white-space: nowrap; cursor: pointer;
      font: 400 13px var(--mui-typography-fontFamily, Roboto, sans-serif);
      color: var(--mui-palette-text-primary, #212121); background: transparent;
      border: 1px solid var(--mui-palette-divider, #c4c4c4);
    }
    """
    ]


def _icon(name: str, color: str) -> pn.pane.HTML:
    return pn.pane.HTML(
        f'<span class="fd-report-icon" aria-hidden="true" style="color: {color}">{name}</span>',
        margin=(2, 0, 0, 0),
        stylesheets=[_ICON_CSS],
    )


class ReportView(Viewer):
    """The Report view of a :class:`~panel_flowdash.editor.FlowDash` editor."""

    editor = param.Parameter(doc="The FlowDash editor whose dashboard the report shows.")

    shown = param.Boolean(
        default=False,
        doc="""
        Whether the view is on screen. While hidden it neither follows the
        outputs its text shows nor rebuilds, and catches up when shown.""",
    )

    def __init__(self, **params):
        super().__init__(**params)
        editor = self.editor
        self._pending = False
        # The wrappers of node views, kept while they show the same view, so a rebuild
        # does not render them again.
        self._wrappers: dict[tuple, Viewable] = {}
        self._control_wrappers: dict[str, pn.Column] = {}
        # Text blocks and the filters line, refilled when an output they read changes.
        self._texts: list[tuple[pn.pane.HTML, str]] = []
        self._watchers: list[tuple[t.Any, t.Any]] = []
        self._filters: list[str] = []

        self._controls = pn.FlexBox(
            align_items="center",
            flex_wrap="wrap",
            gap="0px",
            margin=0,
            sizing_mode="stretch_width",
        )
        self._controls_hint = pmui.Typography(
            "The dashboard's controls. Changing one updates the report.",
            variant="caption",
            margin=(0, 8),
            align="center",
            sx={"color": "text.secondary"},
            stylesheets=[_HIDE_WHEN_NARROW],
        )
        self._controls_row = pn.Row(
            self._controls,
            self._controls_hint,
            margin=0,
            sizing_mode="stretch_width",
            styles={"padding": "8px 24px 4px 22px"},
            css_classes=["fd-report-controls"],
            stylesheets=[_CONTROLS_CSS],
        )
        self._filters_chip = pmui.Button(
            label="Filters",
            icon="tune",
            variant="outlined",
            size="small",
            margin=0,
            sx={"borderRadius": "16px", "textTransform": "none", "whiteSpace": "nowrap"},
            css_classes=["fd-report-filters-chip"],
        )
        self._filters_chip.on_click(self._toggle_filters)
        self._contents = ReportContents(variant="list", margin=0, sizing_mode="stretch_width")
        self._contents_chip = ReportContents(variant="chip", margin=0)
        self._phone_row = pn.Row(
            self._filters_chip,
            self._contents_chip,
            margin=0,
            sizing_mode="stretch_width",
            styles={"padding": "12px 16px", "gap": "8px"},
            css_classes=["fd-report-phone-row"],
            stylesheets=[_PHONE_ROW_CSS],
        )

        self._meta = pn.pane.HTML(
            "", margin=0, sizing_mode="stretch_width", stylesheets=[_META_CSS]
        )
        self._title = pmui.Typography(
            "",
            variant="h4",
            margin=0,
            sx={"fontWeight": 400, "fontSize": {"xs": "1.75rem", "sm": "2.125rem"}},
            css_classes=["fd-report-title"],
        )
        self._customize = pmui.Button(
            label="Customize",
            variant="text",
            size="small",
            disabled=True,
            margin=0,
            align="center",
            css_classes=["fd-report-customize"],
        )
        self._notice_text = pmui.Typography(
            "",
            variant="body2",
            margin=0,
            sx={"color": "inherit"},
            styles={"flex": "1 1 auto", "min-width": "0"},
        )
        self._notice = pmui.Paper(
            pn.FlexBox(
                _icon("info", "var(--mui-palette-info-main, #0288d1)"),
                self._notice_text,
                self._customize,
                flex_wrap="nowrap",
                align_items="center",
                gap="12px",
                margin=0,
                sizing_mode="stretch_width",
            ),
            elevation=0,
            margin=0,
            sizing_mode="stretch_width",
            sx={
                "bgcolor": "var(--mui-palette-Alert-infoStandardBg, #e5f2fa)",
                "color": "var(--mui-palette-Alert-infoColor, #01405f)",
                "py": 0.75,
                "pl": 2,
                "pr": 1,
            },
            css_classes=["fd-report-derived"],
        )
        self._blocks = pn.Column(
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": "28px"},
            stylesheets=[_FIT_CONTENT],
        )
        self._article = pmui.Paper(
            pn.Column(
                self._meta,
                self._title,
                self._notice,
                margin=0,
                sizing_mode="stretch_width",
                styles={"gap": "12px"},
            ),
            self._blocks,
            variant="outlined",
            margin=0,
            sizing_mode="stretch_width",
            sx={
                "px": {"xs": 2, "sm": 7},
                "pt": {"xs": 2.5, "sm": 5.5},
                "pb": {"xs": 4, "sm": 7},
                "gap": "28px",
                "borderRadius": {"xs": 0, "sm": 1},
                "border": {"xs": 0, "sm": 1},
                "borderColor": {"sm": "divider"},
            },
            css_classes=["fd-report-article"],
            stylesheets=[_ARTICLE_CSS],
        )
        self._body = pn.FlexBox(
            pn.Column(self._contents, margin=0, stylesheets=[_SIDE_CSS]),
            self._article,
            pn.Spacer(margin=0, stylesheets=[_SIDE_CSS]),
            flex_wrap="nowrap",
            justify_content="center",
            align_items="flex-start",
            gap="32px",
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_BODY_CSS],
        )
        self._layout = pn.Column(
            self._controls_row,
            self._phone_row,
            self._body,
            margin=0,
            scroll="y-auto",
            sizing_mode="stretch_both",
            css_classes=["flowdash-report"],
            stylesheets=[_LAYOUT_CSS],
        )
        editor.param.watch(self.schedule, ["dashboard", "read_only", "provenance"])

    def __panel__(self):
        """Render the view."""
        return self._layout

    # ------------------------------------------------------------------
    # Keeping up with the dashboard
    # ------------------------------------------------------------------

    @param.depends("shown", watch=True)
    def _on_shown(self):
        if self.shown:
            self.sync()
        else:
            self._unwatch()

    def schedule(self, *_events):
        """Rebuild the report soon, once for a burst of changes, while it is on screen."""
        if not self.shown or self._pending:
            return
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self.sync()
            return
        self._pending = True

        def run():
            self._pending = False
            self.sync()

        doc.add_timeout_callback(run, _COALESCE_MS)

    def _schedule_values(self, *_events):
        if not self.shown:
            return
        doc = pn.state.curdoc
        if doc is None or doc.session_context is None:
            self._refill()
            return
        doc.add_next_tick_callback(self._refill)

    def _refill(self):
        for pane, text in self._texts:
            rendered = render_report_text(self.editor, text, VALUE_CLASS)
            if pane.object != rendered:
                pane.object = rendered
        meta = self._meta_html()
        if self._meta.object != meta:
            self._meta.object = meta

    def _unwatch(self):
        for state, watcher in self._watchers:
            try:
                state.param.unwatch(watcher)
            except Exception:  # pragma: no cover - the state went with its node
                pass
        self._watchers = []

    def _watch_outputs(self, refs: t.Iterable[tuple[str, str]]):
        """Refill the text and the filters line when one of the outputs *refs* (instance id, port) changes."""
        self._unwatch()
        graph = self.editor.graph
        seen = set()
        for instance_id, port in refs:
            if (instance_id, port) in seen:
                continue
            seen.add((instance_id, port))
            state = graph.get_state(instance_id)
            if state is not None and port in state.param:
                self._watchers.append((state, state.param.watch(self._schedule_values, port)))

    # ------------------------------------------------------------------
    # Building the document
    # ------------------------------------------------------------------

    @pn.io.hold()
    def sync(self):
        """Build the report from the dashboard as it is now."""
        if not self.shown:
            return
        editor = self.editor
        resolved = report_blocks(editor)
        self._texts = []
        self._contents_items: list[dict] = []
        self._anchor = 0
        self._used: set[str] = set()
        self._refs: list[tuple[str, str]] = []
        items = {item["instance_id"]: item for item in editor._tile_items}
        body_nodes = {b["node"] for b in resolved.blocks if "node" in b}
        self._filters = [
            iid
            for iid in resolved.filters
            if iid in items and iid not in body_nodes and self._shown_tile(iid)
        ]
        self._used.update(self._filters)
        self._sync_controls()

        self._title.object = escape_markdown(resolved.title)
        self._notice.visible = resolved.derived
        may_edit = not editor.read_only
        self._notice_text.object = escape_markdown(
            f"{DERIVED_NOTICE} {CUSTOMIZE_HINT}" if may_edit else DERIVED_NOTICE
        )
        self._customize.visible = may_edit

        objects: list[Viewable] = []
        for block in resolved.blocks:
            try:
                objects += self._block(block)
            except Exception:
                logger.exception("Could not show a report block: %r", block)
        objects = self._row_kpis(objects)
        if not objects:
            objects = [self._caption(EMPTY_TEXT)]
        self._blocks.objects = objects

        self._wrappers = {k: v for k, v in self._wrappers.items() if k[0] in self._used}
        self._contents.items = self._contents_items
        self._contents_chip.items = self._contents_items
        self._contents.visible = bool(self._contents_items)
        self._contents_chip.visible = bool(self._contents_items)
        for iid in self._filters:
            port = self._first_output(iid)
            if port is not None:
                self._refs.append((iid, port))
        self._watch_outputs(self._refs)
        self._meta.object = self._meta_html()

    def _shown_tile(self, instance_id: str) -> bool:
        return tile_layout.tile_entry(self.editor, instance_id).get("visible") is not False

    def _first_output(self, instance_id: str) -> str | None:
        editor = self.editor
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        spec = editor._component_specs.get(item["component_id"]) if item else None
        return spec.outputs[0].name if spec is not None and spec.outputs else None

    def _meta_html(self) -> str:
        """Return the line above the title: live, and the values the controls hold."""
        editor = self.editor
        parts = []
        items = {item["instance_id"]: item for item in editor._tile_items}
        for iid in self._filters:
            if editor._node_restricted(iid):
                continue
            port = self._first_output(iid)
            state = editor.graph.get_state(iid)
            value = getattr(state, port, None) if state is not None and port else None
            text = placeholder_text(value)
            if not text or len(text) > 60:
                continue
            label = (items[iid].get("config") or {}).get("label")
            title = (
                label
                if isinstance(label, str) and label
                else tile_layout.tile_title(editor, items[iid])
            )
            parts.append(f"{html.escape(title)}: {html.escape(text)}")
        summary = f"<span>{' · '.join(parts)}</span>" if parts else ""
        return (
            f'<div class="fd-report-meta"><span class="fd-report-live">Live</span>{summary}</div>'
        )

    def _sync_controls(self):
        editor = self.editor
        views = []
        for iid in self._filters:
            view = self._view_of(iid)
            if view is None:
                continue
            wrapper = self._control_wrappers.get(iid)
            if wrapper is None or wrapper[0] is not view:
                wrapper = self._control_wrappers[iid] = pn.Column(
                    view,
                    margin=0,
                    width=_row_width(view),
                    styles={"max-width": "100%"},
                    stylesheets=[] if getattr(view, "height", None) else [_FIT_CONTENT],
                    css_classes=["fd-report-control"],
                )
            views.append(wrapper)
        self._control_wrappers = {
            iid: w for iid, w in self._control_wrappers.items() if iid in self._filters
        }
        if list(self._controls) != views:
            self._controls[:] = views
        self._controls_row.visible = bool(views)
        self._filters_chip.visible = bool(views)
        self._filters_chip.label = f"Filters · {len(views)}" if views else "Filters"
        self._phone_row.visible = bool(views) or bool(editor._tile_items)

    def _toggle_filters(self, _event):
        classes = list(self._controls_row.css_classes)
        if "fd-report-filters-open" in classes:
            classes.remove("fd-report-filters-open")
        else:
            classes.append("fd-report-filters-open")
        self._controls_row.css_classes = classes
        self._filters_chip.variant = (
            "contained" if "fd-report-filters-open" in classes else "outlined"
        )

    def _next_anchor(self, label: str, badge: str | None = None) -> str:
        self._anchor += 1
        key = f"{id(self):x}-{self._anchor}"
        self._contents_items.append({"label": label, "key": key, "badge": badge})
        return f"fd-report-anchor-{key}"

    def _heading(self, text: str, level: int = 1, contents: bool = True, badge=None) -> Viewable:
        variant = {1: "h5", 2: "h6", 3: "subtitle1"}.get(level, "h5")
        size = {"fontSize": {1: "1.5rem", 2: "1.25rem"}[level]} if level < 3 else {}
        classes = ["fd-report-heading"]
        if contents and level < 3:
            classes.append(self._next_anchor(text, badge))
        return text_typography(
            text,
            variant=variant,
            margin=0,
            sx={"fontWeight": 400 if level == 1 else 500, "scrollMarginTop": "80px", **size},
            css_classes=classes,
        )

    def _caption(self, text: str) -> Viewable:
        return text_typography(text, variant="body2", margin=0, sx={"color": "text.secondary"})

    def _section(self, *objects: Viewable, gap: int = 14, styles=None, **params) -> pn.Column:
        return pn.Column(
            *objects,
            margin=0,
            sizing_mode="stretch_width",
            styles={"gap": f"{gap}px", **(styles or {})},
            stylesheets=[_FIT_CONTENT],
            **params,
        )

    def _block(self, block: dict) -> list[Viewable]:
        kind = block_kind(block)
        if kind == "heading":
            return [self._heading(block["heading"], block.get("level", 1))]
        if kind == "text":
            return [self._text(block["text"])]
        if kind == "node":
            node = self._node(block["node"], block)
            return [node] if node is not None else []
        if kind == "group":
            return self._group(block["group"])
        return self._turn(block["turn"])

    def _text(self, text: str) -> Viewable:
        from panel_flowdash.agent.yaml_format import node_names

        ids = {name: iid for iid, name in node_names(self.editor._tile_items).items()}
        for match in PLACEHOLDER.finditer(text or ""):
            iid = ids.get(match.group(1))
            if iid is not None:
                spec_port = self._port_of(iid, match.group(2))
                if spec_port is not None:
                    self._refs.append((iid, spec_port))
        pane = pn.pane.HTML(
            render_report_text(self.editor, text, VALUE_CLASS),
            margin=0,
            sizing_mode="stretch_width",
            stylesheets=[_PROSE_CSS],
            css_classes=["fd-report-text"],
        )
        self._texts.append((pane, text))
        return pane

    def _port_of(self, instance_id: str, port: str) -> str | None:
        editor = self.editor
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        spec = editor._component_specs.get(item["component_id"]) if item else None
        if spec is None:
            return None
        resolved = spec.resolve_name(port)
        return resolved if any(p.name == resolved for p in spec.outputs) else None

    def _view_of(self, instance_id: str) -> Viewable | None:
        editor = self.editor
        index = next(
            (i for i, item in enumerate(editor._tile_items) if item["instance_id"] == instance_id),
            None,
        )
        if index is None or index >= len(editor._tile_objects):
            return None
        return editor._tile_objects[index]

    def _shows_table(self, item: dict, block: dict) -> bool:
        """Whether a node block shows its first output's value rather than the node's view.

        It does when the block asks for a table, and for a data node, whose
        table is what a report reader wants, as in the Explore view.
        """
        editor = self.editor
        spec = editor._component_specs.get(item["component_id"])
        if spec is None or not spec.outputs or "placeholder" in item:
            return False
        if block.get("show") == "table":
            return True
        if editor._node_role(item["component_id"]) in ("view", "widget"):
            return False
        return port_types_compatible(spec.outputs[0].type, "DataFrame")

    def _node(self, instance_id: str, block: dict | None = None) -> Viewable | None:
        """Return a node's live output in the report: its tile's view, or its value as a table."""
        editor = self.editor
        block = block or {}
        item = next((i for i in editor._tile_items if i["instance_id"] == instance_id), None)
        if item is None:
            return None
        if instance_id in self._used:
            return None
        self._used.add(instance_id)
        caption = block.get("caption")
        kind = editor._tile_box(item).kind
        table = self._shows_table(item, block)
        framed = (
            kind != "text"
            and "placeholder" not in item
            and not editor._node_restricted(instance_id)
            and editor._tile_framed(item)
        )
        view = self._view_of(instance_id)
        key = (
            instance_id,
            id(view),
            kind,
            table,
            framed,
            caption,
            block.get("height"),
            tile_layout.tile_entry(editor, instance_id).get("height"),
        )
        cached = self._wrappers.get(key)
        if cached is not None:
            return cached
        if editor._node_restricted(instance_id) and "placeholder" not in item:
            body = self._caption(RESTRICTED_TEXT)
        else:
            body, kind = self._node_body(item, block, kind, table)
        objects = [body]
        if caption:
            objects.append(
                text_typography(caption, variant="body2", margin=0, sx={"color": "text.secondary"})
            )
        classes = ["fd-report-node", f"fd-report-{kind}"]
        if framed:
            wrapper = pmui.Paper(
                *objects,
                variant="outlined",
                margin=0,
                sizing_mode="stretch_width",
                sx={"p": 1, "gap": "8px", "overflow": "hidden"},
                css_classes=classes,
                stylesheets=[_HOST_FITS_CONTENT],
            )
        else:
            wrapper = self._section(*objects, gap=8, css_classes=classes)
        wrapper.tags = [kind]
        self._wrappers[key] = wrapper
        return wrapper

    def _node_body(self, item: dict, block: dict, kind: str, table: bool) -> tuple[Viewable, str]:
        editor = self.editor
        instance_id = item["instance_id"]
        if table:
            spec = editor._component_specs.get(item["component_id"])
            body = self._value(instance_id, spec.outputs[0].name)
            return body, "table"
        view = self._view_of(instance_id)
        if view is None:
            return self._caption("This output has nothing to show yet."), kind
        if "placeholder" in item:
            return view, "placeholder"
        if kind == "control":
            return pn.Column(view, margin=0, sizing_mode="stretch_width"), kind
        spec = editor._component_specs.get(item["component_id"])
        size = (spec.default_size if spec is not None else None) or {}
        entry = tile_layout.tile_entry(editor, instance_id).get("height")
        height = block.get("height") or entry or size.get("height")
        if kind in ("chart", "table", "data"):
            height = int(height or BLOCK_HEIGHT)
            return pn.Column(view, height=height, margin=0, sizing_mode="stretch_width"), kind
        if block.get("height"):
            return pn.Column(view, height=int(height), margin=0, sizing_mode="stretch_width"), kind
        # KPIs and text take the height of their content.
        return (
            pn.Column(
                view,
                margin=0,
                sizing_mode="stretch_width",
                stylesheets=[_FIT_CONTENT],
            ),
            kind,
        )

    def _value(self, instance_id: str, port: str) -> Viewable:
        from panel_agents.components.renderers import render

        state = self.editor.graph.get_state(instance_id)

        def show(value):
            if value is None:
                return self._caption("No value yet.")
            try:
                return render(value, page_size=10)
            except Exception:
                logger.exception("Could not show the output of %s", instance_id)
                return self._caption("This output could not be shown.")

        if state is None or port not in state.param:
            return self._caption("No value yet.")
        return pn.panel(param.bind(show, state.param[port]), sizing_mode="stretch_width", margin=0)

    def _row_kpis(self, objects: list[Viewable]) -> list[Viewable]:
        """Set consecutive KPI blocks side by side, as the dashboard shows them."""
        rows: list[Viewable] = []
        run: list[Viewable] = []

        def flush():
            if len(run) > 1:
                for obj in run:
                    obj.styles = {**obj.styles, "flex": "1 1 140px", "min-width": "0"}
                rows.append(
                    pn.FlexBox(
                        *run,
                        flex_wrap="wrap",
                        align_items="stretch",
                        gap="12px",
                        margin=0,
                        sizing_mode="stretch_width",
                        css_classes=["fd-report-kpis"],
                    )
                )
            else:
                rows.extend(run)
            run.clear()

        for obj in objects:
            if "kpi" in (getattr(obj, "tags", None) or []):
                run.append(obj)
                continue
            flush()
            if isinstance(obj, pn.Column) and "fd-report-section" in obj.css_classes:
                obj.objects = self._row_kpis(list(obj.objects))
            rows.append(obj)
        flush()
        return rows

    def _group(self, group_id: str) -> list[Viewable]:
        editor = self.editor
        group = editor._groups.get(group_id)
        if group is None:
            return []
        members = [self._node(iid) for iid in group_members(editor, group_id)]
        members = [m for m in members if m is not None]
        if not members:
            return []
        return [
            self._section(
                self._heading(group.label or group_id, 1),
                *members,
                css_classes=["fd-report-section", "fd-report-group"],
            )
        ]

    def _turn_outputs(self, turn_id: str) -> list[str]:
        """Return the nodes a turn made that no other node it made reads from, in dashboard order."""
        editor = self.editor
        made = [
            item["instance_id"]
            for item in editor._tile_items
            if editor.node_origin(item["instance_id"]) == turn_id
        ]
        feeding = {
            edge["source"]
            for edge in editor._dataflow_graph.edges
            if edge["source"] in made and edge["target"] in made
        }
        leaves = [iid for iid in made if iid not in feeding]
        order = {
            item["instance_id"]: i
            for i, item in enumerate(tile_layout.area_items(editor, tile_layout.GRID))
        }
        return sorted(leaves, key=lambda iid: order.get(iid, len(order)))

    def _turn(self, turn_id: str) -> list[Viewable]:
        editor = self.editor
        turn = next((t for t in editor.turns if t.id == turn_id), None)
        if turn is None:
            return []
        if turn.status == "pending":
            return [self._pending_turn(turn)]
        redacted = editor.run_redacted(turn.run) if turn.run is not None else editor.read_only
        who = "Answered by the assistant" if turn.actor == "agent" else "Edited by hand"
        title = turn.question if turn.question and not redacted else who
        outputs = self._turn_outputs(turn.id)
        meta = [text_typography(who, variant="body2", margin=0, sx={"color": "text.secondary"})]
        made_by_agent = next((iid for iid in outputs if editor.made_by_agent(iid)), None)
        if made_by_agent is not None:
            how = pmui.Button(
                label="How was this made?",
                variant="text",
                size="small",
                margin=0,
                sx={"textTransform": "none", "minWidth": 0, "px": 0.5, "py": 0},
                css_classes=["fd-report-how-made"],
            )
            how.on_click(lambda _event, iid=made_by_agent: self._how_made(iid))
            meta.append(how)
        nodes = [self._node(iid) for iid in outputs]
        nodes = [n for n in nodes if n is not None]
        return [
            self._section(
                self._section(
                    self._heading(title, 1),
                    pn.FlexBox(*meta, align_items="center", gap="8px", margin=0),
                    gap=4,
                ),
                *(nodes or [self._caption("The nodes this request made are no longer here.")]),
                gap=12,
                css_classes=["fd-report-section", "fd-report-turn"],
            )
        ]

    def _how_made(self, instance_id: str):
        self.editor.show_how_made(instance_id)

    def _pending_turn(self, turn: DashboardTurn) -> Viewable:
        question = turn.question or "A planned question"
        anchor = self._next_anchor(question, "Not run")
        run = pmui.Button(
            label="Run",
            icon="play_arrow",
            variant="contained",
            size="small",
            disabled=True,
            margin=0,
            align="center",
            css_classes=["fd-report-run"],
        )
        text = self._section(
            text_typography(
                question,
                variant="subtitle1",
                margin=0,
                sx={"fontWeight": 500, "color": "inherit", "lineHeight": 1.4},
            ),
            text_typography(PENDING_NOTE, variant="body2", margin=0, sx={"color": "inherit"}),
            gap=2,
            styles={"flex": "1 1 220px", "min-width": "0"},
        )
        return pmui.Paper(
            pn.FlexBox(
                _icon("help", "var(--mui-palette-warning-main, #ed6c02)"),
                text,
                run,
                flex_wrap="wrap",
                align_items="center",
                gap="12px 16px",
                margin=0,
                sizing_mode="stretch_width",
            ),
            elevation=0,
            margin=0,
            sizing_mode="stretch_width",
            sx={
                "bgcolor": "var(--mui-palette-Alert-warningStandardBg, #fff4e5)",
                "color": "var(--mui-palette-Alert-warningColor, #663c00)",
                "border": "1px solid rgba(var(--mui-palette-warning-mainChannel, 237 108 2) / 0.45)",
                "p": 2,
                "scrollMarginTop": "80px",
            },
            css_classes=["fd-report-pending", anchor],
        )


__all__ = ["ReportContents", "ReportView"]
