"""The Split tab of the editor: the wiring canvas and the tile grid side by side.

The two panes sit in a panel-splitjs ``HSplit``. Dragging the gutter resizes
them, and the sizes are remembered for the browser in local storage, together
with whether the editor was last in the Split tab and where the code panel
was docked. Collapsing a pane, with the
gutter's buttons or by dragging it to the edge, falls back to the tab of the
pane that is left: collapsing the grid leaves the Dataflow tab, collapsing the
canvas the Layout tab, and the split keeps its last sizes for next time.

:class:`SplitEvents` reports when the canvas pane changed size, so the canvas
can fit its view to the new width.
"""

from __future__ import annotations

import json
import math
import typing as t

import panel as pn
import param
from panel.custom import JSComponent
from panel.viewable import Viewer
from panel_splitjs import HSplit

from panel_flowdash.ui.sidecar import SIDECAR_MAX_WIDTH, SIDECAR_MIN_WIDTH, SIDECAR_WIDTH

STORAGE_KEY = "panel-flowdash.editor.split"

# panel-splitjs draws the divider in --panel-border-color, which stays light in the dark theme.
GUTTER_CSS = """
.gutter > .divider { --color: var(--mui-palette-divider, var(--panel-border-color)); }
.gutter::after { background-color: var(--mui-palette-text-disabled, var(--panel-border-color)); }
"""


def storage_key() -> str:
    """Return the local storage key for the app being served.

    Apps served from one origin share local storage, so the key carries the
    app's path, except for an app at the root, which keeps the plain key.
    """
    try:
        app = (pn.state.app_url or "").strip("/")
    except AttributeError:
        # A document with a partial session context, e.g. one made up in tests.
        app = ""
    return f"{STORAGE_KEY}:/{app}" if app else STORAGE_KEY


DEFAULT_SIZES = (55.0, 45.0)

#: The width of the Ask panel docked left of a dashboard, until the person drags it.
ASK_WIDTH = 420

#: The narrowest the Ask panel and the assistant's drawer, Ask in edit mode, get.
ASK_MIN_WIDTH = 320

#: The widest share of the window the Ask panel and the assistant's drawer get.
ASK_MAX_RATIO = 0.5

#: The width of the assistant's drawer until the person drags its edge.
ASSISTANT_WIDTH = 360

# Narrower than this, a pane counts as collapsed; split.js reports a collapsed
# pane at 0 to 1 percent and a pane dragged into its snap zone at under 5.
_COLLAPSED = 5.0


