"""The editor's component palette: a search field over components grouped by role."""

from __future__ import annotations

import typing as t
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import panel as pn
import panel_material_ui as pmui
import param
from panel.custom import Child, JSComponent
from panel.viewable import Viewer

from panel_flowdash.component_spec import ComponentSpec, _ports_from_metadata
from panel_flowdash.registry import RegistryEntry
from panel_flowdash.ui.roles import ROLE_ICONS, component_role

# Palette group order; "other" holds components whose ports are unknown until imported.
ROLES = ("source", "transform", "view", "other", "widget")

ROLE_LABELS = {
    "source": "Data sources",
    "transform": "Transforms",
    "view": "Views",
    "other": "Components",
    "widget": "Widgets",
}

HINT = (
    "Drag onto the canvas, or drop on a port to add and connect in one step. "
    "Click to add without connecting."
)

EMPTY_MESSAGE = "No components match"

_PALETTE_SX = {
    "px": 0.5,
    "& .MuiListItemButton-root": {"borderRadius": 1, "py": 0.5, "minHeight": 0, "gap": 1.25},
    "& .MuiListItemButton-root:hover .MuiListItemText-primary": {"textDecoration": "none"},
    "& .MuiListItemText-root": {"my": 0, "minWidth": 0},
    "& .MuiListItemText-primary": {"fontSize": "0.8125rem", "fontWeight": 500},
    "& .MuiListItemText-secondary": {
        "fontSize": "0.6875rem",
        "lineHeight": 1.4,
        "color": "text.secondary",
        "whiteSpace": "nowrap",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
    },
    # Text with an icon token is wrapped in an inline-flex span, which needs
    # bounds for its text to be ellipsized.
    "& .MuiListItemText-secondary > span": {"maxWidth": "100%"},
    "& .MuiListItemText-secondary > span > span:last-child": {
        "minWidth": 0,
        "display": "block",
        "overflow": "hidden",
        "textOverflow": "ellipsis",
    },
    "& .MuiListItemIcon-root": {
        "width": 32,
        "height": 32,
        "flex": "none",
        "pr": "0 !important",
        "borderRadius": 1,
        "alignItems": "center",
        "justifyContent": "center",
        "bgcolor": "rgba(var(--mui-palette-primary-mainChannel) / 0.12)",
    },
    "& .MuiListItemIcon-root:has(.MuiIcon-colorAction)": {"bgcolor": "action.hover"},
    "& .MuiListItemIcon-root .MuiIcon-root": {"fontSize": "1.125rem"},
    # The drag handle is drawn in CSS, so the item stays one MenuList entry
    # whose drag payload and click behave exactly as before.
    "& .MuiListItemButton-root[draggable='true']::after": {
        "content": '"drag_indicator"',
        "fontFamily": "'Material Icons'",
        "fontSize": "1.125rem",
        "lineHeight": 1,
        "color": "text.disabled",
        "ml": "auto",
        "opacity": 0,
        "fontFeatureSettings": '"liga"',
    },
    "& .MuiListItemButton-root[draggable='true']:hover::after": {"opacity": 1},
    # Group headers are the list's direct children; components sit in their collapses.
    "& > .MuiListItemButton-root": {"mt": 1, "py": 0.25},
    "& > .MuiListItemButton-root .MuiListItemText-primary": {
        "fontSize": "0.75rem",
        "fontWeight": 500,
        "color": "text.secondary",
    },
    "& > .MuiListItemButton-root .MuiIconButton-root": {"p": 0.25, "color": "text.secondary"},
}


@dataclass(frozen=True)
class PalettePort:
    """A port as the palette summarizes and searches it."""

    name: str
    type: str | None = None


