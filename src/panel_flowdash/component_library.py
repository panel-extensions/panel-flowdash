"""Normalize heterogeneous component declarations into a registry.

The project-directory workflow discovers components by scanning the filesystem
(:func:`~panel_flowdash.registry.build_registry`). The programmatic workflow
hands them over directly: decorated functions, ``Viewer`` subclasses, a mapping
of explicit ids, a directory, or any mix of those. Both funnel into the same
``dict[str, RegistryEntry]`` that the spec builder and the editor consume.
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import pathlib
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from panel_flowdash.registry import PanelAppMetadata, RegistryEntry, build_registry

logger = logging.getLogger("panel_flowdash")


def _is_directory(obj: Any) -> bool:
    return isinstance(obj, pathlib.Path) or (isinstance(obj, str) and pathlib.Path(obj).is_dir())


def _scan_directory(path: pathlib.Path) -> dict[str, RegistryEntry]:
    """Scan a project directory, making its modules importable.

    Scanned entries are imported lazily by module name relative to the project
    directory, so the directory has to be on ``sys.path`` for `RegistryEntry.load`
    to resolve them later. ``flowdash serve`` does this itself; a programmatic
    caller should not have to.
    """
    resolved = path.resolve()
    if str(resolved) not in sys.path:
        sys.path.insert(0, str(resolved))
    return build_registry(resolved)


def _dedupe_id(app_id: str, taken: set[str]) -> str:
    """Return *app_id*, suffixed if needed, so it is unique within *taken*.

    Component ids are persisted as ``DashboardItem.component_id``, so a silent
    collision would make two different components load each other's saved
    nodes. Suffixing keeps both usable; the warning tells the author to pass
    explicit ids if they care which is which.
    """
    if app_id not in taken:
        return app_id
    for suffix in range(2, 1000):
        candidate = f"{app_id}_{suffix}"
        if candidate not in taken:
            logger.warning(
                "Duplicate component id '%s'; registering as '%s'. Pass a dict of "
                "explicit ids to control this.",
                app_id,
                candidate,
            )
            return candidate
    raise ValueError(f"Could not find a unique id for component '{app_id}'.")


def _component_origin(entry: RegistryEntry) -> str:
    app = entry.app
    if app is None:
        return entry.module_name
    return f"{getattr(app, '__module__', None) or entry.module_name}.{entry.name}"


def _same_component(a: RegistryEntry, b: RegistryEntry) -> bool:
    """Whether two entries stand for the same component under the same id."""
    if a is b:
        return True
    if a.app_id != b.app_id:
        return False
    if a.app is not None or b.app is not None:
        return a.app is b.app
    return (a.module_name, a.module_path) == (b.module_name, b.module_path)


def _add(registry: dict[str, RegistryEntry], entry: RegistryEntry, unique: bool = False) -> None:
    existing = registry.get(entry.app_id)
    if unique and existing is not None:
        if _same_component(existing, entry):
            return
        raise ValueError(
            f"'{_component_origin(existing)}' and '{_component_origin(entry)}' both have "
            f"the component id '{entry.app_id}'. Give them explicit ids with a mapping."
        )
    app_id = _dedupe_id(entry.app_id, set(registry))
    if app_id != entry.app_id:
        entry = RegistryEntry(
            app_id=app_id,
            section=entry.section,
            name=entry.name,
            page_path=f"/{app_id}",
            module_name=entry.module_name,
            metadata=entry.metadata,
            module_path=entry.module_path,
            app=entry.app,
        )
    registry[app_id] = entry


def _normalize_one(
    registry: dict[str, RegistryEntry],
    obj: Any,
    *,
    app_id: str | None = None,
    unique: bool = False,
) -> None:
    """Fold a single declaration into *registry*."""
    if obj is None:
        return
    if isinstance(obj, RegistryEntry):
        _add(registry, obj, unique)
        return
    if _is_directory(obj):
        for entry in _scan_directory(pathlib.Path(obj)).values():
            _add(registry, entry, unique)
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if isinstance(value, RegistryEntry):
                _add(registry, value, unique)
            else:
                _normalize_one(registry, value, app_id=key, unique=unique)
        return
    if isinstance(obj, (str, bytes)):
        raise TypeError(
            f"Cannot register component from {obj!r}: strings are only accepted as "
            "paths to an existing project directory."
        )
    if isinstance(obj, Iterable) and not callable(obj):
        for item in obj:
            _normalize_one(registry, item, unique=unique)
        return
    _add(registry, RegistryEntry.from_app(obj, app_id=app_id), unique)


def normalize_components(components: Any, *, unique: bool = False) -> dict[str, RegistryEntry]:
    """Build a registry from any supported component declaration.

    Parameters
    ----------
    components
        One of, or a list mixing any of:

        - a decorated function or ``Viewer`` subclass
        - a :class:`~panel_flowdash.registry.RegistryEntry`
        - a mapping of explicit component id to any of the above
        - a path to a project directory to scan
        - an existing registry mapping
    unique
        Raise a ``ValueError`` when two components share an id, instead of
        suffixing the second one's id.

    Returns
    -------
    dict
        Registry entries keyed by component id.
    """
    registry: dict[str, RegistryEntry] = {}
    _normalize_one(registry, components, unique=unique)
    return registry


#: Module attribute a library sets to declare the components it offers.
LIBRARY_ATTRIBUTE = "__flowdash_components__"


class ComponentLibraryError(ValueError):
    """A component library named by ``FlowDashApp.component_libraries`` cannot be used."""


def _library_object(name: str) -> tuple[Any, list[str]]:
    """Import the object a library name refers to: ``module`` or ``module:attribute``.

    Also returns the names of the components a module without ``__all__``
    imports from elsewhere, which it does not offer.
    """
    module_name, _, attribute = name.partition(":")
    if not module_name or (":" in name and not attribute):
        raise ComponentLibraryError(
            f"Component library {name!r} is not a module name or 'module:attribute'."
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ComponentLibraryError(
            f"Cannot import component library {module_name!r}: {type(exc).__name__}: {exc}"
        ) from exc
    if not attribute:
        if hasattr(module, LIBRARY_ATTRIBUTE):
            return getattr(module, LIBRARY_ATTRIBUTE), []
        names = getattr(module, "__all__", None)
        own_only = names is None
        if own_only:
            names = [n for n in vars(module) if not n.startswith("_")]
        components, imported = [], []
        for attr in names:
            obj = getattr(module, attr, None)
            if not PanelAppMetadata.from_app(obj).component or any(obj is c for c in components):
                continue
            defined_in = getattr(obj, "__module__", None) or ""
            if own_only and not (
                defined_in == module_name or defined_in.startswith(f"{module_name}.")
            ):
                imported.append(attr)
                continue
            components.append(obj)
        return components, imported
    obj = module
    for part in attribute.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise ComponentLibraryError(
                f"Component library {name!r}: {module_name!r} has no attribute {attribute!r}."
            ) from None
    return obj, []


def library_components(name: str) -> dict[str, RegistryEntry]:
    """Return the components a library offers, keyed by component id.

    *name* is an importable module, or ``module:attribute`` naming a mapping
    of component id to component, a list of components or a single one. A
    module offers what its ``__flowdash_components__`` attribute holds, in
    any of those forms, and otherwise every object it exports that
    ``@register`` marks as a component: those named in its ``__all__``, or
    without one, its public names defined in the module or its submodules.
    Only components are taken: a library adds no pages. Every object a
    library names must carry ``@register`` metadata.

    Raises
    ------
    ComponentLibraryError
        If *name* does not import, names an object without ``@register``
        metadata, gives two components one id, or names nothing that holds
        a component.
    """
    obj, imported = _library_object(name)
    try:
        entries = normalize_components(obj, unique=True)
    except (TypeError, ValueError) as exc:
        raise ComponentLibraryError(f"Component library {name!r}: {exc}") from exc
    components = {}
    for app_id, entry in entries.items():
        if entry.app is not None and not PanelAppMetadata.is_registered(entry.app):
            raise ComponentLibraryError(
                f"Component library {name!r} offers {entry.name!r} as '{app_id}', but it "
                "has no @register metadata. Decorate it with @register(component=True)."
            )
        if not entry.metadata.component:
            continue
        if entry.metadata.page:
            entry = dataclasses.replace(
                entry, metadata=dataclasses.replace(entry.metadata, page=False)
            )
        components[app_id] = entry
    if not components and imported:
        verb, pronoun = ("is", "it") if len(imported) == 1 else ("are", "them")
        raise ComponentLibraryError(
            f"Component library {name!r} holds no FlowDash components of its own: "
            f"{', '.join(imported)} {verb} imported from other modules, which a module "
            f"without __all__ does not offer. List {pronoun} in its __all__ or "
            f"{LIBRARY_ATTRIBUTE} to offer {pronoun}."
        )
    if not components:
        raise ComponentLibraryError(
            f"Component library {name!r} holds no FlowDash components. Name a module that "
            f"defines {LIBRARY_ATTRIBUTE} or exports @register(component=True) objects, or "
            "'module:attribute' for a mapping, list or single component."
        )
    return components


def _already(owner: str) -> str:
    """Turn ``"the project offers"`` into ``"the project already offers"``."""
    subject, _, verb = owner.rpartition(" ")
    return f"{subject} already {verb}"


def merge_libraries(
    registry: dict[str, RegistryEntry],
    names: Iterable[str],
    *,
    reserved: Iterable[str] = (),
) -> dict[str, RegistryEntry]:
    """Return *registry* with the components of each library in *names* added.

    An id that is already taken, by *registry*, by another library or by one
    of *reserved* (e.g. the built-in components), raises instead of letting
    one component replace another: dashboards store component ids, so either
    choice would silently change what saved dashboards load. The former ids
    a library component declares with ``aliases`` must not be taken either,
    nor may its id be a former id of another component. Adding a library
    whose components are already in *registry* is a no-op, so a registry can
    be merged again safely.

    Raises
    ------
    ComponentLibraryError
        If a library cannot be used (see :func:`library_components`) or an
        id collides.
    """
    names = list(names)
    if not names:
        return registry
    merged = dict(registry)
    owners = dict.fromkeys(registry, "the project offers")
    owners.update(dict.fromkeys(reserved, "the built-in components offer"))
    former: dict[str, str] = {}
    for app_id, entry in registry.items():
        for alias in entry.metadata.aliases:
            former.setdefault(alias, app_id)
    for name in names:
        for app_id, entry in library_components(name).items():
            existing = merged.get(app_id)
            if existing is not None and _same_component(existing, entry):
                continue
            if app_id in owners:
                raise ComponentLibraryError(
                    f"Component library {name!r} offers '{app_id}', which "
                    f"{_already(owners[app_id])}. Component ids must be unique; give the "
                    "library's components other ids with a mapping, e.g. 'module:COMPONENTS'."
                )
            if app_id in former:
                raise ComponentLibraryError(
                    f"Component library {name!r} offers '{app_id}', which is a former id "
                    f"of '{former[app_id]}'. Give the library's component another id."
                )
            for alias in entry.metadata.aliases:
                if alias in owners:
                    clash = f"an id {_already(owners[alias])}"
                elif alias in former:
                    clash = f"already a former id of '{former[alias]}'"
                else:
                    continue
                raise ComponentLibraryError(
                    f"Component library {name!r} declares '{alias}' as a former id of "
                    f"'{app_id}', but it is {clash}."
                )
            owners[app_id] = f"library {name!r} offers"
            merged[app_id] = entry
            for alias in entry.metadata.aliases:
                former[alias] = app_id
    return merged
