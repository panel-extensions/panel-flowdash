"""Full screen for a whole dashboard: the browser hook and the overlay shown instead of the bar."""

from __future__ import annotations

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent

#: Clicking an element with this class toggles full screen in the browser.
TOGGLE_CLASS = "fd-fullscreen-toggle"
#: Clicking an element with this class leaves the presentation layout.
EXIT_CLASS = "fd-fullscreen-exit"
#: The overlay with the title, health and exit button.
OVERLAY_CLASS = "fd-fullscreen-overlay"
#: Set on the editor while it shows the presentation layout.
PRESENTING_CLASS = "fd-presenting"
#: Dispatched on the document to toggle full screen, e.g. by the F shortcut.
TOGGLE_EVENT = "flowdash:fullscreen"

# Applied to the editor's root column, they lay the dashboard over the page so
# the app's chrome is covered. Real full screen is requested for the whole
# page rather than the editor, so what the page shows above the dashboard,
# like notifications, stays visible there as it does in the window.
PRESENTATION_CSS = f"""
:host(.{PRESENTING_CLASS}) {{
  position: fixed !important;
  inset: 0 !important;
  width: 100vw !important;
  height: 100vh !important;
  height: 100dvh !important;
  max-width: none !important;
  margin: 0 !important;
  z-index: 1250;
  background: var(--mui-palette-background-default);
}}
.{OVERLAY_CLASS} {{
  position: fixed !important;
  right: 16px;
  bottom: 16px;
  z-index: 10;
  transition: opacity 0.2s;
}}
.{OVERLAY_CLASS}:not([data-shown]):not(:focus-within) {{
  opacity: 0;
  pointer-events: none;
}}
"""


