"""The breakpoint bands of the tile grid, named after the screens they stand for.

TileGrid splits widths at its ``breakpoints`` into bands labelled ``"xs"``,
``"sm"``, ``"md"`` and so on, narrowest first. The band holding the width the
layout was arranged at (``reference_width``) is the base band, which shows
``layout`` itself; every other band shows a layout generated from it, or a
custom one in ``responsive_layouts`` once tiles were arranged for that band.
"""

from __future__ import annotations

from itertools import pairwise

# The width TileGrid assumes a layout was authored at when it records none.
DEFAULT_REFERENCE_WIDTH = 1200

_LABELS = ("xs", "sm", "md", "lg", "xl", "xxl")

_NAMES = {
    2: ("Phone", "Desktop"),
    3: ("Phone", "Tablet", "Desktop"),
    4: ("Phone", "Tablet", "Laptop", "Desktop"),
}

#: How each band's layout is arranged, see :func:`band_sources`.
BASE, CUSTOM, AUTOMATIC = "base", "custom", "automatic"


def reference_width(width: int | None, breakpoints: list[int]) -> int:
    """Return the grid width a layout's percentage widths are relative to, as TileGrid does."""
    if width:
        return width
    return max(breakpoints) if breakpoints else DEFAULT_REFERENCE_WIDTH


def band_labels(breakpoints: list[int]) -> list[str]:
    """Return the labels TileGrid gives the bands of *breakpoints*, narrowest first."""
    if not breakpoints:
        return []
    return [_LABELS[i] if i < len(_LABELS) else f"bp{i}" for i in range(len(breakpoints) + 1)]


def band_for_width(breakpoints: list[int], width: float) -> str | None:
    """Return the label of the band *width* falls in, or None without breakpoints."""
    labels = band_labels(breakpoints)
    for label, edge in zip(labels, sorted(breakpoints), strict=False):
        if width < edge:
            return label
    return labels[-1] if labels else None


def base_band(breakpoints: list[int], width: int | None) -> str | None:
    """Return the band whose layout is ``layout`` itself, given the grid's ``reference_width``."""
    return band_for_width(breakpoints, reference_width(width, breakpoints))


def band_names(breakpoints: list[int]) -> dict[str, str]:
    """Return ``{band label: name}``, e.g. ``{"md": "Desktop", "sm": "Tablet", "xs": "Phone"}``.

    The widest band comes first. Beyond four bands the ones between the
    narrowest and the widest are named by their range of widths.
    """
    labels = band_labels(breakpoints)
    if not labels:
        return {}
    edges = sorted(breakpoints)
    names = _NAMES.get(len(labels))
    if names is None:
        names = (
            "Phone",
            *(f"{low}-{high - 1} px" for low, high in pairwise(edges)),
            "Desktop",
        )
    return dict(reversed(list(zip(labels, names, strict=True))))


def band_sources(
    breakpoints: list[int], width: int | None, responsive_layouts: dict[str, list]
) -> dict[str, str]:
    """Return how each band's layout is arranged: :data:`BASE`, :data:`CUSTOM` or :data:`AUTOMATIC`."""
    base = base_band(breakpoints, width)
    sources = {}
    for label in band_labels(breakpoints):
        if label == base:
            sources[label] = BASE
        else:
            sources[label] = CUSTOM if responsive_layouts.get(label) else AUTOMATIC
    return sources


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def band_summary(names: dict[str, str], sources: dict[str, str]) -> str:
    """Describe which bands follow the base band's layout and which were arranged separately."""
    base = next((names[label] for label, source in sources.items() if source == BASE), None)
    custom = [names[label] for label in names if sources.get(label) == CUSTOM]
    automatic = [names[label] for label in names if sources.get(label) == AUTOMATIC]
    parts = []
    if custom:
        verb = "is" if len(custom) == 1 else "are"
        parts.append(f"{_join(custom)} {verb} arranged separately.")
    if automatic:
        verb = "follows" if len(automatic) == 1 else "follow"
        parts.append(f"{_join(automatic)} {verb} the {base} layout automatically.")
    return " ".join(parts)
