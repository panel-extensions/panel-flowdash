"""Transform steps that reshape the value carried along an edge.

A transform is stored as a list of plain step dicts such as
``{"op": "aggregate", "how": "sum"}``, never as code, so loading a dashboard
cannot execute anything arbitrary. Each operation is a Parameterized class
whose params declare and validate the arguments of its step.
"""

from __future__ import annotations

import numbers
import operator
import typing as t

import pandas as pd
import param

TRANSFORMS: dict[str, type[Transform]] = {}


class StepInputError(TypeError):
    """Raised when a step is applied to a kind of value it does not accept."""


def _is_frame(value) -> bool:
    return isinstance(value, pd.DataFrame)


def _is_sequence(value) -> bool:
    return isinstance(value, (pd.Series, list, tuple))


def _is_number(value) -> bool:
    return isinstance(value, numbers.Number) and not isinstance(value, bool)


def _scalar(value):
    """Unwrap a numpy scalar, so downstream ports receive plain Python values."""
    item = getattr(value, "item", None)
    return item() if callable(item) and getattr(value, "ndim", None) == 0 else value


class Transform(param.Parameterized):
    """One step of an edge transform.

    Subclasses set ``op`` to register themselves under that name and implement
    :meth:`apply`. ``accepts`` tells the editor which values a step can follow.
    """

    op: t.ClassVar[str] = ""
    label: t.ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs):
        """Register the subclass under its ``op``."""
        super().__init_subclass__(**kwargs)
        if cls.op:
            TRANSFORMS[cls.op] = cls

    @classmethod
    def accepts(cls, value) -> bool:
        """Return whether this step can be applied to *value*."""
        return True

    @classmethod
    def arguments(cls) -> list[str]:
        """Names of the arguments a step of this operation stores."""
        return [name for name in cls.param if name != "name"]

    def apply(self, value):
        """Return *value* transformed by this step."""
        raise NotImplementedError

    def __call__(self, value):
        """Apply the step, refusing values it does not accept; an unset value stays unset."""
        if value is None:
            return None
        if not self.accepts(value):
            raise StepInputError(f"{self.label} cannot be applied to a {type(value).__name__}.")
        return self.apply(value)

    def summary(self) -> str:
        """Return a few words describing the step, for the label on an edge."""
        return self.label.lower() or self.op

    def to_dict(self) -> dict[str, t.Any]:
        """Serialize the step to the dict it is stored as."""
        return {"op": self.op, **{name: getattr(self, name) for name in self.arguments()}}


class Column(Transform):
    """Select a single column of a DataFrame."""

    op = "column"
    label = "Column"

    column = param.String(default="", doc="Name of the column to select.")

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_frame(value)

    def apply(self, value):
        return value[self.column]

    def summary(self) -> str:
        return self.column


_COMPARISONS: dict[str, t.Callable[[t.Any, t.Any], t.Any]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "in": lambda series, values: series.isin(values),
}


class Filter(Transform):
    """Keep the rows of a DataFrame whose column compares true against a value.

    Without a value every row is kept, so a filter that was just added to an
    edge does not empty it before a value has been entered.
    """

    op = "filter"
    label = "Filter rows"

    column = param.String(default="", doc="Column to compare.")
    comparison = param.Selector(default="==", objects=list(_COMPARISONS))
    value = param.Parameter(default=None, doc="Value to compare against; a list for 'in'.")

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_frame(value)

    def apply(self, value):
        if self.value is None:
            return value
        return value[_COMPARISONS[self.comparison](value[self.column], self.value)]

    def summary(self) -> str:
        return f"{self.column} {self.comparison} {self.value!r}"


class Sort(Transform):
    """Sort a DataFrame by a column, or a Series or list by its values."""

    op = "sort"
    label = "Sort"

    column = param.String(default=None, allow_None=True, doc="Column to sort a DataFrame by.")
    ascending = param.Boolean(default=True)

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_frame(value) or _is_sequence(value)

    def apply(self, value):
        if _is_frame(value):
            return value.sort_values(self.column, ascending=self.ascending)
        if isinstance(value, pd.Series):
            return value.sort_values(ascending=self.ascending)
        return sorted(value, reverse=not self.ascending)

    def summary(self) -> str:
        arrow = "↑" if self.ascending else "↓"
        return f"sort {self.column} {arrow}" if self.column else f"sort {arrow}"


