"""Editor for the transform applied along an edge of the wiring canvas.

It is shown in the inspector while the edge is selected. Steps are edited as a
draft: a draft whose steps all apply is applied to the edge straight away,
leaving out steps that still need an argument chosen, while a draft with a
failing step, or one the target refuses, is kept and flagged until it is fixed
or reverted, with the edge staying on the transform applied last.
"""

from __future__ import annotations

import difflib
import typing as t
from dataclasses import dataclass
from numbers import Number

import pandas as pd
import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.transforms import (
    TRANSFORMS,
    Filter,
    StepInputError,
    Transform,
    parse_transform,
)
from panel_flowdash.ui.node_card import port_label
from panel_flowdash.ui.text import escape_markdown, text_typography
from panel_flowdash.util import is_failure
from panel_flowdash.value_text import error_text

if t.TYPE_CHECKING:
    from panel_flowdash.editor import FlowDash

_ICONS = {
    "column": "view_column",
    "filter": "filter_alt",
    "sort": "sort",
    "head": "vertical_align_top",
    "aggregate": "functions",
    "unique": "category",
    "round": "exposure",
    "format": "text_fields",
}

_DESCRIPTIONS = {
    "column": "Pick one column as a Series",
    "filter": "Keep rows that match a condition",
    "sort": "Order rows or values",
    "head": "Keep the first rows or items",
    "aggregate": "Reduce to a single number",
    "unique": "List the distinct values",
    "round": "Round to a number of digits",
    "format": "Turn a value into text",
}

# What each built-in step needs, for explaining why it cannot follow a value.
_NEEDS = {
    "column": "a table",
    "filter": "a table",
    "sort": "a table, Series or list",
    "head": "a table, Series or list",
    "aggregate": "a Series or list",
    "unique": "a Series or list",
    "round": "a number or Series",
}

_SYMBOLS = {">=": "≥", "<=": "≤", "!=": "≠"}

_CHOOSE = "Choose a column"

_BADGE_CSS = """
.fd-step-badge { display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 50%; font-size: 12px; font-weight: 600;
  background: var(--mui-palette-action-selected, rgba(127, 127, 127, 0.2));
  color: var(--mui-palette-text-primary, inherit); }
.fd-step-badge.fd-error { background: var(--mui-palette-error-main, #d32f2f);
  color: var(--mui-palette-error-contrastText, #fff); }
"""

_SHAPE_SX = {
    "color": "text.secondary",
    "borderLeft": 2,
    "borderColor": "divider",
    "pl": 1.5,
    "ml": 1.5,
    "py": 0.5,
    "display": "block",
}


def describe(value: t.Any) -> str:
    """Summarize a value's type and size in a short line."""
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


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def kind(value: t.Any) -> str:
    """Name the kind of a value in plain words, e.g. 'a table' or 'a number'."""
    if isinstance(value, pd.DataFrame):
        return "a table"
    if isinstance(value, pd.Series):
        return "a Series"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, Number):
        return "a number"
    if isinstance(value, str):
        return "text"
    return f"a {type(value).__name__}"


def shape(value: t.Any, previous: t.Any = None) -> str:
    """Describe the shape of a value between steps, e.g. '17,288 rows, 29 columns'.

    The columns of a table are left out when *previous*, the value before the
    step, was a table with as many columns.
    """
    if value is None:
        return "no value yet"
    if is_failure(value):
        return "a failure from upstream"
    if isinstance(value, pd.DataFrame):
        rows, cols = value.shape
        if isinstance(previous, pd.DataFrame) and previous.shape[1] == cols:
            return _plural(rows, "row")
        return f"{_plural(rows, 'row')}, {_plural(cols, 'column')}"
    if isinstance(value, pd.Series):
        return f"Series of {_plural(len(value), 'value')}"
    if isinstance(value, (list, tuple)):
        return f"list of {_plural(len(value), 'item')}"
    if isinstance(value, bool):
        return f"boolean {value}"
    if isinstance(value, Number):
        try:
            text = f"{value:,.6g}" if isinstance(value, float) else f"{value:,}"
        except (TypeError, ValueError):
            text = str(value)
        return f"number {text}"
    if isinstance(value, str):
        text = value if len(value) <= 32 else value[:31] + "…"
        return f'text "{text}"'
    return type(value).__name__


