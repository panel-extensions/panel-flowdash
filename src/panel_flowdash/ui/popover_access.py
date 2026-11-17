"""Keyboard and screen reader support for a button that opens a ``pmui.Popup``."""

from __future__ import annotations

import panel_material_ui as pmui
import param
from panel.custom import JSComponent


class PopoverAccess(JSComponent):
    """Make a button and the ``pmui.Popup`` it opens behave as a non-modal dialog.

    Place it in the same layout as the button's view (marked with
    ``button_class``) and the popup (marked with ``popup_class``), and link
    :attr:`open` to the popup's. The button then carries ``aria-haspopup``
    and ``aria-expanded``, and the popup's paper is a dialog named
    :attr:`label`. Escape, moving the focus out of the paper and pressing
    outside it close the popup, and focus returns to the button when it was
    inside. The popup should hide its backdrop and let pointer events
    through its root, so the page around it, the button included, stays usable.

    Material UI's own focus handling cannot follow focus into shadow roots,
    which is why this is done here.
    """

    button_class = param.String(doc="CSS class of the button's view.")

    popup_class = param.String(doc="CSS class of the popup's view.")

    label = param.String(doc="Accessible name of the dialog.")

    open = param.Boolean(default=False, doc="Whether the popup is open.")

    _esm = """
    const PAPER = ".MuiPopover-paper"

    function deepFind(root, selector) {
      if (root == null) { return null }
      const found = root.querySelector(selector)
      if (found) { return found }
      for (const el of root.querySelectorAll("*")) {
        if (el.shadowRoot) {
          const inner = deepFind(el.shadowRoot, selector)
          if (inner) { return inner }
        }
      }
      return null
    }

    function within(node, ancestor) {
      for (; node; node = node.parentNode || node.host) {
        if (node === ancestor) { return true }
      }
      return false
    }

    function deepActive() {
      let el = document.activeElement
      while (el && el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement
      }
      return el
    }

    export function render({model, view}) {
      const sibling = (cls) => (view.parent?.child_views ?? []).find(
        (v) => v.el.classList.contains(cls)
      )
      const button = () => deepFind(sibling(model.button_class)?.el.shadowRoot, "button")
      const paper = () => {
        const popup = sibling(model.popup_class)
        return popup ? deepFind(popup.el.shadowRoot ?? popup.el, PAPER) : null
      }
      let current = null
      let focusInside = false

      const close = () => { model.open = false }

      const restoreFocus = (el) => {
        // Material UI moves the focus once the closing transition ends.
        const target = button()
        if (target == null) { return }
        target.focus()
        let tries = 0
        const again = () => {
          if (el.isConnected && tries++ < 40) { setTimeout(again, 25); return }
          const active = deepActive()
          if (active == null || active === document.body || !active.isConnected || within(active, el)) {
            target.focus()
          }
        }
        setTimeout(again, 25)
      }

      const onKeyDown = (event) => {
        if (event.key === "Escape") {
          event.preventDefault()
          event.stopPropagation()
          close()
        }
      }
      const onFocusIn = () => { focusInside = true }
      const onFocusOut = () => {
        // The related target is retargeted to this shadow tree, so the
        // element that took the focus is only known once it has it.
        setTimeout(() => {
          const el = current
          const next = deepActive()
          const target = button()
          // Focus leaving the window keeps the popup open, and a click on the
          // button toggles it through its own handler.
          if (el == null || next == null || next === document.body || within(next, el)) { return }
          if (target != null && within(next, target)) { return }
          close()
        })
      }
      const onPointerDown = (event) => {
        if (current == null) { return }
        const path = event.composedPath()
        const target = button()
        // A press on the button toggles the popup through its own click.
        if (!path.includes(current) && !(target && path.includes(target))) {
          focusInside = false
          close()
        }
      }

      const attach = (el) => {
        current = el
        focusInside = false
        el.setAttribute("role", "dialog")
        el.setAttribute("aria-label", model.label)
        el.addEventListener("keydown", onKeyDown)
        el.addEventListener("focusin", onFocusIn)
        el.addEventListener("focusout", onFocusOut)
        document.addEventListener("pointerdown", onPointerDown, true)
        if (!within(deepActive(), el)) { el.focus() }
      }

      const detach = () => {
        const el = current
        if (el == null) { return }
        current = null
        el.removeEventListener("keydown", onKeyDown)
        el.removeEventListener("focusin", onFocusIn)
        el.removeEventListener("focusout", onFocusOut)
        document.removeEventListener("pointerdown", onPointerDown, true)
        if (focusInside) { restoreFocus(el) }
      }

      const sync = (attempt = 0) => {
        const target = button()
        if (target) {
          target.setAttribute("aria-haspopup", "dialog")
          target.setAttribute("aria-expanded", String(model.open))
        }
        let pending = target == null
        if (!model.open) {
          detach()
        } else {
          const el = paper()
          if (el == null) {
            pending = true
          } else if (el !== current) {
            detach()
            attach(el)
          }
        }
        if (pending && attempt < 40) { requestAnimationFrame(() => sync(attempt + 1)) }
      }

      model.on("open", () => sync())
      model.on("after_render", () => sync())
      model.on("remove", () => { model.open = false; detach() })
      const el = document.createElement("span")
      el.style.display = "none"
      return el
    }
    """


# The popup's root covers the window; with no backdrop, it lets the pointer
# through to the page and only its paper takes it.
NON_MODAL_POPUP_SX = {"pointerEvents": "none", "& .MuiPopover-paper": {"pointerEvents": "auto"}}


def link_popup(popup: pmui.Popup, access: PopoverAccess):
    """Keep *access* and *popup* open together."""
    popup.link(access, open="open", bidirectional=True)
