# Release Notes

## 0.1.0 (unreleased)

Initial release of panel-flowdash.

### Upgrading from 0.1.0rc2

Installation. panel-flowdash requires Python 3.12 or newer and Panel 1.11 or newer, which panel-material-ui 0.17 needs; Python 3.10 and 3.11 are no longer supported. It also requires panel-agents 0.1.0b12, panel-material-ui 0.17.0rc1, panel-reactflow 0.6.0b7 and panel-tiles 0.6.0b2 or later. panel-agents is always installed, while the `agent` extra still adds pydantic-ai for the assistant.

Look and defaults. Dashboards saved with rc2 load unchanged, but several defaults changed and the editor was redesigned, so they look and behave differently:

- `FlowDashApp.nav_variant` (and `flowdash serve --nav-variant`) defaults to `"rail"`: a navigation rail, a breadcrumb with a dashboard switcher and a Ctrl/Cmd+K search. Set `nav_variant="right"` to keep the rc2 drawer; `"left"` and `"menubar"` work as before
- Dashboard tiles are framed (`tile_cards=True`) and have no title row (`tile_titles=False`). Both are dashboard settings, so each dashboard can change them in its settings dialog once it is saved; `FlowDash(tile_cards=..., tile_titles=...)`, or `editor_options` in `FlowDashApp`, sets the default for dashboards that have not saved them. The built-in Divider and Heading are unframed whatever the setting (`@register(framed=False)`), and each tile's Frame option in the Layout tab overrides both
- Components registered with `sidebar=True` render in a controls bar above the tiles, and `FlowDashApp` no longer copies them into the page sidebar. Embedders who place `FlowDash.sidebar` themselves set `FlowDash(controls_bar=False)`
- The floating editor toolbar is replaced by a dashboard bar with separate view and edit modes (Edit, Done, Discard, Save). The Preview switch is gone, since view mode is the preview; the `preview`, `toolbar` and `toolbar_extra` parameters keep working
- Port popups on the wiring canvas open on click (`FlowDash.popup_trigger="click"`); set `popup_trigger="hover"` for the rc2 behaviour. Node configuration moved from a popup into a persistent inspector, and nodes no longer embed their live views
- Node cards no longer show a value's repr: an opaque output is named by its type, and a node with several outputs previews the one picked on its card instead of always the first
- The inspector shows a connected input as a summary of what it carries instead of its repr, and a code field as a five-line read-only snippet whose Edit button opens the code panel under the canvas (or a dialog in windows under 960px and in view mode). Code edited there is applied on Run, not on blur
- Description, "How was this made?" and Remove moved from the inspector body into its header and its menu
- Inspecting outputs opens a side sheet from the tile's Inspect action instead of a dialog behind an info button
- The canvas minimap is off when the editor opens and is toggled from the status bar; it can no longer be turned on by default
- Dashboard titles, descriptions, tile titles, share names, data column names and error messages are shown as plain text: Markdown and HTML in them are no longer rendered. Component titles, port labels and docstrings written in code render as before
- Settings and sharing are one Dashboard settings dialog. Saving it saves the dashboard immediately, and turning on "Everyone can view" drops the explicit Can view entries on save
- New global keyboard shortcuts apply outside text fields and dialogs: Ctrl/Cmd+K, `/`, `?`, R, E and Esc, and Ctrl/Cmd+S saves in edit mode instead of opening the browser's save dialog.
- The `complex_dataflow` example's assistant panel starts closed and opens from the top bar

Permissions. Deployments with authentication should review these:

- Without `admin_groups`, only a dashboard's owner can administer it (rename, delete, share, and edit unless shared with edit rights); in rc2 anyone who could see a dashboard could. Auth-less deployments are unaffected. With OAuth, configure `admin_groups`, and note that dashboards created before OAuth was enabled belong to the server account and can only be administered by an admin group
- Listings and `can_edit_model` check rights through `BaseDashboardStore.can_administer_model`, so a store subclass that only overrides `can_administer` should override `can_administer_model` as well
- Editors without admin rights can no longer rename, delete or share, and an edit link opened by a viewer falls back to view mode
- Component `allow_*` / `deny_*` rules and `authorize` callbacks now also apply in the editor: refused components are left out of the palette, `FlowDash.add_component` raises `PermissionError`, and saved items using them load as locked placeholders that are saved back unchanged. Components without rules stay usable under `default_allow = False`. `authorize_component` is set by `FlowDashApp` and is refused in `editor_options`; override `FlowDashApp._can_use_component` instead
- Importing a dashboard file never adds or changes a component the user may not use, and Download writes such items as a `flowdash:restricted` stand-in with only their id and position and none of their edges
- `FlowDash` has a new `authorize_dashboard` hook that `save()`, reload and discard check against the stored dashboard's current rules, so someone whose edit rights were revoked while the dashboard was open can no longer save it. Like `authorize_component`, it is set by `FlowDashApp` and refused in `editor_options`
- With `inspect_outputs` on, viewers only get Duplicate and Download on dashboards that allow `viewers_can_inspect`, since a copy would otherwise give them inspect rights. `FlowDashApp._dashboard_actions` takes a new `can_copy` keyword
- `Permission` gains `allow_edit_users` and `allow_edit_groups`, and `Permission.is_empty` counts them
- `make_authorize_callback(..., denied_route=...)` returns a redirect URL instead of `False` for refused requests; without `denied_route` it behaves as before

Stores and saved data:

- Dashboards record their turns (each request to the assistant, and each batch of manual edits between requests) and the turn that created each node. The dashboard model is at version 4 with `DashboardModel.turns`, `DashboardItem.origin`, `DashboardModel.parameters` and `DashboardModel.groups`; dashboards saved by rc2 and earlier (version 3) load and are migrated, and models from pre-releases load too; `DashboardStore` adds a `turns_json` column to existing databases on open, and older dashboards load without turns. Custom stores should persist `turns` and each item's `origin`, as `to_dict`/`from_dict` do
- Nodes can be grouped on the Dataflow canvas: select several and choose Group in the inspector. A group is a frame that can be collapsed into one node showing the ports its connections cross, and expanding it pushes the nodes beside and below it aside. `FlowDash` gains `group_nodes`, `ungroup`, `rename_group`, `collapse_group`, `expand_group` and `groups`, `DashboardOps` and the assistant gain `group_nodes` and `ungroup`, and YAML files hold them in a top-level `groups` with a node `group`. `DashboardModel.groups` holds them; `DashboardStore` adds a `groups_json` column on open. Requires panel-reactflow 0.6.0b5 or later.
- Dashboards can declare parameters: named config fields to run them again with other values, with a contract a component can declare (`register(contract=...)`) that checks a bound value before anything changes and names the nodes it would break. `FlowDash` and `DashboardOps` gain `declare_parameter`, `remove_parameter` and `bind_parameters`, and `panel_flowdash.parameters` has `bind_document` for runs without an editor and `parameters_schema` for a binding form. `DashboardModel.parameters` holds them; `DashboardStore` adds a `parameters_json` column on open. YAML files hold them in a top-level `parameters`
- Dashboards can be written as YAML files (`flowdash: 1`), see the Dashboard YAML Format reference. Files from older pre-releases still load
- Every edit becomes a version of the dashboard's edit history, which the store keeps per dashboard: `DashboardStore` adds a `dashboard_history` table to existing databases on open and `JSONFolderDashboardStore` writes a `.history` folder. Custom stores keep it by implementing `load_history`, `append_history`, `replace_history` and `delete_history`, and undo works within a session without them. The assistant's chat shares the history's `RunState`, and its edit tools no longer publish versions of their own while the history is on
- Saved items and edges that cannot be restored (a missing or failing component, a renamed port, a type that no longer fits) are kept as placeholders and saved back unchanged instead of being dropped with a warning. Remove them in the editor to delete them
- `BaseDashboardStore.find_by_id_or_title(ref)` became `find_by_id_or_title(ref, identity=None, *, default_allow=True)`. Custom stores that override it must accept the new arguments, or `FlowDashApp` raises `TypeError` when `home_dashboard` is set
- Stores declare `BaseDashboardStore.random_ids` (default `True`; `False` for `JSONFolderDashboardStore`, whose ids are title slugs). When ids can be guessed, a dashboard the user may not open gets the same not-found page as a missing one instead of naming its owner, so a custom store with guessable ids should set `random_ids = False`
- `save_dashboard` raises `DashboardConflictError` when the model's `revision` is stale. Custom stores should store and check `DashboardModel.revision` the same way; a store that ignores it stays last-writer-wins
- SQLite databases gain `settings_json` and `revision` columns, added automatically on open; the migration is one-way, so back up a database you may want to open with rc2 again. JSON dashboard files gain `settings` and `revision` and keep unresolved items, and on Windows a `.flowdash.lock` file appears in the folder
- `tile_layout` and `responsive_layouts` entries are keyed by `instance_id`. Layouts saved by rc2 are mapped by position on load and saved in the keyed form from then on, which rc2 cannot read back correctly
- Exported dashboard files no longer contain `dashboard_id`, `user_id`, `permission` or `revision`, and `DashboardModel.from_dict` rejects a non-string `dashboard_id`, `user_id` or `title`
- The `format` edge transform caps the width at 1000 and the precision at 100; saved edges above those load as unresolved

