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

import threading
from collections.abc import Iterable

ANY = "any"

_LOCK = threading.Lock()
# Lowercased spelling -> canonical name.
_ALIASES: dict[str, str] = {}
# Canonical name -> canonical names it is a direct subtype of.
_SUPERTYPES: dict[str, set[str]] = {}


def _key(name: str) -> str:
    return name.strip().lower()


def register_port_type(
    name: str,
    *,
    aliases: Iterable[str] = (),
    subtype_of: Iterable[str] = (),
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
    return canonical


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


def _register_builtin_types():
    register_port_type(
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
    )
    # Parameters whose values are strings, lists or tuples with extra
    # constraints are aliases rather than subtypes, so that a plain output can
    # feed them; their Parameter checks the constraint on each value.
    register_port_type(
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
    )
    register_port_type("bytes", aliases=["Bytes", "builtins.bytes"])
    register_port_type("number", aliases=["Number", "float", "builtins.float", "Magnitude"])
    register_port_type("int", aliases=["Integer", "builtins.int"], subtype_of=["number"])
    # bool is an int in Python, but param.Integer and param.Number reject it, so
    # a Boolean output does not feed numeric inputs.
    register_port_type("bool", aliases=["Boolean", "builtins.bool", "Event"])
    register_port_type(
        "list", aliases=["List", "builtins.list", "HookList", "ListSelector", "MultiFileSelector"]
    )
    register_port_type(
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
    )
    register_port_type("dict", aliases=["Dict", "builtins.dict"])
    # Names match case-insensitively, so param's Date and Python's date are one
    # type; param.CalendarDate refusing datetimes is left to the runtime check.
    register_port_type(
        "date", aliases=["CalendarDate", "datetime", "datetime.date", "datetime.datetime"]
    )
    register_port_type("callable", aliases=["Callable", "Action"])
    register_port_type(
        "DataFrame", aliases=["pd.DataFrame", "pandas.DataFrame", "pandas.core.frame.DataFrame"]
    )
    register_port_type(
        "Series", aliases=["pd.Series", "pandas.Series", "pandas.core.series.Series"]
    )
    register_port_type("Array", aliases=["ndarray", "np.ndarray", "numpy.ndarray"])


_register_builtin_types()
