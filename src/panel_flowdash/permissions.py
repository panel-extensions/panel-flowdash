"""The project file's ``permissions`` section: who may do what, layered under the code's rules.

```yaml
permissions:
  default_allow: true
  admin_groups: [admins]
  people:
    ana: {groups: [analysts]}
    priya: {groups: [field-ops], assistant: false}
  assistant_groups: [analysts, admins]
  sensitive_components: ["Lumen/deckgl"]
  approval_rules:
    - tool: run_command
      when: {command: "action:refresh"}
```

``default_allow`` and ``admin_groups`` are those of `AuthConfig` and only
apply when the project's ``__init__.py`` (or the app's ``auth_config``)
does not set them. ``people`` adds groups to people, as
``AuthConfig.user_groups`` does, and with ``assistant: false`` keeps the
assistant from someone. ``assistant_groups`` limits the assistant to their
members, unless the app has an ``agent_authorize``. The
``sensitive_components`` and ``approval_rules`` are added to the app's
``agent_sensitive_components`` and ``agent_approval_rules``; a rule's
``when`` matches calls whose arguments have those values (text may use
``*`` wildcards), and ``enabled: false`` keeps a rule without applying it.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import logging
import os
import threading
import typing as t
from collections.abc import Mapping
from pathlib import Path

from panel_flowdash.auth import AuthConfig, Identity
from panel_flowdash.project import Project, ProjectError, Section, SectionError, _parse

logger = logging.getLogger(__name__)

KEYS = (
    "default_allow",
    "admin_groups",
    "people",
    "assistant_groups",
    "sensitive_components",
    "approval_rules",
)

# The AuthConfig fields the section may set.
AUTH_FIELDS = ("default_allow", "admin_groups")

_RULE_KEYS = ("tool", "when", "enabled")


def _names(value: t.Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def load_permissions(section: Section) -> dict[str, t.Any]:
    """Check the ``permissions`` section and return it as plain data."""
    value = section.value
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SectionError("`permissions` is a mapping, e.g. `default_allow: true`.")
    problems = []

    def problem(path, message, key=False):
        problems.append(section.problem(path, message, key))

    for key, item in value.items():
        if key not in KEYS:
            known = ", ".join(f"`{k}`" for k in KEYS)
            problem((key,), f"Unknown setting {key!r}; known: {known}.", True)
        elif key == "default_allow" and not isinstance(item, bool):
            problem((key,), "`default_allow` is true or false.")
        elif key in ("admin_groups", "assistant_groups") and not _names(item):
            problem((key,), f"`{key}` is a list of group names.")
        elif key == "sensitive_components" and not _names(item):
            problem((key,), "`sensitive_components` is a list of component ids or patterns.")
        elif key == "people":
            if not isinstance(item, Mapping):
                problem((key,), "`people` maps a user name to their `groups` and `assistant`.")
                continue
            for user, person in item.items():
                if not isinstance(person, Mapping):
                    problem((key, user), "A person is a mapping, e.g. `{groups: [analysts]}`.")
                    continue
                for field, setting in person.items():
                    if field == "groups" and not _names(setting):
                        problem((key, user, field), "`groups` is a list of group names.")
                    elif field == "assistant" and not isinstance(setting, bool):
                        problem((key, user, field), "`assistant` is true or false.")
                    elif field not in ("groups", "assistant"):
                        problem(
                            (key, user, field),
                            f"Unknown key {field!r}; known: `groups`, `assistant`.",
                            True,
                        )
        elif key == "approval_rules":
            if not isinstance(item, list):
                problem((key,), "`approval_rules` is a list of rules, e.g. `- tool: run_sql`.")
                continue
            for i, rule in enumerate(item):
                for path, message in rule_problems(rule):
                    problem((key, i, *path), message)
    if problems:
        raise ProjectError(problems)
    return dict(value)


def rule_problems(rule: t.Any) -> list[tuple[tuple, str]]:
    """Return the problems of an approval rule, each with its path inside it."""
    if not isinstance(rule, Mapping):
        return [((), "A rule is a mapping with a `tool`.")]
    problems = []
    for key in rule:
        if key not in _RULE_KEYS:
            problems.append(((key,), f"Unknown key {key!r}; known: `tool`, `when`, `enabled`."))
    if not isinstance(rule.get("tool"), str) or not rule["tool"].strip():
        problems.append((("tool",), "`tool` names a tool, or a pattern such as `delete_*`."))
    when = rule.get("when")
    if when is not None and (
        not isinstance(when, Mapping)
        or not all(isinstance(v, (str, int, float, bool)) for v in when.values())
    ):
        problems.append(
            (("when",), "`when` maps argument names to the values that need approval.")
        )
    if "enabled" in rule and not isinstance(rule["enabled"], bool):
        problems.append((("enabled",), "`enabled` is true or false."))
    return problems


# ---------------------------------------------------------------------------
# Reading the section, failing closed
# ---------------------------------------------------------------------------


class PermissionsUnavailable(RuntimeError):
    """The project file's permissions cannot be read and none were read from it before."""


# The permissions last read from each project file, with the file's modification stamp.
_LAST_GOOD: dict[str, tuple[int, dict[str, t.Any]]] = {}
_LAST_GOOD_LOCK = threading.Lock()


