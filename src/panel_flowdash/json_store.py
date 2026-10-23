"""A dashboard store that keeps each dashboard as a JSON file in a folder."""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from panel_flowdash.auth import Identity, Permission
from panel_flowdash.dashboard_store import BaseDashboardStore, DashboardModel

log = logging.getLogger(__name__)

# The id becomes a file name, so it may not contain separators or lead with a dot.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# What ``json.loads`` and ``DashboardModel.from_dict`` raise on a bad file.
_MALFORMED = (KeyError, TypeError, ValueError, AttributeError)

# A virus scanner or another reader can hold the target open for a few milliseconds
# on Windows, which makes ``os.replace`` raise ``PermissionError``.
_REPLACE_TRIES = 5
_REPLACE_PAUSE = 0.02


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _replace(tmp: str, target: Path) -> None:
    for attempt in range(1, _REPLACE_TRIES + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES:
                raise
            time.sleep(_REPLACE_PAUSE)


class JSONFolderDashboardStore(BaseDashboardStore):
    """Stores each dashboard as ``<folder>/<dashboard_id>.json``.

    The files are plain, human-readable ``DashboardModel`` JSON, so a folder can
    be reviewed, diffed and versioned alongside the project that serves it, or
    shipped read-only with a deployment. Nothing is cached: every listing reads
    the folder, so files edited or added outside the application show up on the
    next request.

    New dashboards get an id derived from their title (``Sales Overview`` becomes
    ``sales-overview``, then ``sales-overview-2`` and so on), which keeps file
    names and ``/dash/<id>`` URLs readable. A file whose ``dashboard_id`` differs
    from its name, or that cannot be parsed, is skipped with one warning.

    Writes are atomic, so a reader never sees a half-written file. Several
    server processes may share a folder, but two processes saving the same
    dashboard at once resolve last-writer-wins.

    Parameters
    ----------
    path
        The folder holding the dashboard files. Created if missing unless
        *read_only* is set.
    read_only
        Serve the dashboards without allowing changes. Writes raise
        ``PermissionError`` and nobody may administer a dashboard, so the editor
        offers no way to modify, rename, share or delete one.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._warned: set[tuple[Path, int]] = set()
        self._unreachable = False
        if not read_only:
            self.path.mkdir(parents=True, exist_ok=True)

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        self._check_writable()
        if not _SAFE_ID.match(dashboard.dashboard_id):
            raise ValueError(
                f"Dashboard id {dashboard.dashboard_id!r} cannot be used as a file name; "
                "use letters, digits, '.', '_' and '-', starting with a letter or digit."
            )
        self._write(dashboard)

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        if not _SAFE_ID.match(dashboard_id):
            return None
        found = self._read(self.path / f"{dashboard_id}.json")
        return found[0] if found else None

    def _all_dashboards(self) -> list[DashboardModel]:
        try:
            with os.scandir(self.path) as entries:
                paths = [Path(e.path) for e in entries if e.name.endswith(".json") and e.is_file()]
        except OSError as exc:
            if not self._unreachable:
                self._unreachable = True
                log.error("Dashboard folder %s is unreachable (%s)", self.path, exc)
            return []
        if self._unreachable:
            self._unreachable = False
            log.info("Dashboard folder %s is reachable again", self.path)
        found = [f for f in map(self._read, paths) if f is not None]
        found.sort(key=lambda f: (-f[1], f[0].dashboard_id))
        return [model for model, _ in found]

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        self._check_writable()
        model = self._load_any(dashboard_id)
        if model is None or model.user_id != user_id:
            return False
        (self.path / f"{dashboard_id}.json").unlink(missing_ok=True)
        return True

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        self._check_writable()
        model = self._load_any(dashboard_id)
        if model is None or model.user_id != user_id:
            return False
        self._write(dataclasses.replace(model, title=new_title))
        return True

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        self._check_writable()
        model = self._load_any(dashboard_id)
        if model is None:
            return False
        self._write(dataclasses.replace(model, permission=permission))
        return True

    def create_dashboard(self, user_id: str, title: str) -> DashboardModel:
        slug = _slug(title) or "dashboard"
        candidates = itertools.chain([slug], (f"{slug}-{n}" for n in itertools.count(2)))
        dashboard_id = next(c for c in candidates if not (self.path / f"{c}.json").exists())
        dashboard = DashboardModel(dashboard_id=dashboard_id, user_id=user_id, title=title)
        self.save_dashboard(dashboard)
        return dashboard

    def can_administer(
        self, identity: Identity, dashboard_id: str, admin_groups: frozenset[str] = frozenset()
    ) -> bool:
        if self.read_only:
            return False
        return super().can_administer(identity, dashboard_id, admin_groups)

    def _check_writable(self) -> None:
        if self.read_only:
            raise PermissionError(f"The dashboard folder {self.path} is read-only.")

    def _read(self, path: Path) -> tuple[DashboardModel, int] | None:
        """Parse one file, returning the model and its mtime, or None if unusable."""
        try:
            mtime = path.stat().st_mtime_ns
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            model = DashboardModel.from_dict(json.loads(text))
        except _MALFORMED as exc:
            self._warn_once(path, mtime, "Skipping dashboard file %s: malformed (%r)", exc)
            return None
        if model.dashboard_id != path.stem:
            self._warn_once(
                path,
                mtime,
                "Skipping dashboard file %s: it holds dashboard id %r but is named for %r",
                model.dashboard_id,
                path.stem,
            )
            return None
        return model, mtime

    def _warn_once(self, path: Path, mtime: int, message: str, *args: object) -> None:
        # Keyed on mtime so a file that is fixed and then broken again warns again.
        if (path, mtime) not in self._warned:
            self._warned.add((path, mtime))
            log.warning(message, path, *args)

    def _write(self, model: DashboardModel) -> None:
        target = self.path / f"{model.dashboard_id}.json"
        handle, tmp = tempfile.mkstemp(dir=self.path, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            json.dump(model.to_dict(), f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            _replace(tmp, target)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
