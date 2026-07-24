"""Per-page and per-dashboard authorization.

The module defines a small, self-contained authorization model shared by both
code-authored pages (permissions declared on the ``@register`` decorator) and
runtime-created dashboards (permissions persisted in the store):

- :class:`Identity` — the resolved principal for a session (OAuth user, system
  user, resolved ``user`` and the set of ``groups`` it belongs to).
- :class:`Permission` — an allow/deny rule set keyed on users and groups.
- :class:`AuthConfig` — project-level configuration controlling how groups are
  discovered and which groups may administer any dashboard.
- :func:`resolve_identity` — builds an :class:`Identity` from the live Panel
  session plus the ``AuthConfig``.
- :func:`is_authorized` — evaluates a :class:`Permission` against an
  :class:`Identity`.

The same :func:`is_authorized` evaluator serves pages, dashboards and (in the
future) components, so the semantics only ever live in one place.
"""

from __future__ import annotations

import getpass
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import panel as pn

if TYPE_CHECKING:
    from panel_flowdash.registry import RegistryEntry

logger = logging.getLogger("panel_flowdash")

ANONYMOUS_USER = "anonymous"

# Claim keys commonly used by IdPs to carry group/role membership. Projects can
# extend or replace this via ``AuthConfig.group_claims``.
DEFAULT_GROUP_CLAIMS: tuple[str, ...] = ("groups", "roles")


@dataclass(frozen=True)
class Permission:
    """An allow/deny rule set evaluated against an :class:`Identity`.

    All four fields match either the resolved ``user`` (OAuth login *or* system
    user) or one of the identity's ``groups``. An empty ``Permission`` declares
    no constraints and defers entirely to the caller's default policy.
    """

    allow_users: frozenset[str] = field(default_factory=frozenset)
    allow_groups: frozenset[str] = field(default_factory=frozenset)
    deny_users: frozenset[str] = field(default_factory=frozenset)
    deny_groups: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        """Whether the permission declares no allow or deny rules."""
        return not (self.allow_users or self.allow_groups or self.deny_users or self.deny_groups)

    @classmethod
    def from_spec(
        cls,
        *,
        allow_users: Iterable[str] | None = None,
        allow_groups: Iterable[str] | None = None,
        deny_users: Iterable[str] | None = None,
        deny_groups: Iterable[str] | None = None,
    ) -> Permission:
        """Build a :class:`Permission` from loosely-typed iterables."""
        return cls(
            allow_users=frozenset(allow_users or ()),
            allow_groups=frozenset(allow_groups or ()),
            deny_users=frozenset(deny_users or ()),
            deny_groups=frozenset(deny_groups or ()),
        )

    def to_dict(self) -> dict[str, list[str]]:
        """Serialize to sorted lists for JSON persistence."""
        return {
            "allow_users": sorted(self.allow_users),
            "allow_groups": sorted(self.allow_groups),
            "deny_users": sorted(self.deny_users),
            "deny_groups": sorted(self.deny_groups),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Permission:
        """Deserialize from a (possibly ``None`` or partial) mapping."""
        data = data or {}
        return cls.from_spec(
            allow_users=data.get("allow_users"),
            allow_groups=data.get("allow_groups"),
            deny_users=data.get("deny_users"),
            deny_groups=data.get("deny_groups"),
        )


@dataclass(frozen=True)
class Identity:
    """The resolved principal for a session."""

    user: str
    oauth_user: str | None = None
    system_user: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)
    user_info: dict[str, Any] = field(default_factory=dict)

    @property
    def user_names(self) -> frozenset[str]:
        """All names this identity may be referenced by in a rule."""
        names = {self.user}
        if self.oauth_user:
            names.add(self.oauth_user)
        if self.system_user:
            names.add(self.system_user)
        return frozenset(names)

    def in_groups(self, groups: Iterable[str]) -> bool:
        """Whether the identity belongs to any of *groups*."""
        return bool(self.groups & frozenset(groups))

    def is_user(self, users: Iterable[str]) -> bool:
        """Whether the identity matches any of *users* (OAuth or system name)."""
        return bool(self.user_names & frozenset(users))


@dataclass(frozen=True)
class AuthConfig:
    """Project-level authorization configuration.

    Loaded from the project's ``__init__.py`` by the serve command. All fields
    are optional; the defaults reproduce the pre-auth behavior (allow by
    default, groups read from the standard claim keys, no admin groups).
    """

    group_claims: tuple[str, ...] = DEFAULT_GROUP_CLAIMS
    user_groups: dict[str, frozenset[str]] = field(default_factory=dict)
    resolve_groups: Callable[[Identity], Iterable[str]] | None = None
    admin_groups: frozenset[str] = field(default_factory=frozenset)
    default_allow: bool = True

    @classmethod
    def from_module(cls, module: Any) -> AuthConfig:
        """Build an :class:`AuthConfig` from names on a project ``__init__``.

        Reads ``group_claims``, ``user_groups``, ``resolve_groups``,
        ``admin_groups`` and ``default_allow`` if present, falling back to the
        defaults otherwise. Missing module or names yield a default config.
        """
        if module is None:
            return cls()
        group_claims = getattr(module, "group_claims", None)
        raw_user_groups = getattr(module, "user_groups", None) or {}
        user_groups = {user: frozenset(groups) for user, groups in raw_user_groups.items()}
        admin_groups = getattr(module, "admin_groups", None) or ()
        default_allow = getattr(module, "default_allow", True)
        return cls(
            group_claims=tuple(group_claims) if group_claims else DEFAULT_GROUP_CLAIMS,
            user_groups=user_groups,
            resolve_groups=getattr(module, "resolve_groups", None),
            admin_groups=frozenset(admin_groups),
            default_allow=bool(default_allow),
        )