class SplitPreferences(param.Parameterized):
    """What the editor remembers about the Split tab for a browser."""

    split = param.Boolean(default=False, doc="Whether the editor was last in the Split tab.")

    sizes = param.NumericTuple(
        default=DEFAULT_SIZES,
        length=2,
        doc="Widths of the canvas and the grid pane in percent of the split.",
    )

    code_dock = param.Selector(
        default="bottom",
        objects=["bottom", "sidecar"],
        doc="Where the code panel is docked: under the canvas or in the sidecar's Spec tab.",
    )

    spec_view = param.Selector(
        default="form",
        objects=["form", "yaml", "json"],
        doc="The view of a spec in the code panel: its form, or its text as YAML or JSON.",
    )

    spec_text = param.Selector(
        default="yaml",
        objects=["yaml", "json"],
        doc="The syntax of a spec's text in the code panel.",
    )

    sidecar_width = param.Integer(
        default=SIDECAR_WIDTH,
        bounds=(SIDECAR_MIN_WIDTH, SIDECAR_MAX_WIDTH),
        doc="The width of the editor's sidecar, set by dragging its left edge.",
    )

    palette_open = param.Boolean(
        default=True, doc="Whether the component palette beside the canvas is open or folded."
    )

    ask_width = param.Integer(
        default=ASK_WIDTH,
        bounds=(ASK_MIN_WIDTH, None),
        doc="The width of the Ask panel docked left of a dashboard, set by dragging its right edge.",
    )

    assistant_width = param.Integer(
        default=ASSISTANT_WIDTH,
        bounds=(ASK_MIN_WIDTH, None),
        doc="The width of the assistant's drawer, Ask in edit mode, set by dragging its left edge.",
    )

    def to_json(self) -> str:
        """Return the preferences as stored in the browser."""
        return json.dumps(
            {
                "split": self.split,
                "sizes": list(self.sizes),
                "code_dock": self.code_dock,
                "spec_view": self.spec_view,
                "spec_text": self.spec_text,
                "sidecar_width": self.sidecar_width,
                "palette_open": self.palette_open,
                "ask_width": self.ask_width,
                "assistant_width": self.assistant_width,
            }
        )

    def load(self, stored: t.Any) -> bool:
        """Apply preferences read back from the browser, returning whether any applied.

        Anything malformed, e.g. written by another version, is ignored rather
        than raising, so a bad entry can never keep the editor from loading.
        """
        if isinstance(stored, str):
            try:
                stored = json.loads(stored)
            except ValueError:
                return False
        if not isinstance(stored, dict):
            return False
        update: dict[str, t.Any] = {}
        if isinstance(stored.get("split"), bool):
            update["split"] = stored["split"]
        sizes = valid_sizes(stored.get("sizes"))
        if sizes is not None:
            update["sizes"] = sizes
        dock = stored.get("code_dock")
        if dock in ("bottom", "sidecar", "right"):
            # Docked to the right of the canvas before the sidecar had a Spec tab.
            update["code_dock"] = "sidecar" if dock == "right" else dock
        if stored.get("spec_view") in self.param.spec_view.objects:
            update["spec_view"] = stored["spec_view"]
        if stored.get("spec_text") in self.param.spec_text.objects:
            update["spec_text"] = stored["spec_text"]
        width = stored.get("sidecar_width")
        if isinstance(width, int) and not isinstance(width, bool):
            update["sidecar_width"] = max(SIDECAR_MIN_WIDTH, min(SIDECAR_MAX_WIDTH, width))
        if isinstance(stored.get("palette_open"), bool):
            update["palette_open"] = stored["palette_open"]
        width = stored.get("ask_width")
        if isinstance(width, int) and not isinstance(width, bool):
            update["ask_width"] = max(ASK_MIN_WIDTH, width)
        width = stored.get("assistant_width")
        if isinstance(width, int) and not isinstance(width, bool):
            update["assistant_width"] = max(ASK_MIN_WIDTH, width)
        if update:
            self.param.update(update)
        return bool(update)


def valid_sizes(sizes: t.Any) -> tuple[float, float] | None:
    """Return *sizes* as two pane widths summing to 100, or None if they are unusable.

    Sizes with a collapsed pane are unusable too: restoring them would open the
    Split tab with one side missing.
    """
    if not isinstance(sizes, (list, tuple)) or len(sizes) != 2:
        return None
    if not all(
        isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) and s >= 0
        for s in sizes
    ):
        return None
    total = sum(sizes)
    if not math.isfinite(total) or total <= 0:
        return None
    left = round(100 * sizes[0] / total, 2)
    right = round(100 - left, 2)
    if min(left, right) <= _COLLAPSED:
        return None
    return left, right


def is_collapsed(collapsed: int | None, sizes: t.Any) -> int | None:
    """Return which pane (0 left, 1 right) the split shows collapsed, or None."""
    if collapsed in (0, 1):
        return collapsed
    if isinstance(sizes, (list, tuple)) and len(sizes) == 2:
        if sizes[0] <= _COLLAPSED:
            return 0
        if sizes[1] <= _COLLAPSED:
            return 1
    return None


