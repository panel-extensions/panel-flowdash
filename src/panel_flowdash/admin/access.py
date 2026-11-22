"""Who may open the admin view: members of the admin groups, or whoever has the admin link.

The admin link bootstraps the first admin. Like Jupyter's token, the
server makes a random admin token when it starts and, while the project
has no admin yet, prints a link with it, ``/admin#token=...``. The token
rides in the URL fragment, which browsers never send to the server, so it
shows up in no access log; the page hands it over the websocket instead.
Whoever opens the link is recorded in the project file's
``general.admins`` and is admin on every later start, with no token.

Without sign-in, every visitor is the account that runs the server, so
admin rights without sign-in apply only to a browser on that machine: a
session whose client is a loopback address (see `client_address`).
``general.admins`` names a local account as ``local:<name>`` and a
signed-in user as ``auth:<name>``, so one never matches the other; a name
without either prefix, as earlier versions wrote, is a local account.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
import threading
import typing as t

import param
from panel.custom import JSComponent

from panel_flowdash.auth import Identity

TOKEN_ENV = "FLOWDASH_ADMIN_TOKEN"

_TOKEN: str | None = None
_TOKEN_LOCK = threading.Lock()


def admin_token() -> str:
    """Return this process's admin token, made when first asked for.

    It is ``FLOWDASH_ADMIN_TOKEN`` when that is set, for scripted setups,
    and otherwise random and new every time the server starts. Either way
    it claims the admin view once per start (see `consume_token`).
    """
    global _TOKEN
    with _TOKEN_LOCK:
        if _TOKEN is None:
            _TOKEN = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
        return _TOKEN


def reset_admin_token() -> None:
    """Forget the admin token, so the next `admin_token` makes a new one, as a restart does."""
    global _TOKEN, _LINK_SHOWN, _USED, _ADDING
    with _TOKEN_LOCK:
        _TOKEN = None
        _LINK_SHOWN = False
        _USED = False
        _ADDING = False


def admin_path(prefix: str = "") -> str:
    """Return the path of the admin link, with the token in its fragment."""
    return f"{prefix.rstrip('/')}/admin#token={admin_token()}"


_LINK_SHOWN = False
# Whether the token was used: it claims the admin view once.
_USED = False
# Whether the token may add an admin to a project that has some (`flowdash serve --admin-link`).
_ADDING = False


def allow_adding_admins(allowed: bool = True) -> None:
    """Let the admin link add an admin to a project that already has admins, as ``--admin-link`` does."""
    global _ADDING
    with _TOKEN_LOCK:
        _ADDING = allowed


def adding_admins_allowed() -> bool:
    """Whether the admin link may add an admin to a project that already has admins."""
    return _ADDING


def show_admin_link(base_url: str = "") -> None:
    """Print the admin link once per token, e.g. when the server starts.

    It is printed rather than logged, so the token stays out of log files
    that are kept or shipped elsewhere. *base_url* is the server's address;
    without it only the path is printed.
    """
    global _LINK_SHOWN
    if _LINK_SHOWN:
        return
    _LINK_SHOWN = True
    print(  # noqa: T201
        (
            "\nOpen this link to add yourself as an admin of this FlowDash project\n"
            if _ADDING
            else "\nNo one administers this FlowDash project yet. Open this link to become its admin\n"
        )
        + "(it works once, until the server restarts):\n"
        f"    {base_url.rstrip('/')}{admin_path()}\n",
        flush=True,
    )


def check_token(candidate: t.Any) -> bool:
    """Whether *candidate* is the admin token and unused, compared in constant time."""
    if not isinstance(candidate, str) or not candidate:
        return False
    token = admin_token()
    return hmac.compare_digest(candidate.encode(), token.encode()) and not _USED


def consume_token(candidate: t.Any) -> bool:
    """Use the admin token: True the first time *candidate* is the token, False ever after."""
    global _USED
    if not check_token(candidate):
        return False
    with _TOKEN_LOCK:
        if _USED:
            return False
        _USED = True
    return True


LOCAL_PREFIX = "local:"
SIGNED_IN_PREFIX = "auth:"


def admin_entry(identity: Identity) -> str | None:
    """Return how ``general.admins`` names *identity*, or None for an anonymous one."""
    if identity.oauth_user:
        return f"{SIGNED_IN_PREFIX}{identity.oauth_user}"
    if identity.system_user:
        return f"{LOCAL_PREFIX}{identity.system_user}"
    return None


def _split(entry: t.Any) -> tuple[str, str]:
    """Return the kind (``"local"`` or ``"auth"``) and name of a ``general.admins`` entry.

    Prefixes are read in any case and the name is trimmed; an entry without
    a prefix, as earlier versions wrote, is a local account.
    """
    entry = str(entry).strip()
    lowered = entry.lower()
    for kind, prefix in (("local", LOCAL_PREFIX), ("auth", SIGNED_IN_PREFIX)):
        if lowered.startswith(prefix):
            return kind, entry[len(prefix) :].strip()
    return "local", entry


def admin_name(entry: t.Any) -> str:
    """Return the user name of a ``general.admins`` entry, without its prefix."""
    return _split(entry)[1]


def admin_names(entries: t.Iterable[t.Any], signed_in: bool) -> set[str]:
    """Return the names of the *entries* that apply with sign-in, or without it."""
    kind = "auth" if signed_in else "local"
    return {name for k, name in map(_split, entries) if k == kind and name}


def namespaced_entry(name: str, signed_in: bool) -> str:
    """Return *name*, typed by an admin, as a ``general.admins`` entry.

    A name with a prefix keeps it, written in lower case; any other is a
    signed-in user on a server with sign-in and a local account on one
    without.
    """
    lowered = name.strip().lower()
    if lowered.startswith((LOCAL_PREFIX, SIGNED_IN_PREFIX)):
        kind, bare = _split(name)
        return f"{LOCAL_PREFIX if kind == 'local' else SIGNED_IN_PREFIX}{bare}"
    return f"{SIGNED_IN_PREFIX if signed_in else LOCAL_PREFIX}{name.strip()}"


def entry_matches(entry: t.Any, identity: Identity) -> bool:
    """Whether the ``general.admins`` *entry* names *identity*.

    A signed-in user matches only ``auth:`` entries; a session without
    sign-in matches ``local:`` entries and unprefixed ones, which earlier
    versions wrote for the local account.
    """
    kind, name = _split(entry)
    if identity.oauth_user:
        return kind == "auth" and name == identity.oauth_user
    return kind == "local" and bool(identity.system_user) and name == identity.system_user


def is_admin(
    identity: Identity,
    admin_groups: t.Iterable[str],
    admins: t.Iterable[str] = (),
    *,
    token: bool = False,
) -> bool:
    """Whether *identity* may open the admin view.

    Members of *admin_groups* may, and so may the users *admins* names (the
    project file's ``general.admins``, see `entry_matches`) and a session
    that presented the admin token (*token*). Where the session connects
    from is checked by the caller (see `session_is_local`).
    """
    return (
        token
        or identity.in_groups(admin_groups)
        or any(entry_matches(entry, identity) for entry in admins)
    )


# Headers a proxy adds; any ``X-Forwarded-*`` header counts too.
_FORWARD_HEADERS = ("forwarded", "via", "x-real-ip", "cf-connecting-ip", "true-client-ip")


def _proxied(headers: t.Mapping[str, t.Any]) -> bool:
    return any(
        str(key).lower() in _FORWARD_HEADERS or str(key).lower().startswith("x-forwarded-")
        for key in headers
    )


def _real_request(request: t.Any) -> t.Any:
    """Return the HTTP request behind Bokeh's request proxy.

    The proxy's headers and cookies come from the session token's payload,
    which the client can write unless sessions are signed; the request is
    what the server received.
    """
    try:
        from bokeh.server.contexts import _RequestProxy
    except ImportError:
        return request
    return request._request if isinstance(request, _RequestProxy) else request


def _header(headers: t.Mapping[str, t.Any], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return ",".join(value) if isinstance(value, list) else str(value)
    return None


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(text.strip().strip("[]").split("%")[0])
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def socket_peer(request: t.Any) -> str | None:
    """Return the IP address of the socket that sent *request*, or None if it has none.

    It is the TCP peer as Tornado accepted it, which ``--use-xheaders``
    does not rewrite; a Unix socket has no IP address.
    """
    context = getattr(getattr(request, "connection", None), "context", None)
    if getattr(context, "address_family", None) not in (socket.AF_INET, socket.AF_INET6):
        return None
    address = getattr(context, "address", None)
    if not address:
        return None
    return str(address[0])


def client_address(request: t.Any, trusted_proxies: t.Iterable[str] = ()) -> str | None:
    """Return the IP address of the client that sent *request*, or None if unknown.

    It is the socket peer. When that peer is one of *trusted_proxies*, the
    client is the last address in ``X-Forwarded-For`` that is not a trusted
    proxy. A request with forwarding headers from a peer that is not trusted
    has an unknown client, so a reverse proxy on the same machine never
    makes its visitors local. Headers are read from the request the server
    received, never from the session token.
    """
    request = _real_request(request)
    peer = socket_peer(request)
    if peer is None:
        return None
    trusted = {ip for ip in (_ip(p) for p in trusted_proxies) if ip is not None}
    headers = getattr(request, "headers", None) or {}
    peer_ip = _ip(peer)
    if peer_ip is None or peer_ip not in trusted:
        if _proxied(headers):
            return None
        return peer
    hops = [_ip(hop) for hop in (_header(headers, "X-Forwarded-For") or "").split(",")]
    if not hops or any(hop is None for hop in hops):
        return None
    for hop in reversed(hops):
        if hop not in trusted:
            return str(hop)
    return str(hops[0])


def is_loopback(address: str | None) -> bool:
    """Whether *address* is a loopback address, e.g. ``127.0.0.1`` or ``::1``."""
    ip = _ip(address) if address else None
    return ip is not None and ip.is_loopback


def shared_password_sign_in() -> bool:
    """Whether users sign in with Panel's basic auth and one shared password.

    With a shared password any user name is accepted, so the name proves
    nothing and does not count as sign-in for admin rights. Basic auth
    with a mapping (or JSON file) of users and passwords does count.
    """
    import panel as pn
    from panel.io.state import state

    if pn.config.oauth_provider:
        return False
    values = [
        config.get("basic_auth")
        for config in getattr(state, "_server_config", {}).values()
        if isinstance(config, dict)
    ]
    values.append(os.environ.get("PANEL_BASIC_AUTH") or getattr(pn.config, "_basic_auth", None))
    for value in values:
        if not value or isinstance(value, dict):
            continue
        if isinstance(value, str) and os.path.isfile(value):
            continue
        return True
    return False


def session_is_local(doc: t.Any, trusted_proxies: t.Iterable[str] = ()) -> bool:
    """Whether the session of Bokeh document *doc* comes from this machine.

    A document without a server session (a script, a notebook or a test)
    is local. A served session is local when the client of the request that
    opened it is a loopback address (see `client_address`).
    """
    context = getattr(doc, "session_context", None) if doc is not None else None
    if context is None:
        return True
    try:
        request = context.request
    except Exception:
        return False
    return is_loopback(client_address(request, trusted_proxies))


class AdminTokenExchange(JSComponent):
    """Hands the admin token in the page's URL fragment to the server.

    The fragment is removed from the address bar as soon as it is read, and
    the token is left out of Panel's log of the values the page sends.
    """

    token = param.String(default="", doc="The token the page's URL fragment carried.")

    def _log(self, msg: str, *args, level: str = "debug") -> None:
        super()._log(msg, *("<redacted>" for _ in args), level=level)

    _esm = """
    export function render({ model }) {
      const match = window.location.hash.match(/(?:^#|&)token=([^&]+)/)
      if (match) {
        const url = window.location.pathname + window.location.search
        window.history.replaceState(window.history.state, "", url)
        model.token = decodeURIComponent(match[1])
      }
      return document.createElement("span")
    }
    """
