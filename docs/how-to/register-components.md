# Register Components

Every component or page in a FlowDash project is a Python module that exports
an `app` object annotated with `@register`. The decorator attaches metadata
without altering runtime behavior.

---

## The `@register` decorator

```python
from panel_flowdash import register

@register(
    page=False,
    component=True,
    title="Stock Filter",
    icon="chart-line",
    description="Filters stock data by ticker symbol.",
    tags=["finance", "filter"],
    provides=[{"key": "filtered", "type": "DataFrame"}],
    requires=[{"key": "ticker", "type": "str"}],
)
def app(config):
    ...
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | `bool` | `True` | Register as a routable page. |
| `component` | `bool` | `False` | Register as a dataflow component (tile). |
| `sidebar` | `bool` | `False` | In dashboard mode, render this component's view in the Page sidebar instead of the tile grid. See [Layout & sizing](layout-and-sizing.md). |
| `title` | `str` | Module name | Human-readable display title. |
| `icon` | `str` | `None` | Icon identifier for the component palette. |
| `description` | `str` | `None` | Short description shown in tooltips. |
| `tags` | `list[str]` | `[]` | Tags for filtering in the component palette. |
| `provides` | `list` | `[]` | Output port declarations. See [Define ports](define-ports.md). |
| `requires` | `list` | `[]` | Input port declarations. See [Define ports](define-ports.md). |
| `default_size` | `dict` | `None` | Default tile size hint, e.g. `{"w": 4, "h": 3}`. See [Layout & sizing](layout-and-sizing.md). |
| `min_size` | `dict` | `None` | Minimum tile size hint. |
| `max_size` | `dict` | `None` | Maximum tile size hint. |
| `singleton` | `bool` | `False` | Only allow one instance on a dashboard. |
| `config_schema` | `type \| dict` | `None` | Design-time configuration definition: a `param.Parameterized` subclass, a Pydantic model, or a JSON-Schema-like dict. See [Configure nodes](configure-nodes.md). |
| `config` | `list[str]` | `[]` | For Viewer components, names the params that are design-time configuration rather than input ports. See [Configure nodes](configure-nodes.md). |
| `config_editor` | `callable` | `None` | Custom editor callable for the node's configuration form. See [Configure nodes](configure-nodes.md). |

---

## Using Viewer subclasses

Panel `Viewer` subclasses work as components without needing explicit port
declarations. FlowDash introspects params and `@param.output` decorators
automatically:

```python
import param
import panel as pn
from panel_flowdash import register

@register(component=True)
class StockFilter(pn.viewable.Viewer):
    ticker = param.String(default="AAPL")
    start_date = param.Date()

    @param.output(param.DataFrame)
    def filtered_data(self):
        ...

    def __panel__(self):
        return pn.Column(
            pn.widgets.TextInput.from_param(self.param.ticker),
            pn.widgets.DatePicker.from_param(self.param.start_date),
        )
```

In this example:

- **Inputs**: `ticker` (String), `start_date` (Date) are discovered from params
- **Outputs**: `filtered_data` (DataFrame) is discovered from `@param.output`

Base Parameterized and Viewable params (`name`, `loading`, etc.) are excluded
automatically.

---

## Decorator overrides on Viewer classes

When you decorate a Viewer with explicit `provides` or `requires`, the decorator
values take precedence over introspection:

```python
@register(
    component=True,
    provides=[{"key": "result", "type": "DataFrame"}],
    requires=[{"key": "query", "type": "str"}],
)
class QueryRunner(pn.viewable.Viewer):
    query = param.String()
    timeout = param.Integer(default=30)

    @param.output(param.DataFrame)
    def result(self):
        ...
```

Here only `query` is exposed as an input (not `timeout`), and `result` is the
sole output, regardless of any other `@param.output` decorators.

---

## Opening a dialog from a component

A component that needs a modal, such as a confirmation before saving or a details view, should not create a `Dialog` inside its own tile. `panel_flowdash.util` provides one dialog per session instead, shared by every component on the page:

```python
import panel as pn
import panel_material_ui as pmui
from panel_flowdash import register
from panel_flowdash.util import confirm, open_dialog


@register(component=True, page=False)
def editor(config):
    def save(_event):
        ...

    def ask_to_save(_event):
        confirm("Save changes?", pn.pane.Markdown("This overwrites the stored copy."), on_confirm=save)

    def show_details(_event):
        open_dialog("Details", pn.Column(...))

    return pn.Row(
        pmui.Button(label="Save", on_click=ask_to_save),
        pmui.Button(label="Details", on_click=show_details),
    )
```

`open_dialog(title, content)` shows `content` and replaces whatever the dialog showed before. The user can dismiss it with the close button, the Escape key or a click outside it, and `close_dialog()` dismisses it from code.

`confirm(title, content=None, *, on_confirm, on_cancel=None)` asks a question. Opening it never runs anything. *Confirm* closes the dialog and then calls `on_confirm(event)`, so the handler can open another dialog. *Cancel* closes it and calls `on_cancel(event)` if given. A confirmation can only be answered with its buttons, and only the first click counts, so a double click does not run the action twice. Handlers may be coroutine functions. Set `confirm_label` and `cancel_label` to rename the buttons, and `width` (`"xs"` to `"xl"`) on either function to change the dialog's maximum width.

If a second `open_dialog` or `confirm` replaces an unanswered confirmation, neither of its handlers is called.

`FlowDashApp` places the dialog on every page. Outside a served session (a plain script, a unit test, a notebook) there is no dialog to show: `open_dialog` and `confirm` log a warning and do nothing, as `notify` falls back to the logger, so a `confirm` handler never runs without an answer. To embed the editor in your own page, see [Embed the Editor](embed-the-editor.md#where-the-page-dialog-goes).

---

## Project directory layout

FlowDash scans one level of subdirectories as "sections":

```
my_project/
    Section_A/
        __init__.py
        component1.py    # exports `app`
        component2.py
    Section_B/
        page1.py
```

Rules:

- Directories starting with `.` or `_` are skipped.
- Files starting with `_` are skipped.
- Each module must export an `app` attribute.
- The `app_id` is `"Section/module_name"` (e.g. `"Section_A/component1"`).
