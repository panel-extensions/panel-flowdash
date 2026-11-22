"""The project file's ``components`` section: which components are offered, and to whom.

```yaml
components:
  panel_flowdash/image:
    enabled: false
  wind_maps/turbine_map:
    placed_by: [analysts]
    approval: true
```

A component with ``enabled: false`` is turned off: it is left out of the
component list, the command palette and what the agent is offered, and
nobody may add it to a dashboard. Dashboards that already place it keep
working, and their editors are told it is turned off. ``placed_by``
limits who may add it to members of those groups (people may still view
and edit dashboards that have it), and ``approval: true`` makes the
agent ask before placing or changing it, like a pattern in the app's
``agent_sensitive_components``. Keys are component ids; a former id
(``aliases``) is read as the component's current id.
"""

from __future__ import annotations

import logging
import threading
import typing as t
from collections.abc import Mapping
from pathlib import Path

from panel_flowdash.auth import Identity
from panel_flowdash.project import Project, ProjectError, Section, SectionError, _parse

logger = logging.getLogger(__name__)

KEYS = ("enabled", "placed_by", "approval")

TURNED_OFF = "turned off by an admin"


def load_components(section: Section) -> dict[str, dict[str, t.Any]]:
    """Check the ``components`` section and return it as plain data."""
    value = section.value
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SectionError(
            "`components` maps component ids to their settings, e.g. "
            "`panel_flowdash/image: {enabled: false}`."
        )
    problems = []
    for cid, settings in value.items():
        if not isinstance(cid, str) or not cid.strip():
            problems.append(section.problem((cid,), "A component id is text.", key=True))
            continue
        if not isinstance(settings, Mapping):
            problems.append(
                section.problem(
                    (cid,), "A component's settings are a mapping, e.g. `{enabled: false}`."
                )
            )
            continue
        for key, item in settings.items():
            if key not in KEYS:
                known = ", ".join(f"`{k}`" for k in KEYS)
                problems.append(
                    section.problem((cid, key), f"Unknown setting {key!r}; known: {known}.", True)
                )
            elif key in ("enabled", "approval") and not isinstance(item, bool):
                problems.append(section.problem((cid, key), f"`{key}` is true or false."))
            elif key == "placed_by" and not (
                isinstance(item, list) and all(isinstance(g, str) and g.strip() for g in item)
            ):
                problems.append(
                    section.problem((cid, key), "`placed_by` is a list of group names.")
                )
    if problems:
        raise ProjectError(problems)
    return {cid: dict(settings) for cid, settings in value.items()}


def dump_components(value: t.Any) -> t.Any:
    """Drop the settings left at their defaults, and components with none left."""
    out = {}
    for cid, settings in (value or {}).items():
        kept = {
            k: v
            for k, v in (settings or {}).items()
            if not (k == "enabled" and v is True)
            and not (k == "approval" and v is False)
            and not (k == "placed_by" and not v)
        }
        if kept:
            out[cid] = kept
    return out


_LAST: dict[str, tuple[int, dict[str, t.Any]]] = {}
_LAST_LOCK = threading.Lock()


def project_components(project: Project) -> dict[str, dict[str, t.Any]]:
    """Return the ``components`` section of *project*, read again when its file changes.

    A file that cannot be read keeps the settings last read from it, so a
    half-written file does not turn components back on; with none read
    before, the loaded project's value is used.
    """
    path = Path(project.path) if project.path else None
    if path is None or not path.is_file():
        try:
            return dict(project.get("components") or {})
        except ProjectError as exc:
            logger.error("Not using the project file's component settings: %s", exc)
            return {}
    key = str(path.resolve())
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    with _LAST_LOCK:
        cached = _LAST.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data, positions = _parse(path.read_text(encoding="utf-8"), key)
        value = (
            load_components(
                Section("components", data["components"], Project(key, data, positions))
            )
            if "components" in data
            else {}
        )
    except (ProjectError, SectionError, OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        logger.error("Could not read the component settings in %s: %s", key, exc)
        if cached is not None:
            return cached[1]
        try:
            return dict(project.get("components") or {})
        except ProjectError:
            return {}
    if stamp is not None:
        with _LAST_LOCK:
            _LAST[key] = (stamp, value)
    return value


def placement_refusal(
    settings: Mapping[str, Mapping[str, t.Any]], component_id: str, identity: Identity | None
) -> str | None:
    """Return why *identity* may not add *component_id* to a dashboard, or None."""
    entry = settings.get(component_id) or {}
    if entry.get("enabled", True) is False:
        return TURNED_OFF
    groups = entry.get("placed_by")
    if groups and (identity is None or not identity.in_groups(groups)):
        return f"only {', '.join(groups)} may add it"
    return None


def disabled_ids(settings: Mapping[str, Mapping[str, t.Any]]) -> list[str]:
    """Return the ids of the components turned off."""
    return [cid for cid, entry in settings.items() if (entry or {}).get("enabled", True) is False]


def approval_ids(settings: Mapping[str, Mapping[str, t.Any]]) -> list[str]:
    """Return the ids of the components the agent needs approval to place or change."""
    return [cid for cid, entry in settings.items() if (entry or {}).get("approval") is True]
