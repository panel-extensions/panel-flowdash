"""The template step of the "Create dashboard" dialog: pick Blank or a template, then bind its slots."""

from __future__ import annotations

import logging
import typing as t

import panel as pn
import panel_material_ui as pmui
import param
from panel.viewable import Viewer

from panel_flowdash.templates import (
    DataBinding,
    Template,
    TemplateError,
    check_bindings,
    column_fits,
    data_columns,
    fill_template,
    slot_choices,
    template_model,
)
from panel_flowdash.ui.choice_group import ChoiceGroup
from panel_flowdash.ui.launcher import _CARD_CSS, thumbnail_html

logger = logging.getLogger(__name__)

BLANK = ""

# The option of a slot's select that leaves it unbound.
NOT_CHOSEN = "Not chosen"

_SLOT_ICONS = {"flowdash:slot/data": "storage", "flowdash:slot/view": "insert_chart"}

_PREVIEW_CSS = (
    _CARD_CSS
    + """
.thumb { border-radius: 8px; border: 1px solid var(--mui-palette-divider); height: 168px; }
.caption { margin-top: 8px; font-size: 13px; line-height: 18px; color: var(--mui-palette-text-secondary); }
"""
)


def preview_html(template: Template | None, icons: t.Mapping[str, str | None]) -> str:
    """Draw *template*'s tiles as the launcher draws a dashboard's, slots by their kind."""
    from html import escape

    if template is None:
        return (
            '<div class="thumb empty">'
            '<span class="mi" aria-hidden="true">space_dashboard</span>'
            "<span>An empty dashboard</span></div>"
            '<div class="caption">Start from an empty canvas and place components yourself.</div>'
        )
    bindings: dict[str, t.Any] = {}
    for slot in template.slots.values():
        if slot.kind in ("data", "view"):
            bindings[slot.name] = DataBinding(f"flowdash:slot/{slot.kind}", {}, "table")
        elif slot.kind == "column":
            bindings[slot.name] = slot.name
    model = template_model(template, bindings, "preview")
    thumb = thumbnail_html(model, {**_SLOT_ICONS, **icons})
    return f'{thumb}<div class="caption">{escape(template.description)}</div>'


