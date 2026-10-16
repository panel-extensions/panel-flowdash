"""Editor for the transform applied along an edge of the wiring canvas."""

from __future__ import annotations

import typing as t
from numbers import Number

import pandas as pd
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.transforms import (
    Filter,
    StepInputError,
    Transform,
    apply_transform,
    available_transforms,
    parse_transform,
)

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

_WIDTH = 340


def describe(value: t.Any) -> str:
    """Summarize a value's type and size in a short line, for step previews."""
    if value is None:
        return "unset"
    if isinstance(value, pd.DataFrame):
        rows, cols = value.shape
        return f"DataFrame · {rows:,} rows × {cols} cols"  # noqa: RUF001
    if isinstance(value, pd.Series):
        return f"Series · {len(value):,} values"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} · {len(value):,} items"
    if isinstance(value, bool):
        return f"bool · {value}"
    if isinstance(value, Number):
        try:
            text = f"{value:,.6g}" if isinstance(value, float) else f"{value:,}"
        except (TypeError, ValueError):
            text = str(value)
        return f"{type(value).__name__} · {text}"
    if isinstance(value, str):
        text = value if len(value) <= 32 else value[:31] + "…"
        return f'str · "{text}"'
    return type(value).__name__


def _value_text(value: t.Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _coerce(text: str, series: pd.Series | None) -> t.Any:
    """Read a typed value as a number when it filters a numeric column."""
    if series is None or not pd.api.types.is_numeric_dtype(series):
        return text
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def _filter_value(text: str, comparison: str, series: pd.Series | None) -> t.Any:
    if comparison == "in":
        return [_coerce(item.strip(), series) for item in text.split(",") if item.strip()] or None
    return _coerce(text, series) if text.strip() else None


class TransformEditor(Viewer):
    """Edit the transform steps of one edge, previewing the value after each step.

    Every change is applied to the edge straight away. A transform the target
    rejects stays in the editor with the reason shown, while the edge keeps its
    previous transform; selecting the edge again discards it.
    """

    def __init__(self, owner: FlowDash, edge_id: str, **params):
        super().__init__(**params)
        self._owner = owner
        self._edge_id = edge_id
        self._steps: list[Transform] = []
        self._status: str | None = None
        self._watched: tuple[param.Parameterized, t.Any] | None = None
        self._body = pmui.Column(sizing_mode="stretch_width", margin=0)
        self._view = pmui.Paper(
            self._body,
            elevation=3,
            width=_WIDTH,
            # No scroll container: dropdowns render inside the panel, so one
            # would clip their option lists.
            sx={"p": 1.5},
        )

    @property
    def _key(self) -> tuple[str, str, str, str] | None:
        return self._owner._edge_id_map.get(self._edge_id)

    def __panel__(self):
        """Return the editor view, loading the edge's transform when first shown."""
        # ReactFlow asks for the view whenever its nodes, edges or selection
        # change, so rebuilding here would replace widgets mid-interaction.
        if self._watched is None:
            self.reset()
        return self._view

    def reset(self):
        """Reload the edge's applied transform, dropping any rejected draft."""
        key = self._key
        transform = self._owner.graph.get_edge_transform(*key) if key else None
        showing = [step.to_dict() for step in self._steps]
        if self._watched is not None and self._status is None and showing == (transform or []):
            # Already showing the applied transform; rebuilding would only swap
            # out widgets the user may be in the middle of using.
            return
        self._steps = parse_transform(transform)
        self._status = None
        self._watch_source(key)
        self._render()

    def dispose(self):
        """Stop following the source port, once the edge is gone."""
        if self._watched is not None:
            state, watcher = self._watched
            state.param.unwatch(watcher)
            self._watched = None

    def _watch_source(self, key):
        self.dispose()
        state = self._owner.graph.get_state(key[0]) if key else None
        if state is not None:
            self._watched = (state, state.param.watch(lambda _: self._render(), key[1]))

    def _source_value(self):
        key = self._key
        expr = self._owner.graph.get_edge_expression(*key, stage=0) if key else None
        return None if expr is None else expr.rx.value

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self):
        key = self._key
        if key is None:
            self._body.objects = []
            return
        _, source_port, _, target_port = key
        rows: list[t.Any] = [
            pmui.Typography("Transform", variant="subtitle1", margin=0),
            pmui.Typography(
                f"{source_port} → {target_port}",
                variant="caption",
                sx={"color": "text.secondary"},
                margin=(0, 0, 8, 0),
            ),
        ]
        value = self._source_value()
        rows.append(self._preview(value))
        failed = False
        for index, step in enumerate(self._steps):
            rows.append(self._step_card(index, step, value, failed))
            if failed:
                continue
            try:
                value = step(value)
            except Exception as exc:
                failed = True
                message = str(exc) if isinstance(exc, StepInputError) else f"Fails here: {exc}"
                rows.append(self._preview(None, error=message))
                continue
            rows.append(self._preview(value))

        if not failed:
            rows.append(self._add_menu(value))
        if not self._steps:
            rows.append(
                pmui.Typography(
                    f"Add a step to reshape the value before it reaches {target_port}.",
                    variant="body2",
                    sx={"color": "text.secondary"},
                    margin=(8, 0, 0, 0),
                )
            )
        if self._status:
            rows.append(
                pmui.Alert(
                    object=f"Not applied: {self._status} The edge keeps its previous transform.",
                    severity="warning",
                    margin=(8, 0, 0, 0),
                    sizing_mode="stretch_width",
                )
            )
        elif self._steps:
            rows.append(
                pmui.Typography(
                    f"✓ Applied to {target_port}",
                    variant="caption",
                    color="success",
                    margin=(8, 0, 0, 0),
                )
            )
        self._body.objects = rows

    @staticmethod
    def _preview(value, error: str | None = None):
        return pmui.Typography(
            error or describe(value),
            variant="caption",
            sx={"fontFamily": "monospace", "color": "error.main" if error else "text.secondary"},
            margin=(2, 0, 2, 4),
        )

    def _step_card(self, index: int, step: Transform, value, disabled: bool):
        remove = pmui.IconButton(icon="close", size="small", description="Remove step", margin=0)
        remove.on_click(lambda _: self._remove(index))
        title = pmui.Typography(
            f"{index + 1}. {step.label}",
            variant="body2",
            sx={"fontWeight": 600},
            margin=(0, 8, 0, 0),
        )
        widgets = [
            widget
            for name in step.arguments()
            if (widget := self._argument_widget(index, step, name, value, disabled)) is not None
        ]
        # Steps with one argument fit on a single line, which keeps long
        # transforms visible without scrolling the panel.
        if len(widgets) <= 1:
            content = [
                pmui.Row(
                    title, *widgets, remove, sizing_mode="stretch_width", align="center", margin=0
                )
            ]
        else:
            spacer = pmui.Row(sizing_mode="stretch_width", margin=0)
            content = [
                pmui.Row(title, spacer, remove, sizing_mode="stretch_width", margin=0),
                *widgets,
            ]
        return pmui.Paper(
            *content,
            variant="outlined",
            sizing_mode="stretch_width",
            sx={"px": 1, "py": 0.5},
            margin=(2, 0),
        )

    def _argument_widget(self, index: int, step: Transform, name: str, value, disabled: bool):
        parameter = step.param[name]
        current = getattr(step, name)
        label = parameter.label or name
        common = {
            "label": label,
            "size": "small",
            "sizing_mode": "stretch_width",
            "disabled": disabled,
            "margin": (4, 0),
        }
        if name == "column":
            if not isinstance(value, pd.DataFrame):
                return None
            options = [str(column) for column in value.columns]
            if current and current not in options:
                options.append(current)
            widget = pmui.Select(options=options, value=current or None, **common)
        elif isinstance(step, Filter) and name == "value":
            widget = pmui.TextInput(
                value=_value_text(current),
                placeholder="comma separated" if step.comparison == "in" else "",
                **common,
            )
        elif isinstance(parameter, param.Selector):
            widget = pmui.Select(options=list(parameter.objects), value=current, **common)
        elif isinstance(parameter, param.Boolean):
            widget = pmui.Switch(value=current, label=label, disabled=disabled, margin=(4, 0))
        elif isinstance(parameter, param.Integer):
            start = (parameter.bounds or (None, None))[0]
            widget = pmui.IntInput(value=current, start=start, **common)
        else:
            widget = pmui.TextInput(value=_value_text(current), **common)
        widget.param.watch(
            lambda event: self._set_argument(index, name, event.new, value), "value"
        )
        return widget

    def _add_menu(self, value):
        operations = available_transforms(value) if value is not None else []
        menu = pmui.MenuButton(
            label="Add step",
            icon="add",
            items=[{"label": cls.label, "op": cls.op} for cls in operations],
            variant="text",
            size="small",
            disabled=not operations,
            margin=(4, 0),
        )
        menu.on_click(lambda item: self._add(item["op"], value))
        return menu

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    def _add(self, op: str, value):
        step = parse_transform([{"op": op}])[0]
        if "column" in step.arguments() and isinstance(value, pd.DataFrame) and len(value.columns):
            step.column = str(value.columns[0])
        self._steps.append(step)
        self._commit()

    def _remove(self, index: int):
        del self._steps[index]
        self._commit()

    def _set_argument(self, index: int, name: str, new, value):
        step = self._steps[index]
        series = None
        if isinstance(step, Filter) and isinstance(value, pd.DataFrame) and step.column in value:
            series = value[step.column]
        try:
            if isinstance(step, Filter) and name == "value":
                new = _filter_value(new, step.comparison, series)
            elif isinstance(step, Filter) and name == "comparison":
                step.value = _filter_value(_value_text(step.value), new, series)
            setattr(step, name, new)
        except Exception as exc:
            self._status = f"{step.label}: {exc}"
            self._render()
            return
        self._commit()

    def _commit(self):
        key = self._key
        if key is None:
            return
        result = self._owner.set_edge_transform(*key, [step.to_dict() for step in self._steps])
        self._status = None if result is True else self._explain(key[3])
        self._render()

    def _explain(self, target_port: str) -> str:
        """Phrase a rejection in terms of the value the transform produces, when it runs."""
        try:
            value = apply_transform(self._steps, self._source_value())
        except Exception:
            return "the step marked in red fails."
        return f"{target_port} does not accept {describe(value)}."