Serving and API:

- `flowdash serve` reads every `FlowDashApp` option it supports from the project's `__init__.py` and exits with an error naming the attribute when a value is invalid. `__init__.py` now runs after the registry scan, and passing `--db-path` while the project defines `store` is an error
- `ComponentSpec`, `InputPort` and `PanelAppMetadata` gained fields in the middle of their argument lists (`max_size` and `min_size` before `config`, `handles_failures` before `parameter`, and `placeholder`, `handles_failures`, `aliases` and `port_aliases`). Pass their arguments by keyword
- The `default_size`, `min_size` and `max_size` hints of `@register` take `{"width": ..., "height": ...}` in pixels; the old `{"w", "h"}` keys never had an effect and are ignored with a warning
- The private `_on_action_edit`, `_on_action_rename`, `_on_action_delete` and `_on_action_create` methods of `FlowDashApp` were removed in favour of `_on_dashboard_action`
- A Viewer component receives a new input inside `pn.io.hold()`, so the whole downstream cascade reaches the browser as one patch. Intermediate updates a component makes during the cascade, such as a progress message, are no longer shown separately
- With pandas older than 3 and copy-on-write off, the built-in transforms copy their input frame on every hop so edges never share a mutable frame

### Components and ports

- `@register` decorator for component/page registration
- Typed port model (`InputPort`, `OutputPort`, `ComponentSpec`)
- Port introspection from Viewer subclasses (`param.output`, param inputs)
- A Viewer can publish a Parameter as an output by naming it in `provides`, without wrapping it in a `@param.output` method
- A Viewer declares a Parameter as an output port with `metadata={"flowdash": {"output": True, "label": ...}}`, so class factories need not repeat output names in `@register(provides=...)`; requires `param>=2.3`
- Port types match across spellings (`str`/`String`, `int`/`Integer`, `DataFrame`/`pd.DataFrame`), an `Integer` output can feed a `Number` input, and untyped or generic ports connect to anything; `register_port_type(name, aliases=..., subtype_of=...)` adds project types
- `@register(aliases=[...], port_aliases={...})` and a Parameter's `metadata={"flowdash": {"aliases": [...]}}` keep saved dashboards loading after a component or port is renamed; the next save writes the new names
- A Viewer component can define `dispose()` to release its resources; FlowDash calls it once when the node is removed, the canvas is cleared or replaced, or the session ends. Removing a node or edge, or loading another dashboard, also releases its wiring, its edge expressions and the subscriptions an async function component made
- A tile shows a placeholder instead of its component while a blocking input is `None`: function components use the `required` and `blocking` fields of `requires`, Viewers declare `param.Parameter(metadata={"flowdash": {"required": True}})` (ignored on a metadata output), and `@register(placeholder=...)` replaces the default "Waiting for ..." text
- The `default_size`, `min_size` and `max_size` hints of `@register` set a new tile's size and bound resizing and stored layouts
- Mutable `requires` fallbacks and input defaults are copied per session and per node instead of shared
- Built-in text components: Markdown (rendered without raw HTML or math, with `{{ value }}` placeholders filled from an optional input), Notes (rich text typed into the tile while editing, read-only for viewers), Heading and Divider
- Built-in Radio Group, Range Slider, Number Input, Date Range, Toggle and Text Input widgets, and KPI and Image views, beside Select, MultiChoice and Slider; every built-in has a description and a default tile size
- A Viewer config param declared with `metadata={"flowdash": {"two_way": True}}` is synced back to the node's config when the component changes it, so it is saved with the dashboard
- A config check can return or raise `CheckDiagnostic(message, field=, line=, column=, ...)` to say where in a code field a problem is, read back with `CheckResult.diagnostics(field)`, and `@register(config_completions=...)` supplies completion words for code fields, which otherwise complete the node's input column names (`node_completions()`)
- `FlowDash(context={...})` hands host resources such as data sources to components, which read them with `dashboard_context()`, a read-only view that previews see too

