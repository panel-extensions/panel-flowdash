"""The project file's ``ai`` section: model providers, the models people may pick and their limits.

```yaml
ai:
  default: openai:gpt-6-luna
  providers:
    openrouter:
      api_key: ${OPENROUTER_API_KEY}
    ollama:
      base_url: http://127.0.0.1:11434
  models:
    - id: openai:gpt-6-luna
      label: Luna
      offered_to: [assistant, explore]
      settings: {max_tokens: 1000}
      monthly_limit: 50
      fallback: anthropic:claude-haiku-5.5
```

A provider's ``api_key`` is a secret (see `panel_flowdash.secret_store`): a
``${VARIABLE}`` reference or a value stored encrypted from the admin view.
A provider without one uses its usual environment variable, e.g.
``OPENAI_API_KEY``. A model is offered to the assistant and/or Explore
(``offered_to``) and, with ``groups``, only to members of those groups.
``settings`` are pydantic-ai model settings merged over the app's
``agent_model_settings`` for that model's requests. When a model's spend
this month reaches ``monthly_limit`` (US dollars), requests use its
``fallback`` instead, or are refused when it has none under its own limit.

Models given in code (``agent_models``, ``--agent-model``) come first; an
entry here with the same id adds its offer, settings and limit to them.
Needs nothing beyond panel-flowdash; the model objects are made with
pydantic-ai when the agent extra is installed.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import datetime as dt
import json
import logging
import math
import os
import tempfile
import threading
import time
import typing as t
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from panel_flowdash.project import ProjectError, SectionError, _retry_sharing
from panel_flowdash.secret_store import SecretError, check_secret, describe_secret, resolve_secret

if t.TYPE_CHECKING:
    from panel_flowdash.auth import Identity
    from panel_flowdash.project import Section
    from panel_flowdash.secret_store import SecretInfo

logger = logging.getLogger(__name__)

SURFACES = {"assistant": "Assistant", "explore": "Explore"}

# The model settings an admin may set per model, with their bounds.
MODEL_SETTINGS: dict[str, tuple[str, float, float | None, bool]] = {
    # key: (label, low, high, integer)
    "temperature": ("Temperature", 0.0, 2.0, False),
    "max_tokens": ("Max tokens", 1, None, True),
    "timeout": ("Timeout", 1, None, False),
}


@dataclass(frozen=True)
class Provider:
    """A model provider: how its key is found and how a key is tested."""

    name: str
    label: str
    env: str | None
    test_url: str
    auth: str = "bearer"
    blurb: str = ""
    local: bool = False
    models_url: str | None = None
    """Where the provider lists its models, if not at ``test_url``."""


PROVIDERS: dict[str, Provider] = {
    p.name: p
    for p in (
        Provider("openai", "OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1/models"),
        Provider(
            "anthropic",
            "Anthropic",
            "ANTHROPIC_API_KEY",
            "https://api.anthropic.com/v1/models",
            auth="x-api-key",
            models_url="https://api.anthropic.com/v1/models?limit=1000",
        ),
        Provider(
            "openrouter",
            "OpenRouter",
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/api/v1/key",
            blurb="One key for models of many providers",
            models_url="https://openrouter.ai/api/v1/models",
        ),
        Provider(
            "google-gla",
            "Google",
            "GOOGLE_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/models",
            auth="x-goog-api-key",
            models_url="https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
        ),
        Provider("groq", "Groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/models"),
        Provider("mistral", "Mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1/models"),
        Provider(
            "ollama",
            "Ollama",
            None,
            "/api/tags",
            auth="none",
            blurb="Models running on a local server",
            local=True,
        ),
    )
}

OLLAMA_URL = "http://127.0.0.1:11434"


def provider_name(model_id: str) -> str | None:
    """Return the provider a pydantic-ai model id names, e.g. ``openai`` for ``openai:gpt-5``."""
    prefix, sep, _ = model_id.partition(":")
    return prefix if sep else None


# ---------------------------------------------------------------------------
# Loading the section
# ---------------------------------------------------------------------------


def _number(value: t.Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_model_settings(settings: t.Any) -> list[tuple[tuple, str]]:
    """Return the problems of a model's ``settings``, each with its path inside them."""
    if settings is None:
        return []
    if not isinstance(settings, Mapping):
        return [((), "`settings` is a mapping, e.g. `{temperature: 0}`.")]
    problems = []
    for key, value in settings.items():
        if key not in MODEL_SETTINGS:
            known = ", ".join(f"`{k}`" for k in MODEL_SETTINGS)
            problems.append(((key,), f"Unknown model setting {key!r}; known: {known}."))
            continue
        _label, low, high, integer = MODEL_SETTINGS[key]
        if not _number(value) or (integer and not float(value).is_integer()):
            kind = "a whole number" if integer else "a number"
            problems.append(((key,), f"`{key}` is {kind}."))
        elif value < low or (high is not None and value > high):
            span = f"between {low:g} and {high:g}" if high is not None else f"at least {low:g}"
            problems.append(((key,), f"`{key}` is {span}."))
    return problems


