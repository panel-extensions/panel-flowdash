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
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

from panel_flowdash.auth import Identity, Permission
from panel_flowdash.dashboard_store import (
    MALFORMED_ERRORS,
    BaseDashboardStore,
    DashboardModel,
    DashboardStoreError,
    _decode_revision,
    check_revision,
    decode_settings,
)

log = logging.getLogger(__name__)

# The id becomes a file name, so it may not contain separators or lead with a dot.
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

# A virus scanner or another reader can hold the target open for a few milliseconds
# on Windows, which makes ``os.replace`` raise ``PermissionError``.
_REPLACE_TRIES = 5
_REPLACE_PAUSE = 0.02

# An empty <id>.json older than this is a reservation whose writer crashed before
# filling it in (see ``_claim``); it holds the id until someone removes it.
_STALE_RESERVATION_NS = 5_000_000_000

# Windows cannot lock a directory, so writers there lock this file in the folder.
_LOCK_FILE = ".flowdash.lock"


class _ReadOnlyError(DashboardStoreError, PermissionError):
    pass


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


def _lock_file(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        # Retries for about ten seconds, then raises OSError.
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)


def _unlock_file(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _claim(tmp: str, target: Path) -> None:
    """Move *tmp* to *target*, raising FileExistsError instead of replacing it."""
    try:
        # A hard link fails if the target exists and publishes the complete file at once.
        os.link(tmp, target)
    except FileExistsError:
        raise
    except OSError:
        # No hard links on this file system: reserve the name, then fill it in.
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        try:
            _replace(tmp, target)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(target)
            raise
    else:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


class JSONFolderDashboardStore(BaseDashboardStore):
    """Stores each dashboard as ``<folder>/<dashboard_id>.json``.

    The files are plain, human-readable ``DashboardModel`` JSON, so a folder can
    be reviewed, diffed and versioned alongside the project that serves it, or
    shipped read-only with a deployment. Every listing scans the folder, so
    files edited or added outside the application show up on the next request;
    only files whose size, modification time or inode changed since the last
    listing are parsed again. Models returned by listings are shared with that
    cache and must not be mutated; ``load_dashboard`` and the other single
    dashboard lookups return a private copy.

    New dashboards get an id derived from their title (``Sales Overview`` becomes
    ``sales-overview``, then ``sales-overview-2`` and so on), which keeps file
    names and ``/dash/<id>`` URLs readable. A file whose ``dashboard_id`` differs
    from its name, or that cannot be parsed, is skipped with one warning.

    Writes are atomic, so a reader never sees a half-written file. Several
    server processes may share a folder: creating a dashboard claims its id
    exclusively, and the other writes hold an advisory lock on the folder
    (on Windows on a ``.flowdash.lock`` file in it) while they check the file
    they replace. Saving
    never replaces a file that belongs to another user, and each file stores
    a ``revision`` that saving checks and increments, so a save based on an
    outdated copy raises :class:`DashboardConflictError`. Edit the files by
    hand only while no server writes to the folder; a hand edit that keeps
    the ``revision`` is not detected as a conflict.

    Parameters
    ----------
    path
        The folder holding the dashboard files. Created if missing unless
        *read_only* is set.
    read_only
        Serve the dashboards without allowing changes. Writes raise
        ``PermissionError`` (also a ``DashboardStoreError``) and nobody may
        edit or administer a dashboard, so the editor offers no way to modify,
        rename, share or delete one.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._warned: set[tuple[Path, int]] = set()
        self._cache: dict[
            Path, tuple[tuple[int, int, int], tuple[DashboardModel, int] | None]
        ] = {}
        self._unreachable = False
        self._thread_lock = threading.Lock()
        self._lock_warned = False
        if not read_only:
            self.path.mkdir(parents=True, exist_ok=True)

    def save_dashboard(self, dashboard: DashboardModel) -> None:
        """Write *dashboard*, refusing to replace a file another user owns.

        Raises :class:`DashboardConflictError` if the file holds a different
        revision than *dashboard* was loaded at.
        """
        self._check_writable()
        if not _SAFE_ID.match(dashboard.dashboard_id):
            raise ValueError(
                f"Dashboard id {dashboard.dashboard_id!r} cannot be used as a file name; "
                "use letters, digits, '.', '_' and '-', starting with a letter or digit."
            )
        with self._locked():
            stored = self._fields_on_disk(self.path / f"{dashboard.dashboard_id}.json")
            owner = stored.get("user_id")
            if isinstance(owner, str) and owner != dashboard.user_id:
                raise DashboardStoreError(
                    f"The dashboard id {dashboard.dashboard_id!r} is already used by another user."
                )
            revision = _decode_revision(stored.get("revision"))
            if stored and revision is None:
                revision = 0
            revision = check_revision(dashboard, revision)
            self._write(dataclasses.replace(dashboard, revision=revision))
        dashboard.revision = revision

    def _load_any(self, dashboard_id: str) -> DashboardModel | None:
        if not _SAFE_ID.match(dashboard_id):
            return None
        path = self.path / f"{dashboard_id}.json"
        try:
            stat = path.stat()
            found = self._parse(path, stat.st_mtime_ns, size=stat.st_size)
        except OSError:
            return None
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
        # Build a fresh cache so deleted files drop out; replacing the dict in one
        # assignment keeps concurrent listings from tripping over each other.
        previous, cache = self._cache, {}
        for path in paths:
            try:
                stat = path.stat()
                key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
                hit = previous.get(path)
                cache[path] = (
                    hit
                    # Empty files are re-checked so a stale reservation gets reported.
                    if hit is not None and hit[0] == key and stat.st_size
                    else (key, self._parse(path, stat.st_mtime_ns, size=stat.st_size))
                )
            except OSError:
                continue
        self._cache = cache
        found = [f for _, f in cache.values() if f is not None]
        found.sort(key=lambda f: (-f[1], f[0].dashboard_id))
        return [model for model, _ in found]

    def delete_dashboard(self, user_id: str, dashboard_id: str) -> bool:
        self._check_writable()
        with self._locked():
            model = self._load_any(dashboard_id)
            if model is None or model.user_id != user_id:
                return False
            (self.path / f"{dashboard_id}.json").unlink(missing_ok=True)
        return True

    def rename_dashboard(self, user_id: str, dashboard_id: str, new_title: str) -> bool:
        self._check_writable()
        with self._locked():
            model = self._load_any(dashboard_id)
            if model is None or model.user_id != user_id:
                return False
            self._write(dataclasses.replace(model, title=new_title))
        return True

    def set_permission(self, dashboard_id: str, permission: Permission) -> bool:
        self._check_writable()
        with self._locked():
            model = self._load_any(dashboard_id)
            if model is None:
                return False
            self._write(dataclasses.replace(model, permission=permission))
        return True

    def create_dashboard(self, user_id: str, title: str) -> DashboardModel:
        self._check_writable()
        slug = _slug(title) or "dashboard"
        for n in itertools.count(1):
            dashboard_id = slug if n == 1 else f"{slug}-{n}"
            dashboard = DashboardModel(
                dashboard_id=dashboard_id, user_id=user_id, title=title, revision=1
            )
            try:
                self._write(dashboard, exclusive=True)
            except FileExistsError:
                continue
            break
        return dashboard

    def can_administer_model(
        self,
        identity: Identity,
        dashboard: DashboardModel,
        admin_groups: frozenset[str] = frozenset(),
    ) -> bool:
        if self.read_only:
            return False
        return super().can_administer_model(identity, dashboard, admin_groups)

    @contextlib.contextmanager
    def _locked(self):
        """Hold the folder's write lock, shared with other processes using the folder."""
        with self._thread_lock:
            if fcntl is not None:
                fd = os.open(self.path, os.O_RDONLY)
            else:
                fd = os.open(self.path / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                locked = self._lock_fd(fd)
                try:
                    yield
                finally:
                    if locked:
                        _unlock_file(fd)
            finally:
                os.close(fd)

    def _lock_fd(self, fd: int) -> bool:
        try:
            _lock_file(fd)
        except OSError as exc:
            # Some network file systems do not support locks; this process's
            # writers are still serialized by the thread lock.
            if not self._lock_warned:
                self._lock_warned = True
                log.warning("Cannot lock the dashboard folder %s (%s)", self.path, exc)
            return False
        return True

    def can_edit_model(
        self,
        identity: Identity,
        dashboard: DashboardModel,
        admin_groups: frozenset[str] = frozenset(),
    ) -> bool:
        if self.read_only:
            return False
        return super().can_edit_model(identity, dashboard, admin_groups)

    def _check_writable(self) -> None:
        if self.read_only:
            raise _ReadOnlyError("Dashboards are read-only on this server.")

    def _parse(
        self, path: Path, mtime: int, size: int | None = None
    ) -> tuple[DashboardModel, int] | None:
        """Parse one file, returning the model and its mtime, or None if unusable.

        Raises ``OSError`` if the file cannot be read, so callers do not cache
        what may be a passing failure.
        """
        if size == 0:
            # Only seen while another process claims an id without hard links,
            # unless that process died before filling the file in.
            if time.time_ns() - mtime > _STALE_RESERVATION_NS:
                self._warn_once(
                    path,
                    mtime,
                    "Skipping dashboard file %s: it is empty, likely left by a writer that "
                    "crashed while creating it; delete it to free the id %r",
                    path.stem,
                )
            return None
        data = path.read_bytes()
        try:
            fields = json.loads(data.decode("utf-8"))
            # Settings are optional, so bad ones are dropped below instead of the file.
            settings = fields.pop("settings", None) if isinstance(fields, dict) else None
            model = DashboardModel.from_dict(fields)
        except MALFORMED_ERRORS as exc:
            self._warn_once(path, mtime, "Skipping dashboard file %s: malformed (%r)", exc)
            return None
        try:
            model.settings = decode_settings(settings)
        except MALFORMED_ERRORS as exc:
            self._warn_once(
                path, mtime, "Ignoring malformed settings in dashboard file %s (%r)", exc
            )
        if model.revision is None:
            model.revision = 0
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

    @staticmethod
    def _fields_on_disk(path: Path) -> dict:
        """Return the JSON object stored in *path*, or {} if it is missing or unreadable.

        Deliberately lenient: on a case-insensitive file system ``Foo.json``
        opens ``foo.json``, whose id does not match the name, and its owner
        must still be found.
        """
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, *MALFORMED_ERRORS):
            return {}
        return data if isinstance(data, dict) else {}

    def _warn_once(self, path: Path, mtime: int, message: str, *args: object) -> None:
        # Keyed on mtime so a file that is fixed and then broken again warns again.
        if (path, mtime) not in self._warned:
            self._warned.add((path, mtime))
            log.warning(message, path, *args)

    def _write(self, model: DashboardModel, *, exclusive: bool = False) -> None:
        """Atomically write *model*; with *exclusive*, raise FileExistsError if it exists."""
        target = self.path / f"{model.dashboard_id}.json"
        handle, tmp = tempfile.mkstemp(dir=self.path, prefix=f".{target.name}.", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            json.dump(model.to_dict(), f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            if exclusive:
                _claim(tmp, target)
            else:
                _replace(tmp, target)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
