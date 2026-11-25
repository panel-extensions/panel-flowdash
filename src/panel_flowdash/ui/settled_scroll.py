"""Scrolls an element into view once it stops moving, e.g. a tile the grid is still placing."""

from __future__ import annotations

from panel.custom import JSComponent


class SettledScroll(JSComponent):
    """Scrolls the first element carrying a CSS class into view once it has settled.

    pmui's ``Reveal`` scrolls as soon as the element exists. A tile just added
    to the grid is still moving then: the grid animates it into place, and a
    drawer opening beside the grid narrows it and lays the tiles out again.
    This waits until the element has kept its place for a few checks, for at
    most ``timeout`` milliseconds, and then scrolls it into view. The page's
    shadow roots are searched until the element is found, and after that
    only its box is read. It renders nothing itself, but must be on the page.
    """

    _esm = """
function collect(root, cls, found) {
  for (const el of root.querySelectorAll("*")) {
    if (el.classList.contains(cls)) {
      found.push(el)
    }
    if (el.shadowRoot) {
      collect(el.shadowRoot, cls, found)
    }
  }
  return found
}

const STEP = 100
const STILL = 3

export function render({model}) {
  model.on("msg:custom", (msg) => {
    const start = performance.now()
    let last = null
    let still = 0
    let el = null
    const step = () => {
      // Looked up across the shadow roots until found, then only measured.
      if (!el || !el.isConnected || !el.classList.contains(msg.css_class)) {
        el = collect(document, msg.css_class, [])[0] || null
      }
      if (el) {
        const box = el.getBoundingClientRect()
        const place = [box.left, box.top, box.width, box.height].map(Math.round).join()
        still = place === last && box.height > 0 ? still + 1 : 0
        last = place
      }
      const late = performance.now() - start > msg.timeout
      if (el && (still >= STILL || late)) {
        const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
        el.scrollIntoView({behavior: reduce ? "auto" : "smooth", block: "nearest"})
      } else if (!late) {
        setTimeout(step, STEP)
      }
    }
    step()
  })
  const span = document.createElement("span")
  span.style.display = "none"
  return span
}
"""

    def scroll_to(self, css_class: str, timeout: int = 3000):
        """Scroll the first element carrying *css_class* into view once it has settled."""
        self._send_msg({"css_class": css_class, "timeout": timeout})
