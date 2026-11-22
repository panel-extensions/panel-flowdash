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


def is_local_request(request: t.Any = None) -> bool:
    """Whether the session's page was requested from this computer by its loopback name.

    Both the peer address and the ``Host`` header have to be loopback, so a
    page served behind a proxy on the same machine for a public host name
    does not count as local.
    """
    if request is None:
        doc = pn.state.curdoc
        context = doc.session_context if doc is not None else None
        request = getattr(context, "request", None)
    if request is None:
        return False
    remote_ip = getattr(request, "remote_ip", None)
    if remote_ip is None and hasattr(request, "_request"):
        remote_ip = getattr(request._request, "remote_ip", None)
    headers = getattr(request, "headers", None) or {}
    host = headers.get("Host") or headers.get("host") or ""
    return _loopback(remote_ip) and _host_name(host) in _LOCAL_HOSTS


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
