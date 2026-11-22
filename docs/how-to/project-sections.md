# Add a Project File Section

A FlowDash app reads one project file, `flowdash.yaml`, made of named
top-level sections. FlowDash and extensions each own some of those sections:
an extension registers a section with a loader that turns the section's YAML
into a typed value, and the app reads that value back with
`Project.get(name)`. This page shows how to register one. The file itself is
described in the [Project File reference](../reference/project-file.md).

---

## 1. Write a loader

A loader receives a `Section`: its `name`, its raw YAML data as `value` and
the `project` it belongs to. It returns whatever the app should work with,
and reports a problem by raising `SectionError` with the path, inside the
section, of the value at fault. FlowDash turns the path into the file, line
and column of that value.

```python
from dataclasses import dataclass

from panel_flowdash.project import ProjectSection, SectionError, register_section


@dataclass
class Branding:
    logo: str
    accent: str = "#0072b5"


def load_branding(section):
    value = section.value
    if not isinstance(value, dict):
        raise SectionError("`branding` is a mapping, e.g. `logo: logo.svg`.")
    unknown = sorted(set(value) - {"logo", "accent"})
    if unknown:
        raise SectionError(f"Unknown branding option {unknown[0]!r}.", path=(unknown[0],), key=True)
    if not isinstance(value.get("logo"), str):
        raise SectionError("`logo` is the path of an image.", path=("logo",))
    return Branding(**value)


def dump_branding(branding):
    return {"logo": branding.logo, "accent": branding.accent}


register_section(
    ProjectSection(
        "branding",
        load_branding,
        title="Branding",
        icon="palette",
        description="The logo and accent color of the app.",
        dump=dump_branding,
    )
)
```

To report several problems at once, collect them with
`section.problem(path, message)` and raise `ProjectError(problems)`. A
`ValueError` or `TypeError` raised by the loader is reported at the
section's key.

With this file

```yaml
branding:
  logo: 42
```

`flowdash serve` stops at startup with

```text
ERROR: /path/to/flowdash.yaml:2:9: `logo` is the path of an image.
```

---

## 2. Register it

Register the section before the project file is loaded. There are two ways:

- **In code**, by calling `register_section` in a module the app imports
  before it starts: the project's `__init__.py`, a component library given
  with `--component-library`, or your own script before it creates
  `FlowDashApp(project=...)`.
- **As an entry point**, so installing your package is enough. Declare a
  `ProjectSection`, a list of them or a function returning either in the
  `panel_flowdash.project_sections` group:

```toml
[project.entry-points."panel_flowdash.project_sections"]
branding = "my_extension.project:BRANDING"
```

Registering a name again replaces the earlier registration, so a module
reloaded by `--dev` can register again.

---

## 3. Read it in the app

`FlowDashApp.project` is the loaded `Project`. `get` returns the loader's
value, or a default when the file has no such section:

```python
from panel_flowdash.project import parse_project

project = parse_project("branding:\n  logo: logo.svg\n")
branding = project.get("branding")
assert branding == Branding(logo="logo.svg")
```

Paths in a section are best resolved against `project.directory`, the
folder the file is in.

---

## 4. Write it back

`Project.set(name, value)` passes the value through the section's `dump`
and writes the section to the file, leaving the other sections and their
comments as they were. Values must read back the same: text that YAML would
read as something else, such as `on` or `yes`, has to be quoted by `dump`'s
caller, or `set` refuses it with a `ProjectError`.

---

## 5. An admin settings tab

A section can also describe its tab in the admin settings with
`settings_view`, a function of the project and whether the viewer may edit
it, returning a Panel component. The admin view is not built yet; the field
is part of the interface so a section can ship its tab with it:

```python
import panel_material_ui as pmui


def branding_settings(project, editable):
    branding = project.get("branding") or Branding(logo="")
    logo = pmui.TextInput(label="Logo", value=branding.logo, disabled=not editable)
    save = pmui.Button(label="Save", disabled=not editable)
    save.on_click(lambda event: project.set("branding", Branding(logo.value, branding.accent)))
    return pmui.Column(logo, save)


BRANDING = ProjectSection(
    "branding",
    load_branding,
    title="Branding",
    icon="palette",
    dump=dump_branding,
    settings_view=branding_settings,
)
```