### Dataflow

- `DataflowGraph` engine with cycle detection, type checking, single-source validation
- Runtime validation via `param.watch` with error callbacks
- `FlowDash(propagate_failures=True)`, also a `FlowDashApp` parameter, carries a failing output or edge transform downstream as a `param.ReactiveError` instead of leaving connected inputs on stale values. Inputs listed in `@register(handles_failures=[...])` receive the failure; any other input keeps its last value, its tile explains which upstream component failed, and the failed node and edge are outlined on the canvas. Needs Param 2.5 or later
- Edge transforms are edited in the editor's inspector as numbered steps that show the value's shape between them; operations that cannot follow the current value are disabled, and a failing step shows the reason with a closest-column suggestion and is not applied until fixed. A new how-to covers the built-in steps and custom `Transform` subclasses
- Switching dashboards in an open editor places them in one canvas update (500 nodes in about 1 s instead of 20 s), `DataflowGraph.batch()` binds list inputs once, and very deep chains finish on the next tick instead of raising `RecursionError`
- Changing a control updates every affected tile in the same frame instead of tile by tile
- A component whose watcher raises no longer stops its siblings from receiving the value, and async outputs no longer run twice per input change

### Editor

- Undo and redo: the canvas keeps an edit history with one version per request to the assistant and one per burst of the person's edits (`FlowDash.history_coalesce`, 3 s). Ctrl/Cmd+Z, Ctrl/Cmd+Shift+Z and the command palette step through it, and undo reaches back into earlier sessions as far as the store keeps the history (`FlowDash.history_limit`, 500 versions, and `history_max_days`, 90). Undo restores turns and node origins as they were, changes only what differs from the canvas, and never brings back a node the user may not use. `FlowDash.undo()`, `redo()`, `can_undo` and `can_redo` do the same from code; `history=False` turns it off
- The editor has separate view and edit modes in one dashboard bar. Edit mode has Dataflow and Layout tabs, a save state (Unsaved changes, Saved, Read-only), Settings, Download, Import file, Clear canvas, Discard and Save; moving a node or tile marks the dashboard as changed
- The wiring canvas shows compact node cards with a status dot, port labels and a value summary, a collapsible inspector for the selected component, edge or dashboard, and a status bar with issue, component and connection counts and the minimap toggle
- Node cards give each port its own row, names only: the type is the handle colour (`register_port_type(color=...)`) and a tooltip, a missing required input has a red ring, and unconnected optional inputs fold into one row. Clicking an output row picks the output the card previews; previews include a table, a KPI value and a chart thumbnail, and a component's `__flowdash_preview__` may take `output=`. The expand icon shows the first 100 rows. Cards collapse to their header, one at a time or all at once from the status bar (C; P toggles previews). The previewed output and the collapsed state are saved with the dashboard and undoable
- The component palette is searchable (press `/`), groups components into data sources, transforms, views and widgets (a `source`, `transform`, `view` or `widget` tag overrides the role), shows each component's ports and marks placed ones
- The Layout tab has a panel to size the selected tile in a 12-column grid within its size hints, hide it, and bring back components that are not on the dashboard, with always visible drag and resize handles
- Controls, KPIs and text can be dragged on the Layout tab between the tile grid, the top bar and the sidebar, and reordered within each bar, or moved with Move to buttons in the inspector and the layout panel; charts and tables stay on the grid. The placement is saved as the tile's `area` and the agent's `place_tile` takes an `area`
- Every way of leaving a dashboard with unsaved changes asks first, including picking a dashboard in the switcher, creating a new one and reloading the tab, and the dialog lists what changed
- An empty canvas shows a start card that adds a data source in one click
- "How was this made?" on a tile the assistant made, and in the inspector, shows the request that made the node with its model and tool calls, and "Changed later by" for later edits. Other people's requests are redacted for viewers and for restricted nodes, and after a reload it shows the turn's question, time and summary. Requires panel-agents 0.1.0b9
- The assistant can offer several models: `FlowDashApp.agent_models` (or `agent_models` in `__init__.py`), `FLOWDASH_AGENT_MODELS="id[=label],..."` or a repeated `flowdash serve --agent-model ID[=LABEL]`. A small picker below the chat input chooses the model of the next request, models with a missing provider key are listed disabled with the reason, and the browser remembers the pick. `agent_model`/`FLOWDASH_AGENT_MODEL` stays the default. Requires panel-agents 0.1.0b10
- With a single model, configured or read off a ready-made `agent`, the assistant's chat names it where the picker would be, so the picker no longer seems to vanish. Needs the next panel-agents release; earlier ones show no label
- An input nothing is connected to gets a typed editor in the inspector, built from the component's Parameter or port type, so a filter value, a KPI reference or a year range needs no extra widget node. The value is stored under a node's `inputs`, next to `config`, and a connection always wins over it; a typed value reaches the component as soon as it is set. A refused value says what the input needs, e.g. "Needs two numbers, like (2010, 2020)."
- Dropping a table output on a number input, such as a KPI's value, offers to connect it through a column and an aggregation, with a preview of the number the input would get; dropping a component from the palette there offers the same. Any other refused drop says why
- SQL, Markdown and other code fields use a code editor with highlighting and an expand dialog; Escape then Tab leaves it, and Save waits for a pending config check. The built-in Markdown component's text uses it
- Code fields open in a resizable code panel docked under the canvas, with the node's live result beside the editor. The text and the node's other config fields form a draft that Run or Ctrl+Enter applies as one checked, undoable edit, or "Run on change" applies after a pause in typing. Unrun drafts are kept per field, shown as "Edits not run" and asked about before leaving the dashboard; positioned check problems are marked in the code with a fix button, and the editor completes input column names. `FlowDash.open_code_panel()` opens it from code
- The node inspector is compact: one header line with the node's name, component, status and a menu with "How was this made?", Duplicate and Remove, shorter fields, input summaries (rows, columns, tables) read from the value without running a query, and code snippets that open the code panel
- A config field holding a spec dict, such as a Vega-Lite chart, can be edited as a form of curated settings beside its JSON, kept in sync both ways. Components declare the settings with `SpecSetting` paths in a `param.Dict`'s `metadata={"flowdash": {"widget": "spec", "settings": [...]}}`; keys no setting covers are kept and flagged, and each edit is one checked, undoable version
- An Explore view, beside View and Edit, reads a dashboard's turns as a feed: each request with its question, steps and answer (or the list of what a manual edit changed), and a live card for each node it created with "Add to dashboard". A composer in the page asks an Explore agent of its own (`FlowDashApp.explore_agent`, by default `create_explore_agent` on the assistant's model), separate from the drawer's assistant, so each shows only its own requests; approvals are answered in the feed, viewers get the feed without the composer, and without an Explore agent it shows every recorded turn
- The KPI's Format is a dropdown of named formats with examples, or a custom format spec, and its Thresholds are rows of an "Up to" limit and a color. Both are stored as before
- The Split tab shows the dashboard at its pane's width, as the dashboard would look at that width, and the Layout tab keeps a row of a heading and a KPI together below 1600 px. Requires panel-tiles 0.6.0b2 or later
- Download a dashboard as JSON from the editor and drop the file onto the canvas to recreate it; `FlowDash.export_dashboard()` and `FlowDash.import_dashboard()` do the same from Python
- `open_dialog`, `close_dialog` and `confirm` in `panel_flowdash.util` show a modal from any component in one dialog per session, which `FlowDashApp` and `FlowDash` lay out for you; `confirm` closes the dialog before calling `on_confirm` or `on_cancel`, and either may be a coroutine function. `FlowDash(include_page_dialog=False)` leaves the placement of `page_dialog()` to the embedder

