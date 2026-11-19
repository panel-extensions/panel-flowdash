"""Curated settings bound to paths in a spec, so a form and the spec's text edit it together.

A component whose config holds a spec, e.g. a Vega-Lite chart, lists the
settings worth a form control, each a path into the spec::

    spec = param.Dict(
        default={},
        metadata={"flowdash": {"widget": "spec", "settings": [
            SpecSetting("title", label="Title", group="Labels"),
            SpecSetting("mark.type", label="Mark", widget="radio", options=["bar", "line"]),
            SpecSetting("encoding.x.field", label="X", choices="columns", of="pipeline"),
        ]}},
    )

The inspector then edits the spec as a form over those settings beside the
spec's JSON. Changing a control writes its path into the spec, and editing
the text reads every path back into the form. Keys no setting covers are kept
exactly as written and reported by :func:`unknown_paths`, so the form never
drops what it cannot show. A setting whose value in the spec its control
cannot show, e.g. a title given as an object, is left alone too
(:meth:`SpecSetting.fits`).

Writing a setting's ``default`` removes its path, along with the objects that
held only it, so a spec says no more than it has to.
"""

from __future__ import annotations

import copy
import json
import typing as t
from dataclasses import dataclass, field

Path = tuple[str, ...]

#: The controls a setting can ask for.
SETTING_WIDGETS = ("text", "select", "radio", "switch", "integer", "number")

#: The ``widget`` metadata value that asks for a spec form.
SPEC_WIDGET = "spec"

_MISSING = object()


class SpecPathError(ValueError):
    """A path cannot be written because a value on the way to it is not an object."""


class SpecTextError(ValueError):
    """Spec text that is not a JSON object, with the line and column (from 1) at fault."""

    def __init__(self, message: str, line: int = 1, column: int = 1):
        super().__init__(message)
        self.line = line
        self.column = column


def as_path(path: str | t.Sequence[str]) -> Path:
    """Return *path*, dotted like ``"encoding.x.title"`` or a sequence of keys, as a tuple of keys."""
    if isinstance(path, str):
        return tuple(path.split("."))
    return tuple(str(key) for key in path)


def path_text(path: Path) -> str:
    """Return *path* dotted, as it is reported to people."""
    return ".".join(path)


def get_path(spec: t.Any, path: str | t.Sequence[str], default: t.Any = None) -> t.Any:
    """Return the value at *path* in *spec*, or *default* if a key on the way is missing."""
    node = spec
    for key in as_path(path):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def has_path(spec: t.Any, path: str | t.Sequence[str]) -> bool:
    """Whether *spec* holds a value at *path*."""
    return get_path(spec, path, _MISSING) is not _MISSING


def set_path(spec: dict, path: str | t.Sequence[str], value: t.Any) -> None:
    """Set *path* in *spec* to *value* in place, adding the objects on the way.

    A key that exists keeps its place among its siblings.

    Raises
    ------
    SpecPathError
        If a value on the way is not an object, e.g. ``"mark": "line"`` when
        setting ``mark.point``; it is not replaced.
    """
    keys = as_path(path)
    node = spec
    for i, key in enumerate(keys[:-1]):
        child = node.get(key, _MISSING)
        if child is _MISSING:
            child = node[key] = {}
        elif not isinstance(child, dict):
            raise SpecPathError(
                f"{path_text(keys[: i + 1])} is {json.dumps(child)}, not an object, "
                f"so {path_text(keys)} cannot be set."
            )
        node = child
    node[keys[-1]] = value


def delete_path(spec: dict, path: str | t.Sequence[str]) -> None:
    """Remove *path* from *spec* in place, and the objects on the way it leaves empty."""
    keys = as_path(path)
    nodes = [spec]
    for key in keys[:-1]:
        child = nodes[-1].get(key)
        if not isinstance(child, dict):
            return
        nodes.append(child)
    if keys[-1] not in nodes[-1]:
        return
    del nodes[-1][keys[-1]]
    for depth in range(len(keys) - 1, 0, -1):
        if nodes[depth]:
            break
        del nodes[depth - 1][keys[depth - 1]]