class FullscreenController(JSComponent):
    """Puts the layout it is placed in into the browser's full screen and back.

    ``active`` is the presentation layout the editor shows, which covers the
    browser window. Turning it on also requests full screen for the page,
    turning it off leaves full screen, and leaving full screen in the browser
    (Esc, the browser's own control) turns it off again. Browsers only grant full screen
    for a user gesture, so clicks on elements with :data:`TOGGLE_CLASS` or
    :data:`EXIT_CLASS` inside the parent, and the :data:`TOGGLE_EVENT` on the
    document, are handled here in the browser rather than on the server. When
    full screen is refused or unsupported ``active`` stays on and the layout
    covers the browser window instead, which is also how ``?kiosk=1`` opens.

    While active the overlay (the element with :data:`OVERLAY_CLASS`) is shown
    after the pointer moves, a tap or a key press and hidden, with the
    pointer, once they rest. The page behind the layout is inert, so focus,
    clicks and screen readers only reach the layout and the dialogs, menus,
    palette and notifications it opens. With several
    editors on a page, the :data:`TOGGLE_EVENT` toggles the one holding the
    focus, else the one presenting, else the first.
    """

    active = param.Boolean(default=False, doc="Whether the presentation layout is shown.")

    enabled = param.Boolean(
        default=True, doc="Whether full screen may be entered, e.g. not in edit mode."
    )

    native = param.Boolean(
        default=False, doc="Set by the browser: whether the layout is really in full screen."
    )

    supported = param.Boolean(
        default=False, doc="Set by the browser: whether it offers full screen for elements."
    )

    overlay_label = param.String(
        default="",
        doc="""
        The accessible name of the overlay, e.g. the dashboard's title, which
        the overlay does not show.""",
    )

    toggle_class = param.String(
        default="",
        doc="""
        A class marking toggles of this layout that sit outside it, e.g. the
        dashboard's Full screen button in an app's top bar.""",
    )

    _esm = f"""
    const IDLE_MS = 2500

    // Every controller on the page, in the order they were rendered.
    const CONTROLLERS = (globalThis.__flowdashFullscreen ??= [])

    function typing(target) {{
      const tag = target?.tagName
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable
    }}

    // Dialogs, menus and popovers the dashboard opens may be rendered outside it.
    function inOverlay(path) {{
      return path.some((node) => node instanceof Element && (
        node.getAttribute("role") === "dialog" || node.getAttribute("aria-modal") === "true" ||
        node.getAttribute("role") === "presentation" || node.hasAttribute("popover") ||
        node.classList.contains("MuiPopover-root") || node.classList.contains("MuiPopper-root")
      ))
    }}

    // Never made inert while presenting: what the layout opens lives outside it.
    const LIVE_TAGS = new Set(["SCRIPT", "STYLE", "LINK", "HEAD", "TEMPLATE"])
    const LIVE_SELECTORS = [
      ".dialog", ".notification-area", "#notistack-container", ".flowdash-command-palette", ".MuiModal-root",
    ]

    // A node and, for Panel's wrappers, the component elements directly inside it.
    function hosts(node) {{
      return [node, ...node.children].filter((el) => el.shadowRoot)
    }}

    // The focused element, which may sit in shadow roots, and its ancestors.
    function focusPath() {{
      const path = []
      let node = document.activeElement
      while (node?.shadowRoot?.activeElement) {{
        node = node.shadowRoot.activeElement
      }}
      while (node) {{
        path.push(node)
        node = node.parentNode ?? node.host
      }}
      return path
    }}

    export function render({{model, view}}) {{
      const target = () => view.parent?.el
      const page = document.documentElement
      model.supported = !!(document.fullscreenEnabled && page.requestFullscreen)

      // Whether this controller put the page into full screen.
      let owned = false
      const isNative = () => owned && document.fullscreenElement === page

      let pending = false
      function requestNative() {{
        if (!model.supported || pending || document.fullscreenElement != null) {{
          return
        }}
        // Without a gesture (a ?kiosk=1 link) the browser refuses, so the
        // layout stays in the window rather than logging a refused request.
        if (navigator.userActivation && !navigator.userActivation.isActive) {{
          return
        }}
        pending = true
        page.requestFullscreen({{navigationUI: "hide"}})
          .then(() => {{ owned = true; onChange() }})
          .catch(() => {{}})
          .finally(() => {{ pending = false }})
      }}
      function exitNative() {{
        if (isNative()) {{
          document.exitFullscreen().catch(() => {{}})
        }}
      }}
      function toggle() {{
        if (!model.enabled) {{
          return
        }}
        if (isNative()) {{
          model.active = false
          exitNative()
        }} else if (model.active && model.supported) {{
          requestNative()
        }} else {{
          model.active = !model.active
          if (model.active) {{
            requestNative()
          }}
        }}
      }}
      function leave() {{
        model.active = false
        exitNative()
      }}

      let timer = null
      const overlay = () => target()?.shadowRoot?.querySelector(".{OVERLAY_CLASS}")
      function name() {{
        const box = overlay()
        if (box) {{
          box.setAttribute("role", "region")
          box.setAttribute("aria-label", model.overlay_label)
        }}
      }}
      function rest() {{
        const el = target()
        const box = overlay()
        if (box?.matches(":hover")) {{
          timer = setTimeout(rest, IDLE_MS)
          return
        }}
        box?.removeAttribute("data-shown")
        if (el && model.active) {{
          el.style.cursor = "none"
        }}
      }}
      function show() {{
        clearTimeout(timer)
        name()
        const el = target()
        if (el) {{
          el.style.cursor = ""
        }}
        if (!model.active) {{
          overlay()?.removeAttribute("data-shown")
          return
        }}
        overlay()?.setAttribute("data-shown", "")
        timer = setTimeout(rest, IDLE_MS)
      }}

      let wasNative = false
      const onChange = () => {{
        if (document.fullscreenElement !== page) {{
          owned = false
        }}
        const now = isNative()
        if (model.native !== now) {{
          model.native = now
        }}
        if (wasNative && !now && model.active) {{
          model.active = false
        }}
        wasNative = now
        show()
      }}
      const onClick = (event) => {{
        const el = target()
        const path = event.composedPath()
        const own = model.toggle_class
        if (own && path.some((node) => node instanceof Element && node.classList.contains(own))) {{
          toggle()
          return
        }}
        if (!el || !path.includes(el)) {{
          return
        }}
        for (const node of path) {{
          if (node === el) {{
            return
          }}
          if (!(node instanceof Element)) {{
            continue
          }}
          if (node.classList.contains("{EXIT_CLASS}")) {{
            leave()
            return
          }}
          if (node.classList.contains("{TOGGLE_CLASS}")) {{
            toggle()
            return
          }}
        }}
      }}
      const self = {{model, target}}
      CONTROLLERS.push(self)
      function chosen() {{
        const live = CONTROLLERS.filter((c) => c.target()?.isConnected && c.model.enabled)
        const path = focusPath()
        return (
          live.find((c) => path.includes(c.target())) ??
          live.find((c) => c.model.active) ??
          live[0]
        )
      }}
      const onRequest = (event) => {{
        if (!event.defaultPrevented && chosen() === self) {{
          event.preventDefault()
          toggle()
        }}
      }}
      const onKey = (event) => {{
        // The browser leaves real full screen on Esc itself; the in-window layout follows it.
        if (event.key !== "Escape" || !model.active || isNative() || event.defaultPrevented) {{
          return
        }}
        const path = event.composedPath()
        const inDialog = path.some((node) => node instanceof Element && (
          node.getAttribute("role") === "dialog" || node.getAttribute("aria-modal") === "true"
        ))
        if (!inDialog && !typing(path[0])) {{
          leave()
        }}
      }}
      const onMove = () => {{
        if (model.active) {{
          show()
        }}
      }}
      // While presenting, everything outside the layout is inert: it takes no
      // focus or clicks and is hidden from screen readers. That is every sibling
      // at each level from the layout up to the body, across shadow roots.
      // Portals, the palette, dialogs and notifications stay live; siblings
      // rendered later, e.g. a header that is re-rendered, are made inert too.
      let inerted = []
      let observers = []
      function inertable(node) {{
        if (!(node instanceof HTMLElement) || node.inert || LIVE_TAGS.has(node.tagName)) {{
          return false
        }}
        return !inOverlay([node]) && !LIVE_SELECTORS.some((sel) => (
          node.matches(sel) || hosts(node).some((host) => [...host.shadowRoot.children].some((el) => el.matches(sel)))
        ))
      }}
      function makeInert(node) {{
        if (inertable(node)) {{
          node.inert = true
          inerted.push(node)
        }}
      }}
      function contain() {{
        release()
        let node = target()
        while (node && node !== document.body) {{
          const parent = node.parentNode
          if (parent == null) {{
            break
          }}
          const keep = node
          for (const sibling of parent.children) {{
            if (sibling !== keep) {{
              makeInert(sibling)
            }}
          }}
          const observer = new MutationObserver((records) => {{
            for (const record of records) {{
              record.addedNodes.forEach((added) => added !== keep && makeInert(added))
            }}
          }})
          observer.observe(parent, {{childList: true}})
          observers.push(observer)
          node = parent instanceof ShadowRoot ? parent.host : parent
        }}
      }}
      function release() {{
        observers.forEach((observer) => observer.disconnect())
        inerted.forEach((node) => {{ node.inert = false }})
        observers = []
        inerted = []
      }}
      const sync = () => (model.active && target() ? contain() : release())

      document.addEventListener("fullscreenchange", onChange)
      document.addEventListener("click", onClick, true)
      document.addEventListener("{TOGGLE_EVENT}", onRequest)
      document.addEventListener("keydown", onKey)
      document.addEventListener("pointermove", onMove)
      document.addEventListener("pointerdown", onMove, true)
      document.addEventListener("keydown", onMove, true)
      model.on("active", () => {{
        if (model.active) {{
          requestNative()
        }} else {{
          exitNative()
        }}
        sync()
        show()
      }})
      model.on("overlay_label", name)
      model.on("remove", () => {{
        clearTimeout(timer)
        document.removeEventListener("fullscreenchange", onChange)
        document.removeEventListener("click", onClick, true)
        document.removeEventListener("{TOGGLE_EVENT}", onRequest)
        document.removeEventListener("keydown", onKey)
        document.removeEventListener("pointermove", onMove)
        document.removeEventListener("pointerdown", onMove, true)
        document.removeEventListener("keydown", onMove, true)
        release()
        const index = CONTROLLERS.indexOf(self)
        if (index >= 0) {{
          CONTROLLERS.splice(index, 1)
        }}
      }})
      if (model.active) {{
        // Opened in the presentation layout; no gesture, so no real full screen.
        setTimeout(() => {{ sync(); show() }})
      }}
      const el = document.createElement("span")
      el.style.display = "none"
      return el
    }}
    """


