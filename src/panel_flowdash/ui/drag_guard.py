"""Keeps a drag in the editor from selecting text on the page.

The tile grid, the canvas, the palette and the report's blocks are each
dragged by a library or script of their own, which at best keeps the dragged
element from being selected, not the text of the tiles, nodes or blocks the
pointer passes over. :class:`DragGuard` turns text selection off for the
whole page from the press on a drag handle until the drop, and clears a
selection left from before the drag.
"""

from __future__ import annotations

import json
import typing as t

from panel.custom import JSComponent

#: What a drag starts from: tile handles (drag and resize), the handles of the
#: components beside the grid, nodes, ports and the empty canvas, and the
#: report's block handles. Native drags of ``draggable`` elements, e.g. palette
#: entries, are guarded from the press, and from their ``dragstart``.
HANDLES = (
    ".muuri-handle",
    ".fd-control-handle",
    ".react-flow__node",
    ".react-flow__handle",
    ".react-flow__pane",
    ".react-flow__nodesselection-rect",
    ".fd-report-handle",
    "[draggable='true']",
)

#: Applied to the tile grid: the dragged or released tile and the resize ghost select nothing.
GRID_DRAG_CSS = """
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
    A selection made before the drag is cleared as the drag starts. Presses
    in text fields, and on parts of a node marked ``nodrag``, are left alone.
    It takes no space on the page.
    """

    _esm = """
    const HANDLES = __HANDLES__

    function editable(node) {
      const tag = node?.tagName
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!node?.isContentEditable
    }

    function startsDrag(path) {
      if (editable(path[0])) { return false }
      for (const node of path) {
        if (!node.matches) { continue }
        if (node.classList.contains("nodrag")) { return false }
        if (HANDLES.some((selector) => node.matches(selector))) { return true }
      }
      return false
    }

    export function render({ model }) {
      let saved = null
      let pressed = false
      let native = false

      const hold = () => {
        if (saved === null) {
          const style = document.body.style
          saved = [style.userSelect, style.webkitUserSelect]
          style.userSelect = "none"
          style.webkitUserSelect = "none"
        }
        const selection = document.getSelection()
        if (selection && selection.type === "Range") { selection.removeAllRanges() }
      }
      const release = () => {
        if (saved === null || pressed || native) { return }
        const style = document.body.style
        ;[style.userSelect, style.webkitUserSelect] = saved
        saved = null
      }

      const onDown = (event) => {
        if (event.button !== 0 || !startsDrag(event.composedPath())) { return }
        pressed = true
        hold()
      }
      const onUp = () => {
        pressed = false
        release()
      }
      // A native drag cancels the pointer as it starts, and ends with dragend or drop.
      const onCancel = () => {
        pressed = false
        if (!native) { release() }
      }
      // Not for dragging selected text or an image, which needs its selection.
      const onDragStart = (event) => {
        if (!startsDrag(event.composedPath())) { return }
        native = true
        hold()
      }
      const onDragEnd = () => {
        native = false
        pressed = false
        release()
      }
      const onBlur = () => {
        pressed = false
        native = false
        release()
      }

      const listeners = [
        [document, "pointerdown", onDown],
        [document, "pointerup", onUp],
        [document, "pointercancel", onCancel],
        [document, "dragstart", onDragStart],
        [document, "dragend", onDragEnd],
        [document, "drop", onDragEnd],
        [window, "blur", onBlur],
      ]
      for (const [target, type, listener] of listeners) { target.addEventListener(type, listener, true) }
      model.on("remove", () => {
        for (const [target, type, listener] of listeners) { target.removeEventListener(type, listener, true) }
        pressed = false
        native = false
        release()
      })
    }
    """.replace("__HANDLES__", json.dumps(HANDLES))

    _stylesheets: t.ClassVar[list[str]] = [":host { display: none !important; }"]

    def __init__(self, **params):
        params.setdefault("margin", 0)
        params.setdefault("width", 0)
        params.setdefault("height", 0)
        super().__init__(**params)
