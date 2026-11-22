"""Who may open the admin view."""

from __future__ import annotations

import ipaddress
import typing as t

import panel as pn

from panel_flowdash.auth import Identity

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[1 : host.find("]")] if "]" in host else host[1:]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _loopback(address: str | None) -> bool:
    if not address:
        return False
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


# Headers a proxy adds; a request carrying any of them came through one.
PROXY_HEADERS = (
    "Forwarded",
    "Via",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-Ip",
)


def _peer_address(request: t.Any) -> str | None:
    """Return the address of the socket the request came in on, ignoring any xheaders."""
    raw = getattr(request, "_request", request)
    context = getattr(getattr(raw, "connection", None), "context", None)
    address = getattr(context, "address", None)
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    if context is not None and hasattr(context, "_orig_remote_ip"):
        return context._orig_remote_ip
    return getattr(raw, "remote_ip", None)


def is_local_request(request: t.Any = None) -> bool:
    """Whether the session's page was requested from this computer by its loopback name.

    The socket's peer address and the ``Host`` header have to be loopback,
    and the request must carry none of the `PROXY_HEADERS`; the peer is read
    from the socket, never from ``X-Real-Ip`` or ``X-Forwarded-For``. A proxy
    on the same machine that adds no such header still looks local, so a
    deployment behind a proxy sets ``FlowDashApp(local_admin=False)``.
    """
    if request is None:
        doc = pn.state.curdoc
        context = doc.session_context if doc is not None else None
        request = getattr(context, "request", None)
    if request is None:
        return False
    headers = {str(k).lower(): v for k, v in dict(getattr(request, "headers", None) or {}).items()}
    if any(name.lower() in headers for name in PROXY_HEADERS):
        return False
    host = headers.get("host") or ""
    return _loopback(_peer_address(request)) and _host_name(host) in _LOCAL_HOSTS


def is_admin(
    identity: Identity,
    admin_groups: t.Iterable[str],
    *,
    local: bool = False,
) -> bool:
    """Whether *identity* may open the admin view.

    Members of *admin_groups* may. Without sign-in, everyone is the system
    user, so the admin view is only offered to a page opened on this
    computer (*local*).
    """
    if identity.in_groups(admin_groups):
        return True
    return identity.oauth_user is None and local
