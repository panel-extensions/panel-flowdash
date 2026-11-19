"""The inspector's editor of a literal value for an unconnected input."""

from __future__ import annotations

import ast
import typing as t
from functools import partial

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.input_literals import explain, literal_parameter, port_literal
from panel_flowdash.ui.choice_widgets import form_widgets, widgets_key

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import InputPort
    from panel_flowdash.config_choices import Choices

VALUE_LABEL = "Value"

_EXAMPLES = (
    (param.Range, "(2010, 2020)"),
    (param.Tuple, "(1, 2)"),
    (param.List, "[1, 2]"),
    (param.Dict, '{"name": 1}'),
)


def _typed_as_text(parameter: param.Parameter) -> bool:
    """Whether a value of *parameter* is typed as text, as a Python literal.

    A range with bounds gets a slider and a selector a dropdown; lists,
    tuples, mappings and untyped values are written out.
    """
    if isinstance(parameter, param.Selector):
        return False
    if isinstance(parameter, param.Range):
        return None in (parameter.bounds or (None, None))
    return isinstance(parameter, (param.List, param.Tuple, param.Dict)) or (
        type(parameter) is param.Parameter
    )


def _as_text(value: t.Any) -> str:
    return "" if value is None else repr(value)


class LiteralEditor(Viewer):
    """A typed field editing the literal value of one input, and a button removing it.

    The button is laid out by the caller, beside the input's name. The field
    follows the input's Parameter, or its port type, and offers the choices
    the component computes for the input, like a config field. Each edit calls
    *on_change* with the new value; *on_change* raises ValueError to refuse
    it, and the reason is shown under the field. Emptying the field keeps the
    value; the button removes it, calling *on_change* with None.
    """

    def __init__(
        self,
        port: InputPort,
        value: t.Any,
        is_set: bool,
        on_change: t.Callable[[t.Any], None],
        choices: Choices | None = None,
    ):
        super().__init__()
        self.port = port
        self._on_change = on_change
        self._syncing = False
        parameter = literal_parameter(port)
        if parameter is None:
            raise ValueError(f"Input '{port.name}' cannot hold a literal value.")
        parameter.label = VALUE_LABEL
        self._parameter = parameter
        self._current = port_literal(port, value) if is_set else None
        self._holder = type(f"Literal_{port.name}", (param.Parameterized,), {"value": parameter})(
            value=self._current if is_set else _initial(port, parameter)
        )
        self._text = None
        if _typed_as_text(parameter):
            self._text = pmui.TextInput(
                label=VALUE_LABEL,
                value=_as_text(self._holder.value),
                sizing_mode="stretch_width",
                margin=(5, 10),
                css_classes=["fd-literal-text"],
            )
            self._text.param.watch(self._typed, "value")
        self.choices_key = ()
        self._form = pn.Column(margin=0, sizing_mode="stretch_width")
        self.set_choices(choices)
        self._error = pmui.Typography(
            "",
            variant="caption",
            color="error",
            visible=False,
            margin=(2, 10, 0, 10),
            css_classes=["fd-literal-error"],
        )
        self.clear_button = pmui.IconButton(
            icon="backspace",
            description="Remove the value set here",
            size="small",
            margin=0,
            visible=is_set,
            css_classes=["fd-literal-clear"],
        )
        self.clear_button.on_click(self._clear)
        self._holder.param.watch(self._edited, "value")
        self._layout = pn.Column(
            self._form,
            self._error,
            margin=(6, 0, 0, 0),
            sizing_mode="stretch_width",
            css_classes=["fd-literal-editor"],
        )

    def set_choices(self, choices: Choices | None):
        """Offer *choices* in the field, rebuilding it only if its widget changes."""
        mapping = {"value": choices} if choices is not None else {}
        widgets = form_widgets(self._holder, ["value"], mapping)
        if self._text is not None and not widgets:
            if not self._form.objects:
                self._form[:] = [self._text]
            return
        key = widgets_key(widgets)
        if key == self.choices_key and self._form.objects:
            return
        current = self._form[0] if self._form.objects else None
        spec = widgets.get("value") or {}
        field = getattr(current, "_widgets", {}).get("value") if current is not None else None
        if field is not None and spec and type(field) is spec.get("type"):
            # The same kind of field with other options: update it, so text
            # being typed into it stays.
            field.param.update(**{k: v for k, v in spec.items() if k != "type"})
            self.choices_key = key
            return
        self.choices_key = key
        self._form[:] = [
            pn.Param(
                self._holder,
                parameters=["value"],
                widgets=widgets or None,
                show_name=False,
                expand_button=False,
                sizing_mode="stretch_width",
                margin=0,
            )
        ]

    def sync(self, value: t.Any, is_set: bool):
        """Show *value*, set elsewhere, e.g. by undo or the agent, without calling *on_change*."""
        self.clear_button.visible = is_set
        self._current = port_literal(self.port, value) if is_set else None
        shown = self._current if is_set else _initial(self.port, self._parameter)
        self._show(shown)
        self._error.visible = False

    def _show(self, value: t.Any):
        self._syncing = True
        try:
            if self._holder.value != value:
                self._holder.value = value
        finally:
            self._syncing = False
        if self._text is not None:
            _later(partial(self._show_text, _as_text(value)))

    def _show_text(self, text: str):
        if self._text.value == text:
            return
        self._syncing = True
        try:
            self._text.value = text
        finally:
            self._syncing = False

    def _refuse(self, reason: str):
        self._error.object = reason
        self._error.visible = True

    def _typed(self, event):
        if self._syncing:
            return
        text = event.new.strip()
        if not text:
            self._show(self._current if self._current is not None else self._holder.value)
            self._error.visible = False
            return
        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
            example = next(
                (e for cls, e in _EXAMPLES if isinstance(self._parameter, cls)), '42 or "text"'
            )
            self._refuse(f"{text!r} is not a value. Write it like {example}.")
            return
        try:
            self._holder.value = port_literal(self.port, value)
        except (ValueError, TypeError) as exc:
            self._refuse(explain(self._parameter, exc))

    def _edited(self, event):
        if self._syncing:
            return
        if event.new is None:
            # An emptied field, or text a number field cannot read, keeps the value;
            # the button beside the input's name removes it.
            self._show(self._current if self._current is not None else event.old)
            self._error.visible = False
            return
        try:
            self._on_change(event.new)
        except ValueError as exc:
            self._refuse(str(exc))
            return
        self._current = event.new
        self._error.visible = False
        self.clear_button.visible = True
        if self._text is not None:
            self._show(event.new)

    def _clear(self, _event):
        self._on_change(None)
        self.sync(None, False)

    def __panel__(self):
        """Render the editor."""
        return self._layout


def _later(callback: t.Callable[[], None]):
    """Run *callback* after the current change from the browser has been processed.

    A value written back to a field from within the callback of the change
    the browser sent for it is not sent back, so the field would keep showing
    what was typed.
    """
    doc = pn.state.curdoc
    if doc is not None and doc.session_context is not None:
        doc.add_next_tick_callback(callback)
    else:
        callback()


def _initial(port: InputPort, parameter: param.Parameter) -> t.Any:
    """Return the value an input shows while nothing is set: its default or its component's."""
    for default in (
        port.default,
        port.parameter.default if port.parameter is not None else None,
    ):
        if default is None:
            continue
        try:
            parameter._validate(default)
        except Exception:
            continue
        return default
    return None
