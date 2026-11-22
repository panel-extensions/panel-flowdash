"""Read and write the project file, ``flowdash.yaml``.

The project file is one YAML 1.2 file of named sections that configure a
FlowDash app, e.g. the admin settings or an extension's ``metrics:``. Every
key and value keeps the line and column it is written at, so a problem is
reported where it is.

``flowdash serve`` reads ``flowdash.yaml`` (``PROJECT_FILE``), given with
``--project`` or found in the served directory (``find_project``). Each
top-level section is read by the loader registered for it
(``register_section``); a section no one registered is ignored with a
warning, so a file written for a newer app or for an extension that is not
installed still loads.

A ``ProjectSection`` is the unit an extension registers, programmatically
or through the ``panel_flowdash.project_sections`` entry point group: the
section's loader, its ``dump`` for writing it back and, optionally, the
factory of its admin settings tab.
"""

from __future__ import annotations

import importlib.metadata
import io
import logging
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, SequenceNode
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

__all__ = [
    "ENTRY_POINT_GROUP",
    "KEY",
    "PROJECT_FILE",
    "Problem",
    "Project",
    "ProjectError",
    "ProjectSection",
    "Section",
    "SectionError",
    "expand_env",
    "find_project",
    "load_entry_points",
    "load_project",
    "parse_project",
    "register_section",
    "sections",
    "unregister_section",
]

PROJECT_FILE = "flowdash.yaml"

ENTRY_POINT_GROUP = "panel_flowdash.project_sections"

logger = logging.getLogger(__name__)

# The last element of a path that names where a key is written rather than its value.
KEY = object()

_NOT_A_MAPPING = "A project file is a mapping of sections, e.g. `metrics:`."


@dataclass(frozen=True)
class Problem:
    """A problem in a project file.

    ``path``, ``line`` and ``column`` say where it is written; ``section``
    and ``keys`` say which value it concerns, e.g. ``"metrics"`` and
    ``("capacity", "table")``, so a settings form can show it at its field.
    """

    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None
    section: str | None = None
    keys: tuple[str | int, ...] = ()

    def __str__(self) -> str:  # noqa: D105
        where = ":".join(
            str(part) for part in (self.path, self.line, self.column) if part is not None
        )
        return f"{where}: {self.message}" if where else self.message


class ProjectError(ValueError):
    """The problems of a project file, each with the file, line and column it concerns."""

    def __init__(self, problems: Sequence[Problem]):
        self.problems = list(problems)
        super().__init__("\n".join(map(str, self.problems)))


class SectionError(ValueError):
    """A section loader's problem at ``path`` inside its section.

    ``path`` is e.g. ``("capacity", "table")``. With ``key=True`` the problem
    is reported where the last key of ``path`` is written instead of its value.
    """

    def __init__(self, message: str, path: Sequence[str | int] = (), key: bool = False):
        super().__init__(message)
        self.message = message
        self.path = tuple(path)
        self.key = key


Loader = Callable[["Section"], Any]
Dumper = Callable[[Any], Any]
# Builds the admin settings tab of a section: ``settings_view(project, editable)``.
SettingsView = Callable[["Project", bool], Any]


@dataclass(frozen=True)
class ProjectSection:
    """A top-level section of the project file, as an extension registers it.

    ``loader`` turns the section's raw YAML data (``Section.value``) into
    the typed value ``Project.get(name)`` gives, reporting problems by
    raising ``SectionError`` (one problem at a path in the section) or
    ``ProjectError`` (several, made with ``Section.problem``) and warnings
    with ``Section.warn``; a ``ValueError`` or ``TypeError`` is reported at
    the section's key. ``dump`` turns a typed value back into plain YAML
    data for ``Project.set``; without it the value is written as it is.
    ``settings_view(project, editable)``, if given, builds the section's tab
    in the admin settings.
    """

    name: str
    loader: Loader
    title: str = ""
    icon: str | None = None
    description: str = ""
    dump: Dumper | None = None
    settings_view: SettingsView | None = None

    @property
    def label(self) -> str:
        """Return the title, or one made from the name."""
        return self.title or self.name.replace("_", " ").capitalize()


