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

# Applied to the editor's root column. Real full screen already makes the
# element fill the screen; without it (no gesture, or no Fullscreen API) the
# same rules lay the dashboard over the page so the app's chrome is covered.
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

    ``active`` is the presentation layout the editor shows. Turning it on
    requests full screen for the parent element (the editor), turning it off
    leaves full screen, and leaving full screen in the browser (Esc, the
    browser's own control) turns it off again. Browsers only grant full screen
    for a user gesture, so clicks on elements with :data:`TOGGLE_CLASS` or
    :data:`EXIT_CLASS` inside the parent, and the :data:`TOGGLE_EVENT` on the
    document, are handled here in the browser rather than on the server. When
    full screen is refused or unsupported ``active`` stays on and the layout
    covers the browser window instead, which is also how ``?kiosk=1`` opens.

    While active the overlay (the element with :data:`OVERLAY_CLASS`) is shown
    after the pointer moves and hidden, with the pointer, once it rests.
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

    toggle_class = param.String(
        default="",
        doc="""
        A class marking toggles of this layout that sit outside it, e.g. the
        dashboard's Full screen button in an app's top bar.""",
    )

    _esm = f"""
    const IDLE_MS = 2500

    function typing(target) {{
      const tag = target?.tagName
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable
    }}

    export function render({{model, view}}) {{
      const target = () => view.parent?.el
      const isNative = () => {{
        const el = target()
        return !!el && document.fullscreenElement != null && el.matches(":fullscreen")
      }}
      model.supported = !!(document.fullscreenEnabled && Element.prototype.requestFullscreen)

      let pending = false
      function requestNative() {{
        const el = target()
        if (!model.supported || !el || pending || isNative()) {{
          return
        }}
        // Without a gesture (a ?kiosk=1 link) the browser refuses, so the
        // layout stays in the window rather than logging a refused request.
        if (navigator.userActivation && !navigator.userActivation.isActive) {{
          return
        }}
        pending = true
        el.requestFullscreen({{navigationUI: "hide"}}).catch(() => {{}}).finally(() => {{ pending = false }})
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
      const onRequest = (event) => {{
        if (!event.defaultPrevented && target()?.isConnected && model.enabled) {{
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

      document.addEventListener("fullscreenchange", onChange)
      document.addEventListener("click", onClick, true)
      document.addEventListener("{TOGGLE_EVENT}", onRequest)
      document.addEventListener("keydown", onKey)
      document.addEventListener("pointermove", onMove)
      model.on("active", () => {{
        if (model.active) {{
          requestNative()
        }} else {{
          exitNative()
        }}
        show()
      }})
      model.on("remove", () => {{
        clearTimeout(timer)
        document.removeEventListener("fullscreenchange", onChange)
        document.removeEventListener("click", onClick, true)
        document.removeEventListener("{TOGGLE_EVENT}", onRequest)
        document.removeEventListener("keydown", onKey)
        document.removeEventListener("pointermove", onMove)
      }})
      if (model.active) {{
        // Opened in the presentation layout; no gesture, so no real full screen.
        setTimeout(show)
      }}
      const el = document.createElement("span")
      el.style.display = "none"
      return el
    }}
    """


class PresentationOverlay(pn.Row):
    """The overlay shown in full screen: the dashboard's title, its health and an exit button.

    *title* is the dashboard bar's title and *health_chip* its health chip,
    whose text and style the overlay follows. The overlay holds its items only while it
    is shown, so the page never carries a hidden copy of the bar's text.
    """

    def __init__(self, title, health_chip, **params):
        self._items = _overlay_items(title, health_chip)
        super().__init__(
            css_classes=[OVERLAY_CLASS],
            margin=0,
            visible=False,
            styles={
                "align-items": "center",
                "padding": "4px 4px 4px 8px",
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


def _overlay_items(title, health_chip) -> list:
    name = pmui.Typography(
        title.param.object,
        variant="subtitle1",
        margin=(0, 4, 0, 8),
        align="center",
        sx={
            "fontWeight": 500,
            "maxWidth": "40vw",
            "whiteSpace": "nowrap",
            "overflow": "hidden",
            "textOverflow": "ellipsis",
        },
    )
    chip = pmui.Typography(
        health_chip.param.label,
        variant="caption",
        margin=(0, 4),
        align="center",
        sx=health_chip.param.sx,
    )
    exit_button = pmui.IconButton(
        icon="fullscreen_exit",
        description="Exit full screen",
        size="small",
        margin=0,
        align="center",
        css_classes=[EXIT_CLASS],
    )
    return [name, chip, exit_button]
