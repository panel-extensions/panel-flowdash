"""Placeholders in text, filled from named values and tables.

A placeholder names a value and a path into it, with an optional format spec
after a colon: ``{{ kpi }}``, ``{{ kpi.share:.1% }}``. On a pandas DataFrame
a name picks a column and ``[n]`` a row, so ``{{ t.region }}`` is the first
row's cell (the usual one-row KPI result), ``{{ t.region[2] }}`` and
``{{ t[2].region }}`` the third row's and ``{{ t.region[-1] }}`` the last's.
A block repeats its text for each row of a table, at most :data:`MAX_ROWS`
times, a name after a dot reading that row::

    {{#each t limit=5}}
    - {{ .region }}: {{ .share:.1% }}
    {{/each}}

There are no expressions, sorting or aggregation: those belong in the query
that makes the table, so every number traces to a node.

Only the author's text is expanded: a block repeats its body with each
placeholder rewritten to an absolute address (``{{ t[3].region }}``), before
any value is read. Values are filled once, after that, by the caller, which
treats them as literal text, so a value is never read as a placeholder,
Markdown or HTML.
"""

from __future__ import annotations

import bisect
import math
import re
import typing as t
from collections.abc import Callable, Mapping

from panel_flowdash.util import is_failure

# Rows one block repeats at most, whatever its limit.
MAX_ROWS = 50
# Characters a text may grow to once its blocks are repeated.
MAX_EXPANDED = 100_000
# Columns a whole row shows at most.
_MAX_COLUMNS = 50

# What may follow the name a placeholder starts with: names and row numbers.
_PATH = r"(?:\.[\w-]+|\[\s*-?\d{1,9}\s*\])*"
_SEGMENT = re.compile(r"\.([\w-]+)|\[\s*(-?\d{1,9})\s*\]")
# A name after a dot inside a block: the current row, or a name in it.
_RELATIVE = r"\.(?:[\w-]+)?"
_HEAD = re.compile(r"\{\{\s*#each\b([^{}]*)\}\}")
_CLOSE = re.compile(r"\{\{\s*/each\s*\}\}")
_BODY_REF = re.compile(r"\{\{(\s*)\.((?:[\w-]+)?)(?=[\s:.\[}])")
_LIMIT = re.compile(r"limit\s*=\s*(\S*)")
# Line breaks in a value, which would end the list item or paragraph it is in.
_BREAKS = re.compile(r"\s*[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]+\s*")
_IDENTIFIER = re.compile(r"[\w-]{1,40}")


class Missing(str):
    """The text a placeholder shows when it names a column or row its table does not have."""

    detail = ""

    @property
    def problem(self) -> str:
        return self.strip("[]") + "." + self.detail


class Unavailable(Missing):
    """The text a placeholder shows when the output it reads failed: not a problem of the text."""


#: What a failed output reads as; a placeholder over it shows ``[unavailable]``.
UNAVAILABLE = Unavailable("[unavailable]")


def names_pattern(names: t.Iterable[str]) -> str:
    """Return a pattern matching exactly one of *names*, the start of a placeholder."""
    ordered = sorted(set(names), key=len, reverse=True) or ["(?!)"]
    return "(?:" + "|".join(map(re.escape, ordered)) + r")(?![\w-])"


def placeholder_pattern(root: str) -> re.Pattern:
    """Return the pattern of placeholders whose name matches *root*.

    It also matches what a block left behind (an unclosed or misplaced
    ``{{#each}}`` or ``{{/each}}``) and a row name outside a block, so they
    show as a problem rather than as raw braces.
    """
    return re.compile(
        r"\{\{\s*(?:"
        rf"(?P<root>{root})(?P<path>{_PATH})"
        rf"|(?P<rel>{_RELATIVE}){_PATH}"
        r"|(?P<tag>[#/]each\b[^{}]*)"
        r")\s*(?::(?P<spec>[^{}]*?))?\s*\}\}"
    )


def segments(root: str, path: str) -> list[str | int]:
    """Return the names and row numbers of a placeholder's name *root* and *path*."""
    parts: list[str | int] = list(root.split("."))
    for name, index in _SEGMENT.findall(path or ""):
        parts.append(name if name else int(index))
    return parts


class Lookup(t.Protocol):
    def __call__(self, value: t.Any, name: str) -> t.Any: ...


def _frame(value: t.Any) -> bool:
    return any(
        c.__name__ == "DataFrame" and c.__module__.startswith("pandas")
        for c in type(value).__mro__
    )


def _series(value: t.Any) -> bool:
    return any(
        c.__name__ == "Series" and c.__module__.startswith("pandas") for c in type(value).__mro__
    )


def _cell(value: t.Any) -> t.Any:
    """Return a cell as a plain value: a NumPy scalar as Python's, a missing value as None."""
    if type(value).__module__ == "numpy" and getattr(value, "ndim", None) == 0:
        try:
            value = value.item()
        except Exception:
            return value
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in ("NAType", "NaTType"):
        return None
    return value


