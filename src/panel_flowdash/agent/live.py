"""Operations on the dashboard open in a FlowDashApp session, as its signed-in user."""

from __future__ import annotations

import asyncio
import typing as t

from panel_flowdash.auth import Permission

from .ops import DashboardOps, OperationError

if t.TYPE_CHECKING:
    from panel_flowdash.app import FlowDashApp

# How long opening a dashboard waits for the app to have loaded it.
_OPEN_TIMEOUT = 10


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

    def open_dashboard(self, dashboard_id: str) -> str:
        """Load a stored dashboard into the app's editor without navigating; see :meth:`open_dashboard_async`."""
        if self.app._has_unsaved_changes():
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
        view_users: t.Collection[str] = (),
        view_groups: t.Collection[str] = (),
        edit_users: t.Collection[str] = (),
        edit_groups: t.Collection[str] = (),
    ) -> None:
        """Replace who a dashboard is shared with; only its administrators may.

        With no viewers and editors given, the dashboard is shared as the
        app's default access policy decides.
        """
        model = self._stored(dashboard_id)
        if not self.app._can_administer_dashboard(model):
            raise PermissionError("Only the dashboard's owner or an administrator may share it.")
        permission = Permission.from_spec(
            allow_users=view_users,
            allow_groups=view_groups,
            deny_users=model.permission.deny_users,
            deny_groups=model.permission.deny_groups,
            allow_edit_users=edit_users,
            allow_edit_groups=edit_groups,
        )
        if not self.app._save_sharing(dashboard_id, permission):
            raise OperationError("Sharing could not be updated.")
