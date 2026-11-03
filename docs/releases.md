# Release Notes

## 0.1.0 (unreleased)

Initial release of panel-flowdash.

- `@register` decorator for component/page registration
- Typed port model (`InputPort`, `OutputPort`, `ComponentSpec`)
- Port introspection from Viewer subclasses (`param.output`, param inputs)
- A Viewer can publish a Parameter as an output by naming it in `provides`, without wrapping it in a `@param.output` method
- A Viewer declares a Parameter as an output port with `metadata={"flowdash": {"output": True, "label": ...}}`, so class factories need not repeat output names in `@register(provides=...)`; requires `param>=2.3`
- A Viewer component can define `dispose()` to release its resources; FlowDash calls it once when the node is removed, the canvas is cleared or replaced, or the session ends
- A tile shows a placeholder instead of its component while a blocking input is `None`: function components use the `required` and `blocking` fields of `requires`, Viewers declare `param.Parameter(metadata={"flowdash": {"required": True}})` (ignored on a metadata output), and `@register(placeholder=...)` replaces the default "Waiting for ..." text
- `FlowDash(inspect_outputs=True)` adds an info button to each dashboard tile that opens a dialog with the current value of the component's outputs, optionally explained by a `__flowdash_inspect__(self, port)` method on the component; `FlowDashApp` turns it on through `editor_options`
- `DataflowGraph` engine with cycle detection, type checking, single-source validation
- Runtime validation via `param.watch` with error callbacks
- `FlowDash(propagate_failures=True)`, also a `FlowDashApp` parameter, carries a failing output or edge transform downstream as a `param.ReactiveError` instead of leaving connected inputs on stale values. Inputs listed in `@register(handles_failures=[...])` receive the failure; any other input keeps its last value, its tile shows a message naming the upstream port and exception, and the failed node and edge are outlined on the canvas. Needs Param 2.5 or later
- SQLite persistence (`DashboardStore`, `DashboardModel`)
- `JSONFolderDashboardStore` keeps each dashboard as a readable JSON file in a folder, optionally read-only; `FlowDashApp` accepts any `BaseDashboardStore`, and a project's `__init__.py` selects one for `flowdash serve` by defining `store`
- A store refuses a create, save, rename, delete or sharing change by raising `DashboardStoreError`; the app shows its message and leaves the dashboard as it was, and logs any other store failure with its traceback
- `JSONFolderDashboardStore` claims a new dashboard's id exclusively, so concurrent creates with one title no longer overwrite each other, refuses to save over another user's file, and parses only the files that changed since the last listing
- One malformed dashboard file or SQLite row, or a project module that is not UTF-8, is skipped with a warning instead of breaking the listing or `flowdash serve`
- Dashboard listings check admin rights through the new `BaseDashboardStore.can_administer_model` instead of loading each dashboard again, and `DashboardStore` reuses one SQLite connection per thread (`DashboardStore.close()` releases them; safe across `--num-procs` forks)
- Responsive tile grid that wraps the authored layout on narrower screens, with optional per-breakpoint custom layouts; `DashboardModel.reference_width` records the width the layout was authored at
- Download a dashboard as JSON from the editor and drop the file onto the canvas to recreate it; `FlowDash.export_dashboard()` and `FlowDash.import_dashboard()` do the same from Python
- `FlowDashApp.editor_options` passes keyword arguments through to the embedded `FlowDash` editor, and `FlowDashApp.editor_class` substitutes a `FlowDash` subclass
- Dashboard actions from the launcher card, navigation drawer and header menu all dispatch through `FlowDashApp._on_dashboard_action`, so a subclass adds an action by extending `_ADMIN_DASHBOARD_ACTIONS` or `_VIEWER_DASHBOARD_ACTIONS` and overriding that one method; the launcher card shows viewer actions to non-administrators. The private `_on_action_edit`, `_on_action_rename`, `_on_action_delete` and `_on_action_create` methods were removed in favour of `_on_dashboard_action`
- `open_dialog`, `close_dialog` and `confirm` in `panel_flowdash.util` show a modal from any component in one dialog per session, which `FlowDashApp` and `FlowDash` lay out for you; `confirm` closes the dialog before calling `on_confirm` or `on_cancel`, and either may be a coroutine function. `FlowDash(include_page_dialog=False)` leaves the placement of `page_dialog()` to the embedder
- CLI: `flowdash serve <project-dir>` with Panel-compatible options
- `flowdash serve --package` (and `build_registry(..., package=...)`) serves a project that is an importable package under its own module names, so FlowDash and the project's code share one module object per file
