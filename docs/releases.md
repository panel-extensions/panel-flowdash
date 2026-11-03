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
- Dashboard-wide settings: `DashboardModel.settings` is persisted by every store (existing SQLite databases are migrated on open), `FlowDash.settings` holds the loaded values with a built-in `refresh_period`, and `FlowDash(settings_class=...)` / `FlowDashApp(settings_class=...)` add application-specific parameters, edited in edit mode through a toolbar form
- `FlowDash.refresh` event, fired every `refresh_period` seconds and by a Refresh button in view mode; components reach the settings and the event through `current_dashboard()`
- CLI: `flowdash serve <project-dir>` with Panel-compatible options