class PresentationOverlay(pn.Row):
    """The overlay shown in full screen: the dashboard's health and an exit button.

    *health_chip* is the dashboard bar's health chip, whose text and style the
    overlay follows. The overlay holds its items only while it is shown, so the
    page never carries a hidden copy of the bar's text. It shows no title; the
    :class:`FullscreenController` names it for screen readers instead.
    """

    def __init__(self, health_chip, **params):
        self._items = _overlay_items(health_chip)
        super().__init__(
            css_classes=[OVERLAY_CLASS],
            margin=0,
            visible=False,
            styles={
                "align-items": "center",
                "padding": "4px",
                "border-radius": "20px",
                "border": "1px solid var(--mui-palette-divider)",
                "background": "var(--mui-palette-background-paper)",
                "box-shadow": "0 4px 16px rgba(0, 0, 0, 0.18)",
            },
            **params,
        )

    def show(self, shown: bool):
        """Show the overlay with its items, or hide it and drop them from the page."""
        with pn.io.hold():
            self[:] = self._items if shown else []
            self.visible = shown


# The chip's style is the bar's button style; laid out as a flex row like the
# button, its dot and text sit on one line inside one rounded background.
_CHIP_CSS = ".MuiTypography-root { display: inline-flex; align-items: center; } p { margin: 0; }"


def _overlay_items(health_chip) -> list:
    chip = pmui.Typography(
        health_chip.param.label,
        variant="caption",
        # As far from the edge as from the top and bottom, so the pills nest.
        margin=(0, 4, 0, 2),
        align="center",
        sx=health_chip.param.sx,
        stylesheets=[_CHIP_CSS],
    )
    exit_button = pmui.IconButton(
        icon="fullscreen_exit",
        description="Exit full screen",
        size="small",
        margin=0,
        align="center",
        css_classes=[EXIT_CLASS],
    )
    return [chip, exit_button]