def _label(name: str) -> str:
    return name if len(name) <= 40 else name[:39] + "…"


class _Column:
    """A column picked from a table by name; on its own it reads its first row."""

    __slots__ = ("name", "series")

    def __init__(self, series: t.Any, name: str):
        self.series, self.name = series, name


class _Row:
    """A row picked from a table by number; on its own it reads as its cells by column."""

    __slots__ = ("frame", "index")

    def __init__(self, frame: t.Any, index: int):
        self.frame, self.index = frame, index


def _position(length: int, index: int) -> int | None:
    position = index + length if index < 0 else index
    return position if 0 <= position < length else None


def _column(frame: t.Any, name: str) -> t.Any:
    try:
        if name in frame.columns:
            return frame[name]
    except Exception:
        return None
    return None


def _no_column(frame: t.Any, name: str) -> Missing:
    missing = Missing(f"[no column {_label(name)}]")
    missing.detail = columns_hint(frame)
    return missing


def _step(value: t.Any, part: str | int, lookup: Lookup) -> t.Any:
    """Return what *part*, a name or row number, picks from *value*: None if nothing, Missing if absent."""
    if value is None:
        return None
    if isinstance(value, Unavailable) or is_failure(value):
        return UNAVAILABLE
    if isinstance(value, _Row):
        if isinstance(part, int):
            return None
        column = _column(value.frame, part)
        if column is None:
            return _no_column(value.frame, part)
        return _cell(column.iloc[value.index])
    if isinstance(value, _Column):
        if not isinstance(part, int):
            return None
        position = _position(len(value.series), part)
        if position is None:
            return Missing(f"[no row {part}]")
        return _cell(value.series.iloc[position])
    if _frame(value):
        if isinstance(part, int):
            position = _position(len(value), part)
            return Missing(f"[no row {part}]") if position is None else _Row(value, position)
        column = _column(value, part)
        return _no_column(value, part) if column is None else _Column(column, part)
    if isinstance(part, int):
        if type(value) in (list, tuple) or _series(value):
            position = _position(len(value), part)
            if position is None:
                return Missing(f"[no item {part}]")
            return _cell(value.iloc[position] if _series(value) else value[position])
        return None
    return _cell(lookup(value, part))


def _finish(value: t.Any) -> t.Any:
    if isinstance(value, _Column):
        if not len(value.series):
            return Missing("[no rows]")
        return _cell(value.series.iloc[0])
    if isinstance(value, _Row):
        names = list(value.frame.columns)[:_MAX_COLUMNS]
        return {str(n): _cell(value.frame[n].iloc[value.index]) for n in names}
    return value


def resolve(
    namespace: Mapping[str, t.Any], parts: list[str | int], lookup: Lookup, finish: bool = True
) -> t.Any:
    """Return the value *parts* name in *namespace*: None if there is none to show, Missing if absent.

    Without *finish* a column stays a column, for a block to repeat over its cells.
    """
    value = namespace.get(parts[0]) if isinstance(namespace, Mapping) else None
    if is_failure(value):
        return UNAVAILABLE
    for part in parts[1:]:
        value = _step(value, part, lookup)
        if isinstance(value, Missing):
            return value
    return _finish(value) if finish else value


def _rows(value: t.Any) -> int | None:
    """Return how many rows a block can repeat over *value*, None if it is not a table or list."""
    if isinstance(value, _Column):
        return len(value.series)
    if _frame(value) or _series(value) or type(value) in (list, tuple):
        return len(value)
    return None


def _ref_text(parts: list[str | int]) -> str:
    text = str(parts[0])
    for part in parts[1:]:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text


class Filled(t.NamedTuple):
    """A text ready to fill: its blocks repeated, and how to fill each placeholder left in it."""

    template: str
    pattern: re.Pattern
    fill: Callable[[re.Match], str]


