# Release Notes

## 0.1.0 (unreleased)

Initial release of panel-flowdash.

- `@register` decorator for component/page registration
- Typed port model (`InputPort`, `OutputPort`, `ComponentSpec`)
- Port introspection from Viewer subclasses (`param.output`, param inputs)
- A Viewer can publish a Parameter as an output by naming it in `provides`, without wrapping it in a `@param.output` method
- `DataflowGraph` engine with cycle detection, type checking, single-source validation
- Runtime validation via `param.watch` with error callbacks
- SQLite persistence (`DashboardStore`, `DashboardModel`)
- `JSONFolderDashboardStore` keeps each dashboard as a readable JSON file in a folder, optionally read-only; `FlowDashApp` accepts any `BaseDashboardStore`, and a project's `__init__.py` selects one for `flowdash serve` by defining `store`
- Responsive tile grid that wraps the authored layout on narrower screens, with optional per-breakpoint custom layouts; `DashboardModel.reference_width` records the width the layout was authored at
- Download a dashboard as JSON from the editor and drop the file onto the canvas to recreate it; `FlowDash.export_dashboard()` and `FlowDash.import_dashboard()` do the same from Python
- Dashboard actions from the launcher card, navigation drawer and header menu all dispatch through `FlowDashApp._on_dashboard_action`, so a subclass adds an action by extending `_ADMIN_DASHBOARD_ACTIONS` or `_VIEWER_DASHBOARD_ACTIONS` and overriding that one method; the launcher card shows viewer actions to non-administrators
- CLI: `flowdash serve <project-dir>` with Panel-compatible options