def _system_user() -> str | None:
    """Return the OS account running the server, or ``None`` if undetermined."""
    try:
        return getpass.getuser()
    except Exception:
        return None


def _groups_from_claims(user_info: dict[str, Any], group_claims: Iterable[str]) -> set[str]:
    """Extract group names from the user_info claims dict.

    A claim value may be a list of strings or a single comma/space-delimited
    string; both shapes are normalized to a set of names.
    """
    groups: set[str] = set()
    for claim in group_claims:
        value = user_info.get(claim)
        if value is None:
            continue
        if isinstance(value, str):
            groups.update(part for part in value.replace(",", " ").split() if part)
        elif isinstance(value, Iterable):
            groups.update(str(v) for v in value if v)
    return groups


def resolve_identity(auth_config: AuthConfig | None = None) -> Identity:
    """Resolve the current session's :class:`Identity`.

    Prefers the OAuth login (``pn.state.user``) when a real provider populated
    it; otherwise falls back to the system user, then to ``"anonymous"``.
    Groups are the union of claim-derived groups, the static ``user_groups``
    mapping and any dynamic ``resolve_groups`` callback.
    """
    auth_config = auth_config or AuthConfig()

    oauth_user = pn.state.user or None
    # Panel sets pn.state.user to a non-None placeholder when no provider is
    # configured; treat the well-known anonymous sentinel as "no OAuth user".
    if oauth_user in (ANONYMOUS_USER, "anonymous"):
        oauth_user = None

    system_user = _system_user()
    user = oauth_user or system_user or ANONYMOUS_USER

    user_info = dict(pn.state.user_info or {})
    groups = _groups_from_claims(user_info, auth_config.group_claims)

    for name in (user, oauth_user, system_user):
        if name and name in auth_config.user_groups:
            groups |= set(auth_config.user_groups[name])

    identity = Identity(
        user=user,
        oauth_user=oauth_user,
        system_user=system_user,
        groups=frozenset(groups),
        user_info=user_info,
    )

    if auth_config.resolve_groups is not None:
        try:
            extra = auth_config.resolve_groups(identity)
        except Exception:
            logger.exception("resolve_groups callback failed")
            extra = None
        if extra:
            groups |= set(extra)
            identity = Identity(
                user=user,
                oauth_user=oauth_user,
                system_user=system_user,
                groups=frozenset(groups),
                user_info=user_info,
            )

    return identity


def is_authorized(
    permission: Permission | None,
    identity: Identity,
    *,
    default_allow: bool = True,
    owner: str | None = None,
) -> bool:
    """Evaluate *permission* against *identity*.

    Order of precedence:

    1. A matching ``deny_users``/``deny_groups`` rule denies access (deny always
       wins, even for the owner).
    2. The *owner*, if given and matching, is allowed.
    3. Any ``allow_*`` rule present: allowed iff the identity matches at least
       one of them.
    4. No allow/deny rules at all: fall back to *default_allow*.
    """
    if permission is None:
        permission = Permission()

    if permission.deny_users and identity.is_user(permission.deny_users):
        return False
    if permission.deny_groups and identity.in_groups(permission.deny_groups):
        return False

    if owner is not None and owner in identity.user_names:
        return True

    if permission.allow_users or permission.allow_groups:
        if permission.allow_users and identity.is_user(permission.allow_users):
            return True
        if permission.allow_groups and identity.in_groups(permission.allow_groups):
            return True
        return False

    return default_allow


def can_administer(identity: Identity, owner: str, admin_groups: Iterable[str]) -> bool:
    """Whether *identity* may administer a resource owned by *owner*.

    When no *admin_groups* are configured administration is unrestricted: the
    running user (however resolved) may administer any resource. This keeps the
    default, auth-less deployment fully editable. Once *admin_groups* are set,
    only the owner and members of those groups may administer a resource.
    """
    admin_groups = frozenset(admin_groups)
    if not admin_groups:
        return True
    if owner in identity.user_names:
        return True
    return identity.in_groups(admin_groups)


def path_permission_lookup(
    registry: dict[str, RegistryEntry],
) -> dict[str, Permission]:
    """Map each page's URL path to its declared :class:`Permission`.

    Used by the HTTP-boundary authorize callback to gate direct URL access to
    code-authored pages. Only page entries are included; component and
    SPA-only routes are gated in-app.
    """
    lookup: dict[str, Permission] = {}
    for entry in registry.values():
        if not entry.metadata.page:
            continue
        lookup[entry.page_path] = entry.metadata.permission
    return lookup


def make_authorize_callback(
    registry: dict[str, RegistryEntry],
    auth_config: AuthConfig,
    *,
    url_prefix: str = "",
) -> Callable[[dict[str, Any] | None, str], bool]:
    """Build a Panel ``config.authorize_callback`` gating page routes.

    The returned callback authorizes direct HTTP access to code-authored page
    URLs against the resolved identity. Non-page routes (``/components``,
    ``/dash/...`` and unknown paths) always pass here and are gated in-app,
    where per-dashboard permissions and richer denied views live.

    ``url_prefix`` strips a server route prefix from the request path before
    matching against registry page paths.
    """
    lookup = path_permission_lookup(registry)

    def authorize(user_info: dict[str, Any] | None, path: str = "") -> bool:
        route = path or ""
        if url_prefix and route.startswith(url_prefix):
            route = route[len(url_prefix) :]
        route = "/" + route.strip("/") if route.strip("/") else "/"
        permission = lookup.get(route)
        if permission is None:
            # Not a code-authored page route; gated in-app if at all.
            return True
        identity = resolve_identity(auth_config)
        return is_authorized(permission, identity, default_allow=auth_config.default_allow)

    return authorize
