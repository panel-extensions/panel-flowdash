"""Port type names, their aliases and the subtype rules connections are checked against.

Port types are plain strings: a ``type`` given in ``provides``/``requires``, or
the class name of the Parameter a Viewer port is introspected from. Different
spellings of one type (``"str"`` and ``"String"``, ``"DataFrame"`` and
``"pandas.DataFrame"``) normalize to one canonical name, and a source type
connects to a target type when they are equal or the source is registered as a
subtype of the target (an ``Integer`` output feeds a ``Number`` input).

The static check only rules out connections that can never work. The input's
Parameter still validates every value it receives at runtime.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable

ANY = "any"

_LOCK = threading.Lock()
# Lowercased spelling -> canonical name.
_ALIASES: dict[str, str] = {}
# Canonical name -> canonical names it is a direct subtype of.
_SUPERTYPES: dict[str, set[str]] = {}
# Canonical name -> what lists the columns of a value of that type.
_COLUMN_READERS: dict[str, Callable] = {}
# Canonical name -> registered handle color (CSS color string).
_COLORS: dict[str, str] = {}
# Canonical name -> built-in dark-theme handle color, used unless overridden.
_BUILTIN_LIGHT_COLORS: dict[str, str] = {}
_BUILTIN_DARK_COLORS: dict[str, str] = {}

_KNOWN_CUSTOM_COLORS: dict[str, tuple[str, str]] = {
    "lumen.source": ("#6D4C41", "#BCAAA4"),
    "lumen.pipeline": ("#0072B5", "#5AB4EE"),
}

_FALLBACK_PALETTE: tuple[tuple[str, str], ...] = (
    ("#0072B5", "#5AB4EE"),
    ("#2A9D8F", "#3FB8A9"),
    ("#8E24AA", "#CE93D8"),
    ("#D97706", "#F5B041"),
    ("#0284C7", "#38BDF8"),
    ("#4F46E5", "#818CF8"),
    ("#DB2777", "#F472B6"),
    ("#059669", "#34D399"),
    ("#6D4C41", "#BCAAA4"),
    ("#7C3AED", "#A78BFA"),
)


def _key(name: str) -> str:
    return name.strip().lower()


def register_port_type(
    name: str,
    *,
    aliases: Iterable[str] = (),
    subtype_of: Iterable[str] = (),
    columns: Callable | None = None,
    color: str | None = None,
) -> str:
    """Register a port type, its alternative spellings and the types it can feed.

    Registering a name again adds to its aliases and supertypes. Aliases and
    supertypes may be spelled any way that normalizes to a registered type;
    an unknown supertype is registered on the fly.

    Parameters
    ----------
    name
        Canonical name of the type, e.g. ``"Customer"``. Matched case-insensitively.
    aliases
        Other spellings of the same type, e.g. ``["crm.Customer"]``.
    subtype_of
        Types an output of this type may connect to, e.g. ``["Person"]``.
        Subtyping is transitive.
    columns
        For a tabular type that is not a DataFrame, e.g. a lazy query, a
        callable that lists the columns of a value without fetching rows:
        ``columns(value)`` returns column names, ``(name, type)`` pairs or a
        ``{name: type}`` dict. Config fields that offer the columns of an
        input of this type use it, see :class:`~panel_flowdash.Choices`.
        Subtypes inherit it.
    color
        Optional CSS color used for canvas handles of this port type.
        Subtypes inherit it unless they register their own.

    Returns
    -------
    str
        The canonical name ports of this type normalize to.

    Raises
    ------
    ValueError
        If an alias already names a different type, or the rule would make
        ``any`` a subtype of something.
    """
    with _LOCK:
        canonical = _ALIASES.get(_key(name), _key(name))
        if canonical == ANY and list(subtype_of):
            raise ValueError("'any' accepts every type and cannot be a subtype.")
        for spelling in (name, *aliases):
            key = _key(spelling)
            existing = _ALIASES.get(key)
            if existing is not None and existing != canonical:
                raise ValueError(
                    f"Port type alias {spelling!r} already names {existing!r}, not {canonical!r}."
                )
            _ALIASES[key] = canonical
        supers = _SUPERTYPES.setdefault(canonical, set())
        for parent in subtype_of:
            parent_key = _ALIASES.setdefault(_key(parent), _key(parent))
            if parent_key != canonical:
                supers.add(parent_key)
        if columns is not None:
            _COLUMN_READERS[canonical] = columns
        if color is not None:
            _COLORS[canonical] = color
    return canonical


def port_type_color(name: str | None, *, dark: bool = False) -> str:
    """Return the handle color for *name*, using dark-mode adjustments when *dark* is True."""
    canonical = normalize_port_type(name)
    if canonical is None:
        canonical = ANY
    with _LOCK:
        registered = _COLORS.get(canonical)
        if registered is not None:
            if dark and registered == _BUILTIN_LIGHT_COLORS.get(canonical):
                return _BUILTIN_DARK_COLORS[canonical]
            return registered
        for parent in sorted(_supertypes(canonical) - {canonical}):
            parent_color = _COLORS.get(parent)
            if parent_color is not None:
                if dark and parent_color == _BUILTIN_LIGHT_COLORS.get(parent):
                    return _BUILTIN_DARK_COLORS[parent]
                return parent_color
    if canonical in _KNOWN_CUSTOM_COLORS:
        return _KNOWN_CUSTOM_COLORS[canonical][1 if dark else 0]
    digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=4).digest()
    pair = _FALLBACK_PALETTE[int.from_bytes(digest, "big") % len(_FALLBACK_PALETTE)]
    return pair[1 if dark else 0]


def column_reader(name: str | None) -> Callable | None:
    """Return the ``columns`` callable registered for a port type or the nearest supertype."""
    canonical = normalize_port_type(name)
    if canonical is None:
        return None
    with _LOCK:
        if canonical in _COLUMN_READERS:
            return _COLUMN_READERS[canonical]
        supers = sorted(_supertypes(canonical) - {canonical})
        return next((_COLUMN_READERS[s] for s in supers if s in _COLUMN_READERS), None)


def normalize_port_type(name: str | None) -> str | None:
    """Return the canonical name of a port type, or None for an untyped port.

    Unregistered names normalize to themselves, lowercased, so two ports with
    the same custom type name still connect.
    """
    if name is None or not name.strip():
        return None
    key = _key(name)
    return _ALIASES.get(key, key)


def _supertypes(canonical: str) -> set[str]:
    seen: set[str] = set()
    stack = [canonical]
    while stack:
        for parent in _SUPERTYPES.get(stack.pop(), ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def port_types_compatible(source: str | None, target: str | None) -> bool:
    """Return whether an output of type *source* may connect to an input of type *target*.

    An untyped port, or one typed ``any`` (``object``, a bare ``Parameter``,
    a ``Selector`` or ``ClassSelector`` whose value type the name does not
    tell), connects to anything; otherwise the types must be equal or *source*
    a registered subtype of *target*.
    """
    source_type = normalize_port_type(source)
    target_type = normalize_port_type(target)
    if source_type in (None, ANY) or target_type in (None, ANY):
        return True
    return source_type == target_type or target_type in _supertypes(source_type)


def _register_builtin(
    name: str,
    *,
    aliases: Iterable[str] = (),
    subtype_of: Iterable[str] = (),
    light: str,
    dark: str,
):
    canonical = register_port_type(name, aliases=aliases, subtype_of=subtype_of, color=light)
    _BUILTIN_LIGHT_COLORS[canonical] = light
    _BUILTIN_DARK_COLORS[canonical] = dark


def _register_builtin_types():
    _register_builtin(
        ANY,
        aliases=[
            "object",
            "Any",
            "typing.Any",
            "Parameter",
            "Selector",
            "ObjectSelector",
            "ClassSelector",
            "Composite",
            "Dynamic",
        ],
        light="#8A8F98",
        dark="#9AA0A8",
    )
    # Parameters whose values are strings, lists or tuples with extra
    # constraints are aliases rather than subtypes, so that a plain output can
    # feed them; their Parameter checks the constraint on each value.
    _register_builtin(
        "str",
        aliases=[
            "String",
            "builtins.str",
            "Color",
            "Path",
            "Filename",
            "Foldername",
            "FileSelector",
        ],
        light="#0284C7",
        dark="#38BDF8",
    )
    _register_builtin(
        "bytes", aliases=["Bytes", "builtins.bytes"], light="#64748B", dark="#94A3B8"
    )
    _register_builtin(
        "number",
        aliases=["Number", "float", "builtins.float", "Magnitude"],
        light="#4F46E5",
        dark="#818CF8",
    )
    _register_builtin(
        "int",
        aliases=["Integer", "builtins.int"],
        subtype_of=["number"],
        light="#4F46E5",
        dark="#818CF8",
    )
    # bool is an int in Python, but param.Integer and param.Number reject it, so
    # a Boolean output does not feed numeric inputs.
    _register_builtin(
        "bool", aliases=["Boolean", "builtins.bool", "Event"], light="#DB2777", dark="#F472B6"
    )
    _register_builtin(
        "list",
        aliases=["List", "builtins.list", "HookList", "ListSelector", "MultiFileSelector"],
        light="#8E24AA",
        dark="#CE93D8",
    )
    _register_builtin(
        "tuple",
        aliases=[
            "Tuple",
            "builtins.tuple",
            "NumericTuple",
            "XYCoordinates",
            "Range",
            "DateRange",
            "CalendarDateRange",
        ],
        light="#7C3AED",
        dark="#A78BFA",
    )
    _register_builtin("dict", aliases=["Dict", "builtins.dict"], light="#D97706", dark="#F5B041")
    # Names match case-insensitively, so param's Date and Python's date are one
    # type; param.CalendarDate refusing datetimes is left to the runtime check.
    _register_builtin(
        "date",
        aliases=["CalendarDate", "datetime", "datetime.date", "datetime.datetime"],
        light="#B45309",
        dark="#FBBF24",
    )
    _register_builtin("callable", aliases=["Callable", "Action"], light="#059669", dark="#34D399")
    _register_builtin(
        "DataFrame",
        aliases=["pd.DataFrame", "pandas.DataFrame", "pandas.core.frame.DataFrame"],
        light="#2A9D8F",
        dark="#3FB8A9",
    )
    _register_builtin(
        "Series",
        aliases=["pd.Series", "pandas.Series", "pandas.core.series.Series"],
        light="#0D9488",
        dark="#2DD4BF",
    )
    _register_builtin(
        "Array",
        aliases=["ndarray", "np.ndarray", "numpy.ndarray"],
        light="#0D9488",
        dark="#2DD4BF",
    )


_register_builtin_types()
