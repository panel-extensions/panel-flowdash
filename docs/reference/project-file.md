# Project File

The project file, `flowdash.yaml`, holds the settings of a FlowDash app
that belong to the deployment rather than to one dashboard, such as an
extension's shared definitions. It is a YAML mapping of named sections:

```yaml
# flowdash.yaml
metrics:          # read by the extension that registered `metrics`
  capacity:
    table: plants
    sum: capacity_mw
```

FlowDash registers `general` and `theme`, which the admin view edits;
extensions add theirs, see
[Add a Project File Section](../how-to/project-sections.md).

---

## Built-in sections

The admin view (`/admin`, the Admin item at the bottom of the rail) writes
these. A setting the app is also given in code, in the project's
`__init__.py`, on the command line or as a `FlowDashApp` parameter, keeps
the value from code and shows locked in the admin view. Each takes effect
for pages opened after it is saved.

```yaml
general:
  edit_workspace: split      # dataflow, layout or split (FlowDash.mode)
  home_dashboard: fleet      # a dashboard id or title (home_dashboard)
  live_values: hover         # click, hover or off (popup_trigger)
  history: true
  history_coalesce: 3        # seconds
  history_limit: 500         # versions
  history_max_days: 90
  history_max_mb: 16
  explore: read_only         # ask or read_only (explore_agent=False)
  agent_palette: true
  provenance: true
  tile_cards: true           # defaults for dashboards that do not set their own
  tile_titles: false
  node_previews: false
  controls_position: left    # top or left

theme:
  color_scheme: dark         # light or dark
  primary_color: "#006D77"
  font_family: Inter         # Roboto, Inter, IBM Plex Sans, Source Sans 3, Open Sans or System UI
  border_radius: 6
  title: Wind Energy Explorer
  logo: logo.svg             # a URL, or a path relative to the project file
```

Without a project file the admin view writes `flowdash.yaml` in the project
directory on the first save.

---

## Where it is found

`flowdash serve` reads, in order:

1. the file given with `--project FILE` (relative to the working directory),
2. `flowdash.yaml` in the served project directory.

A `flowdash.yaml` in the working directory is not read when a directory is
served. Without either the app runs without a project file. An embedded app
passes it as `FlowDashApp(project="flowdash.yaml")`, or a `Project` already
loaded with `panel_flowdash.project.load_project`. Apps that serve data
files can look beside them with `find_project([data_file, ...])`, which
checks the directory of each local file or folder in turn, then the working
directory.

Loaders receive a context: `flowdash serve` and `FlowDashApp` pass
`project_dir` and `registry`; an app can pass its own with
`load_project(path, context={...})`.

---

## Rules

| Rule | What happens otherwise |
|------|------------------------|
| The file is a YAML mapping of sections. | Startup fails. |
| A key appears once per mapping. | Startup fails at the second occurrence. |
| Keys are text. | Startup fails. |
| A section is registered by FlowDash or an installed extension. | The section is ignored with a warning naming the known sections, so a file written for a newer app or for an extension that is not installed still loads. |
| A registered section is valid for its loader. | Startup fails. |

Every problem found is reported together, each as
`FILE:LINE:COLUMN: message` pointing at the value at fault, and with the
`section` and `keys` of that value, e.g.

```text
ERROR: /srv/app/flowdash.yaml:4:5: 'capacity' is given twice.
```

The file is read as YAML 1.2, so only `true` and `false` are booleans and
`yes`, `no`, `on`, `off` or `1:20` are text. When FlowDash writes a section
it quotes such text, so YAML 1.1 readers read it the same.

Problems that do not stop the app, such as unknown sections or a loader's
`Section.warn`, are logged with their position and kept in
`Project.warnings`.

## Secrets

Sections name secrets by environment variable, `${NAME}` or
`${NAME:-default}`, and never hold the value. Loaders keep the reference
and expand it with `expand_env` where it is used, and `dump` writes the
reference back. See [Secrets](../how-to/project-sections.md#secrets).

---

## Python API

| Name | Description |
|------|-------------|
| `load_project(path, context=None)` | Read and load a project file, raising `ProjectError` with every problem. |
| `parse_project(text, path=None, context=None)` | The same for YAML text. |
| `find_project(places=(), cwd=None)` | The `flowdash.yaml` beside the first of `places` that has one, else in `cwd`, or `None`. |
| `Project.get(name, default=None)` | The value the section's loader returned. |
| `Project.raw(name)` | The section's YAML data. |
| `Project.set(name, value)` | Validate the section with its loader and write it back, keeping the rest of the file. |
| `Project.warnings` | The warnings found while loading, as `Problem`s. |
| `Section.warn(path, message)` | Report a problem that does not stop loading. |
| `expand_env(value)` | Expand `${NAME}` references in text, lists and mappings. |
| `Project.problem(path, message, key=False)` | A `Problem` positioned at a value, or at its key. |
| `register_section(section)` | Register a `ProjectSection`, or `register_section(name, loader, **fields)`. |
| `sections()` | The registered sections, including those of entry points. |
| `ProjectSection` | `name`, `loader`, `title`, `icon`, `description`, `dump`, `settings_view`. |
| `SectionError(message, path=(), key=False)` | Raised by a loader for one problem. |
| `ProjectError(problems)` | Raised for the problems of a file; `problems` lists each `Problem`. |

Entry points in the `panel_flowdash.project_sections` group are registered
the first time sections are looked up.

::: panel_flowdash.project
    options:
      show_root_heading: true
      members: true