def needs(cls: type[Transform], value: t.Any) -> str | None:
    """Say why a step cannot follow *value*, or return None when it can."""
    if value is None or is_failure(value) or cls.accepts(value):
        return None
    need = _NEEDS.get(cls.op)
    reason = f"Needs {need}" if need else f"Cannot follow {kind(value)}"
    if isinstance(value, pd.DataFrame) and "column" in TRANSFORMS and len(value.columns):
        if cls.accepts(value.iloc[:, 0]):
            reason += f"; add {TRANSFORMS['column'].label} first"
    return reason


def step_detail(step: Transform) -> str:
    """Describe a step's arguments in a few words, for its collapsed card."""
    op = step.op
    if op == "column":
        return step.column
    if op == "filter":
        if not step.column or step.value is None:
            return ""
        value = step.value
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        return f"{step.column} {_SYMBOLS.get(step.comparison, step.comparison)} {value}"
    if op == "sort":
        if step.column:
            order = "smallest first" if step.ascending else "largest first"
            return f"by {step.column}, {order}"
        return "ascending" if step.ascending else "descending"
    if op == "head":
        return f"{step.n:,}"
    if op == "aggregate":
        return step.how
    if op == "unique":
        return ""
    if op == "round":
        return _plural(step.digits, "digit")
    if op == "format":
        return " ".join(part for part in (step.prefix, step.spec, step.suffix) if part)
    summary = step.summary()
    return "" if summary == step.label.lower() else summary


def missing_argument(step: Transform, value: t.Any) -> str | None:
    """Say what a step still needs before it is applied, or return None when it is complete.

    A filter needs a column and a value, a column step a column, and sorting a
    table a column to sort by. Incomplete steps are left out of the applied
    transform, so a step that was just added changes nothing yet.
    """
    if isinstance(step, Filter):
        if not step.column:
            return "Choose a column and a value to apply this step."
        if step.value is None:
            return "Choose a value to apply this step."
        return None
    if "column" in step.arguments() and not step.column:
        if step.op == "column":
            return "Choose a column to apply this step."
        if isinstance(value, pd.DataFrame):
            return "Choose a column to apply this step."
    return None


@dataclass
class StepCheck:
    """What happens to the value at one step of a draft."""

    before: t.Any
    after: t.Any = None
    error: str | None = None
    suggestion: str | None = None
    missing: str | None = None
    blocked_by: int | None = None


def _column_error(step: Transform, value: t.Any) -> tuple[str, str | None] | None:
    if "column" not in step.arguments() or not isinstance(value, pd.DataFrame):
        return None
    column = step.column
    names = [str(c) for c in value.columns]
    if not column or column in names:
        return None
    closest = difflib.get_close_matches(column, names, n=1, cutoff=0.5)
    return f"No column named {column}.", (closest[0] if closest else None)


def check_steps(steps: list[Transform], value: t.Any) -> list[StepCheck]:
    """Run *value* through *steps*, recording each step's outcome instead of raising.

    Incomplete steps pass the value on unchanged, as they are not applied, and
    the steps after a failing one are blocked by it.
    """
    checks: list[StepCheck] = []
    failed: int | None = None
    for index, step in enumerate(steps):
        if failed is not None:
            checks.append(StepCheck(value, blocked_by=failed))
            continue
        missing = missing_argument(step, value)
        if missing:
            checks.append(StepCheck(value, value, missing=missing))
            continue
        error = _column_error(step, value)
        if error is None:
            try:
                after = step(value)
            except StepInputError:
                need = _NEEDS.get(step.op, "another kind of value")
                error = (f"{step.label} needs {need}, not {kind(value)}.", None)
            except Exception as exc:
                error = (f"This step fails: {error_text(exc)}", None)
        if error is not None:
            failed = index
            checks.append(StepCheck(value, error=error[0], suggestion=error[1]))
            continue
        checks.append(StepCheck(value, after))
        value = after
    return checks


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