def load_ai(section: Section) -> dict[str, t.Any]:
    """Check the ``ai`` section and return it as plain data."""
    value = section.value
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SectionError("`ai` is a mapping with `providers`, `models` and `default`.")
    problems = []
    for key in value:
        if key not in ("default", "providers", "models"):
            problems.append(
                section.problem(
                    (key,), f"Unknown key {key!r}; known: `default`, `providers`, `models`.", True
                )
            )
    providers = value.get("providers") or {}
    if not isinstance(providers, Mapping):
        problems.append(section.problem(("providers",), "`providers` is a mapping by name."))
        providers = {}
    for name, config in providers.items():
        if name not in PROVIDERS:
            known = ", ".join(f"`{p}`" for p in PROVIDERS)
            problems.append(
                section.problem(
                    ("providers", name), f"Unknown provider {name!r}; known: {known}.", True
                )
            )
            continue
        if config is None:
            continue
        if not isinstance(config, Mapping):
            problems.append(
                section.problem(
                    ("providers", name), "A provider is a mapping, e.g. `api_key: ...`."
                )
            )
            continue
        for key, item in config.items():
            if key == "api_key":
                message = check_secret(item, "api_key")
            elif key == "base_url":
                ok = isinstance(item, str) and item.lower().startswith(("http://", "https://"))
                message = None if ok else "`base_url` is an http(s) URL."
            else:
                message = f"Unknown provider setting {key!r}; known: `api_key`, `base_url`."
            if message:
                problems.append(
                    section.problem(
                        ("providers", name, key), message, key not in ("api_key", "base_url")
                    )
                )
    models = value.get("models") or []
    if not isinstance(models, list):
        problems.append(section.problem(("models",), "`models` is a list of models."))
        models = []
    seen: set[str] = set()
    for i, model in enumerate(models):
        problems.extend(_model_problems(section, i, model, seen))
    default = value.get("default")
    if default is not None and (not isinstance(default, str) or not default.strip()):
        problems.append(section.problem(("default",), "`default` is a model id."))
    if problems:
        raise ProjectError(problems)
    return {key: value[key] for key in ("default", "providers", "models") if key in value}


_MODEL_KEYS = (
    "id",
    "label",
    "description",
    "offered_to",
    "groups",
    "settings",
    "monthly_limit",
    "fallback",
)


