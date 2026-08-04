"""Shared helpers for the app shell and the embeddable editor."""

from __future__ import annotations

import logging

import panel as pn

logger = logging.getLogger("panel_flowdash")

_LOG_LEVELS = {
    "success": logging.INFO,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def notify(
    severity: str,
    message: str,
    *,
    duration: int = 3000,
    enabled: bool = True,
) -> None:
    """Emit a Panel notification, falling back to the logger.

    ``pn.state.notifications`` is ``None`` outside a served session (a plain
    script, a test, or a notebook without the notifications extension), so
    calling it unguarded raises. Embedders can also opt out entirely by passing
    ``enabled=False``.
    """
    if not enabled:
        return
    notifications = pn.state.notifications
    if notifications is None:
        logger.log(_LOG_LEVELS.get(severity, logging.INFO), message)
        return
    getattr(notifications, severity)(message, duration=duration)