_SECTIONS: dict[str, ProjectSection] = {}
_REGISTRY_LOCK = threading.RLock()
_entry_points_loaded = False
_entry_points_loading = False

# Serializes the read-modify-write of project files within the process.
_WRITE_LOCK = threading.Lock()


def register_section(
    section: ProjectSection | str, loader: Loader | None = None, **fields: Any
) -> ProjectSection:
    """Register a section of the project file and return it.

    The section is a ``ProjectSection``, or its name with its loader and
    other ``ProjectSection`` fields. Registering a name again replaces it,
    so a module that is reloaded can register again.
    """
    if isinstance(section, str):
        if loader is None:
            raise TypeError(f"register_section({section!r}) needs a loader.")
        section = ProjectSection(section, loader, **fields)
    elif loader is not None or fields:
        raise TypeError("Pass either a ProjectSection or a name with its loader and fields.")
    with _REGISTRY_LOCK:
        previous = _SECTIONS.get(section.name)
        if previous is not None and _qualname(previous.loader) != _qualname(section.loader):
            logger.warning(
                "The project file section %r is registered again; %s replaces %s.",
                section.name,
                _qualname(section.loader),
                _qualname(previous.loader),
            )
        _SECTIONS[section.name] = section
    return section


def unregister_section(name: str) -> None:
    """Remove the section ``name`` from the registry, if it is registered."""
    with _REGISTRY_LOCK:
        _SECTIONS.pop(name, None)


def sections() -> dict[str, ProjectSection]:
    """Return the registered sections by name, including those of installed extensions."""
    load_entry_points()
    with _REGISTRY_LOCK:
        return dict(_SECTIONS)


def load_entry_points(force: bool = False) -> None:
    """Register the sections installed packages declare as entry points, once.

    The group is ``panel_flowdash.project_sections``. Each entry point names
    a ``ProjectSection``, a sequence of them, or a callable returning either.
    One that fails to load is skipped with a warning. Concurrent callers
    wait until every entry point is registered.
    """
    global _entry_points_loaded, _entry_points_loading
    with _REGISTRY_LOCK:
        # An entry point that looks up the sections while it loads sees those registered so far.
        if _entry_points_loading or (_entry_points_loaded and not force):
            return
        _entry_points_loading = True
        try:
            for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
                _register_entry_point(entry_point)
            _entry_points_loaded = True
        finally:
            _entry_points_loading = False


def _register_entry_point(entry_point: importlib.metadata.EntryPoint) -> None:
    try:
        found = entry_point.load()
        if callable(found) and not isinstance(found, ProjectSection):
            found = found()
        for section in [found] if isinstance(found, ProjectSection) else list(found):
            if not isinstance(section, ProjectSection):
                raise TypeError(f"expected a ProjectSection, got {type(section).__name__}")
            register_section(section)
    except Exception as exc:
        logger.warning(
            "Skipping the project file sections of entry point %r (%s): %s",
            entry_point.name,
            entry_point.value,
            exc,
        )


def _qualname(loader: Loader) -> str:
    module = getattr(loader, "__module__", "?")
    return f"{module}.{getattr(loader, '__qualname__', repr(loader))}"