def _model_problems(section: Section, i: int, model: t.Any, seen: set[str]) -> list:
    path = ("models", i)
    if not isinstance(model, Mapping):
        return [section.problem(path, "A model is a mapping with an `id`.")]
    problems = []
    for key in model:
        if key not in _MODEL_KEYS:
            known = ", ".join(f"`{k}`" for k in _MODEL_KEYS)
            problems.append(
                section.problem((*path, key), f"Unknown key {key!r}; known: {known}.", True)
            )
    model_id = model.get("id")
    if not isinstance(model_id, str) or ":" not in model_id:
        problems.append(
            section.problem((*path, "id"), "`id` names the model, e.g. `openai:gpt-5`.")
        )
    elif model_id in seen:
        problems.append(section.problem((*path, "id"), f"{model_id!r} is listed twice."))
    else:
        seen.add(model_id)
    for key in ("label", "description"):
        if model.get(key) is not None and not isinstance(model[key], str):
            problems.append(section.problem((*path, key), f"`{key}` is text."))
    offered = model.get("offered_to", list(SURFACES))
    if not isinstance(offered, list) or any(s not in SURFACES for s in offered):
        problems.append(
            section.problem(
                (*path, "offered_to"), "`offered_to` lists `assistant` and/or `explore`."
            )
        )
    groups = model.get("groups", [])
    if not isinstance(groups, list) or not all(isinstance(g, str) and g for g in groups):
        problems.append(section.problem((*path, "groups"), "`groups` is a list of group names."))
    for sub, message in check_model_settings(model.get("settings")):
        problems.append(section.problem((*path, "settings", *sub), message))
    limit = model.get("monthly_limit")
    if limit is not None and (not _number(limit) or limit < 0):
        problems.append(
            section.problem(
                (*path, "monthly_limit"), "`monthly_limit` is an amount in US dollars."
            )
        )
    fallback = model.get("fallback")
    if fallback is not None and (not isinstance(fallback, str) or fallback == model_id):
        problems.append(
            section.problem((*path, "fallback"), "`fallback` is the id of another model.")
        )
    return problems


# ---------------------------------------------------------------------------
# The models of a session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEntry:
    """A model as the admin view and the sessions see it: from code, the project file or both.

    ``locked`` names where a model given in code is set; its id, label
    and description are then read-only.
    """

    id: str
    label: str | None = None
    description: str | None = None
    offered_to: tuple[str, ...] = tuple(SURFACES)
    groups: tuple[str, ...] = ()
    settings: Mapping[str, t.Any] = field(default_factory=dict)
    monthly_limit: float | None = None
    fallback: str | None = None
    locked: str | None = None
    model: t.Any = None

    @property
    def name(self) -> str:
        """Return the label, or the id."""
        return self.label or self.id

    @property
    def provider(self) -> str | None:
        """Return the provider's name."""
        return provider_name(self.id)

    def offered(self, identity: Identity | None, surface: str) -> bool:
        """Whether the model is offered to *identity* in *surface*, ``assistant`` or ``explore``."""
        if surface not in self.offered_to:
            return False
        return not self.groups or (identity is not None and identity.in_groups(self.groups))

    def to_data(self) -> dict[str, t.Any]:
        """Return the project file entry of the model."""
        data: dict[str, t.Any] = {"id": self.id}
        if not self.locked:
            data.update(
                {k: v for k, v in (("label", self.label), ("description", self.description)) if v}
            )
        if tuple(self.offered_to) != tuple(SURFACES):
            data["offered_to"] = list(self.offered_to)
        if self.groups:
            data["groups"] = list(self.groups)
        if self.settings:
            data["settings"] = dict(self.settings)
        if self.monthly_limit is not None:
            data["monthly_limit"] = self.monthly_limit
        if self.fallback:
            data["fallback"] = self.fallback
        return data


def _entry(data: Mapping[str, t.Any], **extra) -> ModelEntry:
    return ModelEntry(
        id=data["id"],
        label=data.get("label"),
        description=data.get("description"),
        offered_to=tuple(data.get("offered_to", SURFACES)),
        groups=tuple(data.get("groups") or ()),
        settings=dict(data.get("settings") or {}),
        monthly_limit=data.get("monthly_limit"),
        fallback=data.get("fallback"),
        **extra,
    )