def project_permissions(path: str | os.PathLike) -> dict[str, t.Any]:
    """Return the ``permissions`` section of the project file at *path*, read again when it changes.

    A file that cannot be read or parsed, or whose section is invalid, does
    not loosen anything: the permissions last read from it stay in force,
    logged as an error, and with none read before `PermissionsUnavailable`
    is raised, so callers deny access rather than fall back to defaults.
    No file at all sets nothing.
    """
    file = Path(path)
    key = str(file.resolve())
    try:
        stamp = file.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    except OSError as exc:
        return _last_good(key, exc)
    with _LAST_GOOD_LOCK:
        cached = _LAST_GOOD.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data, positions = _parse(file.read_text(encoding="utf-8"), key)
        value = (
            load_permissions(
                Section("permissions", data["permissions"], Project(key, data, positions))
            )
            if "permissions" in data
            else {}
        )
    except (ProjectError, SectionError, OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        return _last_good(key, exc)
    with _LAST_GOOD_LOCK:
        _LAST_GOOD[key] = (stamp, value)
    return value


def _last_good(key: str, exc: Exception) -> dict[str, t.Any]:
    with _LAST_GOOD_LOCK:
        cached = _LAST_GOOD.get(key)
    if cached is None:
        logger.error("Denying access: the permissions in %s cannot be read: %s", key, exc)
        raise PermissionsUnavailable(
            f"The permissions in the project file cannot be read, so access is denied until "
            f"it is fixed: {exc}"
        )
    logger.error(
        "Keeping the permissions last read from %s; the file cannot be read now: %s", key, exc
    )
    return cached[1]


# ---------------------------------------------------------------------------
# Applying the section
# ---------------------------------------------------------------------------


def auth_code_fields(
    auth_config: AuthConfig | None, sources: Mapping[str, str], explicit: bool
) -> dict[str, str]:
    """Return the `AuthConfig` fields set in code, each with where.

    `flowdash serve` names the fields the project's ``__init__.py`` sets
    in ``settings_sources`` as ``auth_config.<field>`` (and the config
    itself as ``auth_config``); an ``auth_config`` given otherwise sets
    every field that differs from the default.
    """
    fields = (*AUTH_FIELDS, "user_groups")
    if "auth_config" in sources or any(k.startswith("auth_config.") for k in sources):
        return {
            name: f"{name} in {sources[f'auth_config.{name}']}"
            for name in fields
            if f"auth_config.{name}" in sources
        }
    if not explicit or auth_config is None:
        return {}
    default = AuthConfig()
    return {
        name: f"{name} set in code"
        for name in fields
        if getattr(auth_config, name) != getattr(default, name)
    }


def effective_auth_config(
    auth_config: AuthConfig | None,
    permissions: Mapping[str, t.Any] | None,
    in_code: t.Collection[str],
) -> AuthConfig:
    """Return *auth_config* with the section's settings for the fields not set in code."""
    config = auth_config or AuthConfig()
    permissions = permissions or {}
    changes: dict[str, t.Any] = {}
    if "default_allow" in permissions and "default_allow" not in in_code:
        changes["default_allow"] = permissions["default_allow"]
    if "admin_groups" in permissions and "admin_groups" not in in_code:
        changes["admin_groups"] = frozenset(permissions["admin_groups"])
    people = permissions.get("people") or {}
    if people:
        user_groups = {user: frozenset(groups) for user, groups in config.user_groups.items()}
        for user, person in people.items():
            extra = frozenset(person.get("groups") or ())
            if extra:
                user_groups[user] = user_groups.get(user, frozenset()) | extra
        changes["user_groups"] = user_groups
    return dataclasses.replace(config, **changes) if changes else config


def assistant_allowed(permissions: Mapping[str, t.Any] | None, identity: Identity) -> bool:
    """Whether the section gives *identity* the assistant."""
    permissions = permissions or {}
    people = permissions.get("people") or {}
    for name in identity.user_names:
        if (people.get(name) or {}).get("assistant") is False:
            return False
    groups = permissions.get("assistant_groups")
    return groups is None or identity.in_groups(groups)


def _matcher(when: Mapping[str, t.Any]):
    def match(args: dict[str, t.Any]) -> bool:
        for name, expected in when.items():
            actual = args.get(name)
            if isinstance(expected, str):
                if not isinstance(actual, str) or not fnmatch.fnmatchcase(actual, expected):
                    return False
            elif actual != expected:
                return False
        return True

    return match


def approval_rules(permissions: Mapping[str, t.Any] | None) -> list:
    """Return the section's enabled approval rules as ``panel_agents.ApprovalRule`` objects."""
    from panel_agents import ApprovalRule

    rules = []
    for rule in (permissions or {}).get("approval_rules") or []:
        if rule.get("enabled", True) is False:
            continue
        when = rule.get("when")
        rules.append(ApprovalRule(rule["tool"], when=_matcher(when) if when else None))
    return rules


def describe_rule(rule: t.Any) -> tuple[str, str]:
    """Return a rule given in code or the file as the tool it matches and when, for the admin view."""
    if isinstance(rule, str):
        return rule, "Always"
    if isinstance(rule, Mapping):
        when = rule.get("when") or {}
        text = ", ".join(f"{k} is {v}" for k, v in when.items())
        return rule.get("tool", ""), text or "Always"
    tool = getattr(rule, "tool", str(rule))
    return tool, "Always" if getattr(rule, "when", None) is None else "Some calls (set in code)"