class Head(Transform):
    """Keep the first rows of a DataFrame or the first items of a sequence."""

    op = "head"
    label = "First rows"

    n = param.Integer(default=5, bounds=(0, None))

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_frame(value) or _is_sequence(value)

    def apply(self, value):
        if isinstance(value, (pd.DataFrame, pd.Series)):
            return value.head(self.n)
        return list(value[: self.n])

    def summary(self) -> str:
        return f"first {self.n}"


class Aggregate(Transform):
    """Reduce a Series or list to a single value."""

    op = "aggregate"
    label = "Aggregate"

    how = param.Selector(default="sum", objects=["sum", "mean", "min", "max", "count", "nunique"])

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_sequence(value)

    def apply(self, value):
        series = value if isinstance(value, pd.Series) else pd.Series(list(value))
        return _scalar(getattr(series, self.how)())

    def summary(self) -> str:
        return self.how


class Unique(Transform):
    """Collect the distinct values of a Series or list, in order of appearance."""

    op = "unique"
    label = "Unique values"

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_sequence(value)

    def apply(self, value):
        if isinstance(value, pd.Series):
            return [_scalar(v) for v in value.dropna().unique()]
        return list(dict.fromkeys(value))


class Round(Transform):
    """Round a number, or every value of a Series, to a number of digits."""

    op = "round"
    label = "Round"

    digits = param.Integer(default=0, bounds=(0, None))

    @classmethod
    def accepts(cls, value) -> bool:
        return _is_number(value) or isinstance(value, pd.Series)

    def apply(self, value):
        if isinstance(value, pd.Series):
            return value.round(self.digits)
        return round(value, self.digits) if self.digits else round(value)

    def summary(self) -> str:
        return f"round {self.digits}"


class Format(Transform):
    """Format a value as a string with a format spec, prefix and suffix.

    Only a format spec is accepted, not a template, because a template
    such as ``{value.__class__}`` could reach attributes of the value.
    """

    op = "format"
    label = "Format"

    spec = param.String(default="", doc="Format spec, for example ',.2f' or '.0%'.")
    prefix = param.String(default="")
    suffix = param.String(default="")

    def apply(self, value):
        return f"{self.prefix}{format(value, self.spec)}{self.suffix}"


def parse_transform(steps: t.Iterable[dict[str, t.Any]] | None) -> list[Transform]:
    """Build the steps of a stored transform, raising ValueError if one is invalid."""
    parsed = []
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict) or "op" not in step:
            raise ValueError(f"Transform step {index + 1} must be a dict with an 'op'.")
        args = dict(step)
        op = args.pop("op")
        cls = TRANSFORMS.get(op)
        if cls is None:
            raise ValueError(f"Unknown transform '{op}'.")
        unknown = set(args) - set(cls.arguments())
        if unknown:
            raise ValueError(
                f"Transform '{op}' got unknown arguments: {', '.join(sorted(unknown))}."
            )
        try:
            parsed.append(cls(**args))
        except Exception as exc:
            raise ValueError(f"Transform '{op}': {exc}") from exc
    return parsed


def apply_transform(steps: t.Iterable[Transform], value):
    """Run *value* through *steps*; an unset value stays unset."""
    for step in steps:
        value = step(value)
    return value


def summarize_transform(steps: t.Iterable[Transform], max_length: int = 40) -> str | None:
    """Describe *steps* in a few words, or return None for an empty transform."""
    text = " › ".join(step.summary() for step in steps)  # noqa: RUF001
    if not text:
        return None
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def available_transforms(value) -> list[type[Transform]]:
    """Return the operations that can follow a step producing *value*."""
    return [cls for cls in TRANSFORMS.values() if cls.accepts(value)]