class TemplatePicker(Viewer):
    """Choose what a new dashboard starts from and bind the chosen template's slots.

    ``step`` is ``choose`` (the list of Blank and the templates, with a
    preview) or ``bind`` (one field per slot of the chosen template).
    """

    step = param.Selector(default="choose", objects=["choose", "bind"])

    selected = param.String(default=BLANK, doc="The id of the chosen template, '' for Blank.")

    valid = param.Boolean(default=True, doc="Whether every required slot is bound.")

    visible = param.Boolean(default=True, doc="Whether the picker is shown.")

    def __init__(self, editor, templates: t.Sequence[Template] = (), icons=None, **params):
        super().__init__(**params)
        self._editor = editor
        self._templates = {template.id: template for template in templates}
        self._icons = dict(icons or {})
        self._columns: dict[str, dict[str, str] | None] = {}
        self._choices: dict[str, list] = {}
        self._fields: dict[str, t.Any] = {}
        # The data slots whose columns are being read, by the choice they are read for.
        self._loading: dict[str, str] = {}
        self._read_for: dict[str, str] = {}
        self._list = ChoiceGroup(
            items=self._items(),
            label="Start from",
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["flowdash-template-list"],
        )
        self._list.param.watch(self._on_pick, "index")
        self._preview = pn.pane.HTML(
            preview_html(None, self._icons),
            stylesheets=[_PREVIEW_CSS],
            sizing_mode="stretch_width",
            margin=0,
            css_classes=["flowdash-template-preview"],
        )
        self._choose = pn.FlexBox(
            pn.Column(
                self._list, sizing_mode="stretch_width", margin=0, styles={"flex": "1 1 220px"}
            ),
            pn.Column(
                self._preview, sizing_mode="stretch_width", margin=0, styles={"flex": "1 1 220px"}
            ),
            gap="16px",
            sizing_mode="stretch_width",
            margin=0,
        )
        self._intro = pmui.Typography(
            "", variant="body2", sx={"color": "text.secondary"}, margin=(0, 0, 8, 0)
        )
        self._form = pn.Column(
            sizing_mode="stretch_width", margin=0, css_classes=["flowdash-template-slots"]
        )
        self._bind = pn.Column(
            self._intro, self._form, sizing_mode="stretch_width", margin=0, visible=False
        )
        self._layout = pn.Column(
            self._choose,
            self._bind,
            sizing_mode="stretch_width",
            margin=0,
            visible=self.param.visible,
        )

    def __panel__(self):
        """Render the step."""
        return self._layout

    @property
    def template(self) -> Template | None:
        """The chosen template, None for Blank."""
        return self._templates.get(self.selected)

    def _items(self) -> list[dict]:
        items = [{"label": "Blank", "description": "An empty dashboard", "icon": "add"}]
        for template in self._templates.values():
            items.append(
                {"label": template.name, "description": template.description, "icon": "dashboard"}
            )
        return items

    def reset(self, templates: t.Sequence[Template] | None = None):
        """Start over at Blank, with *templates* if given."""
        if templates is not None:
            self._templates = {template.id: template for template in templates}
            self._list.items = self._items()
        # Leave the slot step first, so nothing reads a template while it changes.
        self.param.update(step="choose", valid=True)
        self._list.index = 0
        self.selected = BLANK
        self._loading.clear()
        self._preview.object = preview_html(None, self._icons)
        self._show_step()

    def _on_pick(self, event):
        index = event.new or 0
        ids = list(self._templates)
        self.selected = ids[index - 1] if 0 < index <= len(ids) else BLANK
        self._preview.object = preview_html(self.template, self._icons)

    # ------------------------------------------------------------------
    # Binding
    # ------------------------------------------------------------------

    async def start_binding(self):
        """Show the chosen template's slots, offering what the user may bind them to."""
        template = self.template
        if template is None:
            return
        await self._editor.ensure_components_loaded_async()
        self._columns.clear()
        self._loading.clear()
        self._read_for.clear()
        self._fields = {}
        self._choices = {}
        widgets = []
        for slot in template.slots.values():
            label = slot.label if slot.required else f"{slot.label} (optional)"
            if slot.kind in ("data", "view"):
                self._choices[slot.name] = slot_choices(self._editor, slot)
                options = {c.label: c.key for c in self._choices[slot.name]}
                widget = pmui.Select(
                    label=label,
                    options={NOT_CHOSEN: "", **options},
                    value="",
                    description=slot.description or None,
                    sizing_mode="stretch_width",
                    disabled=not options,
                    helper_text="" if options else "Nothing you may place fits.",
                )
            elif slot.kind == "column":
                widget = pmui.Select(
                    label=label,
                    options={NOT_CHOSEN: ""},
                    value="",
                    description=slot.description or None,
                    sizing_mode="stretch_width",
                    disabled=True,
                    helper_text=f"Choose {template.slots[slot.of].label} first.",
                )
            else:
                widget = pmui.TextInput(
                    label=label, value=slot.default, sizing_mode="stretch_width"
                )
            widget.param.watch(self._on_field, "value")
            self._fields[slot.name] = widget
            widgets.append(widget)
        self._form.objects = widgets
        self._intro.object = (
            f"Choose what {template.name} shows. Tiles whose choices you leave empty "
            "say what they need, so you can fill them in later."
        )
        self.step = "bind"
        self._show_step()
        self._update_valid()

    def back(self):
        """Return to the list of templates."""
        self.step = "choose"
        self._show_step()
        self.valid = True

    def _show_step(self):
        binding = self.step == "bind"
        self._choose.visible = not binding
        self._bind.visible = binding

    async def _on_field(self, event):
        template = self.template
        if template is None:
            return
        name = next((n for n, w in self._fields.items() if w is event.obj), None)
        slot = template.slots.get(name) if name else None
        if slot is not None and slot.kind == "data":
            await self._load_columns(slot.name, event.new)
        self._update_valid()

    async def _load_columns(self, data_slot: str, key: str | None):
        template = self.template
        dependents = [
            s for s in template.slots.values() if s.kind == "column" and s.of == data_slot
        ]
        for slot in dependents:
            self._fields[slot.name].param.update(
                options={NOT_CHOSEN: ""},
                value="",
                disabled=True,
                helper_text="Reading the columns…",
            )
        choice = next((c for c in self._choices.get(data_slot, ()) if c.key == key), None)
        self._columns[data_slot] = None
        self._read_for.pop(data_slot, None)
        self._loading[data_slot] = key or ""
        self._update_valid()
        columns = None if choice is None else await data_columns(self._editor, choice.binding)
        if self._loading.get(data_slot) != (key or "") or self.template is not template:
            # The data was changed again while these columns were read.
            return
        del self._loading[data_slot]
        self._columns[data_slot] = columns
        self._read_for[data_slot] = key or ""
        for slot in dependents:
            widget = self._fields[slot.name]
            if choice is None:
                widget.helper_text = f"Choose {template.slots[data_slot].label} first."
                continue
            if not columns:
                widget.helper_text = "Its columns could not be read."
                continue
            fitting = [c for c, dtype in columns.items() if column_fits(dtype, slot.type, c)]
            widget.param.update(
                options={NOT_CHOSEN: "", **{column: column for column in fitting}},
                value="",
                disabled=not fitting,
                helper_text="" if fitting else f"It has no {slot.type} column.",
            )
        # The views offered follow the data's type.
        for slot in template.slots.values():
            if slot.kind == "view" and slot.of == data_slot:
                data = None if choice is None else choice.binding
                self._choices[slot.name] = slot_choices(self._editor, slot, data)
                widget = self._fields[slot.name]
                options = {c.label: c.key for c in self._choices[slot.name]}
                widget.param.update(
                    options={NOT_CHOSEN: "", **options},
                    value=widget.value if widget.value in options.values() else "",
                    disabled=not options,
                )

    def raw_bindings(self) -> dict[str, t.Any]:
        """Return the choices made so far, by slot: choice keys, column names and text."""
        return {
            name: widget.value
            for name, widget in self._fields.items()
            if widget.value not in (None, "")
        }

    def _update_valid(self):
        template = self.template
        if template is None:
            self.valid = True
            return
        bound = self.raw_bindings()
        self.valid = not self._loading and all(
            slot.name in bound
            for slot in template.slots.values()
            if slot.required and slot.kind == "data"
        )

    def bindings(self) -> dict[str, t.Any]:
        """Return the bindings for :func:`~panel_flowdash.templates.fill_template`, checked.

        Raises
        ------
        TemplateError
            If a choice is no longer offered or a column is not the data's.
        """
        template = self.template
        raw = self.raw_bindings()
        for slot in template.slots.values():
            if slot.kind != "data":
                continue
            if slot.name in self._loading or self._read_for.get(slot.name, "") != raw.get(
                slot.name, ""
            ):
                raise TemplateError(
                    f"bindings.{slot.name}", "its columns are still being read; try again."
                )
        return check_bindings(template, raw, self._choices, self._columns)

    def filled(self) -> dict[str, t.Any]:
        """Return the dashboard document the chosen template gives with the choices made."""
        return fill_template(self.template, self.bindings())


__all__ = ["BLANK", "TemplateError", "TemplatePicker", "preview_html"]