### Dashboards in view mode

- Responsive tile grid that wraps the authored layout on narrower screens, with optional per-breakpoint custom layouts; `DashboardModel.reference_width` records the width the layout was authored at. Tile layouts are keyed by `instance_id`, so removing or skipping a tile no longer shifts the others
- Tiles can be framed as cards and titled per dashboard (`tile_cards`, `tile_titles`), and offer Expand (full screen), Inspect and, in `FlowDashApp`, Show in editor as actions that float over the tile on hover or focus
- A tile whose input failed or is still missing says so in plain language and names the upstream component, with the technical details behind a toggle
- `FlowDash(inspect_outputs=True)` adds an Inspect action to each dashboard tile that opens a side sheet with the current value of the component's outputs, optionally explained by a `__flowdash_inspect__(self, port)` method on the component. Only people who can edit see it unless the dashboard's `viewers_can_inspect` setting allows viewers; `FlowDashApp` turns it on through `editor_options`
- The dashboard bar shows the dashboard's `description`, a health chip ("1 failed, 1 waiting"), when it was refreshed and the auto-refresh period, with a banner that points to failed or waiting tiles. Controls registered with `sidebar=True` sit in a controls bar above the tiles with a status dot per pipeline stage
- Dashboard-wide settings: `DashboardModel.settings` is persisted by every store, `FlowDash.settings` holds the loaded values with built-in `description`, `refresh_period`, `tile_cards`, `tile_titles` and `viewers_can_inspect`, and `FlowDash(settings_class=...)` / `FlowDashApp(settings_class=...)` add application-specific parameters, edited in the Dashboard settings dialog
- `FlowDash.refresh` event, fired every `refresh_period` seconds and by the Refresh button in view mode, whose menu also sets the period for the session; components reach the settings and the event through `current_dashboard()`

