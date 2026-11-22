"""The admin view: the project file's sections as settings, for the app's administrators.

Every project file section registered with a ``settings_view`` is a section
of the admin view. ``general`` and ``theme`` are built in.
"""

from panel_flowdash.admin.access import is_admin, is_local_request
from panel_flowdash.admin.form import Group, Setting, SettingsForm, current_app, settings_loader
from panel_flowdash.admin.page import ADMIN_ROUTE, AdminPage, admin_sections
from panel_flowdash.admin.sections import GENERAL, THEME, project_overrides

__all__ = [
    "ADMIN_ROUTE",
    "GENERAL",
    "THEME",
    "AdminPage",
    "Group",
    "Setting",
    "SettingsForm",
    "admin_sections",
    "current_app",
    "is_admin",
    "is_local_request",
    "project_overrides",
    "settings_loader",
]