class LocalPreference(JSComponent):
    """Keeps :attr:`value` in the browser's local storage under :attr:`key`.

    When it renders it sends what is stored to *on_load*; afterwards every
    change of :attr:`value` from Python is written back. It also keeps
    :attr:`window_width` up to date. It takes no space on the page.
    """

    key = param.String(default=STORAGE_KEY, doc="The local storage key.")

    legacy_key = param.String(
        default=None,
        allow_None=True,
        doc="""
        A key the value was stored under before. While nothing is stored under
        :attr:`key`, the value is read from it and copied to :attr:`key`. It
        is left in place, since other apps may read it too.""",
    )

    value = param.String(default=None, allow_None=True, doc="The JSON text to store.")

    window_width = param.Integer(
        default=None, allow_None=True, doc="Width of the browser window in pixels."
    )

    _esm = """
    export function render({ model }) {
      function read() {
        try {
          const value = window.localStorage.getItem(model.key)
          if (value != null || !model.legacy_key) { return value }
          const legacy = window.localStorage.getItem(model.legacy_key)
          if (legacy != null) { window.localStorage.setItem(model.key, legacy) }
          return legacy
        } catch (e) { return null }
      }
      function write() {
        if (model.value == null) { return }
        try { window.localStorage.setItem(model.key, model.value) } catch (e) {}
      }
      function measure() {
        if (model.window_width !== window.innerWidth) { model.window_width = window.innerWidth }
      }
      let timer = null
      function onResize() {
        clearTimeout(timer)
        timer = setTimeout(measure, 150)
      }
      measure()
      model.send_msg({type: "load", value: read()})
      model.on("value", write)
      window.addEventListener("resize", onResize)
      model.on("remove", () => {
        clearTimeout(timer)
        window.removeEventListener("resize", onResize)
      })
    }
    """

    def __init__(self, on_load: t.Callable[[str | None], None] | None = None, **params):
        params.setdefault("height", 0)
        params.setdefault("margin", 0)
        params.setdefault("sizing_mode", "stretch_width")
        super().__init__(**params)
        self.on_load = on_load

    def _handle_msg(self, msg):
        if isinstance(msg, dict) and msg.get("type") == "load" and self.on_load is not None:
            value = msg.get("value")
            self.on_load(value if isinstance(value, str) else None)


class SplitEvents(JSComponent):
    """Reports when the canvas pane is shown or the window resized.

    Placed in the canvas pane, the component calls *on_resize* once the pane
    has its size and whenever the window changes width, but not when a panel
    beside the split opens or closes, so the canvas does not move under the
    pointer. :attr:`pane_size` follows every size change of the pane. It
    takes no space on the page.
    """

    active = param.Boolean(default=False, doc="Whether resizes are reported.")

    pane_size = param.List(
        default=[], item_type=(int, float), doc="Width and height of the canvas pane in pixels."
    )

    _esm = """
    export function render({ model, el }) {
      let timer = null
      function report() {
        clearTimeout(timer)
        timer = setTimeout(() => {
          if (model.active) { model.send_msg({type: "resize"}) }
        }, 200)
      }
      const observer = new ResizeObserver(([entry]) => {
        if (entry.contentRect.width) {
          observer.disconnect()
          report()
        }
      })
      observer.observe(el)
      // The component sits in the pane's Column, whose host is as large as the pane.
      const pane = el.getRootNode()?.host?.getRootNode()?.host
      let sizeTimer = null
      const sizeObserver = new ResizeObserver(([entry]) => {
        clearTimeout(sizeTimer)
        sizeTimer = setTimeout(() => {
          const size = [Math.round(entry.contentRect.width), Math.round(entry.contentRect.height)]
          if (size[0] && (size[0] !== model.pane_size[0] || size[1] !== model.pane_size[1])) {
            model.pane_size = size
          }
        }, 100)
      })
      if (pane) { sizeObserver.observe(pane) }
      // The grid and the splitter dispatch resize events of their own, at an unchanged width.
      let windowWidth = window.innerWidth
      function onWindowResize() {
        if (window.innerWidth !== windowWidth) {
          windowWidth = window.innerWidth
          report()
        }
      }
      window.addEventListener("resize", onWindowResize)
      model.on("remove", () => {
        clearTimeout(timer)
        clearTimeout(sizeTimer)
        observer.disconnect()
        sizeObserver.disconnect()
        window.removeEventListener("resize", onWindowResize)
      })
    }
    """

    def __init__(self, on_resize: t.Callable[[], None] | None = None, **params):
        params.setdefault("height", 0)
        params.setdefault("margin", 0)
        params.setdefault("sizing_mode", "stretch_width")
        super().__init__(**params)
        self.on_resize = on_resize

    def _handle_msg(self, msg):
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "resize" and self.on_resize is not None:
            self.on_resize()