def _caption(text: str, **kwargs) -> pmui.Typography:
    kwargs.setdefault("sx", {"color": "text.secondary"})
    return text_typography(text, variant="caption", **kwargs)


class TransformEditor(Viewer):
    """Edit the transform steps of one edge, showing the shape of the value between steps.

    Checking the steps runs the whole transform on the source value, so a change
    of that value is only rendered while the editor is shown (see `show`).
    Hidden, it catches up when shown again, but still re-checks the `issue`
    the canvas and status bar report while it holds a draft or an issue.
    """

    issue = param.String(
        default=None,
        allow_None=True,
        doc="What keeps the edited steps from being applied, e.g. 'Sort step', or None.",
    )

    message = param.String(default=None, allow_None=True, doc="Why, in a sentence.")

    def __init__(self, owner: FlowDash, edge_id: str, **params):
        super().__init__(**params)
        self._owner = owner
        self._edge_id = edge_id
        self._steps: list[Transform] = []
        # Steps shown expanded, by identity, so expansion follows a moved step.
        self._expanded: set[int] = set()
        self._rejection: str | None = None
        self._adding = False
        self._loaded = False
        # The transform the draft started from, so a change made elsewhere is
        # told apart from an edit made here.
        self._base: list[dict[str, t.Any]] = []
        self._watched: tuple[param.Parameterized, t.Any] | None = None
        self._shown = False
        self._stale = False
        self._body = pn.Column(sizing_mode="stretch_width", margin=0)

    @property
    def _key(self) -> tuple[str, str, str, str] | None:
        return self._owner._edge_id_map.get(self._edge_id)

    def __panel__(self):
        """Return the editor view, loading the edge's transform when first shown."""
        if not self._loaded:
            self.reset()
        return self._body

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _applied(self) -> list[dict[str, t.Any]]:
        key = self._key
        return (self._owner.graph.get_edge_transform(*key) if key else None) or []

    @property
    def has_draft(self) -> bool:
        """Whether the steps shown were edited away from the transform they started from."""
        return self._loaded and [step.to_dict() for step in self._steps] != self._base

    def sync(self):
        """Reload the applied transform if it changed, unless a draft is being edited."""
        if not self._loaded or (not self.has_draft and self._applied() != self._base):
            self.reset()

    def reset(self):
        """Show the transform applied to the edge, dropping any draft."""
        self._loaded = True
        self._base = self._applied()
        self._steps = parse_transform(self._base)
        self._expanded = set()
        self._rejection = None
        self._adding = False
        self._watch_source(self._key)
        self._render()

    def show(self):
        """Follow the source value, rendering a change missed while hidden."""
        self._shown = True
        if self._loaded and self._stale:
            self._render()

    def hide(self):
        """Stop rendering source changes until shown again."""
        self._shown = False

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
            self._watched = (state, state.param.watch(self._on_source_changed, key[1]))

    def _on_source_changed(self, _event):
        if self._shown:
            self._render()
            return
        self._stale = True
        # Without a draft the steps are the edge's own, whose failures the
        # graph reports, so only a draft or an issue needs checking here.
        if self.has_draft or self.issue:
            self._update_issue(check_steps(self._steps, self._source_value()))

    def _update_issue(self, checks: list[StepCheck]) -> int | None:
        """Set `issue` and `message` from *checks*, returning the index of the failed step."""
        failed = next((i for i, check in enumerate(checks) if check.error), None)
        if failed is not None:
            self.param.update(
                issue=f"{self._steps[failed].label} step", message=checks[failed].error
            )
        elif self._rejection:
            self.param.update(issue="Transform", message=self._rejection)
        else:
            self.param.update(issue=None, message=None)
        return failed

    def _source_value(self):
        key = self._key
        expr = self._owner.graph.get_edge_expression(*key, stage=0) if key else None
        return None if expr is None else expr.rx.value

    def _target_title(self) -> str:
        key = self._key
        if key is None:
            return "The target"
        component = self._owner._component_id_for_instance(key[2])
        spec = self._owner._component_specs.get(component or "")
        return spec.title if spec else key[2]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @pn.io.hold()
    def _render(self):
        self._stale = False
        if self._key is None:
            self._body[:] = []
            return
        value = self._source_value()
        checks = check_steps(self._steps, value)
        # A step that fails or still needs an argument opens, to show what to fix.
        self._expanded.update(
            id(step)
            for step, check in zip(self._steps, checks, strict=True)
            if check.error or check.missing
        )
        failed = self._update_issue(checks)

        rows: list[t.Any] = [self._shape_line(f"In: {shape(value)}")]
        for index, (step, check) in enumerate(zip(self._steps, checks, strict=True)):
            rows.append(self._step_card(index, step, check))
            if check.blocked_by is not None:
                rows.append(self._shape_line(f"Out: once step {check.blocked_by + 1} is fixed"))
            elif check.missing:
                rows.append(self._shape_line("Out: unchanged until this step is complete"))
            elif check.error is None:
                rows.append(self._shape_line(f"Out: {shape(check.after, check.before)}"))
        if not self._steps:
            rows.append(
                _caption(
                    f"Add a step to reshape the value before it reaches {self._target_title()}.",
                    margin=(4, 0, 8, 0),
                )
            )
        if self.issue:
            rows.append(self._consequence(rejected=failed is None))
        last = checks[-1].after if checks else value
        rows += self._add_section(last, blocked=failed is not None)
        self._body[:] = rows

    @staticmethod
    def _shape_line(text: str) -> pmui.Typography:
        return text_typography(text, variant="caption", sx=_SHAPE_SX, margin=(0, 0, 0, 0))

    def _consequence(self, rejected: bool) -> pmui.Paper:
        revert = pmui.Button(
            label="Revert to the applied steps",
            variant="text",
            size="small",
            color="error",
            margin=(4, 0, 0, 0),
            sx={"textTransform": "none", "px": 0},
        )
        revert.on_click(lambda _event: self.reset())
        lines = []
        if rejected and self._rejection:
            lines.append(text_typography(self._rejection, variant="body2", margin=0))
        lines.append(
            _caption(
                f"Not applied yet. {self._target_title()} keeps its last value until this "
                "is fixed.",
                margin=(2, 0, 0, 0),
                sx={"color": "error.main"},
            )
        )
        return pmui.Paper(
            *lines,
            revert,
            variant="outlined",
            sizing_mode="stretch_width",
            margin=(8, 0, 0, 0),
            sx={"p": 1, "borderColor": "error.main"},
        )

    def _badge(self, index: int, error: bool) -> pn.pane.HTML:
        css = "fd-step-badge fd-error" if error else "fd-step-badge"
        return pn.pane.HTML(
            f'<div class="{css}">{index + 1}</div>',
            width=22,
            height=22,
            margin=(5, 8, 0, 0),
            stylesheets=[_BADGE_CSS],
        )

    def _step_card(self, index: int, step: Transform, check: StepCheck) -> pmui.Paper:
        expanded = id(step) in self._expanded
        detail = step_detail(step)
        title = pmui.Typography(
            f"**{escape_markdown(step.label)}** {escape_markdown(detail)}".rstrip(),
            disable_math=True,
            variant="body2",
            margin=(6, 4, 0, 0),
            sizing_mode="stretch_width",
            sx={"overflowWrap": "anywhere"},
        )

        def button(icon, description, action, disabled=False):
            widget = pmui.IconButton(
                icon=icon, description=description, size="small", margin=0, disabled=disabled
            )
            widget.on_click(lambda _event: action())
            return widget

        last = len(self._steps) - 1
        header = pn.Row(
            self._badge(index, check.error is not None),
            title,
            button("arrow_upward", "Move step up", lambda: self._move(index, -1), index == 0),
            button(
                "arrow_downward", "Move step down", lambda: self._move(index, 1), index == last
            ),
            button(
                "expand_less" if expanded else "expand_more",
                "Collapse step" if expanded else "Expand step",
                lambda: self._toggle(index),
            ),
            button("close", "Remove step", lambda: self._remove(index)),
            sizing_mode="stretch_width",
            margin=0,
        )
        content: list[t.Any] = [header]
        if expanded:
            content += [
                widget
                for name in step.arguments()
                if (widget := self._argument_widget(index, step, name, check.before)) is not None
            ]
        if check.error:
            content.append(
                text_typography(
                    check.error
                    + (f" Did you mean {check.suggestion}?" if check.suggestion else ""),
                    variant="body2",
                    margin=(4, 0, 0, 0),
                    sx={"color": "error.main"},
                )
            )
            if check.suggestion:
                use = pmui.Button(
                    label=f"Use {check.suggestion}",
                    variant="outlined",
                    size="small",
                    color="error",
                    margin=(6, 0, 2, 0),
                    sx={"textTransform": "none"},
                )
                use.on_click(lambda _event: self._set_argument(index, "column", check.suggestion))
                content.append(use)
        elif check.missing:
            content.append(_caption(check.missing, margin=(4, 0, 0, 0)))
        return pmui.Paper(
            *content,
            variant="outlined",
            sizing_mode="stretch_width",
            margin=(2, 0),
            sx={
                "px": 1,
                "py": 0.5,
                "borderColor": "error.main" if check.error else "divider",
                "borderWidth": 2 if check.error else 1,
            },
        )

    def _argument_widget(self, index: int, step: Transform, name: str, value):
        parameter = step.param[name]
        current = getattr(step, name)
        label = parameter.label or name
        common = {
            "label": label,
            "size": "small",
            "sizing_mode": "stretch_width",
            "margin": (4, 0),
        }
        if name == "column":
            if not isinstance(value, pd.DataFrame):
                return None
            names = [str(column) for column in value.columns]
            if current and current not in names:
                names.append(current)
            # An explicit empty choice, as a select would otherwise show the
            # first column although none is chosen yet.
            empty = None if parameter.allow_None else ""
            options = {_CHOOSE: empty, **{name: name for name in names}}
            widget = pmui.Select(options=options, value=current or empty, **common)
        elif isinstance(step, Filter) and name == "value":
            widget = pmui.TextInput(
                value=_value_text(current),
                placeholder="comma separated" if step.comparison == "in" else "",
                **common,
            )
        elif isinstance(parameter, param.Selector):
            widget = pmui.Select(options=list(parameter.objects), value=current, **common)
        elif isinstance(parameter, param.Boolean):
            widget = pmui.Switch(value=current, label=label, margin=(4, 0))
        elif isinstance(parameter, param.Integer):
            start = (parameter.bounds or (None, None))[0]
            widget = pmui.IntInput(value=current, start=start, **common)
        else:
            widget = pmui.TextInput(value=_value_text(current), **common)
        widget.param.watch(lambda event: self._set_argument(index, name, event.new), "value")
        return widget

    def _add_section(self, value, blocked: bool) -> list[t.Any]:
        """Build the Add step button and, while it is open, every step with what it needs."""
        add = pmui.Button(
            label="Add step",
            icon="add",
            variant="outlined",
            size="small",
            disabled=blocked,
            margin=(12, 0, 0, 0),
            sx={"textTransform": "none"},
        )
        add.on_click(lambda _event: self._toggle_adding())
        objects: list[t.Any] = [add]
        if blocked:
            objects.append(_caption("Fix the flagged step to add more.", margin=(4, 0, 0, 0)))
        elif self._adding:
            objects.append(self._operations(value))
        return objects

    def _operations(self, value) -> pmui.Paper:
        items = []
        for cls in TRANSFORMS.values():
            reason = needs(cls, value)
            doc = (cls.__doc__ or "").strip().splitlines()
            description = _DESCRIPTIONS.get(cls.op) or (doc[0].rstrip(".") if doc else "")
            option = pmui.Button(
                label=cls.label or cls.op,
                icon=_ICONS.get(cls.op, "tune"),
                variant="text",
                size="small",
                disabled=reason is not None,
                sizing_mode="stretch_width",
                margin=(4, 0, 0, 0),
                sx={"justifyContent": "flex-start", "textTransform": "none"},
            )
            option.on_click(lambda _event, op=cls.op: self._add(op))
            items += [
                option,
                _caption(
                    reason or description,
                    margin=(0, 0, 4, 34),
                    sx={"color": "text.disabled" if reason else "text.secondary"},
                ),
            ]
        return pmui.Paper(
            *items,
            elevation=3,
            sizing_mode="stretch_width",
            margin=(4, 0, 0, 0),
            sx={"py": 0.5, "px": 1},
        )

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    def _toggle(self, index: int):
        key = id(self._steps[index])
        if key in self._expanded:
            self._expanded.discard(key)
        else:
            self._expanded.add(key)
        self._render()

    def _toggle_adding(self):
        self._adding = not self._adding
        self._render()

    def _add(self, op: str):
        step = parse_transform([{"op": op}])[0]
        self._steps.append(step)
        self._expanded.add(id(step))
        self._adding = False
        self._commit()

    def _remove(self, index: int):
        del self._steps[index]
        self._commit()

    def _move(self, index: int, offset: int):
        target = index + offset
        if 0 <= target < len(self._steps):
            steps = self._steps
            steps[index], steps[target] = steps[target], steps[index]
            self._commit()

    def _set_argument(self, index: int, name: str, new):
        step = self._steps[index]
        value = check_steps(self._steps, self._source_value())[index].before
        series = None
        if isinstance(step, Filter) and isinstance(value, pd.DataFrame) and step.column in value:
            series = value[step.column]
        try:
            if isinstance(step, Filter) and name == "value":
                new = _filter_value(new, step.comparison, series)
            elif isinstance(step, Filter) and name == "comparison":
                step.value = _filter_value(_value_text(step.value), new, series)
            elif isinstance(step, Filter) and name == "column" and isinstance(value, pd.DataFrame):
                if new in value and step.value is not None:
                    step.value = _filter_value(
                        _value_text(step.value), step.comparison, value[new]
                    )
            setattr(step, name, new)
        except Exception as exc:
            self._rejection = f"{step.label}: {error_text(exc)}"
            self._render()
            return
        self._commit()

    def _commit(self):
        """Apply the complete steps of the draft, unless one of them fails."""
        key = self._key
        if key is None:
            return
        self._rejection = None
        checks = check_steps(self._steps, self._source_value())
        if not any(check.error for check in checks):
            steps = [
                s.to_dict() for s, c in zip(self._steps, checks, strict=True) if not c.missing
            ]
            if steps != self._applied():
                result = self._owner.set_edge_transform(*key, steps)
                if result is not True:
                    self._rejection = self._explain(checks)
            if self._rejection is None and len(steps) == len(self._steps):
                self._base = steps
        self._render()

    def _explain(self, checks: list[StepCheck]) -> str:
        key = self._key
        port = self._owner._port_spec(key[2], key[3], output=False) if key else None
        label = port_label(port) if port else (key[3] if key else "The input")
        value = checks[-1].after if checks else self._source_value()
        return f"{label} does not accept {kind(value)}."