def model_entries(
    ai: Mapping[str, t.Any] | None, code_models: Sequence[t.Any] = (), locked: str | None = None
) -> list[ModelEntry]:
    """Return the models: those given in code (*code_models*, ModelChoices) first, then the file's.

    A file entry with the id of a model given in code adds its offer,
    settings, limit and fallback to it.
    """
    listed = {m["id"]: m for m in (ai or {}).get("models") or []}
    entries = []
    for choice in code_models:
        data = listed.pop(choice.id, {"id": choice.id})
        entries.append(
            _entry(
                {**data, "label": choice.label, "description": choice.description},
                locked=locked or "set in code",
                model=choice.model,
            )
        )
    entries.extend(_entry(data) for data in listed.values())
    return entries


def default_model_id(
    ai: Mapping[str, t.Any] | None, entries: Sequence[ModelEntry], code_default: str | None
) -> str | None:
    """Return the default model: the one given in code, else the file's, else the first."""
    ids = [e.id for e in entries]
    for candidate in (code_default, (ai or {}).get("default")):
        if candidate and candidate in ids:
            return candidate
    return ids[0] if ids else None


def provider_config(ai: Mapping[str, t.Any] | None, name: str | None) -> dict[str, t.Any]:
    """Return the project file's settings of provider *name*."""
    return dict(((ai or {}).get("providers") or {}).get(name or "") or {})


def provider_key(ai: Mapping[str, t.Any] | None, name: str) -> t.Any:
    """Return how provider *name*'s key is written: the file's, else its usual variable."""
    configured = provider_config(ai, name).get("api_key")
    if configured is not None:
        return configured
    provider = PROVIDERS.get(name)
    return f"${{{provider.env}}}" if provider is not None and provider.env else None


def model_object(entry: ModelEntry, ai: Mapping[str, t.Any] | None) -> t.Any:
    """Return the pydantic-ai model *entry* runs with when its provider's key or URL is in the file.

    The key goes to the provider's client, never into ``os.environ``. None
    leaves the id to pydantic-ai, which reads the usual environment
    variable, e.g. when the file sets no key or pydantic-ai is missing.
    """
    if entry.model is not None:
        return entry.model
    name = entry.provider
    config = provider_config(ai, name)
    if not config or name not in PROVIDERS:
        return None
    try:
        from pydantic_ai.models import infer_model
        from pydantic_ai.providers import infer_provider_class
    except ImportError:
        return None
    kwargs: dict[str, t.Any] = {}
    try:
        if "api_key" in config:
            kwargs["api_key"] = resolve_secret(config["api_key"])
    except SecretError as exc:
        logger.warning("The %s key cannot be used: %s", PROVIDERS[name].label, exc)
        return None
    if "base_url" in config:
        base = config["base_url"].rstrip("/")
        kwargs["base_url"] = base if base.endswith("/v1") or name != "ollama" else f"{base}/v1"
    try:
        return infer_model(entry.id, provider_factory=lambda n: infer_provider_class(n)(**kwargs))
    except Exception as exc:
        logger.warning("Could not make the model %s: %s", entry.id, type(exc).__name__)
        return None


def model_choice(entry: ModelEntry, ai: Mapping[str, t.Any] | None):
    """Return the ``panel_agents.ModelChoice`` a session offers for *entry*."""
    from panel_agents import ModelChoice

    return ModelChoice(entry.id, entry.label, entry.description, model_object(entry, ai))


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------


def month_key(when: dt.datetime | None = None) -> str:
    """Return the month spend is counted in, e.g. ``2026-11`` (UTC)."""
    return (when or dt.datetime.now(dt.UTC)).strftime("%Y-%m")