@dataclass(frozen=True)
class SpecSetting:
    """One setting of a spec that a form shows as a control bound to a path.

    Parameters
    ----------
    path
        Where the setting lives in the spec, dotted (``"encoding.x.title"``)
        or as a sequence of keys.
    label
        The control's label; defaults to the last key of the path.
    widget
        The control: ``"text"``, ``"select"``, ``"radio"`` (a row of
        buttons), ``"switch"``, ``"integer"`` or ``"number"``.
    options
        The values a select or radio offers, a list or a ``{label: value}`` dict.
    group
        The heading the control is listed under.
    default
        What the spec means when it leaves the path out. The control shows
        it then, and setting it removes the path.
    doc
        A sentence shown as the control's tooltip.
    choices, of
        ``choices="columns"`` offers the columns of the node's input *of*,
        like a config param's choices.
    get, set
        For a setting that is not one path: ``get(spec)`` returns its value
        and ``set(spec, value)`` writes it into a copy of the spec in place.
    covers
        Further paths the setting controls, e.g. those its ``get`` reads.
        Keys under a covered path are not reported as unknown.
    enabled
        ``enabled(spec)`` says whether the control can be used, e.g. a width
        only while the size is fixed.
    restrict
        Whether a select or radio accepts only its options.
    """

    path: str | tuple[str, ...]
    label: str | None = None
    widget: str = "text"
    options: list[t.Any] | dict[str, t.Any] | None = None
    group: str | None = None
    default: t.Any = None
    doc: str | None = None
    choices: str | None = None
    of: str | None = None
    get: t.Callable[[dict], t.Any] | None = field(default=None, compare=False)
    set: t.Callable[[dict, t.Any], None] | None = field(default=None, compare=False)
    covers: tuple[str | tuple[str, ...], ...] = ()
    enabled: t.Callable[[dict], bool] | None = field(default=None, compare=False)
    restrict: bool = True

    def __post_init__(self):
        """Check the widget and turn the paths into tuples of keys."""
        if self.widget not in SETTING_WIDGETS:
            raise ValueError(
                f"SpecSetting {self.path!r} asks for widget {self.widget!r}; "
                f"use one of {', '.join(SETTING_WIDGETS)}."
            )
        object.__setattr__(self, "path", as_path(self.path))
        object.__setattr__(self, "covers", tuple(as_path(p) for p in self.covers))

    @property
    def key(self) -> str:
        """The setting's dotted path, which names it, e.g. in choices."""
        return path_text(self.path)

    @property
    def title(self) -> str:
        return self.label or self.path[-1]

    @property
    def paths(self) -> tuple[Path, ...]:
        """The paths the setting controls."""
        return (self.path, *self.covers)

    def labelled(self) -> dict[str, t.Any]:
        """Return the options as ``{label: value}``."""
        if isinstance(self.options, dict):
            return dict(self.options)
        return {str(value): value for value in self.options or []}

    def read(self, spec: dict) -> t.Any:
        """Return the setting's value in *spec*, its default if the spec leaves it out."""
        if self.get is not None:
            return self.get(spec)
        return get_path(spec, self.path, self.default)

    def write(self, spec: dict, value: t.Any) -> dict:
        """Return a copy of *spec* with the setting set to *value*.

        Everything else is kept as it was, in its order. Setting the
        default removes the path.

        Raises
        ------
        SpecPathError
            If the path runs through a value that is not an object.
        """
        if self.widget == "text" and value == "":
            value = None
        new = copy.deepcopy(spec)
        if self.set is not None:
            self.set(new, value)
        elif value == self.default:
            delete_path(new, self.path)
        else:
            set_path(new, self.path, value)
        return new

    def fits(self, value: t.Any) -> bool:
        """Whether the control can show *value* without changing it.

        A select or radio shows any plain value, one outside its options as
        an extra option.
        """
        if value is None or value == self.default:
            return True
        if self.widget == "text":
            return isinstance(value, str)
        if self.widget == "switch":
            return isinstance(value, bool)
        if self.widget == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if self.widget == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, (str, int, float, bool))

    def accepts(self, value: t.Any) -> bool:
        """Whether *value* fits the control and, for a restricted select or radio, its options."""
        if not self.fits(value):
            return False
        if self.widget not in ("select", "radio") or not self.restrict or self.options is None:
            return True
        return value is None or value == self.default or value in self.labelled().values()

    def is_enabled(self, spec: dict) -> bool:
        return self.enabled is None or bool(self.enabled(spec))


def as_setting(value: t.Any) -> SpecSetting:
    """Return *value*, a :class:`SpecSetting` or a dict of its fields, as a SpecSetting."""
    if isinstance(value, SpecSetting):
        return value
    if isinstance(value, dict):
        return SpecSetting(**value)
    raise TypeError(f"A spec setting must be a SpecSetting or a dict, not {type(value).__name__}.")


def declared_settings(parameter: t.Any) -> list[SpecSetting] | None:
    """Return the settings a param's FlowDash metadata declares for a spec form, or None.

    A ``param.Dict`` with ``metadata={"flowdash": {"widget": "spec", "settings": [...]}}``
    declares them.
    """
    import param

    if not isinstance(parameter, param.Dict):
        return None
    flowdash = (parameter.metadata or {}).get("flowdash")
    if not isinstance(flowdash, dict):
        return None
    if flowdash.get("widget") != SPEC_WIDGET:
        return None
    return [as_setting(s) for s in flowdash.get("settings") or []]