_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    """Replace ``${NAME}`` and ``${NAME:-default}`` references with environment values.

    Text in ``value``, or in the lists and mappings it holds, is expanded;
    other values are returned as they are. Raises ``ValueError`` naming a
    variable that is not set and has no default. A loader keeps the
    reference and expands it where it is used, so ``dump`` writes the
    reference back and never the secret.
    """
    env = os.environ if environ is None else environ
    if isinstance(value, str):

        def replace(match: re.Match) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ValueError(f"The environment variable {name} is not set.")

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, Mapping):
        return {key: expand_env(item, env) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [expand_env(item, env) for item in value]
    return value


@dataclass(frozen=True)
class Section:
    """One top-level section of a project file, as a loader receives it."""

    name: str
    value: Any
    project: Project

    @property
    def context(self) -> Mapping[str, Any]:
        """Return what the app loaded the project with, e.g. its data sources."""
        return self.project.context

    def position(self, path: Sequence[str | int] = ()) -> tuple[int | None, int | None]:
        """Return the line and column of the value at ``path`` in this section."""
        return self.project.position((self.name, *path))

    def problem(self, path: Sequence[str | int], message: str, key: bool = False) -> Problem:
        """Return a problem at the value at ``path`` in this section (with ``key``, at its key)."""
        return self.project.problem((self.name, *path), message, key=key)

    def warn(self, path: Sequence[str | int], message: str, key: bool = False) -> Problem:
        """Report a problem at ``path`` that does not stop the section from loading."""
        problem = self.problem(path, message, key=key)
        self.project.warnings.append(problem)
        logger.warning("%s", problem)
        return problem


@dataclass
class Project:
    """A loaded project file.

    It holds the raw data, where each key and value is written, by path
    (e.g. ``("metrics", "capacity", "table")``), the value each registered
    loader made of its section, the warnings found while loading and the
    ``context`` the loaders were given.
    """

    path: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    positions: dict[tuple, tuple[int, int]] = field(default_factory=dict)
    context: Mapping[str, Any] = field(default_factory=dict)
    warnings: list[Problem] = field(default_factory=list)
    _values: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def directory(self) -> Path | None:
        """Return the directory the file is in, against which paths it names resolve."""
        return Path(self.path).parent if self.path is not None else None

    def __contains__(self, name: str) -> bool:  # noqa: D105
        return name in self.data

    def get(self, name: str, default: Any = None) -> Any:
        """Return the value of section ``name``, or ``default`` if the file has none.

        The value is what the section's loader returned. A section whose loader
        was registered after the file was loaded is loaded now (raising
        ``ProjectError``); a section no loader is registered for gives its
        raw data.
        """
        if name not in self.data:
            return default
        if name not in self._values:
            section = sections().get(name)
            if section is None:
                return self.data[name]
            problems: list[Problem] = []
            self._load(name, section, problems)
            if problems:
                raise ProjectError(problems)
        return self._values[name]

    def raw(self, name: str, default: Any = None) -> Any:
        """Return the raw YAML data of section ``name``."""
        return self.data.get(name, default)

    def position(self, path: Sequence) -> tuple[int | None, int | None]:
        """Return the 1-based line and column of the value at ``path``.

        A path that is not in the file gives the position of its nearest
        enclosing value.
        """
        path = tuple(path)
        while path:
            if path in self.positions:
                return self.positions[path]
            path = path[:-1]
        return self.positions.get((), (None, None))

    def problem(self, path: Sequence, message: str, key: bool = False) -> Problem:
        """Return a problem at the value at ``path`` (with ``key``, at the key naming it)."""
        path = tuple(path)
        line, column = self.position((*path, KEY) if key and path else path)
        return _problem(message, self.path, line, column, path)

    def set(self, name: str, value: Any) -> None:
        """Set section ``name`` to ``value`` and write it to the project's file.

        The value goes through the section's ``dump`` and the result through
        its loader, so a value the loader rejects raises ``ProjectError``,
        positioned where it would be written, and the file is left alone.
        Only the lines of this section change in the file; comments inside it
        are not kept. Text a YAML 1.1 reader would take for something else,
        such as ``no`` or ``1:20``, is quoted.
        """
        section = sections().get(name)
        data = section.dump(value) if section is not None and section.dump is not None else value
        with _WRITE_LOCK:
            target = Path(self.path).resolve() if self.path is not None else None
            text = _read(target) if target is not None else _dump(self.data)
            old_data, old_positions = _parse(text, self.path)
            new_text = _replace_section(text, old_data, old_positions, name, data)
            new_data, new_positions = _parse(new_text, self.path)
            _check_written(name, data, old_data, new_data, self.path)
            candidate = Project(self.path, new_data, new_positions, self.context)
            if section is not None:
                problems: list[Problem] = []
                candidate._load(name, section, problems)
                if problems:
                    raise ProjectError(problems)
            if target is not None:
                _write_atomically(target, new_text)
        for key in list(self._values):
            if key != name and new_data.get(key) != self.data.get(key):
                del self._values[key]
        self.data, self.positions = new_data, new_positions
        self.warnings = [w for w in self.warnings if w.section != name] + candidate.warnings
        self._values[name] = candidate._values.get(name, value)

    def _load(self, name: str, section: ProjectSection, problems: list[Problem]) -> None:
        try:
            self._values[name] = section.loader(Section(name, self.data[name], self))
        except ProjectError as exc:
            problems.extend(exc.problems)
        except SectionError as exc:
            problems.append(self.problem((name, *exc.path), exc.message, key=exc.key))
        except (TypeError, ValueError) as exc:
            problems.append(self.problem((name,), f"Section {name!r}: {exc}", key=True))

    def _load_all(self) -> None:
        problems: list[Problem] = []
        registered = sections()
        for name in self.data:
            section = registered.get(name)
            if section is None:
                known = ", ".join(f"`{s}`" for s in sorted(registered)) or "none"
                warning = self.problem(
                    (name,),
                    f"Ignoring the unknown section {name!r}; the known sections are: {known}.",
                    key=True,
                )
                self.warnings.append(warning)
                logger.warning("%s", warning)
                continue
            self._load(name, section, problems)
        if problems:
            raise ProjectError(problems)


def _problem(
    message: str, source: str | None, line: int | None, column: int | None, path: Sequence
) -> Problem:
    keys = tuple(part for part in path if part is not KEY)
    return Problem(message, source, line, column, keys[0] if keys else None, keys[1:])


def _mark(node: Node) -> tuple[int, int]:
    return node.start_mark.line + 1, node.start_mark.column + 1


def _build(yaml: YAML, node: Node, path: tuple, positions: dict, problems: list, source):
    positions[path] = _mark(node)
    constructor = yaml.constructor
    if isinstance(node, MappingNode):
        constructor.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = constructor.construct_object(key_node, deep=True)
            if key is None or not isinstance(key, (str, int, float, bool)):
                problems.append(_problem("Keys must be text.", source, *_mark(key_node), path))
                continue
            if key in result:
                problems.append(
                    _problem(f"{key!r} is given twice.", source, *_mark(key_node), (*path, key))
                )
                continue
            result[key] = _build(yaml, value_node, (*path, key), positions, problems, source)
            # A mapping, list or empty value is reported where its key is, a scalar where its value is.
            if (
                isinstance(value_node, (MappingNode, SequenceNode))
                or value_node.tag == "tag:yaml.org,2002:null"
            ):
                positions[(*path, key)] = _mark(key_node)
            positions[(*path, key, KEY)] = _mark(key_node)
        return result
    if isinstance(node, SequenceNode):
        return [
            _build(yaml, item, (*path, i), positions, problems, source)
            for i, item in enumerate(node.value)
        ]
    return constructor.construct_object(node, deep=True)


def _parse(text: str, source: str | None) -> tuple[dict[str, Any], dict[tuple, tuple[int, int]]]:
    yaml = YAML(typ="safe", pure=True)
    positions: dict[tuple, tuple[int, int]] = {}
    problems: list[Problem] = []
    try:
        node = yaml.compose(text)
        if node is None:
            return {}, positions
        data = _build(yaml, node, (), positions, problems, source)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        line, column = (mark.line + 1, mark.column + 1) if mark else (None, None)
        message = f"Not valid YAML: {getattr(exc, 'problem', None) or exc}"
        raise ProjectError([Problem(message, source, line, column)]) from None
    if not isinstance(data, dict):
        raise ProjectError([Problem(_NOT_A_MAPPING, source, 1, 1)])
    if problems:
        raise ProjectError(problems)
    return data, positions


# Text a YAML 1.1 reader resolves to a boolean, null or sexagesimal number.
_AMBIGUOUS = re.compile(
    r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO|true|True|TRUE|false|False|FALSE"
    r"|on|On|ON|off|Off|OFF|~|null|Null|NULL"
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?)$"
)


