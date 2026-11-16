# Manage Permissions

FlowDash can restrict who sees each page and each saved dashboard. Permissions
are evaluated against a resolved *identity* and expressed as allow/deny rules on
users and groups. The same evaluator drives both code-authored pages (declared
on the `@register` decorator) and runtime-created dashboards (configured from the
editor), so the semantics only ever live in one place.

By default nothing is restricted: a project with no rules behaves exactly as it
did before permissions existed. You opt in page by page, dashboard by dashboard.

---

## How identity is resolved

Every session resolves to a single `Identity` made up of:

- **OAuth user** — `pn.state.user`, populated when an OAuth provider is
  configured. This is preferred when present.
- **System user** — the OS account running the server (`getpass.getuser()`),
  used only when there is no OAuth login. This is what lets a local,
  auth-less deployment still key rules to a real user. A signed-in OAuth user
  never also carries the server account's name, so they do not inherit its
  dashboards or `user_groups` entry.
- **Groups** — the union of three sources: group claims read from
  `pn.state.user_info` (by default the `groups` and `roles` claims), the static
  `user_groups` entry for the resolved user, and an optional dynamic
  `resolve_groups` callback.

When no OAuth login and no system user can be determined, the identity falls
back to the `anonymous` user with no groups.

A rule that names a user matches the resolved user: the OAuth login when there
is one, otherwise the system user name. A rule that names a group matches any of the identity's resolved groups.

---

## Rules: allow and deny