@dataclass(frozen=True)
class PaletteEntry:
    """What the palette shows and searches for one component."""

    component_id: str
    title: str
    role: str
    icon: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    inputs: tuple[PalettePort, ...] = ()
    outputs: tuple[PalettePort, ...] = ()
    placed: bool = False
    draggable: bool = True

    @property
    def summary(self) -> str:
        """Port summary such as ``in: dataset, out: filtered (DataFrame)``.

        Types are spelled out for the last group only: that is the side a
        component is usually wired from, and it keeps the line short.
        """
        groups = [("in", self.inputs), ("out", self.outputs)]
        groups = [(label, ports) for label, ports in groups if ports]
        parts = []
        for index, (label, ports) in enumerate(groups):
            typed = index == len(groups) - 1
            names = ", ".join(
                f"{port.name} ({port.type})" if typed and port.type else port.name
                for port in ports
            )
            parts.append(f"{label}: {names}")
        return ", ".join(parts)

    def matches(self, query: str) -> bool:
        """Whether every word of *query* occurs in the title, description, tags or ports."""
        words = query.lower().split()
        if not words:
            return True
        haystack = " ".join(
            [
                self.title,
                self.description or "",
                *self.tags,
                *(f"{port.name} {port.type or ''}" for port in (*self.inputs, *self.outputs)),
            ]
        ).lower()
        return all(word in haystack for word in words)

    def item(self) -> dict[str, t.Any]:
        """Return the MenuList item for this entry."""
        secondary = self.summary
        if self.placed:
            prefix = ":material/check_circle@color=primary: Placed"
            secondary = f"{prefix}, {secondary}" if secondary else prefix
        return {
            "label": self.title,
            "secondary": secondary or None,
            "icon": self.icon or ROLE_ICONS[self.role],
            "color": "action" if self.role == "widget" else "primary",
            "tooltip": self.description or None,
            "component_id": self.component_id,
            "draggable": self.draggable,
        }


def palette_entry(
    entry: RegistryEntry,
    spec: ComponentSpec | None = None,
    *,
    placed: bool = False,
) -> PaletteEntry:
    """Describe *entry* for the palette.

    Components are imported lazily, so until *spec* exists the ports come from
    the ``provides``/``requires`` declared on ``@register``.
    """
    meta = entry.metadata
    # An undecorated port list is introspected from a Viewer on import.
    ports_known = spec is not None or bool(meta.provides or meta.requires)
    if spec is None:
        outputs, inputs = _ports_from_metadata(meta)
    else:
        outputs, inputs = spec.outputs, spec.inputs
    in_ports = tuple(PalettePort(p.label or p.name, p.type) for p in inputs)
    out_ports = tuple(PalettePort(p.label or p.name, p.type) for p in outputs)
    return PaletteEntry(
        component_id=entry.app_id,
        title=entry.title,
        role=component_role(entry, in_ports, out_ports, ports_known=ports_known),
        icon=meta.icon,
        description=meta.description or (spec.description if spec is not None else None),
        tags=tuple(str(tag) for tag in meta.tags),
        inputs=in_ports,
        outputs=out_ports,
        placed=placed,
        draggable=not (placed and meta.singleton),
    )


def palette_items(entries: Iterable[PaletteEntry], query: str = "") -> list[dict[str, t.Any]]:
    """MenuList items for the *entries* matching *query*, one group per role."""
    groups: dict[str, list[dict[str, t.Any]]] = {role: [] for role in ROLES}
    for entry in entries:
        if entry.matches(query):
            groups[entry.role].append(entry.item())
    return [
        {
            "label": ROLE_LABELS[role],
            "icon": None,
            "draggable": False,
            "selectable": False,
            "items": items,
        }
        for role, items in groups.items()
        if items
    ]


_SEARCH_SX = {
    "& .MuiOutlinedInput-root": {"borderRadius": 2, "bgcolor": "action.hover", "pl": 1.25},
    "& .MuiOutlinedInput-notchedOutline": {"borderColor": "transparent"},
    # Adornments drawn in CSS, as TextInput has none: a search icon and the `/` key.
    "& .MuiOutlinedInput-root::before": {
        "content": '"search"',
        "fontFamily": "'Material Icons'",
        "fontSize": "1.25rem",
        "color": "text.secondary",
        "fontFeatureSettings": '"liga"',
    },
    "& .MuiOutlinedInput-root::after": {
        "content": '"/"',
        "mr": 1.25,
        "px": 0.75,
        "fontSize": "0.75rem",
        "lineHeight": "16px",
        "borderRadius": 0.5,
        "border": 1,
        "borderColor": "divider",
        "color": "text.secondary",
    },
    "& .MuiOutlinedInput-root.Mui-focused::after": {"display": "none"},
    "& .MuiOutlinedInput-input": {"px": 1},
}


