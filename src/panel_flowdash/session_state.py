"""Per-session shared state built from registry provides/requires declarations."""

from __future__ import annotations

from typing import Any

import param

from panel_flowdash.registry import RegistryEntry


def build_session_state_class(
    registry: dict[str, RegistryEntry],
) -> type[param.Parameterized]:
    """Build a Parameterized subclass with one param per declared state key.

    Scans the registry for all `provides` and `requires` keys and creates
    a dynamic class whose parameters represent shared session state.
    """
    state_keys: dict[str, Any] = {}

    for entry in registry.values():
        for key in entry.metadata.provides:
            if isinstance(key, str) and key not in state_keys:
                state_keys[key] = None
            elif isinstance(key, dict):
                k = key.get("key", "")
                if k and k not in state_keys:
                    state_keys[k] = None
        for req in entry.metadata.requires:
            if isinstance(req, str):
                if req not in state_keys:
                    state_keys[req] = None
            elif isinstance(req, dict):
                k = req.get("key", "")
                if k and k not in state_keys:
                    state_keys[k] = req.get("fallback")

    params = {
        key: param.Parameter(default=default, allow_None=True)
        for key, default in state_keys.items()
    }

    return type("SessionState", (param.Parameterized,), params)


def check_requirements(state: param.Parameterized, requires: list) -> list[dict]:
    """Check which required keys are unsatisfied on the given state instance.

    Returns a list of dicts describing each unsatisfied requirement.
    An empty list means all requirements are met.
    """
    unsatisfied = []
    for req in requires:
        if isinstance(req, str):
            key, required, blocking, fallback = req, True, True, None
        else:
            key = req.get("key", "")
            required = req.get("required", True)
            blocking = req.get("blocking", True)
            fallback = req.get("fallback")

        if not key or not required:
            continue

        value = getattr(state, key, None)
        if value is None:
            unsatisfied.append({"key": key, "blocking": blocking, "fallback": fallback})

    return unsatisfied
