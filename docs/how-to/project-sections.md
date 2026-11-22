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
section's key. Each `Problem` carries the file, line and column, and the
`section` and `keys` of the value, e.g. `("branding", ("logo",))`, so a
settings form can show it next to its field.

A problem that should not stop the app, such as an entry that refers to
something the app does not have, is reported with `section.warn(path,
message)`: it is logged with its position and kept in `project.warnings`,
and the loader carries on.

### The loader's context

`section.context` holds what the app loaded the project with. `flowdash
serve` and `FlowDashApp` pass `project_dir` and `registry` (the component
registry). An app that knows more, such as its data sources, loads the file
itself with `load_project(path, context={...})` and passes the `Project` to
`FlowDashApp(project=...)`.

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

`Project.set(name, value)` passes the value through the section's `dump`,
runs the loader on the result and, if the loader accepts it, writes the
section to the file. A value the loader rejects raises `ProjectError`,
positioned where it would have been written, and the file is left as it
was. Only the lines of this section change: the other sections keep their
formatting and comments, while comments inside the written section are not
kept. Text that an older YAML reader would take for something else, such as
`on`, `no` or `1:20`, is quoted.

The file is replaced atomically, with its permissions kept, and through a
symlink the target is updated. Writes from one process are serialized and
each rewrites only its own section, so concurrent `set` calls for different
sections all land.

---

## Secrets

Never write a secret into the project file. A section names an environment
variable instead, as `${NAME}` or `${NAME:-default}`, and the loader keeps
that reference in its value, expanding it with
`panel_flowdash.project.expand_env` only where it is used. `dump` then
writes the reference back, so saving the section from the admin never puts
the resolved value in the file:

```python
import dataclasses

from panel_flowdash.project import expand_env


@dataclasses.dataclass
class AgentSettings:
    api_key: str = "${OPENROUTER_API_KEY}"

    @property
    def resolved_api_key(self):
        return expand_env(self.api_key)


AGENT = ProjectSection(
    "agent",
    lambda section: AgentSettings(**section.value),
    dump=dataclasses.asdict,
)
```

---

## 5. An admin settings tab

A section can also describe its page in the admin view with
`settings_view`, a function of the project and whether the viewer may edit
it. Every registered section with a `settings_view` is listed in the admin
view, after the built-in General and Theme. The simplest view is any Panel
component that saves on its own:

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

A view that returns a `panel_flowdash.admin.SettingsForm` gets the admin
view's Save and Discard and its unsaved-changes dot instead. The form is
described by groups of `Setting`s, each a key of the section's mapping;
Save writes the changed keys with `Project.set`, and the problems of a
value the loader rejects are shown next to the fields their `keys` name.
`settings_loader(groups)` checks a section against the same settings:

```python
from panel_flowdash.admin import Group, Setting, SettingsForm, settings_loader

LIMIT_GROUPS = [
    Group(
        "Queries",
        [
            Setting("rows", "Row limit", "Rows a query may return.", kind="number",
                    integer=True, bounds=(1, None), default=10_000),
            Setting("timeout", "Timeout", kind="number", unit="Seconds", default=30),
        ],
    )
]

LIMITS = ProjectSection(
    "limits",
    settings_loader(LIMIT_GROUPS),
    title="Limits",
    icon="speed",
    settings_view=lambda project, editable: SettingsForm(
        project, "limits", LIMIT_GROUPS, editable=editable
    ),
)
```

A setting whose value comes from somewhere the admin cannot change, such as
a command-line flag, is shown locked when the loader's context lists it:
`load_project(path, context={"locked_settings": {"limits": {"rows": "--rows"}}})`.
