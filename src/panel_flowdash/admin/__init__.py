"""The admin view: the project file's sections as settings, for the app's administrators.

Every project file section registered with a ``settings_view`` is a section
of the admin view. ``general``, ``theme``, ``ai`` and ``permissions`` are built in. The setup
wizard (``/admin/setup``) walks a first admin through them.
"""

from panel_flowdash.admin.access import admin_path, admin_token, is_admin
from panel_flowdash.admin.ai import AI, AISettings
from panel_flowdash.admin.form import Group, Setting, SettingsForm, current_app, settings_loader
from panel_flowdash.admin.page import ADMIN_ROUTE, AdminPage, admin_sections
from panel_flowdash.admin.permissions import PERMISSIONS, PermissionsForm
from panel_flowdash.admin.sections import GENERAL, THEME, project_overrides
from panel_flowdash.admin.wizard import SetupWizard, WizardStep

__all__ = [
    "ADMIN_ROUTE",
    "AI",
    "GENERAL",
    "PERMISSIONS",
    "THEME",
    "AISettings",
    "AdminPage",
    "Group",
    "PermissionsForm",
    "Setting",
    "SettingsForm",
    "SetupWizard",
    "WizardStep",
    "admin_path",
    "admin_sections",
    "admin_token",
    "current_app",
    "is_admin",
    "project_overrides",
    "settings_loader",
]
