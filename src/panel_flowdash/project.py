"""Read and write the project file, ``flowdash.yaml``.

The project file is one YAML file of named sections that configure a
FlowDash app, e.g. the admin settings or an extension's ``metrics:``. Every
key and value keeps the line and column it is written at, so a problem is
reported where it is.

``flowdash serve`` reads ``flowdash.yaml`` (``PROJECT_FILE``), given with
``--project`` or found in the served directory or the working directory
(``find_project``). Each top-level section is read by the loader registered
for it (``register_section``); a section no one registered is ignored with a
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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import YAMLError as RuamelYAMLError

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
    "find_project",
    "load_entry_points",
    "load_project",
    "parse_project",
    "register_section",
    "sections",
    "unregister_section",
]

PROJECT_FILE = "flowdash.yaml"

logger = logging.getLogger(__name__)

PathLike = tuple[str | int, ...]

# The last element of a path that names where a key is written rather than its value.
KEY = object()


@dataclass(frozen=True)
class Problem:
    """A problem in a project file, with the file, line and column it concerns."""

    message: str
    path: str | None = None
    line: int | None = None
    column: int | None = None

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

ENTRY_POINT_GROUP = "panel_flowdash.project_sections"


@dataclass(frozen=True)
class ProjectSection:
    """A top-level section of the project file, as an extension registers it.

    ``loader`` turns the section's raw YAML data (``Section.value``) into
    the typed value ``Project.get(name)`` gives, reporting problems by
    raising ``SectionError`` (one problem at a path in the section) or
    ``ProjectError`` (several, made with ``Section.problem``); a
    ``ValueError`` or ``TypeError`` is reported at the section's key.
    ``dump`` turns a typed value back into plain YAML data for
    ``Project.set``; without it the value is written as it is.
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
_entry_points_loaded = False


def register_section(
    section: ProjectSection | str, loader: Loader | None = None, **fields: Any
) -> ProjectSection:
    """Register a section of the project file and return it.

    The section is a ``ProjectSection``, or its name with its loader and
    other ``ProjectSection`` fields.

    Registering a name again replaces it, so a module that is reloaded can
    register again.
    """
    if isinstance(section, str):
        if loader is None:
            raise TypeError(f"register_section({section!r}) needs a loader.")
        section = ProjectSection(section, loader, **fields)
    elif loader is not None or fields:
        raise TypeError("Pass either a ProjectSection or a name with its loader and fields.")
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
    _SECTIONS.pop(name, None)


def sections() -> dict[str, ProjectSection]:
    """Return the registered sections by name, including those of installed extensions."""
    load_entry_points()
    return dict(_SECTIONS)


def load_entry_points(force: bool = False) -> None:
    """Register the sections installed packages declare as entry points, once.

    The group is ``panel_flowdash.project_sections``. Each entry point names a ``ProjectSection``, a sequence of them, or a callable
    returning either. One that fails to load is skipped with a warning.
    """
    global _entry_points_loaded
    if _entry_points_loaded and not force:
        return
    _entry_points_loaded = True
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
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


@dataclass(frozen=True)
class Section:
    """One top-level section of a project file, as a loader receives it."""

    name: str
    value: Any
    project: Project

    def position(self, path: Sequence[str | int] = ()) -> tuple[int | None, int | None]:
        """Return the line and column of the value at ``path`` in this section."""
        return self.project.position((self.name, *path))

    def problem(self, path: Sequence[str | int], message: str, key: bool = False) -> Problem:
        """Return a problem at the value at ``path`` in this section (with ``key``, at its key)."""
        return self.project.problem((self.name, *path), message, key=key)


@dataclass
class Project:
    """A loaded project file.

    It holds the raw data, where each key and value is written, by path
    (e.g. ``("metrics", "capacity", "table")``), and the value each
    registered loader made of its section.
    """

    path: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    positions: dict[tuple, tuple[int, int]] = field(default_factory=dict)
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
        return Problem(message, self.path, line, column)

    def set(self, name: str, value: Any) -> None:
        """Set section ``name`` to ``value`` and write it to the project's file.

        The file, if the project has one, keeps the other sections and the
        comments outside this one. The value goes through the section's
        ``dump``, if it has one. Raises ``ProjectError`` if the written
        YAML would not read back as the same data.
        """
        section = sections().get(name)
        data = section.dump(value) if section is not None and section.dump is not None else value
        if self.path is None:
            self.data[name] = data
            self.positions = {k: v for k, v in self.positions.items() if not (k and k[0] == name)}
        else:
            text = _write_section(Path(self.path), name, data)
            self.data, self.positions = _parse(text, self.path)
        self._values[name] = value

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
                logger.warning(
                    "%s",
                    self.problem(
                        (name,),
                        f"Ignoring the unknown section {name!r}; the known sections are: {known}.",
                        key=True,
                    ),
                )
                continue
            self._load(name, section, problems)
        if problems:
            raise ProjectError(problems)


