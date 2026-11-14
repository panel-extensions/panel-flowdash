# Release Notes

## 0.1.0 (unreleased)

Initial release of panel-flowdash.

- `@register` decorator for component/page registration
- Typed port model (`InputPort`, `OutputPort`, `ComponentSpec`)
- Port introspection from Viewer subclasses (`param.output`, param inputs)
- A Viewer can publish a Parameter as an output by naming it in `provides`, without wrapping it in a `@param.output` method
- A Viewer component can define `dispose()` to release its resources; FlowDash calls it once when the node is removed, the canvas is cleared or replaced, or the session ends
- `DataflowGraph` engine with cycle detection, type checking, single-source validation
- Runtime validation via `param.watch` with error callbacks
- SQLite persistence (`DashboardStore`, `DashboardModel`)
- `JSONFolderDashboardStore` keeps each dashboard as a readable JSON file in a folder, optionally read-only; `FlowDashApp` accepts any `BaseDashboardStore`, and a project's `__init__.py` selects one for `flowdash serve` by defining `store`
- A store refuses a create, save, rename, delete or sharing change by raising `DashboardStoreError`; the app shows its message and leaves the dashboard as it was, and logs any other store failure with its traceback
- Responsive tile grid that wraps the authored layout on narrower screens, with optional per-breakpoint custom layouts; `DashboardModel.reference_width` records the width the layout was authored at
- Download a dashboard as JSON from the editor and drop the file onto the canvas to recreate it; `FlowDash.export_dashboard()` and `FlowDash.import_dashboard()` do the same from Python
- `FlowDashApp.editor_options` passes keyword arguments through to the embedded `FlowDash` editor, and `FlowDashApp.editor_class` substitutes a `FlowDash` subclass
- Dashboard actions from the launcher card, navigation drawer and header menu all dispatch through `FlowDashApp._on_dashboard_action`, so a subclass adds an action by extending `_ADMIN_DASHBOARD_ACTIONS` or `_VIEWER_DASHBOARD_ACTIONS` and overriding that one method; the launcher card shows viewer actions to non-administrators. The private `_on_action_edit`, `_on_action_rename`, `_on_action_delete` and `_on_action_create` methods were removed in favour of `_on_dashboard_action`
- CLI: `flowdash serve <project-dir>` with Panel-compatible options
- `flowdash serve --package` (and `build_registry(..., package=...)`) serves a project that is an importable package under its own module names, so FlowDash and the project's code share one module object per file