### App

- `FlowDashApp` has an app shell with a navigation rail (Home, Dashboards, Components, Pages, Help), a breadcrumb with a dashboard switcher, and a Ctrl/Cmd+K command palette for dashboards, components and actions. R refreshes, E edits, Esc returns to view mode, Ctrl/Cmd+S saves and `?` lists the shortcuts
- The launcher lists dashboards as cards with a layout thumbnail, last edit, owner, component count and sharing, with filters (All, Owned by me, Shared with me), sorting, a list view and a first-run state. Card actions are Open, Edit, Rename, Duplicate, Share, Download as file and Delete, which asks you to type the dashboard's name
- `/components` is a component gallery with search, role and "Accepts <type>" filters, and a detail panel with the ports, the dashboards using a component, and Add to dashboard
- Not found, access denied (naming the owner to ask, never the contents, and only for stores with random ids), unknown URLs (still status 404) and loading each have their own page; restricted code pages redirect to an in-app access-denied page
- `FlowDashApp.editor_options` passes keyword arguments through to the embedded `FlowDash` editor, and `FlowDashApp.editor_class` substitutes a `FlowDash` subclass
- Dashboard actions from the launcher card, navigation and dashboard bar menus all dispatch through `FlowDashApp._on_dashboard_action`, so a subclass adds an action by extending `_ADMIN_DASHBOARD_ACTIONS` or `_VIEWER_DASHBOARD_ACTIONS` and overriding that one method; editors and viewers see the actions their role allows
- A slow dashboard load no longer overwrites a newer navigation
- The assistant makes independent edits as parallel tool calls and looks up components in one `list_components` call (which takes a `components` list), so building a dashboard takes fewer model turns, and each dashboard tool may fail three times per run before it ends the run (`create_agent(tool_retries=...)`). Its change outlines are patched instead of redrawn after each edit and sit on the tile's visible edge
- The `wind_energy` example is an offline showcase project with three dashboards, two pages and 20 components built on the US Wind Turbine Database. Its charts, map and tables update in place, and its port types are registered in the project `__init__.py` so its components connect before any source is placed

