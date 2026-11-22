"""The pieces of the Report view that edit a report: dragging blocks, the insert bar and the text editor.

Every change they make goes through the editor (``FlowDash.set_report`` and
the helpers of :mod:`panel_flowdash.report`), so a gesture is one version of
the edit history and Undo takes it back. :class:`BlockDrag` moves blocks by
their drag handles; :class:`InsertBar` offers the kinds of block to insert
between two blocks; and :class:`TextBlockEditor` edits a text block's Markdown in place, with a small
toolbar limited to formatting that round-trips to Markdown and a picker for
the live values a text may quote.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent
from panel.viewable import Viewer

if t.TYPE_CHECKING:
    from collections.abc import Callable

#: The Material icon of each kind of tile, as the node picker shows them.
KIND_ICONS = {
    "chart": "bar_chart",
    "table": "table_chart",
    "kpi": "speed",
    "data": "storage",
    "control": "tune",
    "text": "notes",
}

KIND_LABELS = {
    "chart": "Chart",
    "table": "Table",
    "kpi": "KPI",
    "data": "Data",
    "control": "Control",
    "text": "Text",
}

#: How many matches the node picker lists at most.
MAX_PICKS = 50


class BlockDrag(JSComponent):
    """Moves the blocks of a report being edited by their handles, with the pointer or the arrow keys.

    The blocks are the children carrying ``block_class`` of the layout
    carrying ``list_class``, in order; each has a handle carrying
    ``handle_class``. Dragging a handle shows where the block would go, and
    dropping it, or pressing the up or down arrow while the handle has focus,
    calls ``on_move(index, to)``, *to* being the index the block ends up at.
    """

    list_class = param.String(default="fd-report-blocks")

    block_class = param.String(default="fd-report-block")

    handle_class = param.String(default="fd-report-handle")

    enabled = param.Boolean(default=False, doc="Whether blocks can be moved.")

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

    export function render({ model }) {
      const blocks = () => {
        const list = find(document, `.${model.list_class}`)
        const root = list?.shadowRoot
        return root ? [...root.children].filter((el) => el.classList.contains(model.block_class)) : []
      }
      const handleOf = (event) => event.composedPath().find(
        (el) => el.classList?.contains(model.handle_class)
      )
      const blockOf = (handle) => {
        const all = blocks()
        return all.findIndex((block) => block === handle || block.shadowRoot?.contains(handle) ||
          find(block.shadowRoot ?? block, `.${model.handle_class}`) === handle)
      }
      const line = document.createElement("div")
      line.className = "fd-report-drop-line"
      Object.assign(line.style, {
        position: "fixed", height: "3px", borderRadius: "2px", pointerEvents: "none", zIndex: "2000",
        background: "var(--mui-palette-primary-main, #0072b5)", display: "none",
      })
      document.body.appendChild(line)

      // The moved block's handle takes the focus back once the server has
      // re-rendered the blocks in their new order: focusing it any earlier
      // lands on the block about to move away, and the re-render drops it.
      let focusAfter = null
      const handleButton = (block) => find(block.shadowRoot ?? block, `.${model.handle_class}`)
        ?.shadowRoot?.querySelector("button")
      const refocus = (started = performance.now()) => {
        if (focusAfter === null) { return }
        const now = performance.now()
        const button = blocks()[focusAfter.to] === focusAfter.block && handleButton(focusAfter.block)
        if (button) {
          const lost = !document.activeElement || document.activeElement === document.body
          if (focusAfter.placed === undefined || lost) {
            button.focus()
            focusAfter.placed ??= now
          }
          // Keep it for a moment, as the last re-render can still replace the button.
          if (now - focusAfter.placed > 300) { focusAfter = null; return }
        }
        if (now - started < 10000) {
          requestAnimationFrame(() => refocus(started))
        } else {
          focusAfter = null
        }
      }

      const onDown = (event) => {
        if (!model.enabled || event.button !== 0) { return }
        const handle = handleOf(event)
        if (!handle) { return }
        const all = blocks()
        const index = blockOf(handle)
        if (index < 0) { return }
        event.preventDefault()
        const dragged = all[index]
        dragged.style.opacity = "0.45"
        let target = null
        const move = (e) => {
          // The gap nearest the pointer: before block i, or after the last one.
          const boxes = all.map((block) => block.getBoundingClientRect())
          let best = boxes.length
          for (let i = 0; i < boxes.length; i++) {
            if (e.clientY < boxes[i].top + boxes[i].height / 2) { best = i; break }
          }
          target = best === index || best === index + 1 ? null : best
          if (target === null) { line.style.display = "none"; return }
          const ref = boxes[Math.min(target, boxes.length - 1)]
          const y = target < boxes.length ? ref.top - 14 : ref.bottom + 12
          Object.assign(line.style, {
            display: "block", top: `${y}px`, left: `${ref.left}px`, width: `${ref.width}px`,
          })
        }
        const up = (e) => {
          document.removeEventListener("pointermove", move, true)
          document.removeEventListener("pointerup", up, true)
          document.removeEventListener("pointercancel", up, true)
          dragged.style.opacity = ""
          line.style.display = "none"
          if (target !== null && e.type === "pointerup") {
            model.send_msg({ index, to: target > index ? target - 1 : target })
          }
        }
        document.addEventListener("pointermove", move, true)
        document.addEventListener("pointerup", up, true)
        document.addEventListener("pointercancel", up, true)
      }
      const onKey = (event) => {
        if (!model.enabled || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) { return }
        const handle = handleOf(event)
        if (!handle) { return }
        const index = blockOf(handle)
        const to = index + (event.key === "ArrowUp" ? -1 : 1)
        if (index < 0 || to < 0 || to >= blocks().length) { return }
        event.preventDefault()
        focusAfter = { block: blocks()[index], to }
        model.send_msg({ index, to })
        refocus()
      }
      document.addEventListener("pointerdown", onDown, true)
      document.addEventListener("keydown", onKey, true)
      model.on("remove", () => {
        document.removeEventListener("pointerdown", onDown, true)
        document.removeEventListener("keydown", onKey, true)
        line.remove()
      })
    }
    """

    _stylesheets: t.ClassVar[list[str]] = [":host { display: none !important; }"]

    def __init__(self, on_move: Callable[[int, int], None] | None = None, **params):
        params.setdefault("margin", 0)
        params.setdefault("width", 0)
        params.setdefault("height", 0)
        super().__init__(**params)
        self._on_move = on_move

    def _handle_msg(self, msg):
        if not isinstance(msg, dict) or not self.enabled or self._on_move is None:
            return
        index, to = msg.get("index"), msg.get("to")
        if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (index, to)):
            self._on_move(index, to)