@contextlib.contextmanager
def file_lock(path: Path):
    """Hold an exclusive lock on *path* (``fcntl`` on POSIX, ``msvcrt`` on Windows), across processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            for attempt in range(6):
                try:
                    # Each attempt retries for about ten seconds before raising.
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if attempt == 5:
                        raise
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# How many of the admins' destructive actions the ledger keeps.
ADMIN_LOG_SIZE = 500

# How often the dashboard opens noted in memory are written to the ledger.
OPENS_FLUSH_SECONDS = 60.0


class UsageLedger:
    """What models cost, who opened the app and its dashboards, and what admins did, by the project.

    It is a JSON file. Without a path everything is kept in memory for the process. Every
    process serving the project adds to the same file: each change is a
    read-modify-write under an OS file lock (``usage.json.lock``), so
    several workers, or people sharing the project on a network drive, do
    not lose each other's spend.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else None
        self._thread_lock = threading.Lock()
        self._memory: dict[str, t.Any] = {}
        self._opens: dict[str, str] = {}
        self._opens_lock = threading.Lock()
        self._opens_flushed = time.monotonic()
        atexit.register(self.flush_opens)

    @contextlib.contextmanager
    def _lock(self):
        with self._thread_lock:
            if self.path is None:
                yield
                return
            with file_lock(self.path.with_name(self.path.name + ".lock")):
                yield

    def _read(self) -> dict[str, t.Any]:
        if self.path is None:
            return self._memory
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, t.Any]) -> None:
        if self.path is None:
            self._memory = data
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".usage.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=1, sort_keys=True)
            _retry_sharing(lambda: os.replace(temporary, self.path))
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def add(self, model_id: str, cost: float | None, when: dt.datetime | None = None) -> None:
        """Count a request to *model_id* that cost *cost* US dollars (None: unknown)."""
        try:
            with self._lock():
                self._add(model_id, cost, when)
        except OSError as exc:
            logger.warning("Could not record the spend of %s: %s", model_id, exc)

    def _add(self, model_id: str, cost: float | None, when: dt.datetime | None) -> None:
        data = self._read()
        month = data.setdefault("spend", {}).setdefault(month_key(when), {})
        spent = month.setdefault(model_id, {"usd": 0.0, "requests": 0})
        spent["usd"] = round(spent["usd"] + (cost or 0.0), 6)
        spent["requests"] += 1
        self._write(data)

    def month(self, model_id: str, when: dt.datetime | None = None) -> tuple[float, int]:
        """Return the US dollars spent on *model_id* this month and the number of requests."""
        spent = self._locked_read().get("spend", {}).get(month_key(when), {}).get(model_id) or {}
        return float(spent.get("usd", 0.0)), int(spent.get("requests", 0))

    def spent(self, model_id: str) -> float:
        """Return the US dollars spent on *model_id* this month."""
        return self.month(model_id)[0]

    def see(
        self, user: str, groups: t.Iterable[str] = (), when: dt.datetime | None = None
    ) -> None:
        """Note that *user*, a member of *groups*, opened the app."""
        try:
            with self._lock():
                data = self._read()
                data.setdefault("people", {})[user] = {
                    "last_seen": (when or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"),
                    "groups": sorted(groups),
                }
                self._write(data)
        except OSError as exc:
            logger.warning("Could not record a visit: %s", exc)

    def _locked_read(self) -> dict[str, t.Any]:
        try:
            with self._lock():
                return self._read()
        except OSError:
            return self._read()

    def people(self) -> dict[str, dict[str, t.Any]]:
        """Return who opened the app, with when they were last seen and their groups then."""
        people = self._locked_read().get("people")
        return dict(people) if isinstance(people, dict) else {}

    def opened(self, dashboard_id: str, when: dt.datetime | None = None) -> None:
        """Note that someone opened the dashboard *dashboard_id*.

        Opens are kept in memory and written together at most every
        ``OPENS_FLUSH_SECONDS`` (and when the process exits), so opening a
        dashboard does not take the file lock each time.
        """
        stamp = (when or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds")
        with self._opens_lock:
            previous = self._opens.get(dashboard_id)
            if previous is None or stamp > previous:
                self._opens[dashboard_id] = stamp
            due = time.monotonic() - self._opens_flushed >= OPENS_FLUSH_SECONDS
        if due:
            self.flush_opens()

    def flush_opens(self) -> None:
        """Write the opens noted since the last write."""
        with self._opens_lock:
            opens, self._opens = self._opens, {}
            self._opens_flushed = time.monotonic()
        if not opens:
            return
        try:
            with self._lock():
                data = self._read()
                known = data.setdefault("dashboards", {})
                for dashboard_id, stamp in opens.items():
                    previous = (known.get(dashboard_id) or {}).get("last_opened")
                    if not isinstance(previous, str) or stamp > previous:
                        known[dashboard_id] = {"last_opened": stamp}
                self._write(data)
        except OSError as exc:
            logger.warning("Could not record which dashboards were opened: %s", exc)

    def dashboards(self) -> dict[str, dict[str, t.Any]]:
        """Return when each dashboard was last opened, by id, including opens not written yet."""
        dashboards = self._locked_read().get("dashboards")
        result = dict(dashboards) if isinstance(dashboards, dict) else {}
        with self._opens_lock:
            pending = dict(self._opens)
        for dashboard_id, stamp in pending.items():
            previous = (result.get(dashboard_id) or {}).get("last_opened")
            if not isinstance(previous, str) or stamp > previous:
                result[dashboard_id] = {"last_opened": stamp}
        return result

    def forget_dashboards(self, dashboard_ids: t.Iterable[str]) -> None:
        """Drop what is noted about the dashboards *dashboard_ids*, e.g. once deleted."""
        ids = set(dashboard_ids)
        with self._opens_lock:
            for dashboard_id in ids:
                self._opens.pop(dashboard_id, None)
        try:
            with self._lock():
                data = self._read()
                opened = data.get("dashboards")
                if isinstance(opened, dict) and ids & set(opened):
                    data["dashboards"] = {k: v for k, v in opened.items() if k not in ids}
                    self._write(data)
        except OSError as exc:
            logger.warning("Could not update the record of opened dashboards: %s", exc)

    def record(
        self,
        user: str,
        action: str,
        dashboards: t.Sequence[t.Mapping[str, str]] = (),
        when: dt.datetime | None = None,
        **details: t.Any,
    ) -> dict[str, t.Any]:
        """Add an admin's *action* on *dashboards* (each an ``id`` and ``title``) to the admin log."""
        entry = {
            "when": (when or dt.datetime.now(dt.UTC)).isoformat(timespec="seconds"),
            "user": user,
            "action": action,
            "dashboards": [dict(d) for d in dashboards],
            **details,
        }
        logger.info(
            "Admin %s: %s %s", user, action, ", ".join(d.get("id", "") for d in dashboards)
        )
        try:
            with self._lock():
                data = self._read()
                log = data.get("admin_log")
                log = log if isinstance(log, list) else []
                data["admin_log"] = [*log, entry][-ADMIN_LOG_SIZE:]
                self._write(data)
        except OSError as exc:
            logger.warning("Could not write the admin log: %s", exc)
        return entry

    def admin_log(self) -> list[dict[str, t.Any]]:
        """Return the admin log, oldest first."""
        log = self._locked_read().get("admin_log")
        return [e for e in log if isinstance(e, dict)] if isinstance(log, list) else []


_LEDGERS: dict[str, UsageLedger] = {}
_LEDGERS_LOCK = threading.Lock()

USAGE_FILE = Path(".flowdash") / "usage.json"


def ledger_for(directory: str | os.PathLike | None) -> UsageLedger:
    """Return the spend ledger of the project in *directory* (in memory without one)."""
    key = str(Path(directory).resolve() / USAGE_FILE) if directory is not None else ""
    with _LEDGERS_LOCK:
        if key not in _LEDGERS:
            _LEDGERS[key] = UsageLedger(key or None)
        return _LEDGERS[key]


def _dollars(amount: float) -> str:
    return f"${amount:,.2f}"


def select_model(
    entries: Mapping[str, ModelEntry], model_id: str, spent: Callable[[str], float]
) -> tuple[str | None, str | None]:
    """Return the model a request picked as *model_id* runs with, and a note on why if another.

    A model whose spend this month (*spent*) reached its limit gives way
    to its fallback, and that one to its own; None (with the reason) when
    none is under its limit.
    """
    current: str | None = model_id
    seen: set[str] = set()
    first = entries.get(model_id)
    if first is None:
        return model_id, None
    while current is not None and current not in seen:
        seen.add(current)
        entry = entries.get(current)
        if entry is None and current != model_id:
            # A fallback that is no longer a model.
            break
        if entry is None or entry.monthly_limit is None or spent(current) < entry.monthly_limit:
            if current == model_id:
                return current, None
            name = entry.name if entry is not None else current
            return current, (
                f"{first.name} reached its monthly limit of {_dollars(first.monthly_limit)}, "
                f"so this request uses {name}."
            )
        current = entry.fallback
    return None, (
        f"{first.name} reached its monthly spending limit of {_dollars(first.monthly_limit)}"
        + (" and its fallback is over its limit too." if first.fallback else ".")
        + " An admin can raise the limit under Admin, AI."
    )


# ---------------------------------------------------------------------------
# Testing a key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyCheck:
    """How a provider answered a test of its key, never holding the key."""

    ok: bool
    message: str
    when: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


# The last test of each provider's key in this process, shown on its card.
KEY_CHECKS: dict[str, KeyCheck] = {}

Fetch = Callable[[str, Mapping[str, str]], t.Awaitable[tuple[int, str]]]


async def _fetch(url: str, headers: Mapping[str, str]) -> tuple[int, str]:
    from tornado.httpclient import AsyncHTTPClient, HTTPClientError

    try:
        response = await AsyncHTTPClient().fetch(
            url, headers=dict(headers), request_timeout=15, raise_error=False
        )
    except HTTPClientError as exc:  # pragma: no cover - raise_error=False
        return exc.code, exc.message or ""
    return response.code, response.reason or ""


def _request(
    provider: Provider, key: str | None, base_url: str | None, url: str
) -> tuple[str, dict[str, str]]:
    """Return *url* (a path on a local server) and the headers that carry *key*."""
    headers: dict[str, str] = {}
    if provider.local:
        return (base_url or OLLAMA_URL).rstrip("/").removesuffix("/v1") + url, headers
    if key:
        if provider.auth == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers[provider.auth] = key
    if provider.name == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    return url, headers


async def check_key(
    name: str, key: str | None = None, base_url: str | None = None, fetch: Fetch | None = None
) -> KeyCheck:
    """Ask provider *name* whether *key* is valid (or a local server whether it is up)."""
    provider = PROVIDERS[name]
    if not provider.local and not key:
        return KeyCheck(False, "No key is set.")
    url, headers = _request(provider, key, base_url, provider.test_url)
    try:
        status, reason = await asyncio.wait_for((fetch or _fetch)(url, headers), 20)
    except Exception as exc:
        message = f"{provider.label} could not be reached ({type(exc).__name__})."
        return KeyCheck(False, message)
    if 200 <= status < 300:
        return KeyCheck(True, f"{provider.label} accepted the key." if key else "Reachable.")
    if status in (401, 403):
        return KeyCheck(
            False,
            f"{provider.label} rejected the key ({status} {reason or 'Unauthorized'}). "
            "Check that it was copied in full and has not been revoked.",
        )
    if status == 599:
        return KeyCheck(False, f"{provider.label} could not be reached.")
    return KeyCheck(False, f"{provider.label} answered {status} {reason}.".rstrip() + ".")


# ---------------------------------------------------------------------------
# The models a provider offers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelList:
    """The model ids a provider listed, as pydantic-ai ids (``openai:gpt-5``), or why it did not."""

    ids: tuple[str, ...]
    message: str = ""


# Listed models per provider and key, for MODEL_LIST_TTL seconds; keyed by a hash, never the key.
_MODEL_LISTS: dict[tuple[str, str], tuple[float, ModelList]] = {}
MODEL_LIST_TTL = 600

FetchBody = Callable[[str, Mapping[str, str]], t.Awaitable[tuple[int, str]]]

# Ids from OpenAI and Groq lists that are not chat models.
_NOT_CHAT = (
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    "moderation",
    "transcribe",
    "babbage",
    "davinci",
    "guard",
    "image",
    "audio",
    "realtime",
    "gpt-live",
)


async def _fetch_body(url: str, headers: Mapping[str, str]) -> tuple[int, str]:
    from tornado.httpclient import AsyncHTTPClient

    response = await AsyncHTTPClient().fetch(
        url, headers=dict(headers), request_timeout=15, raise_error=False
    )
    body = response.body.decode("utf-8", "replace") if response.body else ""
    return response.code, body


def _created(entry: Mapping[str, t.Any]) -> float:
    """Return when a listed model was created, as a timestamp, or 0 if the list does not say."""
    value = entry.get("created", entry.get("created_at"))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    return 0.0


def _model_names(name: str, data: t.Any) -> list[str]:
    """Return the chat model names in provider *name*'s list response *data*, newest first."""
    if name == "google-gla":
        return [
            m["name"].removeprefix("models/")
            for m in data.get("models") or []
            if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])
        ]
    if name == "ollama":
        entries = [{"id": m.get("name") or m.get("model")} for m in data.get("models") or []]
    else:
        entries = data.get("data") or []
    if name == "mistral":
        entries = [
            m for m in entries if (m.get("capabilities") or {}).get("completion_chat", True)
        ]
    if name in ("openai", "groq"):
        entries = [
            m for m in entries if not any(part in (m.get("id") or "") for part in _NOT_CHAT)
        ]
    entries = sorted(entries, key=lambda m: (-_created(m), m.get("id") or ""))
    return [m.get("id") for m in entries]


