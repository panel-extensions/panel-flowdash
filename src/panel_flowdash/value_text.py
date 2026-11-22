"""Text for arbitrary values shown to a user or an agent, never an object's own repr.

A ``param.Parameterized`` repr lists every parameter, so a data source
would show its connection URI with the password, its tokens and the like.
Only plain data (strings, numbers, dates and the like) is written out; any
other object is named by its type, and a Parameterized by its name too.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import fractions
import numbers
import pathlib
import re
import reprlib
import typing as t
import uuid

import param

_PLAIN_TYPES = (
    str,
    bytes,
    bool,
    numbers.Number,
    dt.date,
    dt.time,
    dt.timedelta,
    decimal.Decimal,
    fractions.Fraction,
    uuid.UUID,
    pathlib.PurePath,
    enum.Enum,
)
_CONTAINERS = (list, tuple, set, frozenset, dict)
_MAX_NAME = 60
_URL_PASSWORD = re.compile(r"(\b[a-zA-Z][\w+.-]*://[^\s:/@]+:)[^\s@/]+@")
_SECRET_PARAM = re.compile(
    r"(\b(?:password|passwd|pwd|secret|token|access_token|api_key|apikey)=)[^\s&;,'\"]+",
    re.IGNORECASE,
)


def is_plain(value: t.Any) -> bool:
    """Whether *value* is plain data whose text is the data itself, not an object's internals."""
    if value is None or isinstance(value, _PLAIN_TYPES):
        return True
    # numpy and pandas scalars (np.datetime64, pd.NaT, pd.NA) print as their value.
    module = type(value).__module__.split(".")[0]
    if module not in ("numpy", "pandas") or getattr(value, "ndim", 0) != 0:
        return False
    # A 0-d object array prints the object it holds.
    return getattr(getattr(value, "dtype", None), "kind", None) != "O"


def _auto_name(obj: param.Parameterized, name: str) -> bool:
    """Whether *name* is one param generated, like ``FileSource00012``."""
    prefix = name.rstrip("0123456789")
    return len(name) - len(prefix) >= 5 and any(
        prefix == cls.__name__ for cls in type(obj).__mro__
    )


def object_label(value: t.Any) -> str:
    """Name *value* by its type, and a Parameterized by its own name too, e.g. ``FileSource · turbines``."""
    kind = type(value).__name__
    if isinstance(value, param.Parameterized):
        name = getattr(value, "name", None)
        if isinstance(name, str) and name.strip() and not _auto_name(value, name):
            name = name.strip()
            if len(name) > _MAX_NAME:
                name = name[:_MAX_NAME] + "…"
            return f"{kind} · {name}"
    return kind


def held_table_names(value: t.Any) -> list[str] | None:
    """Return the table names a source already holds, never calling ``get_tables()``, which may query it."""
    tables = getattr(value, "tables", None)
    if isinstance(tables, dict) and tables:
        return [str(k) for k in tables]
    if isinstance(tables, (list, tuple)) and tables and all(isinstance(x, str) for x in tables):
        return list(tables)
    return None


def redact_secrets(text: str) -> str:
    """Mask a URL's password and ``password=``/``token=`` style values, e.g. in an error message."""
    text = _URL_PASSWORD.sub(r"\1***@", text)
    return _SECRET_PARAM.sub(r"\1***", text)


def safe_frame(frame: t.Any) -> t.Any:
    """Return a copy of a pandas *frame* whose object cells that are not plain data show their label.

    Pass only the rows to show: every object column of *frame* is copied.
    """
    try:
        import pandas as pd
    except ImportError:
        return frame
    if not isinstance(frame, pd.DataFrame):
        return frame
    columns = [i for i, dtype in enumerate(frame.dtypes) if dtype.kind == "O"]
    if not columns:
        return frame
    frame = frame.copy()
    for i in columns:
        frame.isetitem(i, frame.iloc[:, i].map(lambda v: v if is_plain(v) else safe_repr(v)))
    return frame


def safe_str(value: t.Any) -> str:
    """Return ``str(value)`` for plain data and the object's label for anything else."""
    return str(value) if is_plain(value) else object_label(value)


def safe_repr(value: t.Any) -> str:
    """Return ``repr(value)`` for plain data and containers of it, labelling any other object."""
    if is_plain(value):
        return repr(value)
    if isinstance(value, _CONTAINERS):
        return _SAFE_REPR.repr(value)
    return object_label(value)


class SafeRepr(reprlib.Repr):
    """A ``reprlib.Repr`` that writes containers out but names any other object by its label."""

    def repr_instance(self, x: t.Any, level: int) -> str:
        if isinstance(x, dict):
            return self.repr_dict(x, level)
        if isinstance(x, list):
            return self.repr_list(x, level)
        if isinstance(x, tuple):
            return self.repr_tuple(x, level)
        if isinstance(x, (set, frozenset)):
            return self.repr_set(x, level)
        if not is_plain(x):
            return object_label(x)
        return super().repr_instance(x, level)


_SAFE_REPR = SafeRepr()
_SAFE_REPR.maxstring = _SAFE_REPR.maxother = 2000
_SAFE_REPR.maxlevel = 3