class MarkdownArea(JSComponent):
    """A text block edited in place: its rendered prose, which a click turns into its Markdown source.

    ``value`` is the Markdown, synced when the text area loses focus and
    after each :meth:`apply`; ``html`` is what the block shows while not
    ``active``. :meth:`apply` formats the selection, or inserts text at it,
    and returns the focus to the text area, so a toolbar button does not
    end the editing.
    """

    value = param.String(default="", doc="The block's Markdown.")

    html = param.String(default="", doc="The rendered block, shown while not active.")

    active = param.Boolean(default=False, doc="Whether the Markdown source is being edited.")

    placeholder = param.String(default="Write something…")

    _esm = """
    export function render({ model, el }) {
      const preview = document.createElement("div")
      preview.className = "fd-md-preview"
      preview.tabIndex = 0
      preview.setAttribute("role", "button")
      preview.setAttribute("aria-label", "Edit this text")
      const area = document.createElement("textarea")
      area.className = "fd-md-area"
      area.rows = 3
      area.setAttribute("aria-label", "Text, in Markdown")
      area.placeholder = model.placeholder
      area.value = model.value
      el.append(preview, area)

      const grow = () => {
        area.style.height = "auto"
        area.style.height = `${area.scrollHeight + 2}px`
      }
      const focused = () => area.getRootNode().activeElement === area
      const commit = () => { if (area.value !== model.value) { model.value = area.value } }
      const show = () => {
        if (model.html) {
          preview.innerHTML = model.html
        } else {
          preview.innerHTML = ""
          const empty = document.createElement("p")
          empty.className = "fd-md-empty"
          empty.textContent = model.placeholder
          preview.append(empty)
        }
        preview.hidden = model.active
        area.hidden = !model.active
        if (model.active) {
          requestAnimationFrame(() => {
            grow()
            if (!focused()) { area.focus() }
          })
        }
      }
      const open = (event) => {
        if (event.target.closest && event.target.closest("a")) { return }
        model.active = true
      }
      preview.addEventListener("click", open)
      preview.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); model.active = true }
      })
      area.addEventListener("input", grow)
      area.addEventListener("blur", commit)
      area.addEventListener("keydown", (event) => {
        if (event.key === "Escape" || (event.key === "Enter" && (event.ctrlKey || event.metaKey))) {
          event.preventDefault()
          event.stopPropagation()
          commit()
          model.active = false
        }
      })

      const select = (start, end) => { area.setSelectionRange(start, end); area.focus() }
      const wrap = (before, after, fallback) => {
        const start = area.selectionStart
        const end = area.selectionEnd
        const text = area.value.slice(start, end) || fallback
        area.setRangeText(before + text + after, start, end, "end")
        select(start + before.length, start + before.length + text.length)
      }
      const list = () => {
        const value = area.value
        const start = value.lastIndexOf("\\n", area.selectionStart - 1) + 1
        let end = value.indexOf("\\n", Math.max(area.selectionEnd - 1, start))
        if (end < 0) { end = value.length }
        const lines = value.slice(start, end).split("\\n")
        const listed = lines.every((line) => line.startsWith("- "))
        const text = lines.map((line) => listed ? line.slice(2) : `- ${line}`).join("\\n")
        area.setRangeText(text, start, end, "end")
        select(start + text.length, start + text.length)
      }
      model.on("msg:custom", (msg) => {
        if (!model.active) { return }
        if (msg.action === "bold") { wrap("**", "**", "bold text") }
        else if (msg.action === "italic") { wrap("*", "*", "italic text") }
        else if (msg.action === "link") {
          const start = area.selectionStart
          const text = area.value.slice(start, area.selectionEnd) || "link text"
          area.setRangeText(`[${text}](https://)`, start, area.selectionEnd, "end")
          select(start + text.length + 3, start + text.length + 11)
        }
        else if (msg.action === "list") { list() }
        else if (msg.action === "insert" && typeof msg.text === "string") {
          area.setRangeText(msg.text, area.selectionStart, area.selectionEnd, "end")
          area.focus()
        }
        grow()
        commit()
      })
      model.on("value", () => {
        if (area.value !== model.value && !focused()) { area.value = model.value; grow() }
      })
      model.on("html", show)
      model.on("active", show)
      show()
    }
    """

    _stylesheets: t.ClassVar[list[str]] = [
        """
    :host { display: block; width: 100%; }
    .fd-md-preview {
      cursor: text; border-radius: 4px; margin: -4px -8px; padding: 4px 8px; min-height: 1.7em;
      outline: 1px solid transparent;
    }
    .fd-md-preview:hover { outline-color: var(--mui-palette-divider, rgba(0, 0, 0, 0.12)); }
    .fd-md-preview:focus-visible { outline: 2px solid var(--mui-palette-primary-main, #0072b5); }
    .fd-md-empty { color: var(--mui-palette-text-secondary, #666); font-style: italic; }
    .fd-md-area {
      display: block; box-sizing: border-box; width: 100%; resize: none; overflow: hidden;
      font: 400 15px/1.6 "Roboto Mono", monospace; padding: 10px 12px; border-radius: 4px;
      color: var(--mui-palette-text-primary, #212121);
      background: var(--mui-palette-background-paper, #fff);
      border: 1px solid var(--mui-palette-primary-main, #0072b5);
    }
    .fd-md-area:focus { outline: 1px solid var(--mui-palette-primary-main, #0072b5); }
    [hidden] { display: none !important; }
    """
    ]

    def apply(self, action: str, text: str | None = None):
        """Format the selection (``bold``, ``italic``, ``link``, ``list``) or ``insert`` *text* at it."""
        self._send_msg({"action": action, "text": text})


