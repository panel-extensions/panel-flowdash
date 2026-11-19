"""The models the assistant may use, from FlowDashApp options and the environment.

Needs nothing beyond panel-flowdash, so ``flowdash serve`` can read them without the agent extra.
"""

from __future__ import annotations

import os
import typing as t

from panel_agents import ModelChoice

# Names the default agent's model, e.g. ``openai:gpt-5``.
MODEL_ENV = "FLOWDASH_AGENT_MODEL"

# Names the models a person may pick from, e.g. ``openai:gpt-5=GPT-5,anthropic:<model>``.
MODELS_ENV = "FLOWDASH_AGENT_MODELS"


def default_model() -> str | None:
    """Return the model the default agent uses, from ``FLOWDASH_AGENT_MODEL``, or None."""
    return os.environ.get(MODEL_ENV) or None


def parse_models(
    text: str | t.Iterable[str] | None, source: str = MODELS_ENV
) -> list[dict[str, str]]:
    """Read models written as ``id[=label]``, comma-separated, as ``ModelChoice`` dicts.

    *text* is one string or several, e.g. repeated command-line flags, each of
    which may list several models. An entry without an id raises a ValueError
    naming *source*.
    """
    if not text:
        return []
    texts = [text] if isinstance(text, str) else list(text)
    models = []
    for item in (item for chunk in texts for item in chunk.split(",")):
        if not item.strip():
            continue
        model_id, _, label = item.partition("=")
        if not model_id.strip():
            raise ValueError(
                f"{source}: {item.strip()!r} names no model; write it as id or id=label."
            )
        choice = {"id": model_id.strip()}
        if label.strip():
            choice["label"] = label.strip()
        models.append(choice)
    return models


_FIELDS = {"id", "label", "description", "model"}


def validate_models(models: t.Sequence[t.Any], source: str = "agent_models") -> list[ModelChoice]:
    """Return *models* as ModelChoices, or raise a ValueError naming the first entry that is not one."""
    choices = []
    for i, entry in enumerate(models):
        where = f"{source}[{i}] ({entry!r})"
        if isinstance(entry, str):
            if not entry.strip():
                raise ValueError(f"{where} is an empty model id.")
            choices.append(ModelChoice(entry.strip()))
        elif isinstance(entry, dict):
            unknown = sorted(set(entry) - _FIELDS, key=str)
            if unknown:
                raise ValueError(
                    f"{where} has unknown keys {', '.join(map(repr, unknown))}; "
                    f"use {', '.join(sorted(_FIELDS))}."
                )
            if not isinstance(entry.get("id"), str) or not entry["id"].strip():
                raise ValueError(
                    f"{where} needs an 'id', the model's name, e.g. 'openai:<model>'."
                )
            for key in ("label", "description"):
                if entry.get(key) is not None and not isinstance(entry[key], str):
                    raise ValueError(f"{where}: {key!r} must be a string.")
            choices.append(ModelChoice(**{**entry, "id": entry["id"].strip()}))
        elif isinstance(entry, ModelChoice):
            choices.append(entry)
        else:
            raise ValueError(
                f"{where} must be a model id, a dict with an 'id' or a panel_agents.ModelChoice."
            )
    return choices


def configured_models(
    models: t.Sequence[t.Any] | None, model: str | None
) -> tuple[list[ModelChoice], str | None]:
    """Return the models a person may pick from, each id once, and the default one's id.

    *models* default to ``FLOWDASH_AGENT_MODELS`` and *model* to
    ``FLOWDASH_AGENT_MODEL``, else the first of *models*. A default that is
    not among *models* is offered first. Raises a ValueError for an entry
    that is not a model.
    """
    if models:
        listed = validate_models(models)
    else:
        listed = validate_models(parse_models(os.environ.get(MODELS_ENV)), MODELS_ENV)
    choices: dict[str, ModelChoice] = {}
    for choice in listed:
        choices.setdefault(choice.id, choice)
    model = model or default_model() or next(iter(choices), None)
    if model is not None and model not in choices:
        choices = {model: ModelChoice(model), **choices}
    return list(choices.values()), model
