"""Keeps a drag in the editor from selecting text on the page.

The tile grid, the canvas, the palette and the report's blocks are each
dragged by a library or script of their own, which at best keeps the dragged
element from being selected, not the text of the tiles, nodes or blocks the
pointer passes over. :class:`DragGuard` turns text selection off for the
whole page from the press on a drag handle until the drop, and clears any
selection the browser makes during the drag, keeping one made before it.
"""

from __future__ import annotations

import json
import typing as t

from panel.custom import JSComponent

#: What a drag starts from: tile handles (drag and resize), the handles of the
#: components beside the grid, anything on the canvas (nodes, ports, the empty
#: pane and its panels), and the report's block handles. Native drags of ``draggable`` elements, e.g. palette
#: entries, are guarded from the press, and from their ``dragstart``.
HANDLES = (
    ".muuri-handle",
    ".fd-control-handle",
    ".react-flow",
    ".fd-report-handle",
    "[draggable='true']",
)

#: Applied to the tile grid. In edit mode a press on the grid around the tiles
#: starts no selection, while the tiles' own content stays selectable; the
#: dragged or released tile and the resize ghost select nothing.
GRID_DRAG_CSS = """
.tile-grid:has(.muuri-grid:not(.muuri-no-handles)),
.muuri-grid:not(.muuri-no-handles) {
  -webkit-user-select: none;
  user-select: none;
}
.muuri-grid:not(.muuri-no-handles) .muuri-grid-item > div:not(.muuri-handle) > * {
  -webkit-user-select: text;
  user-select: text;
}
.muuri-handle, .muuri-resize-ghost,
.muuri-item-dragging, .muuri-item-dragging *,
.muuri-item-releasing, .muuri-item-releasing * {
  -webkit-user-select: none !important;
  user-select: none !important;
}
"""


class DragGuard(JSComponent):
    """Turns off text selection on the page while something in the editor is dragged.

    From a press on one of :data:`HANDLES`, or a native ``dragstart`` of one, until
    the pointer is released or the native drag ends, the body's
    ``user-select`` is ``none``; afterwards its own inline value is put back.
    A selection the browser makes during the drag all the same, as WebKit
    does, is cleared, while one made before the drag is kept (WebKit itself
    collapses it on the press). Presses in text fields, and on parts of a node
    marked ``nodrag``, are left alone.
    It takes no space on the page.
    """

    _esm = """
    const HANDLES = __HANDLES__
    const GRID_BACKGROUND = [
      ".muuri-grid:not(.muuri-no-handles) > .muuri-grid-item",
      ".muuri-grid:not(.muuri-no-handles)",
      ".tile-grid:has(.muuri-grid:not(.muuri-no-handles))",
    ].join(", ")

    function editable(node) {
      const tag = node?.tagName
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!node?.isContentEditable
    }

    function startsDrag(path) {
      if (editable(path[0])) { return false }
      // A tile's resize edge, which some browsers report as the tile itself, and the
      // edited grid around the tiles, where WebKit starts a selection despite the CSS.
      if (path[0]?.matches?.(GRID_BACKGROUND)) { return true }
      for (const node of path) {
        if (!node.matches) { continue }
        if (node.classList.contains("nodrag")) { return false }
        if (HANDLES.some((selector) => node.matches(selector))) { return true }
      }
      return false
    }

    function shadowRoots(root = document, found = []) {
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) {
          found.push(el.shadowRoot)
          shadowRoots(el.shadowRoot, found)
        }
      }
      return found
    }

    // The ends of the selected range in the shadow roots holding them, where the
    // document's selection only gives their hosts. Older WebKit takes the roots
    // as arguments, older Chromium has no composed ranges.
    function rangeOf(selection, roots) {
      if (!selection || selection.type !== "Range") { return null }
      let range = null
      try {
        range = selection.getComposedRanges?.({ shadowRoots: roots })[0]
      } catch {
        try { range = selection.getComposedRanges(...roots)[0] } catch {}
      }
      range ??= selection.getRangeAt(0)
      return [range.startContainer, range.startOffset, range.endContainer, range.endOffset]
    }

    // One guard per document, shared by every DragGuard rendered on the page, so
    // a second one never takes the first one's "none" for the body's own value.
    function install() {
      const state = { users: 0, saved: null, kept: null, roots: [], pointers: new Set(), native: false }

      const hold = () => {
        if (state.saved !== null) { return }
        const style = document.body.style
        state.saved = [style.userSelect, style.webkitUserSelect]
        style.userSelect = "none"
        style.webkitUserSelect = "none"
        const selection = document.getSelection()
        if (selection?.type === "Range") {
          state.roots = shadowRoots()
          state.kept = rangeOf(selection, state.roots)
        }
      }
      const release = (force = false) => {
        if (!force && (state.pointers.size || state.native)) { return }
        state.pointers.clear()
        state.native = false
        if (state.saved === null) { return }
        const style = document.body.style
        ;[style.userSelect, style.webkitUserSelect] = state.saved
        state.saved = state.kept = null
        state.roots = []
      }

      const onDown = (event) => {
        if (event.button !== 0 || !startsDrag(event.composedPath())) { return }
        state.pointers.add(event.pointerId)
        hold()
      }
      const onUp = (event) => {
        state.pointers.delete(event.pointerId)
        release()
      }
      // A native drag cancels the pointer as it starts, and ends with dragend or drop.
      const onCancel = onUp
      // Not for dragging selected text or an image, which needs its selection.
      const onDragStart = (event) => {
        if (!startsDrag(event.composedPath())) { return }
        state.native = true
        hold()
      }
      // Some browsers (WebKit) send no pointerup or pointercancel after a native drag.
      const onDragEnd = () => release(true)
      const onBlur = () => release(true)
      // WebKit extends a selection into selectable text whatever the body's style.
      const onSelectionChange = () => {
        if (state.saved === null) { return }
        const selection = document.getSelection()
        const range = rangeOf(selection, state.roots)
        const { kept } = state
        if (range && !(kept && range.every((part, i) => part === kept[i]))) {
          selection.removeAllRanges()
        }
      }

      const listeners = [
        [document, "pointerdown", onDown],
        [document, "pointerup", onUp],
        [document, "pointercancel", onCancel],
        [document, "dragstart", onDragStart],
        [document, "dragend", onDragEnd],
        [document, "drop", onDragEnd],
        [window, "blur", onBlur],
        [document, "selectionchange", onSelectionChange],
      ]
      for (const [target, type, listener] of listeners) { target.addEventListener(type, listener, true) }
      state.uninstall = () => {
        for (const [target, type, listener] of listeners) { target.removeEventListener(type, listener, true) }
        release(true)
      }
      return state
    }

    export function render({ model }) {
      const state = window.__flowdashDragGuard ??= install()
      state.users += 1
      model.on("remove", () => {
        state.users -= 1
        if (state.users <= 0) {
          state.uninstall()
          if (window.__flowdashDragGuard === state) { delete window.__flowdashDragGuard }
        }
      })
    }
    """.replace("__HANDLES__", json.dumps(HANDLES))

    _stylesheets: t.ClassVar[list[str]] = [":host { display: none !important; }"]

    def __init__(self, **params):
        params.setdefault("margin", 0)
        params.setdefault("width", 0)
        params.setdefault("height", 0)
        super().__init__(**params)
