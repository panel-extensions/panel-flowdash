"""The widgets the inspector's config form uses for fields that offer choices."""

from __future__ import annotations

import typing as t

import panel_material_ui as pmui
import param

if t.TYPE_CHECKING:
    from panel_flowdash.config_choices import Choices

EMPTY = "Not set"
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


def _empty_value(parameter: param.Parameter, current: t.Any) -> tuple[bool, t.Any]:
    """Return whether *parameter* can be left empty and the value that leaves it so.

    A text field is empty as ``""``, or as None when it holds None; any
    other field only as None, if it allows None.
    """
    if isinstance(parameter, param.String):
        return True, None if current is None and parameter.allow_None else ""
    return bool(parameter.allow_None), None


def _select(parameter: param.Parameter, options: dict[str, t.Any], current: t.Any) -> dict:
    """Return a dropdown spec over *options*, with an empty option and the current value."""
    values = list(options.values())
    can_be_empty, empty = _empty_value(parameter, current)
    if current not in values and current not in (None, ""):
        options = {_missing(current): current, **options}
    if can_be_empty and empty not in values:
        options = {EMPTY: empty, **options}
    return {"type": ChoiceSelect, "options": options, "searchable": len(options) > _SEARCHABLE}


def choice_widget(
    parameter: param.Parameter, choices: Choices, current: t.Any
) -> dict[str, t.Any]:
    """Return the ``pn.Param`` widget spec for a field with *choices* holding *current*.

    A list field gets a multi-select, a field restricted to its choices a
    dropdown, and any other a text field suggesting them. A dropdown offers
    an empty option when the field may be left empty. A current value that
    is not among the choices stays selectable, marked as not available, so
    showing the form never changes the config.
    """
    options = choices.labelled()
    values = list(options.values())
    if isinstance(parameter, (param.List, param.ListSelector)):
        extra = [value for value in current or [] if value not in values]
        options.update({_missing(value): value for value in extra})
        return {"type": ChoiceMultiSelect, "options": options}
    if choices.restrict or not isinstance(parameter, param.String):
        return _select(parameter, options, current)
    return {
        "type": SuggestInput,
        "options": [str(value) for value in values],
        "restrict": False,
        "min_characters": 0,
        "case_sensitive": False,
        "search_strategy": "includes",
    }


def plain_widget(parameter: param.Parameter, current: t.Any) -> dict[str, t.Any] | None:
    """Return the widget spec for a field without choices, or None for Panel's default.

    A list selector is a multi-select, a selector that allows None a dropdown
    with an empty option, and an optional number that is None an empty number
    field that shows None as empty rather than as 0 or its lower bound.
    """
    if isinstance(parameter, param.ListSelector):
        options = dict(parameter.get_range())
        extra = [value for value in current or [] if value not in options.values()]
        options.update({_missing(value): value for value in extra})
        return {"type": ChoiceMultiSelect, "options": options}
    if isinstance(parameter, param.Selector) and parameter.allow_None:
        return _select(parameter, dict(parameter.get_range()), current)
    if isinstance(parameter, param.Number) and parameter.allow_None:
        widget = pmui.IntInput if isinstance(parameter, param.Integer) else pmui.FloatInput
        return {"type": widget, "placeholder": EMPTY}
    return None


def form_widgets(
    state: param.Parameterized, names: t.Iterable[str], choices: t.Mapping[str, Choices]
) -> dict[str, dict[str, t.Any]]:
    """Return the widget specs of the fields of *state* among *names* that need one.

    Fields with *choices* get :func:`choice_widget`, others :func:`plain_widget`
    where Panel's default would not let them be left empty.
    """
    widgets = {}
    for name in names:
        parameter, current = state.param[name], getattr(state, name)
        if name in choices:
            widgets[name] = choice_widget(parameter, choices[name], current)
        elif (spec := plain_widget(parameter, current)) is not None:
            widgets[name] = spec
    return widgets


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
