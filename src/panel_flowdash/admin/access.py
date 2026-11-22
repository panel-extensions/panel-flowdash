"""Who may open the admin view: members of the admin groups, or whoever has the admin link.

The admin link bootstraps the first admin. Like Jupyter's token, the
server makes a random admin token when it starts and, while the project
has no admin yet, prints a link with it, ``/admin#token=...``. The token
rides in the URL fragment, which browsers never send to the server, so it
shows up in no access log; the page hands it over the websocket instead.
Whoever opens the link is recorded in the project file's
``general.admins`` and is admin on every later start, with no token.
"""

from __future__ import annotations

import hmac
import os
import secrets
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


def is_admin(
    identity: Identity,
    admin_groups: t.Iterable[str],
    admins: t.Iterable[str] = (),
    *,
    token: bool = False,
) -> bool:
    """Whether *identity* may open the admin view.

    Members of *admin_groups* may, and so may the users *admins* names (the
    project file's ``general.admins``) and a session that presented the
    admin token (*token*).
    """
    return token or identity.in_groups(admin_groups) or identity.user in set(admins)


class AdminTokenExchange(JSComponent):
    """Hands the admin token in the page's URL fragment to the server.

    The fragment is removed from the address bar as soon as it is read.
    """

    token = param.String(default="", doc="The token the page's URL fragment carried.")

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