async def list_models(
    name: str,
    key: str | None = None,
    base_url: str | None = None,
    fetch: FetchBody | None = None,
) -> ModelList:
    """Ask provider *name* which chat models *key* can use, as pydantic-ai ids, newest first.

    Answers for the same provider, key and server are reused for
    :data:`MODEL_LIST_TTL` seconds. A provider that cannot be reached or
    refuses the key gives no ids and a message saying why.
    """
    import hashlib

    provider = PROVIDERS[name]
    if not provider.local and not key and name != "openrouter":
        return ModelList((), "No key is set.")
    token = hashlib.sha256(f"{key or ''}|{base_url or ''}".encode()).hexdigest()[:16]
    cached = _MODEL_LISTS.get((name, token))
    if cached is not None and time.monotonic() - cached[0] < MODEL_LIST_TTL:
        return cached[1]
    url, headers = _request(provider, key, base_url, provider.models_url or provider.test_url)
    try:
        status, body = await asyncio.wait_for((fetch or _fetch_body)(url, headers), 20)
    except Exception as exc:
        return ModelList((), f"{provider.label} could not be reached ({type(exc).__name__}).")
    if not 200 <= status < 300:
        return ModelList((), f"{provider.label} did not list its models ({status}).")
    try:
        names = _model_names(name, json.loads(body))
    except (ValueError, AttributeError, KeyError, TypeError):
        return ModelList((), f"{provider.label} answered with a list FlowDash cannot read.")
    result = ModelList(tuple(dict.fromkeys(f"{name}:{n}" for n in names if n)))
    _MODEL_LISTS[(name, token)] = (time.monotonic(), result)
    return result


def key_status(ai: Mapping[str, t.Any] | None, name: str) -> tuple[SecretInfo, KeyCheck | None]:
    """Return where provider *name*'s key comes from and the last test of it."""
    return describe_secret(provider_key(ai, name)), KEY_CHECKS.get(name)
