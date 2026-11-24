"""A bar dragged to resize the panel docked beside or under it."""

from __future__ import annotations

import typing as t

import param
from panel.custom import JSComponent


class DockHandle(JSComponent):
    """A bar dragged to resize the layout holding it, which reports its new :attr:`size`.

    A horizontal bar sits on top of a layout docked under the page and sets
    its height; a vertical one sits on the inner edge of a layout docked to
    the side and sets its width: on the left of one docked to the right, or
    on the right of one docked to the left (``edge="end"``). While dragging, the layout follows the pointer
    in the page; :attr:`size` is set once the pointer is released. Arrow keys
    resize it by 24 pixels when the bar has focus.
    """

    size = param.Integer(
        default=None, allow_None=True, doc="The height or width the bar resized its layout to."
    )

    min_size = param.Integer(default=160)

    max_size = param.Integer(default=None, allow_None=True, doc="The largest size, if any.")

    label = param.String(default="Resize the code panel", doc="The bar's accessible name.")

    max_ratio = param.Number(
        default=0.8, doc="The largest share of the parent's height or width it may take."
    )

    orientation = param.Selector(
        default="horizontal",
        objects=["horizontal", "vertical"],
        doc="A horizontal bar resizes the height, a vertical one the width.",
    )

    edge = param.Selector(
        default="start",
        objects=["start", "end"],
        doc="""
        Which edge of its layout a vertical bar sits on: the left ("start"),
        for a layout docked to the right, or the right ("end"), for one
        docked to the left, which grows as the bar is dragged right.""",
    )

    _stylesheets: t.ClassVar[list[str]] = [
        """
        :host { cursor: row-resize; }
        :host(.fd-dock-vertical) { cursor: col-resize; }
        .fd-dock-handle { height: 7px; display: flex; align-items: center; justify-content: center;
          touch-action: none; outline: none; }
        .fd-dock-handle.vertical { height: 100%; width: 7px; }
        .fd-dock-handle::after { content: ""; width: 32px; height: 3px; border-radius: 2px;
          background: var(--mui-palette-divider, rgba(0, 0, 0, 0.2)); }
        .fd-dock-handle.vertical::after { width: 3px; height: 32px; }
        .fd-dock-handle:hover::after, .fd-dock-handle:focus-visible::after, .fd-dock-handle.dragging::after {
          background: var(--mui-palette-primary-main, #0072b5); }
        """
    ]

    _esm = """
    export function render({ el, model }) {
      const vertical = model.orientation === "vertical"
      // Dragging away from the layout's docked side grows it.
      const grow = vertical && model.edge === "end" ? 1 : -1
      const bar = document.createElement("div")
      bar.className = vertical ? "fd-dock-handle vertical" : "fd-dock-handle"
      bar.tabIndex = 0
      bar.setAttribute("role", "separator")
      // A separator's orientation is that of the line it draws, across the resize.
      bar.setAttribute("aria-orientation", vertical ? "vertical" : "horizontal")
      bar.setAttribute("aria-label", model.label)
      el.appendChild(bar)
      function dock() {
        const host = el.getRootNode().host
        return host && host.getRootNode().host
      }
      function extent(rect) {
        return vertical ? rect.width : rect.height
      }
      function highest(target) {
        // A panel docked to the side may take a share of the window; one docked
        // under the page a share of the layout holding it.
        let room = window.innerWidth
        if (!vertical) {
          const parent = target.parentElement || (target.getRootNode() && target.getRootNode().host)
          room = parent ? extent(parent.getBoundingClientRect()) : window.innerHeight
        }
        return Math.round(Math.min(room * model.max_ratio, model.max_size == null ? Infinity : model.max_size))
      }
      function clamp(size, target) {
        return Math.round(Math.max(model.min_size, Math.min(size, highest(target))))
      }
      function describe(size) {
        const target = dock()
        bar.setAttribute("aria-valuemin", String(model.min_size))
        if (target) bar.setAttribute("aria-valuemax", String(highest(target)))
        if (size != null) bar.setAttribute("aria-valuenow", String(Math.round(size)))
      }
      requestAnimationFrame(() => {
        const target = dock()
        describe(target ? extent(target.getBoundingClientRect()) : model.size)
      })
      function resize(target, size) {
        describe(size)
        if (vertical) {
          target.style.width = `${size}px`
          target.style.minWidth = `${size}px`
        } else {
          target.style.height = `${size}px`
        }
      }
      let start = null
      bar.addEventListener("pointerdown", (event) => {
        const target = dock()
        if (!target) return
        start = {
          at: vertical ? event.clientX : event.clientY,
          size: extent(target.getBoundingClientRect()),
          target,
        }
        bar.setPointerCapture(event.pointerId)
        bar.classList.add("dragging")
        event.preventDefault()
      })
      bar.addEventListener("pointermove", (event) => {
        if (!start) return
        const at = vertical ? event.clientX : event.clientY
        const size = clamp(start.size + grow * (at - start.at), start.target)
        resize(start.target, size)
        start.last = size
      })
      function end() {
        if (!start) return
        bar.classList.remove("dragging")
        if (start.last != null) model.size = start.last
        start = null
      }
      bar.addEventListener("pointerup", end)
      bar.addEventListener("pointercancel", end)
      bar.addEventListener("keydown", (event) => {
        const keys = vertical
          ? {ArrowLeft: -24 * grow, ArrowRight: 24 * grow}
          : {ArrowUp: 24, ArrowDown: -24}
        const step = keys[event.key]
        const target = dock()
        if (!step || !target) return
        event.preventDefault()
        const size = clamp(extent(target.getBoundingClientRect()) + step, target)
        resize(target, size)
        model.size = size
      })
    }
    """

    def __init__(self, **params):
        super().__init__(**params)
        if self.orientation == "vertical":
            self.css_classes = [*self.css_classes, "fd-dock-vertical"]