Both pages and dashboards share the same four rule fields (dashboards add two
more for editors, see [Sharing edit rights](#sharing-edit-rights)):

| Field | Matches |
|-------|---------|
| `allow_users` | OAuth login or system user name |
| `allow_groups` | any of the identity's groups |
| `deny_users` | OAuth login or system user name |
| `deny_groups` | any of the identity's groups |

Evaluation order:

1. **Deny wins.** If the identity matches any `deny_users` or `deny_groups`
   rule, access is refused, even for the resource owner.
2. **Owner is allowed.** For dashboards, the creating user always retains
   access (unless denied above).
3. **Editors are allowed.** For dashboards, anyone granted edit rights may also
   view it.
4. **Allow rules.** If any `allow_*` rule is present, the identity must match at
   least one of them.
5. **Default policy.** With no allow or deny rules at all, the project's default
   policy applies (allow by default).

---

## Restricting pages

Declare rules directly on the `@register` decorator. Literal rules are picked up
statically from the source (no import needed), so a restricted page is gated
even before its module is loaded. Rules given as expressions (a module-level
constant such as `allow_groups=ADMINS`, or an `authorize` callable) cannot be
read without importing the module, so such a page is denied until its module
has been imported and the decorator supplies the real rules. `flowdash serve`
imports these modules at startup, and the app imports any still pending in the
background before showing the launcher and navigation, so listings never block
on an import. A module that fails to import stays denied and is not imported
again until the server restarts. A literal rule that is not a list of names,
such as `allow_groups=5`, is logged as an error and the page is denied; the
rest of the project still loads.

```python
from panel_flowdash import register

@register(
    page=True,
    title="Revenue",
    allow_groups=["finance", "execs"],
    deny_users=["contractor-bob"],
)
def app():
    ...
```

This page is visible to members of `finance` or `execs`, except for
`contractor-bob`, who is denied regardless of group membership.

| Parameter | Type | Description |
|-----------|------|-------------|
| `allow_users` | `list[str]` | Users granted access. |
| `allow_groups` | `list[str]` | Groups granted access. |
| `deny_users` | `list[str]` | Users refused access (deny wins). |
| `deny_groups` | `list[str]` | Groups refused access (deny wins). |
| `authorize` | `callable` | Custom check taking the resolved `Identity` and returning a bool. Overrides the rule fields. |

Use `authorize` when a static rule set is not enough:

```python
@register(page=True, title="Beta Feature", authorize=lambda identity: "beta" in identity.groups)
def app():
    ...
```

Restricted pages are enforced at two points. Direct navigation to the page URL
is checked at the HTTP boundary, so an unauthorized user cannot reach it even by
typing the address. In-app navigation applies the same check and hides pages the
identity may not see from the launcher and navigation menu.

---

## Restricting dashboards

Saved dashboards start out accessible only to their owner (the user who created
them). To share one, open it in the editor and click **Share** in the
editor's side panel next to *Save*. The button is only shown when you are allowed to
administer that dashboard.

The share dialog exposes the same four rule fields as allow/deny lists of groups
and users. Add entries by group or by user; deny always wins. With no rules the
project default applies, so a dashboard shared with no rules under the default
allow policy is visible to everyone, while under a default-deny policy it stays
private to its owner.

Sharing changes the read model from "my dashboards" to "dashboards I own plus
dashboards shared with me": the launcher and navigation menu list every
dashboard the current identity is authorized to open.

### Sharing edit rights

The share dialog also has **Editor groups** and **Editor users**, stored as
`allow_edit_groups` and `allow_edit_users` on the dashboard's `Permission`. An
editor may:

- view the dashboard, even when the view rules do not name them;
- open it in edit mode (the **Edit** action, or an `?edit=true` link), change
  tiles, wiring and the dashboard settings, and save.

An editor may not rename, delete or re-share the dashboard; those stay with its
administrators (below), so an editor cannot lock the owner out or hand the
dashboard to someone else. Deny rules win over edit rights too. Edit rules only
add editors, they do not restrict who else may view: a dashboard with editors
and no view rules still follows the project default policy for everyone else.

```python
from panel_flowdash import Permission

store.set_permission(
    dashboard_id,
    Permission.from_spec(allow_groups=["sales"], allow_edit_groups=["sales-leads"]),
)
```

Editors can always [inspect tile outputs](embed-the-editor.md#inspecting-outputs)
when the editor has `inspect_outputs` on; people who may only view need the
dashboard's **Viewers can inspect outputs** setting.

---

## Who can administer a dashboard

Renaming, deleting and re-sharing a dashboard are *administration* actions,
distinct from viewing and editing it. Only the dashboard's owner and members of
the configured **admin groups** (see below) may administer it, and they may
always edit it too. Without admin groups
only the owner can. In a local, auth-less deployment every session resolves to
the same system user, who owns every dashboard it creates, so that deployment
stays fully editable.

Dashboards created before OAuth was enabled are owned by the system user; add
an admin group to keep administering them once users sign in. A user with view-only access who
follows an edit link is silently downgraded to view mode, and the edit-only
controls are hidden. Editors get edit mode but not the share button or the
rename and delete actions.

---

## Project-level configuration

Group discovery and the default policy are configured with module-level names in
the project's `__init__.py`. The serve command reads them at startup. All are
optional; omitting them reproduces the default behavior.

```python
# my_project/__init__.py

# Extra claim keys to read group membership from (defaults to groups and roles).
group_claims = ["cognito:groups"]

# Static user-to-group mapping, unioned with claim- and callback-derived groups.
user_groups = {
    "alice@example.com": ["finance", "admins"],
    "svc-runner": ["admins"],
}

# Groups whose members may administer any dashboard.
admin_groups = ["admins"]

# Default access policy for resources with no rules. True (allow) is the
# default; set False to make everything private unless explicitly shared.
default_allow = True

# Optional callback for dynamic group resolution. Receives the partially
# resolved Identity and returns extra group names.
def resolve_groups(identity):
    if identity.user.endswith("@example.com"):
        return ["employees"]
    return []
```

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `group_claims` | `list[str]` | `["groups", "roles"]` | `user_info` claim keys to read groups from. Values may be lists or comma/space-separated strings. |
| `user_groups` | `dict[str, list[str]]` | `{}` | Static user-to-group mapping. |
| `admin_groups` | `list[str]` | `[]` | Groups that may administer any dashboard. Empty means only owners administer their dashboards. |
| `default_allow` | `bool` | `True` | Access policy for resources with no rules. Must be a bool (or the string `"true"`/`"false"`). |
| `resolve_groups` | `callable` | `None` | Dynamic group resolution given the `Identity`. |

A single string where a list is expected (`admin_groups = "admins"`) is read as
one name.

---

## Choosing a default policy

The `default_allow` flag decides what happens for a page or dashboard that
declares no rules:

- **Allow (default).** Unrestricted resources are open to everyone; add rules
  only where you need to lock something down. Best when most content is public
  and a few pages are sensitive.
- **Deny.** Unrestricted resources are hidden unless a rule grants access
  (dashboard owners always keep access to their own). Best when content is
  private by default and sharing is explicit.

Set it once in `__init__.py`:

```python
default_allow = False
```

---

## Recipes

**Lock down a page to one group**

```python
@register(page=True, title="Board Pack", allow_groups=["board"])
def app():
    ...
```

**Publish a page to everyone except a group**

```python
@register(page=True, title="Internal Roadmap", deny_groups=["contractors"])
def app():
    ...
```

**Private-by-default project with an admin team**

```python
# __init__.py
admin_groups = ["platform-admins"]
default_allow = False
```

Nothing is visible until a page declares `allow_*` rules or a dashboard is
explicitly shared, and only owners and `platform-admins` can administer
dashboards.