def _mark(node: yaml.Node) -> tuple[int, int]:
    return node.start_mark.line + 1, node.start_mark.column + 1


class _Loader(yaml.SafeLoader):
    pass


def _build(
    loader: _Loader,
    node: yaml.Node,
    path: PathLike,
    positions: dict,
    problems: list,
    source: str | None,
):
    positions[path] = _mark(node)
    if isinstance(node, yaml.MappingNode):
        loader.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            if key is None or not isinstance(key, (str, int, float, bool)):
                problems.append(Problem("Keys must be text.", source, *_mark(key_node)))
                continue
            if key in result:
                problems.append(Problem(f"{key!r} is given twice.", source, *_mark(key_node)))
                continue
            result[key] = _build(loader, value_node, (*path, key), positions, problems, source)
            # A mapping, list or empty value is reported where its key is, a scalar where its value is.
            if (
                isinstance(value_node, (yaml.MappingNode, yaml.SequenceNode))
                or value_node.tag == "tag:yaml.org,2002:null"
            ):
                positions[(*path, key)] = _mark(key_node)
            positions[(*path, key, KEY)] = _mark(key_node)
        return result
    if isinstance(node, yaml.SequenceNode):
        return [
            _build(loader, item, (*path, i), positions, problems, source)
            for i, item in enumerate(node.value)
        ]
    return loader.construct_object(node, deep=True)


def _yaml_problem(exc: yaml.YAMLError, source: str | None) -> Problem:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line, column = (mark.line + 1, mark.column + 1) if mark else (None, None)
    return Problem(f"Not valid YAML: {getattr(exc, 'problem', None) or exc}", source, line, column)


def _parse(text: str, name: str | None) -> tuple[dict[str, Any], dict[tuple, tuple[int, int]]]:
    loader = _Loader(text)
    positions: dict[tuple, tuple[int, int]] = {}
    problems: list[Problem] = []
    try:
        try:
            node = loader.get_single_node()
            if node is None:
                return {}, positions
            data = _build(loader, node, (), positions, problems, name)
        except yaml.YAMLError as exc:
            raise ProjectError([_yaml_problem(exc, name)]) from None
    finally:
        loader.dispose()
    if not isinstance(data, dict):
        raise ProjectError(
            [Problem("A project file is a mapping of sections, e.g. `metrics:`.", name, 1, 1)]
        )
    if problems:
        raise ProjectError(problems)
    return data, positions


def _write_section(path: Path, name: str, data: Any) -> str:
    rt = YAML()
    rt.preserve_quotes = True
    rt.indent(mapping=2, sequence=4, offset=2)
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        document = rt.load(existing) if existing.strip() else None
    except (OSError, UnicodeDecodeError, RuamelYAMLError) as exc:
        raise ProjectError(
            [Problem(f"Cannot update the project file: {exc}", str(path))]
        ) from None
    if document is None:
        document = CommentedMap()
    elif not isinstance(document, dict):
        raise ProjectError(
            [Problem("A project file is a mapping of sections, e.g. `metrics:`.", str(path), 1, 1)]
        )
    document[name] = data
    buffer = io.StringIO()
    rt.dump(document, buffer)
    text = buffer.getvalue()
    written, _ = _parse(text, str(path))
    if written.get(name) != data:
        raise ProjectError(
            [
                Problem(
                    f"Section {name!r} does not read back as written; quote its text values.",
                    str(path),
                )
            ]
        )
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return text


def parse_project(text: str, path: str | os.PathLike | None = None) -> Project:
    """Parse the project file in ``text`` and load each registered section.

    Raises ``ProjectError`` with every problem found: invalid YAML, a file
    that is not a mapping, a key given twice or a section its loader
    rejects. Unknown sections are ignored with a warning.
    """
    name = str(path) if path is not None else None
    data, positions = _parse(text, name)
    project = Project(name, data, positions)
    project._load_all()
    return project


def load_project(path: str | os.PathLike) -> Project:
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
    return parse_project(text, path)


def find_project(places: Iterable[Any] = (), cwd: str | os.PathLike | None = None) -> Path | None:
    """Find the project file an app reads when none is given.

    It is ``PROJECT_FILE`` in the first of ``places`` that has one (a local directory, or a local file
    whose directory is used; URLs and other values are skipped), else in
    ``cwd`` (the working directory by default). None if there is none.
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
