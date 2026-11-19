"""The dialog offering to connect a table to a number input through transform steps.

A DataFrame output cannot feed a Number input as it is, but a column of it
reduced to one value can. Dropping such a connection opens this dialog
instead of refusing it: the person picks the column and the aggregation, sees
the value the input would get, and the edge is created with the two steps,
editable later in the inspector like any edge transform.
"""

from __future__ import annotations

import math
import numbers
import re
import typing as t
import weakref

import panel as pn
import panel_material_ui as pmui
from panel.viewable import Viewer

from panel_flowdash.config_choices import columns_of
from panel_flowdash.port_types import ANY, normalize_port_type, port_types_compatible
from panel_flowdash.transforms import apply_transform, parse_transform
from panel_flowdash.ui.node_card import port_label

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

#: The aggregations offered, by label.
AGGREGATIONS = {
    "Sum": "sum",
    "Mean": "mean",
    "Count": "count",
    "Minimum": "min",
    "Maximum": "max",
    "Last value": "last",
}
#: The aggregations that give a number for a column that does not hold numbers.
_ANY_COLUMN = {"Count": "count"}

_NUMBER_TYPES = ("number", "int")
_NUMERIC_COLUMNS = ("integer", "number")
# Column names that rarely hold the measure a KPI shows, so another numeric
# column is picked first when there is one.
_KEY_LIKE = re.compile(r"(^|[_\s])(id|key|code|index|idx|year|yr|zip|fips)($|[_\s])", re.I)

_ERROR_STYLE = {"color": "var(--mui-palette-error-main)"}


def _is_subtype(source: str | None, of: str) -> bool:
    return normalize_port_type(source) not in (None, ANY) and port_types_compatible(source, of)


def bridge_kind(source_type: str | None, target_type: str | None) -> str | None:
    """Return how transform steps can turn an output of *source_type* into *target_type*.

    ``"column"`` for a DataFrame, or a type registered as one of its subtypes,
    feeding a number (pick a column, then aggregate it), ``"aggregate"`` for a
    Series feeding a number, or None if no steps bridge the two.
    """
    if normalize_port_type(target_type) not in _NUMBER_TYPES:
        return None
    if _is_subtype(source_type, "DataFrame"):
        return "column"
    if _is_subtype(source_type, "Series"):
        return "aggregate"
    return None


def bridge_steps(column: str | None, how: str) -> list[dict[str, t.Any]]:
    """Return the transform steps reducing *column* (or a Series, without one) with *how*."""
    steps: list[dict[str, t.Any]] = [] if column is None else [{"op": "column", "column": column}]
    return [*steps, {"op": "aggregate", "how": how}]


def likely_measure(columns: list[tuple[str, str | None]]) -> str | None:
    """Return the column a number is most likely wanted from: numeric, and not an id or a year."""
    numeric = [name for name, kind in columns if kind in _NUMERIC_COLUMNS]
    measures = [name for name in numeric if not _KEY_LIKE.search(name)]
    return next(iter(measures or numeric or [name for name, _ in columns]), None)


def as_number(value: t.Any) -> float | int | None:
    """Return *value* if it is a finite real number a number input can show, else None."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return None
    return value if math.isfinite(value) else None


def show_number(value: float | int) -> str:
    """Show a number as the KPI shows it by default: thousands separators, two decimals."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