_TOOL_SX = {"width": "32px", "height": "32px"}


class TextBlockEditor(Viewer):
    """A text block in an edited report: its prose, edited in place with a small Markdown toolbar.

    ``on_commit(text)`` is called with the block's new Markdown;
    ``on_open()`` when the block starts being edited. *stylesheets* style
    the rendered prose. ``values`` lists the
    live values the text can quote, each a dict with ``label`` and ``ref``
    (``node.port``); picking one inserts ``{{ node.port }}``.
    """

    values = param.List(default=[], doc="The values Insert value offers.")

    def __init__(
        self,
        text: str,
        html: str,
        on_commit: Callable[[str], None],
        on_open: Callable[[], None] | None = None,
        stylesheets: list[str] | None = None,
        **params,
    ):
        super().__init__(**params)
        self._on_commit = on_commit
        self._on_open = on_open
        self._updating = False
        self.area = MarkdownArea(
            value=text,
            html=html,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-md"],
            stylesheets=list(stylesheets or []),
        )
        buttons = []
        for action, icon, label in (
            ("bold", "format_bold", "Bold"),
            ("italic", "format_italic", "Italic"),
            ("link", "link", "Link"),
            ("list", "format_list_bulleted", "Bulleted list"),
        ):
            button = pmui.IconButton(
                icon=icon, size="small", description=label, margin=0, sx=_TOOL_SX
            )
            button.on_click(lambda _event, action=action: self.area.apply(action))
            buttons.append(button)
        self.insert_value = pmui.MenuButton(
            label="Insert value",
            icon="data_object",
            items=[],
            size="small",
            variant="text",
            margin=0,
            sx={"textTransform": "none"},
            css_classes=["fd-insert-value"],
        )
        self.insert_value.on_click(self._on_value)
        self.done = pmui.Button(
            label="Done", size="small", variant="text", margin=0, css_classes=["fd-text-done"]
        )
        self.done.on_click(lambda _event: self.close())
        self.toolbar = pn.FlexBox(
            *buttons,
            pmui.Divider(orientation="vertical", height=20, margin=(0, 4), align="center"),
            self.insert_value,
            pn.Spacer(height=0, margin=0, styles={"flex": "1 1 auto"}),
            self.done,
            align_items="center",
            flex_wrap="wrap",
            gap="2px",
            margin=(0, 0, 6, 0),
            sizing_mode="stretch_width",
            visible=False,
            styles={
                "border-bottom": "1px solid var(--mui-palette-divider, rgba(0,0,0,0.12))",
                "padding-bottom": "4px",
            },
            css_classes=["fd-text-toolbar"],
        )
        self._layout = pn.Column(
            self.toolbar,
            self.area,
            margin=0,
            sizing_mode="stretch_width",
            css_classes=["fd-report-text-editor"],
        )
        self.area.param.watch(self._on_value_synced, "value")
        self.area.param.watch(self._on_active, "active")
        self._sync_values()

    def __panel__(self):
        """Render the editor."""
        return self._layout

    @param.depends("values", watch=True)
    def _sync_values(self):
        self.insert_value.items = [
            {"label": value["label"], "ref": value["ref"]} for value in self.values
        ] or [{"label": "No values to insert", "ref": None}]

    def update(self, text: str, html: str):
        """Show the block's committed *text*, rendered as *html*."""
        self._updating = True
        try:
            self.area.param.update(value=text, html=html)
        finally:
            self._updating = False

    def open(self):
        """Start editing the block."""
        self.area.active = True

    def close(self):
        """Stop editing the block, showing its prose."""
        self.area.active = False

    def _on_active(self, event):
        self.toolbar.visible = event.new
        if event.new and self._on_open is not None:
            self._on_open()

    def _on_value_synced(self, event):
        if event.new != event.old and not self._updating:
            self._on_commit(event.new)

    def _on_value(self, item):
        ref = item.get("ref") if isinstance(item, dict) else None
        if ref:
            self.area.apply("insert", f"{{{{ {ref} }}}}")


