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

FlowDash itself registers no sections yet; extensions add theirs, see
[Add a Project File Section](../how-to/project-sections.md).

---

## Where it is found

`flowdash serve` reads, in order:

1. the file given with `--project FILE` (relative to the working directory),
2. `flowdash.yaml` in the served project directory,
3. `flowdash.yaml` in the working directory.

Without any of them the app runs without a project file. An embedded app
passes it as `FlowDashApp(project="flowdash.yaml")`, or a `Project` already
loaded with `panel_flowdash.project.load_project`. Apps that serve data
files can look beside them with `find_project([data_file, ...])`, which
checks the directory of each local file or folder in turn, then the working
directory.

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
`FILE:LINE:COLUMN: message` pointing at the value at fault, e.g.

```text
ERROR: /srv/app/flowdash.yaml:4:5: 'capacity' is given twice.
```

The file is read as YAML 1.1 with PyYAML's safe loader, so unquoted `yes`,
`no`, `on` and `off` are booleans; quote them to keep them as text.

---

## Python API

| Name | Description |
|------|-------------|
| `load_project(path)` | Read and load a project file, raising `ProjectError` with every problem. |
| `parse_project(text, path=None)` | The same for YAML text. |
| `find_project(places=(), cwd=None)` | The `flowdash.yaml` beside the first of `places` that has one, else in `cwd`, or `None`. |
| `Project.get(name, default=None)` | The value the section's loader returned. |
| `Project.raw(name)` | The section's YAML data. |
| `Project.set(name, value)` | Write the section back, keeping the rest of the file. |
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