def _plain(value: Any, quote: bool = False) -> Any:
    """Return ``value`` with tuples as lists and, with ``quote``, ambiguous text quoted."""
    if isinstance(value, Mapping):
        return {_plain(key, quote): _plain(item, quote) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, quote) for item in value]
    if quote and isinstance(value, str) and _AMBIGUOUS.match(value):
        return DoubleQuotedScalarString(value)
    return value


def _dump(document: Mapping[str, Any]) -> str:
    yaml = YAML()
    yaml.width = 4096
    buffer = io.StringIO()
    yaml.dump(_plain(document, quote=True), buffer)
    return buffer.getvalue()


def _replace_section(text: str, data: dict, positions: dict, name: str, value: Any) -> str:
    """Return ``text`` with section ``name`` set to ``value`` and its other lines untouched."""
    if any(positions[(key, KEY)][1] != 1 for key in data):
        # A flow mapping, or a document that starts on a key's line: write it whole.
        return _dump({**data, name: value})
    section_text = _dump({name: value})
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if name not in data:
        return "".join(lines) + section_text
    starts = {key: positions[(key, KEY)][0] - 1 for key in data}
    start = starts[name]
    end = min((line for line in starts.values() if line > start), default=len(lines))
    # Comments and blank lines before the next section belong to it.
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].startswith("#")):
        end -= 1
    return "".join([*lines[:start], section_text, *lines[end:]])


