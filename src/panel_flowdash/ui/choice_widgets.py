"""The widgets the inspector's config form uses for fields that offer choices."""

from __future__ import annotations

import typing as t

import panel_material_ui as pmui
import param

if t.TYPE_CHECKING:
    from panel_flowdash.config_choices import Choices

CHOOSE = "Choose one"
# More options than this make a dropdown searchable.
_SEARCHABLE = 8


class _KeepValue:
    """Keeps a value the options do not hold instead of resetting it.

    Panel's selects reset such a value to their default while rendering it,
    which would write that default into the node's config through the form's
    link. The form is rebuilt with the value among its options once the
    choices are asked for again.
    """

    def _process_param_change(self, msg):
        value = self.value
        with param.parameterized.discard_events(self):
            props = super()._process_param_change(msg)
            self.value = value
        return props


class ChoiceSelect(_KeepValue, pmui.Select):
    """A dropdown of a field's choices."""


class ChoiceMultiSelect(_KeepValue, pmui.MultiChoice):
    """A multi-select of a list field's choices."""


class SuggestInput(_KeepValue, pmui.AutocompleteInput):
    """A text field suggesting a field's choices."""


def _missing(value: t.Any) -> str:
    return f"{value} (not available)"


def choice_widget(
    parameter: param.Parameter, choices: Choices, current: t.Any
) -> dict[str, t.Any]:
    """Return the ``pn.Param`` widget spec for a field with *choices* holding *current*.

    A list field gets a multi-select, a field restricted to its choices a
    dropdown, and any other a text field suggesting them. A current value
    that is not among the choices stays selectable, marked as not available,
    so showing the form never changes the config.
    """
    options = choices.labelled()
    values = list(options.values())
    if isinstance(parameter, (param.List, param.ListSelector)):
        extra = [value for value in current or [] if value not in values]
        options.update({_missing(value): value for value in extra})
        return {"type": ChoiceMultiSelect, "options": options}
    if choices.restrict or not isinstance(parameter, param.String):
        if current not in values:
            options = {CHOOSE if current in (None, "") else _missing(current): current, **options}
        return {"type": ChoiceSelect, "options": options, "searchable": len(options) > _SEARCHABLE}
    return {
        "type": SuggestInput,
        "options": [str(value) for value in values],
        "restrict": False,
        "min_characters": 0,
        "case_sensitive": False,
        "search_strategy": "includes",
    }


def form_widgets(
    state: param.Parameterized, names: t.Iterable[str], choices: t.Mapping[str, Choices]
) -> dict[str, dict[str, t.Any]]:
    """Return the widget specs of the fields of *state* among *names* that have *choices*."""
    return {
        name: choice_widget(state.param[name], choices[name], getattr(state, name))
        for name in names
        if name in choices
    }


def widgets_key(widgets: t.Mapping[str, dict[str, t.Any]]) -> tuple:
    """Return a comparable summary of widget specs, to tell whether a form must be rebuilt."""

    def freeze(value):
        if isinstance(value, dict):
            return tuple((k, freeze(v)) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return tuple(freeze(v) for v in value)
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    return tuple(sorted((name, freeze(spec)) for name, spec in widgets.items()))