class TransformOffer(Viewer):
    """A dialog connecting a table or series output to a number input through steps."""

    def __init__(self, editor: FlowDash, **params):
        super().__init__(**params)
        self._editor = weakref.ref(editor)
        self._mapping: tuple[str, str, str, str] | None = None
        self._kind: str | None = None
        self._types: dict[str, str | None] = {}
        self._message = pmui.Typography("", variant="body2", margin=(0, 10, 8, 10))
        self._column = pmui.Select(label="Column", options={}, sizing_mode="stretch_width")
        self._column_text = pmui.TextInput(
            label="Column", sizing_mode="stretch_width", visible=False
        )
        self._how = pmui.Select(
            label="Aggregate", options=AGGREGATIONS, value="sum", sizing_mode="stretch_width"
        )
        self._result = pmui.Typography(
            "", variant="caption", margin=(4, 10, 0, 10), css_classes=["fd-offer-result"]
        )
        self._cancel = pmui.Button(label="Cancel", variant="text")
        self._confirm = pmui.Button(label="Connect", color="primary", disabled=True)
        self._cancel.on_click(lambda _event: self.close())
        self._confirm.on_click(lambda _event: self.confirm())
        self._column.param.watch(lambda _event: self._column_changed(), "value")
        for widget in (self._column_text, self._how):
            widget.param.watch(lambda _event: self._preview(), "value")
        self.dialog = pmui.Dialog(
            objects=[
                self._message,
                self._column,
                self._column_text,
                self._how,
                self._result,
                pn.Row(
                    pn.layout.HSpacer(),
                    self._cancel,
                    self._confirm,
                    sizing_mode="stretch_width",
                    margin=(12, 0, 0, 0),
                ),
            ],
            title="Connect with a transform",
            open=False,
            min_width=420,
            css_classes=["fd-transform-offer"],
        )
        self.dialog.param.watch(self._closed, "open")

    @property
    def mapping(self) -> tuple[str, str, str, str] | None:
        """The connection offered, while the dialog is open."""
        return self._mapping

    def offer(self, mapping: tuple[str, str, str, str], kind: str):
        """Open the dialog for connecting *mapping* through steps of *kind*, see :func:`bridge_kind`."""
        editor = self._editor()
        if editor is None:
            return
        self._mapping, self._kind = mapping, kind
        source_id, source_port, target_id, target_port = mapping
        value = getattr(editor.graph.get_state(source_id), source_port, None)
        source = self._describe(editor, source_id, source_port, output=True)
        target = self._describe(editor, target_id, target_port, output=False)
        source = source[:1].upper() + source[1:]
        if kind == "column":
            self._message.object = (
                f"{source} is a table and {target} takes a number. Pick a column "
                "and how to reduce it to one value."
            )
            columns = columns_of(value) or []
            self._types = dict(columns)
            numeric = [name for name, kind_ in columns if kind_ in _NUMERIC_COLUMNS]
            ordered = numeric + [name for name, _ in columns if name not in numeric]
            options = {
                f"{name} ({self._types[name]})" if self._types.get(name) else name: name
                for name in ordered
            }
            self._column.param.update(options=options, value=likely_measure(columns))
            self._column.visible = bool(options)
            self._column_text.param.update(visible=not options, value="")
        else:
            self._message.object = (
                f"{source} is a series and {target} takes a number. Pick how to reduce "
                "it to one value."
            )
            self._types = {}
            self._column.visible = self._column_text.visible = False
        self._column_changed()
        self.dialog.open = True

    @staticmethod
    def _describe(editor: FlowDash, node_id: str, port_name: str, *, output: bool) -> str:
        port = editor._port_spec(node_id, port_name, output=output)
        return f"{port_label(port) if port else port_name} of {editor._instance_title(node_id)}"

    def steps(self) -> list[dict[str, t.Any]]:
        """Return the transform steps the dialog's choices make."""
        if self._kind != "column":
            return bridge_steps(None, self._how.value)
        column = self._column.value if self._column.visible else self._column_text.value
        return bridge_steps(column or "", self._how.value)

    def _column_changed(self):
        """Offer the aggregations that give a number for the chosen column."""
        kind = self._types.get(self._column.value) if self._column.visible else None
        options = AGGREGATIONS if kind in (None, *_NUMERIC_COLUMNS) else _ANY_COLUMN
        if self._how.options != options:
            how = self._how.value if self._how.value in options.values() else None
            self._how.param.update(options=options, value=how or next(iter(options.values())))
        self._preview()

    def _refuse(self, reason: str):
        self._result.param.update(object=reason, styles=_ERROR_STYLE)
        self._confirm.disabled = True

    def _preview(self):
        """Show the number the input would get, and allow connecting only if there is one."""
        editor = self._editor()
        if editor is None or self._mapping is None:
            return
        if self._kind == "column" and not self.steps()[0]["column"]:
            self._refuse("Pick a column.")
            return
        reason = editor._validate_connection(*self._mapping, transform=self.steps())
        if reason is not None:
            self._refuse(reason)
            return
        source_id, source_port = self._mapping[:2]
        value = getattr(editor.graph.get_state(source_id), source_port, None)
        if value is None:
            self._refuse("The table has no value yet, so there is no number to show.")
            return
        try:
            result = apply_transform(parse_transform(self.steps()), value)
        except Exception as exc:
            self._refuse(f"This gives no number: {exc}")
            return
        number = as_number(result)
        if number is None:
            shown = "nothing" if result is None else repr(result)
            self._refuse(f"This gives {shown}, not a number the input can show.")
            return
        self._result.param.update(object=f"Gives {show_number(number)}", styles={})
        self._confirm.disabled = False

    def confirm(self) -> bool | str:
        """Create the connection with the chosen steps and close, or show why it failed."""
        editor = self._editor()
        if editor is None or self._mapping is None or self._confirm.disabled:
            return False
        result = editor.connect(*self._mapping, transform=self.steps())
        if result is not True:
            self._refuse(result)
            return result
        self.close()
        return True

    def close(self):
        """Close the dialog without connecting."""
        self.dialog.open = False
        self._mapping = self._kind = None

    def _closed(self, event):
        # Closed by Escape or a click outside it too.
        if not event.new:
            self._mapping = self._kind = None

    def __panel__(self):
        """Render the dialog."""
        return self.dialog
