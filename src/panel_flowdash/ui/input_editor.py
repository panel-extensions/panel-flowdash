"""The inspector's editor of a literal value for an unconnected input."""

from __future__ import annotations

import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.input_literals import literal_parameter, port_literal
from panel_flowdash.ui.choice_widgets import form_widgets, widgets_key

if t.TYPE_CHECKING:
    from panel_flowdash.component_spec import InputPort
    from panel_flowdash.config_choices import Choices

VALUE_LABEL = "Value"


class LiteralEditor(Viewer):
    """A typed field editing the literal value of one input, and a button removing it.

    The button is laid out by the caller, beside the input's name.

    The field follows the input's Parameter, or its port type, and offers the
    choices the component computes for the input, like a config field. Each
    edit calls *on_change* with the new value, or None once it is cleared;
    *on_change* raises ValueError to refuse a value, which is shown under the
    field.
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
        self._holder = type(f"Literal_{port.name}", (param.Parameterized,), {"value": parameter})(
            value=port_literal(port, value) if is_set else _initial(port, parameter)
        )
        self.choices_key = ()
        self._form = pn.Column(margin=0, sizing_mode="stretch_width")
        self.set_choices(choices)
        self._error = pmui.Typography(
            "", variant="caption", color="error", visible=False, margin=(2, 0, 0, 0)
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
        key = widgets_key(widgets)
        if key == self.choices_key and self._form.objects:
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
        if not is_set:
            value = _initial(self.port, self._holder.param.value)
        else:
            value = port_literal(self.port, value)
        if self._holder.value != value:
            self._syncing = True
            try:
                self._holder.value = value
            finally:
                self._syncing = False
        self._error.visible = False

    def _edited(self, event):
        if self._syncing:
            return
        try:
            self._on_change(event.new)
        except ValueError as exc:
            self._error.object = str(exc)
            self._error.visible = True
            return
        self._error.visible = False
        self.clear_button.visible = event.new is not None

    def _clear(self, _event):
        self._on_change(None)
        self.sync(None, False)

    def __panel__(self):
        """Render the editor."""
        return self._layout


def _initial(port: InputPort, parameter: param.Parameter) -> t.Any:
    """Return the value an input shows while nothing is set: its default, if the field takes it."""
    default = port.default
    if default is None:
        return None
    try:
        parameter._validate(default)
    except Exception:
        return None
    return default