def _covered(path: Path, covered: t.Iterable[Path]) -> bool:
    return any(path[: len(c)] == c for c in covered)


def _leads_to(path: Path, covered: t.Iterable[Path]) -> bool:
    return any(len(c) > len(path) and c[: len(path)] == path for c in covered)


def unknown_paths(spec: dict, settings: t.Iterable[SpecSetting]) -> list[Path]:
    """Return the paths of *spec* no setting covers, outermost first, in the spec's order.

    An object holding both covered and other keys is looked into, so
    ``encoding.x.axis`` is reported for an ``encoding.x`` whose ``field`` a
    setting covers. A plain value where a setting expects an object, such as
    Vega-Lite's ``"mark": "bar"`` for a setting of ``mark.type``, is that
    setting's shorthand and not reported.
    """
    covered = [path for setting in settings for path in setting.paths]
    found: list[Path] = []

    def walk(node: dict, prefix: Path):
        for key, value in node.items():
            path = (*prefix, str(key))
            if _covered(path, covered):
                continue
            if not _leads_to(path, covered):
                found.append(path)
            elif isinstance(value, dict):
                walk(value, path)

    if isinstance(spec, dict):
        walk(spec, ())
    return found


def shown_in_spec(spec: dict, settings: t.Iterable[SpecSetting]) -> list[SpecSetting]:
    """Return the settings whose value in *spec* their control cannot show, kept as written."""
    return [s for s in settings if not s.fits(s.read(spec))]


def problems(spec: dict, settings: t.Iterable[SpecSetting]) -> list[str]:
    """Return what is wrong with the values of *spec* at restricted settings, for a config check.

    Only a select or radio restricted to its options is checked, since a
    value the other controls cannot show may still be valid in the spec.
    """
    found = []
    for setting in settings:
        if setting.widget not in ("select", "radio") or not setting.restrict:
            continue
        if setting.options is None or setting.get is not None:
            continue
        value = get_path(spec, setting.path, _MISSING)
        if value is _MISSING or setting.accepts(value):
            continue
        allowed = ", ".join(json.dumps(v) for v in setting.labelled().values())
        found.append(f"{setting.key} is {json.dumps(value)}; it must be one of {allowed}.")
    return found


def changed_paths(old: t.Any, new: t.Any, prefix: Path = ()) -> list[Path]:
    """Return the paths whose value differs from *old* to *new*, innermost, in *new*'s order.

    Removed keys are not listed, since they have no place in *new*.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [] if old == new else [prefix]
    changed = []
    for key, value in new.items():
        path = (*prefix, str(key))
        if key not in old:
            changed.append(path)
        elif old[key] != value:
            changed.extend(changed_paths(old[key], value, path))
    if not changed and list(old) != list(new) and prefix:
        changed.append(prefix)
    return changed


def format_spec(spec: t.Any, indent: int = 2) -> tuple[str, dict[Path, tuple[int, int]]]:
    """Return *spec* as JSON text, as ``json.dumps(spec, indent=indent)`` writes it, and its lines.

    The lines map each path to the first and last line (from 0) its key and
    value take, so a form can point at the lines a setting changed.
    """
    lines: list[str] = []
    spans: dict[Path, tuple[int, int]] = {}

    def dump(value: t.Any, path: Path, depth: int, lead: str, trail: str):
        pad = " " * (indent * depth)
        start = len(lines)
        if isinstance(value, dict) and value:
            lines.append(f"{pad}{lead}{{")
            items = list(value.items())
            for i, (key, item) in enumerate(items):
                key_text = json.dumps(str(key), ensure_ascii=False)
                dump(
                    item,
                    (*path, str(key)),
                    depth + 1,
                    f"{key_text}: ",
                    "," if i < len(items) - 1 else "",
                )
            lines.append(f"{pad}}}{trail}")
        elif isinstance(value, list) and value:
            lines.append(f"{pad}{lead}[")
            for i, item in enumerate(value):
                dump(item, (*path, str(i)), depth + 1, "", "," if i < len(value) - 1 else "")
            lines.append(f"{pad}]{trail}")
        else:
            lines.append(f"{pad}{lead}{json.dumps(value, ensure_ascii=False)}{trail}")
        if path:
            spans[path] = (start, len(lines) - 1)

    dump(spec, (), 0, "", "")
    return "\n".join(lines), spans


def parse_spec(text: str) -> dict:
    """Return the spec JSON *text* holds, ``{}`` for blank text.

    Raises
    ------
    SpecTextError
        Saying where the text is not valid JSON, or that it is not an object.
    """
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecTextError(
            f"Line {exc.lineno}, column {exc.colno}: {exc.msg}.", exc.lineno, exc.colno
        ) from exc
    if not isinstance(value, dict):
        raise SpecTextError("The spec must be a JSON object, in { }.")
    return value
