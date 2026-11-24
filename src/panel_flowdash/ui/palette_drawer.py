"""The editor's component palette beside the canvas, which folds to a narrow strip like the sidecar.

The fold button sits beside the palette's search field; the folded strip
shows a button opening it again. Pressing ``/`` while it is folded opens it
and focuses the search field, as ``/`` focuses the field of the open palette.
Whether it is open is remembered for the browser with the editor's other
preferences, see :class:`~panel_flowdash.ui.split_view.SplitPreferences`.
"""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import JSComponent

from panel_flowdash.ui.sidecar import FOLDED_WIDTH

if t.TYPE_CHECKING:
    from collections.abc import Callable

    from panel_flowdash.ui.palette import ComponentPalette

FOLD_LABEL = "Hide components"
UNFOLD_LABEL = "Show components"

FOLDED_CLASS = "fd-palette-folded"


def fold_button() -> pmui.IconButton:
    """Return the button folding the palette, for the palette's header."""
    return pmui.IconButton(
        icon="keyboard_double_arrow_left",
        description=FOLD_LABEL,
        size="small",
        margin=(8, 4, 0, 0),
        align="end",
    )


class PaletteFoldBridge(JSComponent):
    """Gives the palette's fold buttons their ARIA state, moves the focus and opens it on ``/``.

    The buttons, the palette and its search field are found on the page by
    CSS classes ending in :attr:`uid`. An ``aria-controls`` id cannot reach
    across shadow roots, so the buttons reference the palette through
    ``ariaControlsElements`` where the browser supports it, which sets
    ``aria-controls`` to an empty value, and by its id elsewhere.
    """

    uid = param.String(default="", doc="The suffix of the CSS classes of the palette's parts.")

    collapsed = param.Boolean(default=False, doc="Whether the palette is folded.")

    focus_target = param.Selector(
        default=None,
        objects=[None, "search", "unfold"],
        doc="What takes the focus on the next focus request.",
    )

    focus_count = param.Integer(default=0, doc="Bumped to move the focus to the focus target.")

    label_count = param.Integer(
        default=0, doc="Bumped to label the buttons again, e.g. after they were rendered anew."
    )

    _esm = """
    function findHosts(root, cls, found = []) {
      for (const el of root.querySelectorAll("*")) {
        if (el.classList?.contains(cls)) { found.push(el) }
        if (el.shadowRoot) { findHosts(el.shadowRoot, cls, found) }
      }
      return found
    }

    // The one on screen where a part was rendered more than once, e.g. in the phone's sheet.
    function findHost(root, cls) {
      const hosts = findHosts(root, cls)
      return hosts.find((el) => el.getClientRects().length) ?? hosts[0] ?? null
    }

    function findInput(root) {
      if (!root) { return null }
      const input = root.querySelector("input")
      if (input) { return input }
      for (const el of root.querySelectorAll("*")) {
        const found = el.shadowRoot && findInput(el.shadowRoot)
        if (found) { return found }
      }
      return null
    }

    function editable(target) {
      const tag = target?.tagName
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable
    }

    export function render({ model, el }) {
      const host = (part) => findHost(document, `fd-palette-${part}-${model.uid}`)
      const button = (part) => host(part)?.shadowRoot?.querySelector("button") ?? null

      const buttons = (part) => findHosts(document, `fd-palette-${part}-${model.uid}`)
        .map((el) => el.shadowRoot?.querySelector("button")).filter(Boolean)

      const label = () => {
        const region = host("region")
        const fold = buttons("fold")
        const unfold = buttons("unfold")
        if (!region || !fold.length || !unfold.length) { return false }
        region.id = `fd-component-palette-${model.uid}`
        region.setAttribute("role", "region")
        region.setAttribute("aria-label", "Components")
        for (const b of [...fold, ...unfold]) {
          b.setAttribute("aria-expanded", String(!model.collapsed))
          if ("ariaControlsElements" in b) {
            b.ariaControlsElements = [region]
          } else {
            b.setAttribute("aria-controls", region.id)
          }
        }
        return true
      }

      // Labelled for a few seconds after each change, as the buttons can be rendered
      // anew after they were first found, e.g. when the palette moves back from the
      // phone's sheet.
      let labelling = null
      const relabel = () => {
        clearInterval(labelling)
        const started = performance.now()
        label()
        labelling = setInterval(() => {
          const elapsed = performance.now() - started
          if ((label() && elapsed > 3000) || elapsed > 20000) { clearInterval(labelling) }
        }, 100)
      }

      let focusing = null
      const focus = (target, started = performance.now()) => {
        cancelAnimationFrame(focusing)
        const node = target === "search"
          ? findInput(host("search")?.shadowRoot)
          : button("unfold")
        if (node && node.getClientRects().length) {
          node.focus()
          return
        }
        if (performance.now() - started < 5000) {
          focusing = requestAnimationFrame(() => focus(target, started))
        }
      }

      const onKey = (event) => {
        if (event.key !== "/" || !model.collapsed || event.ctrlKey || event.metaKey || event.altKey) {
          return
        }
        // Only where the folded palette is on screen, e.g. not in the Layout tab.
        if (editable(event.composedPath()[0]) || !el.getClientRects().length) { return }
        event.preventDefault()
        model.send_msg({ type: "open" })
      }

      model.on("collapsed", () => relabel())
      model.on("label_count", () => relabel())
      model.on("focus_count", () => {
        relabel()
        if (model.focus_target) { focus(model.focus_target) }
      })
      document.addEventListener("keydown", onKey)
      model.on("remove", () => {
        clearInterval(labelling)
        cancelAnimationFrame(focusing)
        document.removeEventListener("keydown", onKey)
      })
      relabel()
    }
    """

    def __init__(self, on_open: Callable[[], None] | None = None, **params):
        params.setdefault("width", 0)
        params.setdefault("height", 0)
        params.setdefault("margin", 0)
        super().__init__(**params)
        self._on_open = on_open

    def _handle_msg(self, msg):
        if isinstance(msg, dict) and msg.get("type") == "open" and self._on_open is not None:
            self._on_open()

    def request_focus(self, target: str):
        """Move the focus to *target*, ``"search"`` or ``"unfold"``, once it is on screen."""
        self.param.update(focus_target=target, focus_count=self.focus_count + 1)


