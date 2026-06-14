# Release Notes

## 0.1.0 (unreleased)

Initial release of panel-flowdash.

- `@register` decorator for component/page registration
- Typed port model (`InputPort`, `OutputPort`, `ComponentSpec`)
- Port introspection from Viewer subclasses (`param.output`, param inputs)
- `DataflowGraph` engine with cycle detection, type checking, single-source validation
- Runtime validation via `param.watch` with error callbacks
- SQLite persistence (`DashboardStore`, `DashboardModel`)
- CLI: `flowdash serve <project-dir>` with Panel-compatible options
