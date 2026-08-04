"""Normalize heterogeneous component declarations into a registry.

The project-directory workflow discovers components by scanning the filesystem
(:func:`~panel_flowdash.registry.build_registry`). The programmatic workflow
hands them over directly: decorated functions, ``Viewer`` subclasses, a mapping
of explicit ids, a directory, or any mix of those. Both funnel into the same
``dict[str, RegistryEntry]`` that the spec builder and the editor consume.
"""

from __future__ import annotations

import logging
import pathlib
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from panel_flowdash.registry import RegistryEntry, build_registry

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


def _add(registry: dict[str, RegistryEntry], entry: RegistryEntry) -> None:
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
) -> None:
    """Fold a single declaration into *registry*."""
    if obj is None:
        return
    if isinstance(obj, RegistryEntry):
        _add(registry, obj)
        return
    if _is_directory(obj):
        for entry in _scan_directory(pathlib.Path(obj)).values():
            _add(registry, entry)
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if isinstance(value, RegistryEntry):
                _add(registry, value)
            else:
                _normalize_one(registry, value, app_id=key)
        return
    if isinstance(obj, (str, bytes)):
        raise TypeError(
            f"Cannot register component from {obj!r}: strings are only accepted as "
            "paths to an existing project directory."
        )
    if isinstance(obj, Iterable) and not callable(obj):
        for item in obj:
            _normalize_one(registry, item)
        return
    _add(registry, RegistryEntry.from_app(obj, app_id=app_id))


def normalize_components(components: Any) -> dict[str, RegistryEntry]:
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

    Returns
    -------
    dict
        Registry entries keyed by component id.
    """
    registry: dict[str, RegistryEntry] = {}
    _normalize_one(registry, components)
    return registry
