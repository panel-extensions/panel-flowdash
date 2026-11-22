"""Whether the editor's sidecar is open or folded to its strip, decided in one place.

The sidecar (:class:`~panel_flowdash.ui.inspector.Inspector`) shares the
window's width with the palette, the canvas or the tile grid, the code panel
docked to the right and the assistant's drawer. Three things decide whether
it is open:

- The person's choice, made with its fold and unfold buttons. It is kept
  across tabs, and a new selection never overrides it; the folded strip then
  shows a dot for the selection.
- The assistant's drawer. In a window narrower than :data:`SHARED_SLOT_BELOW`
  the two share one slot: opening the drawer folds the sidecar, and opening
  the sidecar closes the drawer.
- The space budget. Where the open sidecar would leave the content less than
  its minimum, it folds, until the person opens it or selects something
  there; that holds until the window is resized.
"""

from __future__ import annotations

import param

# Below this window width the assistant's drawer and the sidecar share one slot.
SHARED_SLOT_BELOW = 1600

SIDECAR_WIDTH = 360
SIDECAR_MIN_WIDTH = 300
SIDECAR_MAX_WIDTH = 560
FOLDED_WIDTH = 44

# The least room the open sidecar leaves the canvas or the tile grid, and the
# canvas and the grid side by side in the Split tab. The single pane's minimum is
# low: in narrow windows the sidecar is where code fields are edited, as the code
# panel needs more room, so it folds only where the canvas would be unusable.
CONTENT_MIN_WIDTH = 240
SPLIT_MIN_WIDTH = 980


class SidecarFold(param.Parameterized):
    """The one owner of the sidecar's ``collapsed`` state, see the module docstring."""

    user_open = param.Boolean(
        default=True, doc="Whether the person last opened rather than folded it."
    )

    drawer_open = param.Boolean(default=False, doc="Whether the assistant's drawer is open.")

    drawer_width = param.Integer(
        default=0,
        doc="The width the open drawer takes from the page; 0 while it overlays the page.",
    )

    on_close_drawer = param.Callable(
        default=None,
        allow_None=True,
        doc="Closes the assistant's drawer, to free the shared slot.",
    )

    def __init__(self, editor, **params):
        super().__init__(**params)
        self.editor = editor
        # Which of the two opened last, when they share a slot: "sidecar" or "drawer".
        self._latest = "sidecar"
        # Opened by the person although the budget had no room for it.
        self._override = False
        self._setting = False

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def user_toggle(self, open_: bool):
        """Record the person opening or folding the sidecar, and apply it."""
        self.user_open = open_
        if open_:
            self._latest = "sidecar"
            self._override = True
            if self.drawer_open and self._shares_slot() and self.on_close_drawer is not None:
                self.on_close_drawer()
        self.sync()

    def drawer_changed(self, open_: bool, width: int = 0):
        """Record the assistant's drawer opening or closing; *width* is what it takes from the page."""
        if open_ and not self.drawer_open:
            self._latest = "drawer"
            self._override = False
        self.param.update(drawer_open=open_, drawer_width=width if open_ else 0)
        self.sync()

    def selection_made(self):
        """Open the sidecar the budget folded, as the person selected something to see in it."""
        if self.user_open and not self._override and not self.fits():
            self._override = True
            self.sync()

    def window_resized(self):
        self._override = False
        self.sync()

    def on_collapsed(self, collapsed: bool):
        """Follow the sidecar folded or opened by anyone but this owner, e.g. a test or a phone sheet."""
        if self._setting:
            return
        if collapsed != self.folded():
            self.user_toggle(not collapsed)

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------

    def _window(self) -> int | None:
        return self.editor._split_view.storage.window_width

    def _shares_slot(self) -> bool:
        width = self._window()
        # Before the browser reports its width the two share a slot, the narrower case.
        return width is None or width < SHARED_SLOT_BELOW

    def room(self) -> int | None:
        """Return the width left to the canvas or the grid beside the open sidecar, or None if unknown."""
        width = self._window()
        if width is None:
            return None
        editor = self.editor
        taken = editor._inspector.width
        if editor._side_panel.visible:
            taken += editor._side_panel.width or 0
        if editor._code_side.visible:
            taken += editor._code_side_width
        if self.drawer_open:
            taken += self.drawer_width
        return width - taken

    def fits(self) -> bool:
        room = self.room()
        if room is None:
            return True
        minimum = SPLIT_MIN_WIDTH if self.editor.mode == "split" else CONTENT_MIN_WIDTH
        return room >= minimum

    def folded(self) -> bool:
        """Whether the sidecar is folded, from the person's choice, the drawer and the budget."""
        if not self.user_open:
            return True
        if (
            self.drawer_open
            and self._latest == "drawer"
            and (self._shares_slot() or not self.fits())
        ):
            return True
        return not self._override and not self.fits()

    def sync(self):
        """Fold or open the sidecar as :meth:`folded` decides."""
        inspector = getattr(self.editor, "_inspector", None)
        if inspector is None or not inspector.visible:
            return
        collapsed = self.folded()
        if inspector.collapsed == collapsed:
            return
        self._setting = True
        try:
            inspector.collapsed = collapsed
        finally:
            self._setting = False