class InsertBar(Viewer):
    """The kinds of block that can be inserted at a place in a report, and the pickers for them.

    ``on_insert(block)`` inserts a block; ``on_ask(question)`` adds a question
    not run yet; ``on_close()`` closes the bar. ``nodes`` lists the nodes the
    picker offers, each a dict with ``id``, ``title``, ``name``, ``kind`` and
    ``on_dashboard``; ``groups`` the groups, with ``id``, ``label`` and ``size``.
    """

    nodes = param.List(default=[])

    groups = param.List(default=[])

    def __init__(
        self,
        on_insert: Callable[[dict], None],
        on_ask: Callable[[str], None],
        on_close: Callable[[], None],
        **params,
    ):
        super().__init__(**params)
        self._on_insert = on_insert
        self._on_ask = on_ask
        self._on_close = on_close
        kinds = []
        for kind, icon, label in (
            ("heading", "title", "Heading"),
            ("text", "notes", "Text"),
            ("node", "insert_chart", "Node"),
            ("group", "workspaces", "Group"),
            ("ask", "help_outline", "Ask a question"),
        ):
            button = pmui.Button(
                label=label,
                icon=icon,
                size="small",
                variant="outlined",
                margin=0,
                sx={"textTransform": "none"},
                css_classes=[f"fd-insert-{kind}"],
            )
            button.on_click(lambda _event, kind=kind: self._pick(kind))
            kinds.append(button)
        self._kinds = dict(zip(("heading", "text", "node", "group", "ask"), kinds, strict=True))
        close = pmui.IconButton(
            icon="close", size="small", description="Close", margin=0, sx=_TOOL_SX
        )
        close.on_click(lambda _event: self._on_close())
        self.find = pmui.TextInput(
            placeholder="Find a node by title",
            label="Find a node",
            size="small",
            margin=0,
            sizing_mode="stretch_width",
        )
        self.find.param.watch(self._sync_nodes, "value_input")
        self.node_list = pmui.MenuList(
            items=[], dense=True, margin=0, sizing_mode="stretch_width", on_click=self._on_node
        )
        self.group_list = pmui.MenuList(
            items=[], dense=True, margin=0, sizing_mode="stretch_width", on_click=self._on_group
        )
        self.question = pmui.TextInput(
            label="Question",
            placeholder="What should the report answer?",
            size="small",
            margin=0,
            sizing_mode="stretch_width",
        )
        self.question.param.watch(lambda _event: self._ask(), "enter_pressed")
        ask = pmui.Button(
            label="Add question",
            size="small",
            variant="contained",
            margin=0,
            align="end",
            css_classes=["fd-insert-ask-add"],
        )
        ask.on_click(lambda _event: self._ask())
        self.empty = pmui.Typography(
            "", variant="body2", margin=(4, 0), sx={"color": "text.secondary"}
        )
        self._node_panel = pn.Column(
            self.find, self.node_list, margin=0, sizing_mode="stretch_width", visible=False
        )
        self._group_panel = pn.Column(
            self.group_list, margin=0, sizing_mode="stretch_width", visible=False
        )
        self._ask_panel = pn.FlexBox(
            self.question,
            ask,
            flex_wrap="nowrap",
            align_items="flex-end",
            gap="8px",
            margin=0,
            sizing_mode="stretch_width",
            visible=False,
        )
        self._layout = pmui.Paper(
            pn.FlexBox(
                pmui.Typography(
                    "Insert", variant="caption", margin=0, sx={"color": "text.secondary"}
                ),
                *kinds,
                pn.Spacer(height=0, margin=0, styles={"flex": "1 1 auto"}),
                close,
                align_items="center",
                flex_wrap="wrap",
                gap="6px",
                margin=0,
                sizing_mode="stretch_width",
            ),
            self._node_panel,
            self._group_panel,
            self._ask_panel,
            self.empty,
            variant="outlined",
            margin=0,
            sizing_mode="stretch_width",
            sx={"p": 1.5, "gap": "8px", "borderColor": "primary.main", "borderStyle": "dashed"},
            css_classes=["fd-report-insert-bar"],
        )

    def __panel__(self):
        """Render the editor."""
        return self._layout

    def reset(self):
        """Show the kinds of block only, closing any picker."""
        self._show(None)
        for widget in (self.find, self.question):
            # Triggered even when unchanged, so text typed but not entered is cleared too.
            widget.value = ""
            widget.param.trigger("value")

    def _show(self, panel: str | None):
        self._node_panel.visible = panel == "node"
        self._group_panel.visible = panel == "group"
        self._ask_panel.visible = panel == "ask"
        self.empty.object = ""
        for kind, button in self._kinds.items():
            button.variant = "contained" if kind == panel else "outlined"

    def _pick(self, kind: str):
        if kind == "heading":
            self._on_insert({"heading": "New section"})
        elif kind == "text":
            self._on_insert({"text": ""})
        elif kind == "node":
            self._show("node")
            self._sync_nodes()
        elif kind == "group":
            self._show("group")
            self._sync_groups()
        else:
            self._show("ask")

    @param.depends("nodes", watch=True)
    def _sync_nodes(self, *_events):
        query = (self.find.value_input or "").strip().lower()
        matches = [
            node
            for node in self.nodes
            if not query or query in node["title"].lower() or query in node["name"].lower()
        ]
        self.node_list.items = [
            {
                "label": node["title"],
                "secondary": (
                    f"{KIND_LABELS.get(node['kind'], 'Node')} · "
                    f"{'on the dashboard' if node['on_dashboard'] else 'not on the dashboard'}"
                ),
                "icon": KIND_ICONS.get(node["kind"], "widgets"),
                "id": node["id"],
            }
            for node in matches[:MAX_PICKS]
        ]
        if self._node_panel.visible:
            self.empty.object = (
                ""
                if matches
                else "Every node is in the report already."
                if not self.nodes
                else "No node matches."
            )

    @param.depends("groups", watch=True)
    def _sync_groups(self):
        self.group_list.items = [
            {
                "label": group["label"],
                "secondary": f"Group · {group['size']} shown",
                "icon": "workspaces",
                "id": group["id"],
            }
            for group in self.groups
        ]
        if self._group_panel.visible:
            self.empty.object = "" if self.groups else "There are no groups to add."

    def _on_node(self, item):
        if isinstance(item, dict) and item.get("id"):
            self._on_insert({"node": item["id"]})

    def _on_group(self, item):
        if isinstance(item, dict) and item.get("id"):
            self._on_insert({"group": item["id"]})

    def _ask(self):
        text = (self.question.value_input or self.question.value or "").strip()
        if text:
            self._on_ask(text)


__all__ = ["BlockDrag", "InsertBar", "MarkdownArea", "TextBlockEditor"]