def _check_written(name: str, value: Any, old: dict, new: dict, source: str | None) -> None:
    if new.get(name) != _plain(value):
        message = f"Section {name!r} does not read back as written."
        raise ProjectError([Problem(message, source, section=name)])
    changed = sorted(key for key in old if key != name and old[key] != new.get(key))
    if changed:
        message = (
            f"Writing section {name!r} would change {', '.join(map(repr, changed))}, "
            "which refer to it with an alias; edit the file by hand."
        )
        raise ProjectError([Problem(message, source, section=name)])


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectError([Problem(f"Cannot read the project file: {exc}", str(path))]) from None


def _write_atomically(target: Path, text: str) -> None:
    """Replace ``target`` with ``text`` through a temporary file in its directory."""
    if target.exists():
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def parse_project(
    text: str, path: str | os.PathLike | None = None, context: Mapping[str, Any] | None = None
) -> Project:
    """Parse the project file in ``text`` and load each registered section.

    ``context`` is handed to the loaders as ``Section.context``. Raises
    ``ProjectError`` with every problem found: invalid YAML, a file that is
    not a mapping, a key given twice or a section its loader rejects.
    Unknown sections and loader warnings are logged and kept in
    ``Project.warnings``.
    """
    name = str(path) if path is not None else None
    data, positions = _parse(text, name)
    project = Project(name, data, positions, dict(context or {}))
    project._load_all()
    return project


def load_project(path: str | os.PathLike, context: Mapping[str, Any] | None = None) -> Project:
    """Read and parse the project file at ``path``, see ``parse_project``."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(
            [Problem(f"Cannot read the project file: {exc.strerror or exc}", str(path))]
        ) from None
    except UnicodeDecodeError as exc:
        raise ProjectError(
            [Problem(f"The project file is not UTF-8 text: {exc.reason}", str(path))]
        ) from None
    return parse_project(text, path, context)


def find_project(places: Iterable[Any] = (), cwd: str | os.PathLike | None = None) -> Path | None:
    """Find the project file an app reads when none is given.

    It is ``PROJECT_FILE`` in the first of ``places`` that has one (a local
    directory, or a local file whose directory is used; URLs and other
    values are skipped), else in ``cwd`` (the working directory by default).
    None if there is none.
    """
    candidates: list[Path] = []
    for item in places:
        if not isinstance(item, (str, os.PathLike)) or "://" in str(item):
            continue
        place = Path(item)
        if place.exists():
            candidates.append((place if place.is_dir() else place.parent).resolve() / PROJECT_FILE)
    candidates.append(Path(cwd or Path.cwd()).resolve() / PROJECT_FILE)
    return next(
        (candidate for candidate in dict.fromkeys(candidates) if candidate.is_file()), None
    )