class SplitView(Viewer):
    """The canvas and the tile grid in two resizable panes.

    *on_collapse* is called with the pane that was collapsed (0 for the
    canvas, 1 for the grid), *on_load* once the preferences stored in the
    browser were read and any usable ones applied, and *on_resize* when the
    canvas pane was shown or changed width.
    """

    preferences = param.ClassSelector(class_=SplitPreferences, doc="The remembered state.")

    def __init__(
        self,
        on_collapse: t.Callable[[int], None] | None = None,
        on_load: t.Callable[[], None] | None = None,
        on_resize: t.Callable[[], None] | None = None,
        **params,
    ):
        params.setdefault("preferences", SplitPreferences())
        super().__init__(**params)
        self.on_collapse = on_collapse
        self.on_load = on_load
        self.on_resize = on_resize
        sizes = self.preferences.sizes
        self.left = pn.Column(margin=0, sizing_mode="stretch_both")
        # Tiles keep their minimum widths, so a narrow grid pane scrolls sideways too.
        self.right = pn.Column(margin=0, sizing_mode="stretch_both", scroll="both-auto")
        self.split = HSplit(
            self.left,
            self.right,
            sizes=sizes,
            expanded_sizes=sizes,
            # The buttons collapse a pane in one click when it is near its usual size.
            collapse_threshold=5,
            show_buttons=True,
            snap_size=80,
            gutter_size=8,
            sizing_mode="stretch_both",
            stylesheets=[GUTTER_CSS],
            margin=0,
        )
        self.split.param.watch(self._on_split_changed, ["sizes", "collapsed"])
        self.events = SplitEvents(on_resize=on_resize)
        # Applied once: a re-render must not pull in what another tab stored since.
        self.loaded = False
        key = storage_key()
        # Before keys carried the app's path every app on an origin shared the plain one.
        self.storage = LocalPreference(
            on_load=self._on_stored,
            key=key,
            legacy_key=None if key == STORAGE_KEY else STORAGE_KEY,
        )
        self.preferences.param.watch(
            self._store,
            [
                "split",
                "sizes",
                "code_dock",
                "spec_view",
                "spec_text",
                "sidecar_width",
                "palette_open",
                "ask_width",
                "assistant_width",
            ],
        )

    def _on_split_changed(self, *_events):
        split = self.split
        collapsed = is_collapsed(split.collapsed, split.sizes)
        if collapsed is not None:
            if self.on_collapse is not None:
                self.on_collapse(collapsed)
            return
        sizes = valid_sizes(split.sizes)
        if sizes is None or sizes == self.preferences.sizes:
            return
        self.preferences.sizes = sizes
        split.expanded_sizes = sizes
        if self.on_resize is not None:
            self.on_resize()

    def show(self, left: list, right: list):
        """Put *left* (the canvas) and *right* (the grid and its controls) in the panes."""
        self.left[:] = [*left, self.events]
        self.right[:] = right

    def restore(self):
        """Open both panes at the remembered sizes, e.g. after one was collapsed."""
        sizes = self.preferences.sizes
        self.split.param.update(collapsed=None, sizes=sizes, expanded_sizes=sizes)

    def _on_stored(self, value: str | None):
        if self.loaded:
            return
        self.loaded = True
        if value is not None and self.preferences.load(value):
            self.restore()
        if self.on_load is not None:
            self.on_load()

    def _store(self, *_events):
        self.storage.value = self.preferences.to_json()

    def __panel__(self):
        """Render the split."""
        return self.split