def prepare(
    template: str,
    root: str,
    namespace: Mapping[str, t.Any],
    format: Callable[[t.Any, str], str],
    lookup: Lookup,
    problems: list[str] | None = None,
) -> Filled:
    """Repeat the blocks of *template* over their tables and return it with its filler.

    *root* is the pattern of the names placeholders start with, *namespace*
    maps the first part of such a name to its value, *format* writes a value
    by a format spec and *lookup* picks a name from a value that is not a
    table. A block over no data yet repeats nothing; one over a value that is
    not a table, an unclosed or a nested one shows a problem. Each problem is
    added to *problems*, once.
    """
    problems = [] if problems is None else problems
    pattern = placeholder_pattern(root)
    head_args = re.compile(rf"\s+(?P<root>{root})(?P<path>{_PATH})(?P<options>[^{{}}]*)")
    notes: list[str] = []

    def problem(text: str):
        if text not in problems:
            problems.append(text)

    def note(text: str, detail: str | None) -> str:
        if detail is not None:
            problem(detail)
        notes.append(Missing(text))
        return f"{{{{#each!{len(notes) - 1}}}}}"

    def expand(head: re.Match, body: str, room: int) -> str:
        args = head_args.fullmatch(head.group(1))
        tag = head.group(0)
        if args is None:
            return note("[each names no table]", f"'{tag}' names no table it can read.")
        parts = segments(args.group("root"), args.group("path"))
        options = args.group("options").strip()
        limit = MAX_ROWS
        if options:
            found = _LIMIT.fullmatch(options)
            if found is None:
                return note(
                    "[each takes only limit=N]",
                    f"'{tag}': a block takes only 'limit=N'; sort and filter in the query instead.",
                )
            digits = found.group(1)
            if digits.isascii() and digits.isdigit():
                digits = digits.lstrip("0")
            if not digits.isascii() or not digits.isdigit():
                return note(
                    "[each limit must be 1 to 50]",
                    f"'{tag}': limit must be an integer from 1 to {MAX_ROWS}.",
                )
            if len(digits) > 2 or int(digits) > MAX_ROWS:
                problem(
                    f"'{tag}': limit must be an integer from 1 to {MAX_ROWS}, "
                    f"so it repeats at most {MAX_ROWS} rows."
                )
                limit = MAX_ROWS
            else:
                limit = int(digits)
        value = resolve(namespace, parts, lookup, finish=False)
        if isinstance(value, Unavailable):
            return note(str(value), None)
        if isinstance(value, Missing):
            return note(f"[each: {value.strip('[]')}]", f"'{tag}': {value.problem}")
        if value is None:
            return ""
        count = _rows(value)
        if count is None:
            return note("[each needs a table]", f"'{tag}' names a value that is not a table.")
        ref = _ref_text(parts)
        if body.startswith("\r\n"):
            body = body[2:]
        elif body.startswith("\n"):
            body = body[1:]
        out = []
        size = 0
        for i in range(min(count, limit)):
            row = _BODY_REF.sub(
                lambda m, i=i: (
                    f"{{{{{m.group(1)}{ref}[{i}]" + (f".{m.group(2)}" if m.group(2) else "")
                ),
                body,
            )
            size += len(row)
            if size > room:
                problem(f"'{tag}' made the text too long; it stops after {i} rows.")
                break
            out.append(row)
        return "".join(out)

    template = template or ""
    heads = list(_HEAD.finditer(template))
    if heads:
        closes = list(_CLOSE.finditer(template))
        starts = [c.start() for c in closes]
        pieces: list[str] = []
        pos = 0
        total = 0
        for n, head in enumerate(heads):
            if head.start() < pos:
                continue
            if total + head.start() - pos >= MAX_EXPANDED:
                break
            k = bisect.bisect_left(starts, head.end())
            if k == len(closes):
                problem(f"'{head.group(0)}' has no '{{{{/each}}}}'.")
                continue
            close = closes[k]
            if n + 1 < len(heads) and heads[n + 1].start() < close.start():
                problem(f"'{head.group(0)}': blocks cannot be nested.")
                continue
            before = template[pos : head.start()]
            total += len(before)
            repeated = expand(head, template[head.end() : close.start()], MAX_EXPANDED - total)
            total += len(repeated)
            pieces += [before, repeated]
            pos = close.end()
        pieces.append(template[pos : pos + max(0, MAX_EXPANDED - total)])
        template = "".join(pieces)

    def fill(match: re.Match) -> str:
        tag = match.group("tag")
        if tag is not None:
            found = re.fullmatch(r"#each!(\d+)\s*", tag)
            if found is not None and int(found.group(1)) < len(notes):
                return notes[int(found.group(1))]
            if tag.startswith("/"):
                problem("'{{/each}}' closes no block.")
                return Missing("[each without start]")
            return Missing("[each not repeated]")
        if match.group("rel") is not None:
            problem(f"'{match.group(0)}' reads a row, so it belongs inside {{{{#each}}}}.")
            return Missing("[row outside each]")
        parts = segments(match.group("root"), match.group("path"))
        value = resolve(namespace, parts, lookup)
        if isinstance(value, Missing):
            if not isinstance(value, Unavailable):
                problem(f"'{match.group(0)}': {value.problem}")
            return value
        return _BREAKS.sub(" ", format(value, (match.group("spec") or "").strip()))

    return Filled(template, pattern, fill)


def columns_hint(value: t.Any) -> str:
    """Return the addressable column names of a table, for a problem message."""
    if not _frame(value):
        return ""
    names = [str(c) for c in list(value.columns)[:20] if _IDENTIFIER.fullmatch(str(c))]
    return f" Its columns: {', '.join(names)}." if names else ""


__all__ = [
    "MAX_EXPANDED",
    "MAX_ROWS",
    "UNAVAILABLE",
    "Filled",
    "Missing",
    "Unavailable",
    "columns_hint",
    "names_pattern",
    "placeholder_pattern",
    "prepare",
    "resolve",
    "segments",
]