class SearchShortcut(JSComponent):
    """Shows ``search`` and focuses its input when ``/`` is pressed outside a text field.

    Escape in the field clears it.
    """

    search = Child()

    _esm = """
function findInput(root) {
  if (!root) {
    return null
  }
  const input = root.querySelector("input")
  if (input) {
    return input
  }
  for (const node of root.querySelectorAll("*")) {
    const found = node.shadowRoot && findInput(node.shadowRoot)
    if (found) {
      return found
    }
  }
  return null
}

function editable(target) {
  const tag = target?.tagName
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || !!target?.isContentEditable
}

export function render({model, el}) {
  const child = model.get_child("search")
  el.append(child)
  const onKey = (event) => {
    const target = event.composedPath()[0]
    const input = findInput(child.shadowRoot || child)
    if (event.key === "Escape" && input && target === input) {
      // Cleared like typing would, so the empty value follows the keystrokes
      // still on their way to the server instead of racing them.
      const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set
      setValue.call(input, "")
      input.dispatchEvent(new Event("input", {bubbles: true}))
      input.blur()
      return
    }
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || editable(target)) {
      return
    }
    // A hidden palette (e.g. in dashboard mode) must not take the key.
    if (!input || !el.getClientRects().length) {
      return
    }
    event.preventDefault()
    input.focus()
  }
  document.addEventListener("keydown", onKey)
  model.on("remove", () => document.removeEventListener("keydown", onKey))
}
"""


class ComponentPalette(Viewer):
    """Search field, grouped component list and usage hint.

    ``menu`` is the MenuList the canvas accepts drops from. Its items come
    from the ``items`` callable, which reads ``query`` and is called again
    whenever the query changes or :meth:`refresh` is called.
    """

    query = param.String(default="", doc="Current search text.")

    def __init__(
        self,
        *,
        drag_type: str,
        items: Callable[[], list[dict]],
        on_click: Callable[[dict], t.Any],
        **params,
    ):
        super().__init__(**params)
        self._items = items
        self.menu = pmui.MenuList(
            draggable=True,
            drag_type=drag_type,
            highlight=False,
            level_indent=0,
            sizing_mode="stretch_width",
            margin=0,
            # Panel caps a child at its parent's height, which would let the
            # entries overflow the list instead of extending its scroll area.
            styles={"max-height": "none"},
            sx=_PALETTE_SX,
        )
        self.menu.on_click(on_click)
        self.menu.param.watch(self._sync_empty, "items")
        self.search = pmui.TextInput(
            placeholder="Search components",
            size="small",
            sizing_mode="stretch_width",
            margin=0,
            sx=_SEARCH_SX,
        )
        self.search.param.watch(self._on_search_input, "value_input")
        self._shortcut = SearchShortcut(
            search=self.search, sizing_mode="stretch_width", margin=(12, 12, 4, 12)
        )
        self._empty = pmui.Typography(
            EMPTY_MESSAGE,
            variant="body2",
            sx={"color": "text.secondary"},
            margin=(12, 16),
            visible=False,
        )
        self._hint = pmui.Paper(
            pmui.Typography(
                HINT,
                variant="caption",
                sx={
                    "color": "text.secondary",
                    "lineHeight": 1.5,
                    "display": "block",
                    # The text is rendered as Markdown, i.e. inside a paragraph.
                    "& > p": {"m": 0},
                    "& p::before": {
                        "content": '"lightbulb"',
                        "fontFamily": "'Material Icons'",
                        "fontSize": "1rem",
                        "verticalAlign": "-3px",
                        "mr": 0.5,
                        "fontFeatureSettings": '"liga"',
                    },
                },
            ),
            variant="outlined",
            sizing_mode="stretch_width",
            margin=(12, 12),
            sx={"p": 1, "borderRadius": 2, "bgcolor": "background.default"},
        )
        # The list scrolls in its own region between the search field and the
        # hint, so neither scrolls away and the hint never covers an entry.
        self._list = pn.Column(
            self._empty,
            self.menu,
            sizing_mode="stretch_both",
            scroll="y-auto",
            margin=0,
        )
        self._layout = pn.Column(
            self._shortcut,
            self._list,
            self._hint,
            sizing_mode="stretch_both",
            margin=0,
        )

    def _on_search_input(self, event):
        self.query = event.new or ""

    @param.depends("query", watch=True)
    def _sync_search(self):
        # value_input is read-only, so set the text through value. value may
        # already hold the query while the field shows other typed text, then
        # re-triggering value pushes it into value_input again.
        if self.search.value_input == self.query:
            return
        if self.search.value == self.query:
            self.search.param.trigger("value")
        else:
            self.search.value = self.query

    @param.depends("query", watch=True)
    def refresh(self):
        """Rebuild the menu items from the ``items`` callable."""
        items = self._items()
        if items != self.menu.items:
            # MenuList remaps `expanded` onto new items, which would override an
            # `expanded` set in the same update.
            self.menu.items = items
            self.menu.expanded = [(i,) for i in range(len(items))]
        self._sync_empty()

    def _sync_empty(self, *_events):
        self._empty.visible = bool(self.query.strip()) and not self.menu.items

    def __panel__(self):
        """Return the search field, the grouped list and the hint."""
        return self._layout
