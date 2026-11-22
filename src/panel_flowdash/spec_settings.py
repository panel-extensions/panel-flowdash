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
import datetime as dt
import io
import json
import typing as t
from dataclasses import dataclass, field

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

Path = tuple[str, ...]

#: The controls a setting can ask for.
SETTING_WIDGETS = ("text", "select", "radio", "switch", "integer", "number")

#: The ``widget`` metadata value that asks for a spec form.
SPEC_WIDGET = "spec"

_MISSING = object()


class SpecPathError(ValueError):
    """A path cannot be written because a value on the way to it is not an object."""


class SpecTextError(ValueError):
    """Spec text that is not a JSON object or YAML mapping, with the line and column (from 1) at fault."""

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
        only while the size is fixed. Returning a sentence instead of False
        disables it and shows the sentence under the form.
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
    enabled: t.Callable[[dict], bool | str] | None = field(default=None, compare=False)
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

    def disabled_reason(self, spec: dict) -> str | None:
        """Return why the control cannot be used for *spec*, or None if it can.

        ``enabled`` may return a sentence saying why instead of False.
        """
        if self.enabled is None:
            return None
        enabled = self.enabled(spec)
        if isinstance(enabled, str):
            return enabled or None
        return None if enabled else ""

    def is_enabled(self, spec: dict) -> bool:
        return self.disabled_reason(spec) is None


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


def unrepresented_paths(spec: dict, settings: t.Iterable[SpecSetting]) -> list[Path]:
    """Return the paths of *spec* the form cannot show, which are kept as written.

    Those are the paths no setting covers and those holding a value their
    setting's control cannot show, e.g. a list of colours where a scheme is
    picked.
    """
    settings = list(settings)
    found = unknown_paths(spec, settings)
    for setting in shown_in_spec(spec, settings):
        if has_path(spec, setting.path) and setting.path not in found:
            found.append(setting.path)
    return found


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


def _yaml() -> YAML:
    """Return a YAML 1.2 round-trip reader and writer, so `on` or `no` stay strings and comments are kept."""
    yaml = YAML()
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Long strings, e.g. Vega expressions, stay on one line.
    yaml.width = 4096
    yaml.representer.add_representer(
        type(None), lambda r, _value: r.represent_scalar("tag:yaml.org,2002:null", "null")
    )
    return yaml


def _as_json(spec: t.Any) -> t.Any:
    """Return a copy of *spec* holding JSON values only, as :func:`format_spec` writes them."""
    return json.loads(json.dumps(spec, ensure_ascii=False, default=str))


def _plain(value: t.Any) -> t.Any:
    """Return what YAML read as plain JSON values: dicts, lists, strings, numbers, booleans and None.

    A date, which YAML reads from an unquoted ``2024-01-31``, stays the
    string it was written as.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if hasattr(value, "value"):
        # A tagged scalar, e.g. `!foo bar`, keeps its value.
        return _plain(value.value)
    return value


def _same(a: t.Any, b: t.Any) -> bool:
    """Whether two plain values are the same as JSON, so ``1`` and ``True`` differ and key order counts."""
    try:
        return json.dumps(a, default=str) == json.dumps(b, default=str)
    except (TypeError, ValueError):
        return False


def parse_spec_yaml(text: str) -> tuple[dict, t.Any]:
    """Return the spec YAML 1.2 *text* holds, ``{}`` for blank text, and the document it was read from.

    The document keeps the text's comments and layout, see :func:`format_spec_yaml`.

    Raises
    ------
    SpecTextError
        Saying where the text is not valid YAML, or that it is not a mapping.
    """
    if not text.strip():
        return {}, None
    try:
        doc = _yaml().load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        line, column = (mark.line + 1, mark.column + 1) if mark is not None else (1, 1)
        problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        raise SpecTextError(f"Line {line}, column {column}: {problem}.", line, column) from exc
    if doc is None:
        return {}, None
    if not isinstance(doc, dict):
        raise SpecTextError("The spec must be a YAML mapping of keys to values.")
    return _plain(doc), doc


def _merged(node: t.Any, value: t.Any) -> t.Any:
    """Return *value* written into the YAML *node* in place, keeping the comments of what stays."""
    if isinstance(node, CommentedMap) and isinstance(value, dict):
        for key in [k for k in node if str(k) not in value]:
            del node[key]
        keys = {str(k): k for k in node}
        for key, item in value.items():
            if key in keys:
                node[keys[key]] = _merged(node[keys[key]], item)
            else:
                node[key] = item
        for key in value:
            # The spec's order, which a form keeps, wins over the text's.
            node.move_to_end(keys.get(key, key))
        return node
    if isinstance(node, CommentedSeq) and isinstance(value, list) and len(node) == len(value):
        for i, item in enumerate(value):
            node[i] = _merged(node[i], item)
        return node
    return node if _same(_plain(node), value) else value


def format_spec_yaml(
    spec: t.Any, doc: t.Any = None
) -> tuple[str, dict[Path, tuple[int, int]], t.Any]:
    """Return *spec* as YAML 1.2 text, its lines per path, and the document written.

    With the *doc* of earlier text, see :func:`parse_spec_yaml`, the spec is
    written into it, so the comments and layout of the keys that stay are
    kept. An empty spec is blank text.
    """
    if not spec:
        return "", {}, None
    if isinstance(doc, CommentedMap):
        doc = _merged(doc, _as_json(spec))
    else:
        doc = _yaml().load(_dump(_as_json(spec)))
    text = _dump(doc)
    return text, text_spans(text), doc


def _dump(value: t.Any) -> str:
    out = io.StringIO()
    _yaml().dump(value, out)
    return out.getvalue().rstrip("\n")


def text_spans(text: str) -> dict[Path, tuple[int, int]]:
    """Return the first and last line (from 0) each path takes in spec text, JSON or YAML as written.

    A path ends on the line before the next key or item at its level, so
    comments after it count as its own, leaving out closing brackets and
    blank lines. Text that does not parse has no lines.
    """
    try:
        doc = _yaml().load(text) if text.strip() else None
    except YAMLError:
        return {}
    spans: dict[Path, tuple[int, int]] = {}
    lines = text.splitlines()
    last = len(text.rstrip().splitlines()) - 1

    def walk(node: t.Any, prefix: Path, end: int):
        if isinstance(node, CommentedMap):
            entries = [((*prefix, str(k)), node.lc.key(k)[0], node[k]) for k in node]
        elif isinstance(node, CommentedSeq):
            entries = [((*prefix, str(i)), node.lc.item(i)[0], v) for i, v in enumerate(node)]
        else:
            return
        for i, (path, start, value) in enumerate(entries):
            stop = entries[i + 1][1] - 1 if i + 1 < len(entries) else end
            # Closing brackets and blank lines after a value are not its own.
            while stop > start and lines[stop].strip().rstrip(",") in ("", "}", "]"):
                stop -= 1
            spans[path] = (start, stop)
            walk(value, path, stop)

    walk(doc, (), last)
    return spans
