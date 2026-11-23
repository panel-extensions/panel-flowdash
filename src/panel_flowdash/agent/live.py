"""Operations on the dashboard open in a FlowDashApp session, as its signed-in user."""

from __future__ import annotations

import asyncio
import typing as t

from .ops import DashboardOps, OperationError

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

# How long opening a dashboard waits for the app to have loaded it.
_OPEN_TIMEOUT = 10


def share_key(name: str) -> tuple[str, str]:
    """Return the name and kind of a user name or ``group:<name>``, refusing empty names."""
    if not isinstance(name, str):
        raise OperationError(f"{name!r} is not a user or group name.")
    kind = "user"
    if name.startswith("group:"):
        name, kind = name[6:], "group"
    if not name.strip() or name != name.strip():
        raise OperationError(
            f"{name!r} is not a {kind} name; name users as they log in and groups as "
            '"group:<name>".'
        )
    return name, kind


class LiveDashboardOps(DashboardOps):
    """:class:`DashboardOps` on the editor of a running :class:`~panel_flowdash.app.FlowDashApp`.

    Changes show on the user's canvas as they are made. The first change to a
    dashboard shown in view mode switches the app to edit mode, so the user
    sees the change, the Save and Discard actions and the unsaved-changes
    review before leaving. Opening, deleting and sharing go through the app,
    with its checks of the signed-in user's rights.
    """

    def __init__(self, app: FlowDashApp):
        super().__init__(app._editor)
        self.app = app

    def _require_edit(self):
        app = self.app
        current = app._current_dashboard
        if current is None:
            raise OperationError("No dashboard is open; open one with open_dashboard first.")
        if not self.editor.editable and app._can_edit_dashboard(current):
            app._show_edit_mode()
        super()._require_edit()

    async def open_dashboard_async(self, dashboard_id: str) -> str:
        """Show a stored dashboard in the app, in edit mode when the user may edit it.

        Refused while the open dashboard has unsaved changes, which only the
        user may save or discard.
        """
        app = self.app
        model = self._stored(dashboard_id)
        if app._has_unsaved_changes():
            raise OperationError(
                "The open dashboard has unsaved changes. Ask the user to save or discard "
                "them before opening another dashboard."
            )
        from panel_flowdash.app import DASH_ROUTE_PREFIX

        edit = app._can_edit_dashboard(model)
        app._navigate_to(f"{DASH_ROUTE_PREFIX}{dashboard_id}", "?edit=true" if edit else "")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _OPEN_TIMEOUT
        while loop.time() < deadline:
            current = self.editor.dashboard
            if current is not None and current.dashboard_id == dashboard_id:
                break
            await asyncio.sleep(0.05)
        else:
            raise OperationError(f"'{model.title}' did not open in time; try again.")
        if not self._can_read_source(model):
            return f"Opened '{model.title}' read-only; its definition is hidden from viewers."
        return self.get_dashboard()

    def open_dashboard(self, dashboard_id: str, *, discard: bool = False) -> str:
        """Load a stored dashboard into the app's editor without navigating; see :meth:`open_dashboard_async`."""
        if self.app._has_unsaved_changes() and not discard:
            raise OperationError(
                "The open dashboard has unsaved changes. Ask the user to save or discard "
                "them before opening another dashboard."
            )
        self._stored(dashboard_id)
        self.app._load_dashboard_sync(dashboard_id, edit=True)
        return self.get_dashboard()

    def save(self) -> str:
        """Save the open dashboard through the app, as its Save action does."""
        self._require_edit()
        if self.app._current_dashboard is None:
            raise OperationError("No dashboard is open.")
        if not self.app._save_current_dashboard():
            raise PermissionError("The dashboard was not saved; the user was told why.")
        return self.app._current_dashboard.dashboard_id

    def delete_dashboard(self, dashboard_id: str) -> None:
        """Delete a stored dashboard; only its administrators may."""
        model = self._stored(dashboard_id)
        if not self.app._can_administer_dashboard(model):
            raise PermissionError("Only the dashboard's owner or an administrator may delete it.")
        if not self.app._delete_dashboard(dashboard_id):
            raise OperationError("The dashboard could not be deleted.")

    def share_dashboard(
        self,
        dashboard_id: str,
        *,
        add_viewers: t.Collection[str] = (),
        add_editors: t.Collection[str] = (),
        remove: t.Collection[str] = (),
        unblock: t.Collection[str] = (),
        everyone: bool | None = None,
    ) -> dict[str, t.Any]:
        """Change who a dashboard is shared with, as the Sharing settings do, and return the result.

        Names are user names, or ``group:<name>`` for a group. Everyone not
        named keeps their access, and the dashboard stays private (only the
        people listed) unless *everyone* is True. Only the dashboard's
        administrators may change its sharing.

        Parameters
        ----------
        add_viewers, add_editors
            People and groups to give view or edit access.
        remove
            People and groups to take off the viewers and editors. A blocked
            name stays blocked.
        unblock
            Blocked people and groups to take off the list, which may let them
            view the dashboard again.
        everyone
            True lets everyone who may use the app view the dashboard, False
            limits it to the people listed, None keeps it as it is.

        Returns
        -------
        dict
            The resulting access: ``everyone``, ``viewers``, ``editors`` and ``blocked``.
        """
        from panel_flowdash.ui.settings_dialog import (
            ShareEntry,
            entries_from_permission,
            permission_from_entries,
        )

        for name in (*add_viewers, *add_editors, *remove, *unblock):
            share_key(name)
        model = self._stored(dashboard_id)
        app = self.app
        if not app._can_administer_dashboard(model):
            raise PermissionError("Only the dashboard's owner or an administrator may share it.")
        entries, current_everyone = entries_from_permission(model.permission, model.owner)

        roles = {(e.name, e.kind): e.role for e in entries}
        for name in remove:
            if roles.get(share_key(name)) in ("view", "edit"):
                del roles[share_key(name)]
        for name in unblock:
            if roles.get(share_key(name)) == "blocked":
                del roles[share_key(name)]
        for names, role in ((add_viewers, "view"), (add_editors, "edit")):
            for name in names:
                if roles.get(share_key(name)) != "blocked":
                    roles[share_key(name)] = role
        if everyone is None:
            everyone = current_everyone
        everyone = bool(everyone) and app._default_allow()
        permission = permission_from_entries(
            [ShareEntry(name, kind, role) for (name, kind), role in roles.items()],
            everyone=everyone,
            owner=model.owner,
        )
        if not app._save_sharing(dashboard_id, permission):
            raise OperationError("Sharing could not be updated.")
        return self.sharing(dashboard_id)

    def sharing(self, dashboard_id: str) -> dict[str, t.Any]:
        """Describe who may view and edit a stored dashboard."""
        from panel_flowdash.ui.settings_dialog import entries_from_permission

        model = self._stored(dashboard_id)
        entries, everyone = entries_from_permission(model.permission, model.owner)

        def named(role):
            return sorted(
                f"group:{e.name}" if e.kind == "group" else e.name
                for e in entries
                if e.role == role
            )

        return {
            "everyone": everyone and self.app._default_allow(),
            "owner": model.owner,
            "viewers": named("view"),
            "editors": named("edit"),
            "blocked": named("blocked"),
        }