### Sharing and permissions

- Dashboards can be shared with edit rights (`Permission.allow_edit_users`, `allow_edit_groups`). Editors can change and save a dashboard; renaming, deleting and sharing stay with the owner and `admin_groups`
- The Dashboard settings dialog combines general settings, a refresh period and sharing, with "Everyone can view" and Owner, Can view, Can edit and Blocked roles for people and groups that map onto the stored allow, edit and deny rules
- Component permissions apply in the palette, on placement, on load and on import and download, and the component gallery only lists components the user may place
- Text stored in dashboards and data is escaped wherever it is shown, so markup in a title or column name cannot run script in another user's session
- Page rules that `@register` cannot read statically (module constants, expressions, `authorize` callables) now fail closed until the module is imported, and the HTTP gate checks the live registry and `authorize` on every request
- An OAuth user no longer also carries the server's OS account name, so they no longer match rules or ownership keyed to it
- A lone string in a permission rule, `admin_groups`, `user_groups` or `group_claims` is read as one name, and `default_allow = "False"` is read as false
- Saving from an open editor keeps sharing and title changes made elsewhere in the meantime, and re-checks edit rights, so revoking them applies to dashboards already open. A refused save keeps the changes and switches the editor to read-only
- The launcher's sharing chip counts the viewers and editors the settings dialog shows
- A home dashboard configured by title only resolves to a dashboard the user may open, and an ambiguous title shows the launcher

### Stores

- SQLite persistence (`DashboardStore`, `DashboardModel`)
- `JSONFolderDashboardStore` keeps each dashboard as a readable JSON file in a folder, optionally read-only; `FlowDashApp` accepts any `BaseDashboardStore`, and a project's `__init__.py` selects one for `flowdash serve` by defining `store`
- Every built-in store keeps a per-dashboard `revision` and refuses a stale save with `DashboardConflictError`; the editor then offers to reload the dashboard or save a copy, and `FlowDash.save_as(title)` saves a copy from Python
- A store refuses a create, save, rename, delete or sharing change by raising `DashboardStoreError`; the app shows its message and leaves the dashboard as it was, and logs any other store failure with its traceback
- `JSONFolderDashboardStore` claims a new dashboard's id exclusively, so concurrent creates with one title no longer overwrite each other, refuses to save over another user's file, and parses only the files that changed since the last listing
- One malformed dashboard file or SQLite row, or a project module that is not UTF-8, is skipped with a warning instead of breaking the listing or `flowdash serve`
- Dashboard listings check admin rights through the new `BaseDashboardStore.can_administer_model` instead of loading each dashboard again, and `DashboardStore` reuses one SQLite connection per thread (`DashboardStore.close()` releases them; safe across `--num-procs` forks)

### Serving

- CLI: `flowdash serve <project-dir>` with Panel-compatible options
- `flowdash serve` reads `settings_class`, `editor_class`, `editor_options`, `propagate_failures`, `breakpoints`, `include_builtin_components`, `notifications`, `page_options`, `title`, `nav_variant` and `home_dashboard` from the project's `__init__.py`, and command-line flags take precedence
- `flowdash serve --package` (and `build_registry(..., package=...)`) serves a project that is an importable package under its own module names, so FlowDash and the project's code share one module object per file