class PaletteDrawer(param.Parameterized):
    """The component palette in its column beside the canvas, open or folded to a strip.

    :attr:`panel` is the column. *fold_button*, made by :func:`fold_button`,
    sits in the palette's header. On a phone the palette moves to a bottom
    sheet (see :meth:`detach`), where it does not fold.
    """

    open = param.Boolean(default=True, doc="Whether the palette is open rather than folded.")

    def __init__(
        self,
        palette: ComponentPalette,
        *,
        width: int,
        fold_button: pmui.IconButton,
        **params,
    ):
        super().__init__(**params)
        self.palette = palette
        self.width = width
        uid = f"{id(self):x}"
        self.fold_button = fold_button
        fold_button.css_classes = [*fold_button.css_classes, f"fd-palette-fold-{uid}"]
        self.unfold_button = pmui.IconButton(
            icon="keyboard_double_arrow_right",
            description=UNFOLD_LABEL,
            size="small",
            margin=(8, 4),
            visible=False,
            css_classes=[f"fd-palette-unfold-{uid}"],
        )
        self._region = region = palette.__panel__()
        region.css_classes = [*region.css_classes, f"fd-palette-region-{uid}"]
        palette.search.css_classes = [*palette.search.css_classes, f"fd-palette-search-{uid}"]
        self._attached = True
        self.bridge = PaletteFoldBridge(uid=uid, on_open=lambda: self.toggle(True, focus=True))
        fold_button.on_click(lambda _event: self.toggle(False, focus=True))
        self.unfold_button.on_click(lambda _event: self.toggle(True, focus=True))
        self.panel = pn.Column(
            region,
            self.unfold_button,
            self.bridge,
            width=width,
            sizing_mode="stretch_height",
            margin=0,
            styles={"border-right": "1px solid var(--mui-palette-divider, #e0e0e0)"},
            # Folded by a class, so the palette's own visibility stays the sheet's to use.
            stylesheets=[
                f":host(.{FOLDED_CLASS}) .fd-palette-region-{uid} {{ display: none !important; }}",
            ],
        )
        self._sync()

    def toggle(self, open_: bool, focus: bool = False):
        """Open or fold the palette; with *focus*, move the focus to what is shown in its place."""
        self.open = open_
        if focus:
            self.bridge.request_focus("search" if open_ else "unfold")

    @pn.io.hold()
    @param.depends("open", watch=True)
    def _sync(self):
        self.panel.width = self.width if self.open else FOLDED_WIDTH
        classes = [c for c in self.panel.css_classes if c != FOLDED_CLASS]
        self.panel.css_classes = classes if self.open else [*classes, FOLDED_CLASS]
        self.unfold_button.visible = self._attached and not self.open
        self.bridge.collapsed = not self.open

    def detach(self):
        """Take the palette out of the column, to show it in the phone's sheet."""
        self._attached = False
        self.panel[:] = [self.unfold_button, self.bridge]
        self._sync()

    def attach(self):
        """Put the palette back in the column after the phone layout."""
        self._attached = True
        self.panel[:] = [self._region, self.unfold_button, self.bridge]
        self._sync()
        self.bridge.label_count += 1
