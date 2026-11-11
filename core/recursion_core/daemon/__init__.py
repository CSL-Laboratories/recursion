"""Linux user daemon: durable intent and conservative reconciliation (CSL-738)."""
from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)

RUN_ID = re.compile(r'[a-zA-Z0-9_-]{1,64}')
UNIT = re.compile(r'recursion-run-[a-zA-Z0-9_-]{1,64}\.service')
# Terminal states are sticky: once recorded they are never re-observed or re-adopted.
# terminated: the run's process is attested gone in this boot. A scripted "ended"
# observation is not enough while a listener is still alive. Attested ends are: the
# cgroup subtree read empty and no listed pid, including a descendant cgroup,
# was alive; the loaded unit reached
# inactive/failed/exited with its cgroup pruned and its main pid gone; or a
# synchronous systemctl stop returned and the unit was then garbage-collected.
# ENOENT and ESRCH are the only read errors that mean a pid or cgroup is gone.
# Any other error reading cgroup.procs or /proc/<pid>/stat is unknown, not death.
# lost_reboot: the run belongs to an earlier boot (boot_id changed). Its processes
# cannot exist in this kernel, but nothing saw them end, so it is not "terminated".
# Nothing is relaunched: workload restart after reboot needs a founder policy.
TERMINAL = frozenset({'terminated', 'lost_reboot'})
SCHEMA_VERSION = 1
# A delegated unit cgroup is shallow. Past either bound the walk is not a
# positive empty result: callers treat None as unknown, never as terminated.
CGROUP_WALK_MAX_DEPTH = 64
CGROUP_WALK_MAX_NODES = 4096


def require_free_threaded():
    """Call at startup and after every plugin import (which may enable the GIL)."""
    check = getattr(sys, '_is_gil_enabled', None)
    if (sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 14)
            or not sysconfig.get_config_var('Py_GIL_DISABLED')
            or check is None or check()):
        raise RuntimeError('Requires free-threaded CPython 3.14t with -X gil=0')


def read_boot_id(path=Path('/proc/sys/kernel/random/boot_id')):
    """Return this kernel boot's id, or None when it cannot be read."""
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    return value or None


class AlreadyRunning(RuntimeError):
    pass


class RegistryIntegrityError(RuntimeError):
    """The registry file is torn or partial. It is not a fresh empty registry."""


class RegistryDurabilityError(OSError):
    """The new registry file is published, but the directory fsync did not finish.

    The rename can be visible to the next open without being crash-durable.
    The file stays in place. Callers must not start or stop a unit until a
    later directory fsync succeeds.
    """


class UserLock:
    """A kernel lease, not a stale PID/token-file check. Never unlink the inode."""
    def __init__(self, runtime: Path):
        self.path = runtime / 'recursion-daemon.lock'
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid():
                raise PermissionError('Runtime lock must be owned by operator')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, BlockingIOError):
                raise AlreadyRunning('One daemon per user per machine') from exc
            raise
        self.fd = fd
        return self

    def __exit__(self, *args):
        os.close(self.fd)
        self.fd = None


@dataclass(frozen=True)
class Run:
    run_id: str
    unit: str
    stop_requested: bool
    state: str
    boot_id: str | None
    detail: str


def _require_private_dir(directory: Path):
    """Create ``directory`` at mode 0700, including any missing parents.

    ``Path.mkdir(parents=True)`` creates missing parents at 0777 masked by the
    process umask, so umask 000 leaves them world-writable. ``os.mkdir``'s mode
    is masked the same way; ``chmod`` is the explicit mode and ignores umask.
    An existing directory this process owns is tightened to 0700. A directory
    owned by someone else, or a symlink, is refused.
    """
    directory = Path(directory)
    missing = []
    current = directory
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
            continue
        if stat.S_ISLNK(info.st_mode):
            raise PermissionError('Registry directory must be operator-owned mode 0700')
        break
    for path in reversed(missing):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            if path.is_symlink():
                raise PermissionError('Registry directory must be operator-owned mode 0700')
        else:
            os.chmod(path, 0o700)
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise PermissionError('Registry directory must be operator-owned mode 0700') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError('Registry directory must be operator-owned mode 0700')
        # Group/other bits on a directory we own are tightened, not refused.
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o700)
            info = os.fstat(fd)
            if info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise PermissionError('Registry directory must be operator-owned mode 0700')
    finally:
        os.close(fd)


def _create_private_file(path: Path):
    """Create a new regular file at mode 0600. umask is not the mode."""
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _fsync_file(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(directory: Path):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_directory(directory: Path):
    """Fsync a directory immediately after its new entry is renamed into place.

    Two attempts. The renamed file is already visible, so a lasting failure is
    not a rolled-back write and not an acknowledged commit: it raises
    RegistryDurabilityError. Callers must not take an external action until a
    later call gets a directory fsync to succeed.
    """
    last = None
    for _ in range(2):
        try:
            _fsync_dir(directory)
            return
        except OSError as exc:
            last = exc
    err = last.errno if last is not None and last.errno is not None else errno.EIO
    raise RegistryDurabilityError(
        err, 'registry directory fsync did not finish; commit is not durable') from last


def _pid_is_gone(pid):
    """True only when the kernel reports that ``pid`` does not exist."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError as exc:
        return exc.errno in (errno.ENOENT, errno.ESRCH)
    return False


_TEMP_NAME = re.compile(r'^\.runs\.sqlite3\.tmp-(\d+)-\d+(?:-wal|-shm|-journal)?$')


_RUNS_TABLE = (
    'CREATE TABLE runs (run_id TEXT PRIMARY KEY, '
    'unit TEXT UNIQUE NOT NULL, stop INTEGER NOT NULL, '
    'state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, '
    'created REAL NOT NULL, updated REAL NOT NULL)')
_CONTROL_TABLE = ('CREATE TABLE IF NOT EXISTS control_runs '
                  '(run_id TEXT PRIMARY KEY, record TEXT NOT NULL)')


class Registry:
    """Durable run records. A commit is a temp file, fsync, and rename.

    The live file's WAL, SHM, and rollback journal are checkpointed and removed
    before the rename. A crash after the rename must not leave the previous WAL
    beside the new file: SQLite would replay it onto the new bytes. A crash
    before the rename leaves the previous file, with its committed rows
    checkpointed into it.     A zero-length or truncated file is refused. The directory fsync is the
    barrier after the rename and before the file is reopened: if it fails the
    published file stays, and the error is RegistryDurabilityError. If the
    new file cannot be reopened, later calls raise RegistryIntegrityError until
    an open succeeds.
    """
    def __init__(self, directory: Path):
        directory = Path(directory)
        _require_private_dir(directory)
        self.directory = directory
        self.path = directory / 'runs.sqlite3'
        self.lock = threading.RLock()
        self.closed = False
        self.db = None
        self._open_error = None
        self._dir_durable = False
        self._sweep_stale_temps()
        try:
            self.path.lstat()
        except FileNotFoundError:
            self._publish_new()
        else:
            self._assert_sound(self.path)
            # The name already on disk is not acknowledged until its directory
            # entry is durable.
            _sync_directory(self.directory)
        self._dir_durable = True
        try:
            self._reopen()
        except sqlite3.Error as exc:
            raise RegistryIntegrityError('registry is not open') from exc
        self._seal_files()

    def _temp_path(self):
        return self.directory / f'.runs.sqlite3.tmp-{os.getpid()}-{threading.get_ident()}'

    def _discard(self, tmp: Path):
        for suffix in ('', '-journal', '-wal', '-shm'):
            try:
                os.unlink(f'{tmp}{suffix}')
            except FileNotFoundError:
                pass

    def _sweep_stale_temps(self):
        """Remove temp copies left by a process that no longer exists.

        A live pid's temp file may belong to an in-flight write and is kept.
        """
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            log.warning('could not list registry temps in %s', self.directory, exc_info=True)
            return
        own = os.getpid()
        for entry in entries:
            match = _TEMP_NAME.fullmatch(entry.name)
            if match is None:
                continue
            pid = int(match.group(1))
            if pid == own or not _pid_is_gone(pid):
                continue
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            try:
                os.unlink(entry)
            except FileNotFoundError:
                pass
            except OSError:
                log.warning('could not remove stale registry temp %s', entry, exc_info=True)

    def _seal_files(self):
        """Force 0600 on the registry and any sqlite sidecar. umask does not apply."""
        for entry in self.directory.iterdir():
            name = entry.name
            if name != 'runs.sqlite3' and not name.startswith('runs.sqlite3-'):
                continue
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RegistryIntegrityError('torn or partial registry')
            if stat.S_ISREG(info.st_mode):
                os.chmod(entry, 0o600)

    def _open_temp(self):
        tmp = self._temp_path()
        self._discard(tmp)
        _create_private_file(tmp)
        dest = sqlite3.connect(tmp, check_same_thread=False, isolation_level=None)
        dest.execute('PRAGMA synchronous=FULL')
        # No rollback journal and no WAL sidecar. The temp file is discarded
        # unless the publish below renames it, so a crash cannot tear the live file.
        dest.execute('PRAGMA journal_mode=OFF')
        return tmp, dest

    def _require_intact(self, conn):
        verdict = conn.execute('PRAGMA integrity_check').fetchone()
        if not verdict or verdict[0] != 'ok':
            raise RegistryIntegrityError('refusing to publish a registry that failed integrity_check')

    def _publish(self, tmp: Path):
        """Fsync the temp file and rename it onto the live registry.

        The caller fsyncs the directory before it reopens the new file. A
        failure there must not discard bytes the rename already published.
        """
        _fsync_file(tmp)
        os.replace(tmp, self.path)

    def _require_dir_durable(self):
        """Retry a directory fsync that an earlier publish left unfinished.

        A read, a sweep, a start, and a stop all wait on this. None of them
        returns until the directory fsync succeeds.
        """
        if self._dir_durable:
            return
        _sync_directory(self.directory)
        self._dir_durable = True

    def _publish_new(self):
        tmp, dest = self._open_temp()
        published = False
        try:
            dest.execute('BEGIN IMMEDIATE')
            dest.execute(_RUNS_TABLE)
            dest.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            dest.execute('COMMIT')
            if dest.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                dest.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self._require_intact(dest)
            dest.close()
            dest = None
            # Nothing is live at this name yet. Remove a journal that SQLite
            # would replay onto the file this rename is about to publish.
            self._drop_live_sidecars()
            if not self._journals_gone():
                raise RegistryIntegrityError('registry journal was not removed before publish')
            self._publish(tmp)
            published = True
            _sync_directory(self.directory)
        except BaseException:
            if dest is not None:
                dest.close()
            if not published:
                self._discard(tmp)
            raise

    def _assert_sound(self, path: Path):
        """Refuse a partial file. Never let sqlite treat one as a new database."""
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RegistryIntegrityError('torn or partial registry')
        if info.st_uid != os.getuid():
            raise PermissionError('Registry file must be operator-owned mode 0600')
        if info.st_size < 100:
            raise RegistryIntegrityError('torn or partial registry')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, 0o600)
            header = os.read(fd, 16)
        finally:
            os.close(fd)
        if header != b'SQLite format 3\x00':
            raise RegistryIntegrityError('torn or partial registry')
        uri = 'file:' + urllib.parse.quote(path.resolve().as_posix(), safe='/') + '?mode=ro'
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.DatabaseError as exc:
            raise RegistryIntegrityError('torn or partial registry') from exc
        try:
            try:
                verdict = conn.execute('PRAGMA integrity_check').fetchone()
            except sqlite3.DatabaseError as exc:
                raise RegistryIntegrityError('torn or partial registry') from exc
            if not verdict or verdict[0] != 'ok':
                raise RegistryIntegrityError('torn or partial registry')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION:
                if version == 0:
                    raise RegistryIntegrityError('torn or partial registry')
                raise RuntimeError(f'Unknown registry schema {version}; refusing to guess')
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
            if table is None:
                raise RegistryIntegrityError('torn or partial registry')
        finally:
            conn.close()

    def _drop_live_sidecars(self):
        """Unlink journals beside the live name. A missing file is not an error."""
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                os.unlink(self.directory / f'runs.sqlite3{suffix}')
            except FileNotFoundError:
                pass
            except OSError:
                # The caller checks the names are actually gone.
                pass

    def _journals_gone(self):
        for suffix in ('-journal', '-wal', '-shm'):
            if (self.directory / f'runs.sqlite3{suffix}').exists():
                return False
        return True

    def _restore_open(self):
        """Reopen the file that is still live. A failed reopen stays typed."""
        if self.closed or self.db is not None:
            return
        self._ensure_open()

    def _checkpoint_and_drop_journals(self):
        """Checkpoint the live database and remove its journals before rename.

        SQLite replays ``runs.sqlite3-wal`` onto whatever inode occupies that
        name. Checkpoint first so a crash after the journals are gone, and
        before the rename, still has every committed row in the old file.
        """
        conn = self.db
        if conn is not None:
            try:
                conn.execute('PRAGMA query_only=OFF')
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            except sqlite3.Error as exc:
                try:
                    conn.execute('PRAGMA query_only=ON')
                except sqlite3.Error:
                    pass
                raise RegistryIntegrityError('registry checkpoint failed') from exc
            if row is None or row[0]:
                try:
                    conn.execute('PRAGMA query_only=ON')
                except sqlite3.Error:
                    pass
                raise RegistryIntegrityError('registry checkpoint did not finish')
            try:
                conn.close()
            finally:
                self.db = None
        self._drop_live_sidecars()
        if not self._journals_gone():
            try:
                self._restore_open()
            except RegistryIntegrityError:
                pass
            raise RegistryIntegrityError('registry journal was not removed before publish')

    def _reopen(self, drop_sidecars=False):
        previous = self.db
        if previous is not None:
            previous.close()
            self.db = None
        if drop_sidecars:
            self._drop_live_sidecars()
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA query_only=ON')
        except sqlite3.Error as exc:
            self.db = None
            self._open_error = exc
            raise
        self.db = conn
        self._open_error = None

    def _ensure_open(self):
        """Return with a live connection, or raise RegistryIntegrityError.

        A publish whose directory fsync has not finished is not readable yet.
        Reopening the file without that fsync would let a sweep act on a Stop
        that is not crash-durable.
        """
        if self.closed:
            raise RuntimeError('Registry closed')
        if self.db is None:
            try:
                self._reopen()
            except sqlite3.Error as exc:
                raise RegistryIntegrityError('registry is not open') from exc
        self._require_dir_durable()

    @contextmanager
    def _write(self):
        """Apply one change on a copy, then publish it with fsync and rename."""
        with self.lock:
            self._ensure_open()
            self._require_dir_durable()
            tmp, dest = self._open_temp()
            published = False
            try:
                self.db.backup(dest)
                # backup copies the source journal mode (WAL on an older registry).
                # Switch the copy to a single file before the rename.
                dest.execute('PRAGMA journal_mode=OFF')
                dest.execute('PRAGMA synchronous=FULL')
                dest.execute('BEGIN IMMEDIATE')
                try:
                    yield dest
                    dest.execute('COMMIT')
                except BaseException:
                    try:
                        dest.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                if dest.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    dest.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                self._require_intact(dest)
                dest.close()
                dest = None
                self._checkpoint_and_drop_journals()
                try:
                    self._publish(tmp)
                except BaseException:
                    self._restore_open()
                    raise
                published = True
                # Barrier: the rename is visible. Do not reopen, and do not
                # return, until the directory entry is durable.
                try:
                    _sync_directory(self.directory)
                except RegistryDurabilityError:
                    self._dir_durable = False
                    raise
                self._dir_durable = True
                try:
                    self._reopen()
                except sqlite3.Error as exc:
                    self.db = None
                    self._open_error = exc
                    raise RegistryIntegrityError('registry reopen failed') from exc
                self._seal_files()
            except BaseException:
                if dest is not None:
                    dest.close()
                if not published:
                    self._discard(tmp)
                raise

    def add(self, run_id, boot_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('Invalid run id')
        now = time.time()
        with self._write() as db:
            db.execute('INSERT INTO runs VALUES (?, ?, 0, ?, ?, ?, ?, ?)',
                       (run_id, f'recursion-run-{run_id}.service', 'starting',
                        boot_id, '', now, now))
        return self.get(run_id)

    def all(self):
        with self.lock:
            self._ensure_open()
            rows = self.db.execute('SELECT run_id, unit, stop, state, boot_id, detail '
                                   'FROM runs ORDER BY run_id').fetchall()
        return [Run(a, b, bool(c), d, e, f) for a, b, c, d, e, f in rows]

    def get(self, run_id):
        with self.lock:
            self._ensure_open()
            row = self.db.execute('SELECT run_id, unit, stop, state, boot_id, detail '
                                  'FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        a, b, c, d, e, f = row
        return Run(a, b, bool(c), d, e, f)

    def request_stop(self, run_id):
        """Durable Stop intent. Terminal runs keep their state; the intent is still recorded."""
        with self._write() as db:
            cursor = db.execute(
                "UPDATE runs SET stop=1, updated=?, state=CASE WHEN state IN "
                "('terminated', 'lost_reboot') THEN state ELSE 'stopping' END "
                "WHERE run_id=?", (time.time(), run_id))
            if cursor.rowcount != 1:
                raise KeyError(run_id)

    def state(self, run_id, value, detail=''):
        """Record an observation. A terminal state is never overwritten.

        B14: a run with a recorded Stop is never written as running, even by a caller
        holding a Run read before the Stop committed; a live unit becomes stopping.
        """
        with self._write() as db:
            db.execute("UPDATE runs SET state=CASE WHEN stop=1 AND ?='running' "
                       "THEN 'stopping' ELSE ? END, detail=?, updated=? WHERE run_id=? "
                       "AND state NOT IN ('terminated', 'lost_reboot')",
                       (value, value, detail[:500], time.time(), run_id))

    # Control-API run records (CSL-798 record shape), one JSON document per run. They share
    # this file and its publish discipline, so a control record and the run row it belongs
    # to are covered by the same fsync, rename and directory-fsync barrier. The table is
    # additive: SCHEMA_VERSION stays 1 and a daemon that predates it ignores the table.
    def control_put(self, run_id, document):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('Invalid run id')
        if not isinstance(document, str):
            raise ValueError('control record must be a JSON string')
        with self._write() as db:
            db.execute(_CONTROL_TABLE)
            db.execute('INSERT OR REPLACE INTO control_runs VALUES (?, ?)', (run_id, document))

    def control_all(self):
        """Every stored control record as (run_id, JSON text). No table yet is no records."""
        with self.lock:
            self._ensure_open()
            table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='control_runs'").fetchone()
            if table is None:
                return []
            return self.db.execute('SELECT run_id, record FROM control_runs ORDER BY run_id').fetchall()

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                if self.db is not None:
                    self.db.close()
                    self.db = None


class UnitNotLoaded(subprocess.SubprocessError):
    """systemctl stop reported the unit as not loaded (exit 5, "Unit <unit> not loaded.")."""
    def __init__(self, unit):
        super().__init__(f'Unit {unit} not loaded')
        self.unit = unit


class ServiceManager(Protocol):
    def start(self, unit: str, argv: list[str]) -> None: ...
    def stop(self, unit: str) -> None:
        """Stop and wait for inactive. Raises UnitNotLoaded only for systemd's not-loaded error."""
        ...
    def observe(self, unit: str) -> str:
        """Return running, empty (positively observed ended), gone (not loaded), or unknown."""
        ...
    def processes_gone(self, unit: str) -> bool:
        """True only when this manager attests that no process of the unit is still alive."""
        ...
    def owned_units(self) -> set[str]: ...


class Daemon:
    def __init__(self, registry: Registry, manager: ServiceManager,
                 boot_id: Callable[[], str | None] = read_boot_id):
        self.registry, self.manager, self.boot_id = registry, manager, boot_id
        self.lock = threading.RLock()
        self.closed = False

    def _open(self):
        if self.closed:
            raise RuntimeError('Daemon closed')

    def start(self, run_id, argv):
        with self.lock:
            self._open()
            boot = self.boot_id()
            if boot is None:
                raise RuntimeError('boot_id unreadable; refusing a run that reboot could not reconcile')
            run = self.registry.add(run_id, boot)
            try:
                self.manager.start(run.unit, argv)
            except BaseException as exc:
                # Keep the original launch error; the record says unknown, never terminated.
                try:
                    self._reconcile(self.registry.get(run_id), f'launch error: {type(exc).__name__}')
                except Exception:
                    log.exception('reconcile after launch failure of %s failed', run_id)
                raise
            self._reconcile(self.registry.get(run_id))
            return self.registry.get(run_id)

    def stop(self, run_id):
        with self.lock:
            self._open()
            self.registry.request_stop(run_id)
            self._reconcile(self.registry.get(run_id))
            return self.registry.get(run_id)

    def reconcile(self, run_id):
        """Observe one run now and return its record. Raises KeyError for an unknown id."""
        with self.lock:
            self._open()
            self._reconcile(self.registry.get(run_id))
            return self.registry.get(run_id)

    def _reconcile(self, run, detail=''):
        if run.state in TERMINAL:
            return
        boot = self.boot_id()
        if run.boot_id and boot and run.boot_id != boot:
            self.registry.state(run.run_id, 'lost_reboot',
                                f'started in boot {run.boot_id}; not relaunched')
            return
        try:
            if boot is None:
                raise OSError('boot_id unreadable')
            stopped = not_loaded = False
            if run.stop_requested:
                try:
                    self.manager.stop(run.unit)
                    stopped = True
                except UnitNotLoaded:
                    not_loaded = True
            observation = self.manager.observe(run.unit)
            # A gone (not-found) unit is terminated only with a recorded Stop and either
            # this reconcile's stop returning (systemctl stop waits for inactive, then
            # systemd garbage-collects the unit) or systemd answering that stop with "not
            # loaded": a replayed Stop whose earlier stop already ended and collected the
            # unit, or a run that never launched. Either way nothing runs under the unit,
            # and a same-name unit that appears later is stopped by the orphan sweep. A unit
            # that is merely absent with no recorded Stop may never have launched: unknown.
            if observation == 'gone' and (stopped or not_loaded):
                observation = 'empty'
                if not_loaded and not detail:
                    detail = 'stop: unit not loaded, unit gone'
            state = {'running': 'stopping' if run.stop_requested else 'running',
                     'empty': 'terminated'}.get(observation, 'unknown')
        except (OSError, subprocess.SubprocessError) as exc:
            state, detail = 'unknown', detail or f'{type(exc).__name__}: {exc}'
        # 'empty' from a scripted observe(), or a unit name that is already gone,
        # is not termination while a listener is still attested alive.
        if state == 'terminated' and not self._termination_attested(run.unit):
            state = 'stopping' if run.stop_requested else 'unknown'
            detail = detail or 'process still alive; termination not attested'
        self.registry.state(run.run_id, state, detail)

    def _termination_attested(self, unit):
        attest = getattr(self.manager, 'processes_gone', None)
        if not callable(attest):
            return False
        try:
            return attest(unit) is True
        except Exception:
            log.exception('termination attestation for %s failed', unit)
            return False

    def sweep(self):
        """Startup and periodic sweep: re-adopt, replay Stop, stop reserved-name orphans."""
        with self.lock:
            self._open()
            for run in self.registry.all():
                try:
                    self._reconcile(run)
                except Exception:
                    log.exception('reconcile of %s failed', run.run_id)
            runs = self.registry.all()
            # Units of live (non-terminal) runs are left alone. Terminal runs' leftover
            # units and unknown units in the reserved namespace are stopped; unrelated
            # user services are never touched.
            live = {r.unit for r in runs if r.state not in TERMINAL}
            try:
                owned = self.manager.owned_units()
            except (OSError, subprocess.SubprocessError):
                log.exception('listing run units failed; orphans not swept this pass')
                owned = set()
            for unit in sorted(owned - live):
                if UNIT.fullmatch(unit):
                    try:
                        self.manager.stop(unit)
                    except (OSError, subprocess.SubprocessError):
                        log.exception('stopping orphan %s failed', unit)
            return runs

    def close(self):
        """Close the registry after in-flight calls finish. Never stops run units."""
        with self.lock:
            self.closed = True
            self.registry.close()


class SystemdUserManager:
    """Independent transient user services: no PartOf/BindsTo daemon relationship."""
    def __init__(self, cgroup_root=Path('/sys/fs/cgroup')):
        self.cgroup_root = cgroup_root

    def _call(self, argv):
        return subprocess.run(argv, check=True, text=True, capture_output=True, timeout=15).stdout

    def start(self, unit, argv):
        if not argv:
            raise ValueError('Empty launch')
        self._call(['systemd-run', '--user', '--unit=' + unit, '--service-type=exec',
                    '--property=KillMode=control-group', '--property=Delegate=yes',
                    '--property=RemainAfterExit=yes', '--', *argv])

    def stop(self, unit):
        try:
            self._call(['systemctl', '--user', 'stop', '--', unit])
        except subprocess.CalledProcessError as exc:
            # systemctl exits 5 with "Failed to stop <unit>: Unit <unit> not loaded." for a
            # unit systemd has no record of. Anything else stays a plain stop failure.
            if exc.returncode == 5 and f'Unit {unit} not loaded' in (exc.stderr or ''):
                raise UnitNotLoaded(unit) from exc
            raise

    def pid_alive(self, pid):
        """True when alive, False when gone, None when the read did not say.

        Only ENOENT and ESRCH mean the process is gone. A zombie is gone. Any
        other error, including EACCES and EIO, is unknown and must not be treated
        as death.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        try:
            text = Path(f'/proc/{pid}/stat').read_text()
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH):
                return False
            return None
        try:
            state = text[text.rindex(')') + 2:].split()[0]
        except (ValueError, IndexError):
            return True
        return state not in ('Z', 'X')

    def _read_level(self, directory: Path):
        """Return (pids, populated) for one cgroup directory, or None if unreadable.

        A missing ``cgroup.procs`` is an empty level, not an error. Any other
        read error is an incomplete look at this level. ``populated`` is True
        only when this directory's own ``cgroup.events`` says so.
        """
        try:
            text = (directory / 'cgroup.procs').read_text()
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                return None
            text = ''
        pids = []
        for token in text.split():
            if not token.isdigit():
                return None
            pids.append(int(token))
        populated = False
        try:
            events = (directory / 'cgroup.events').read_text()
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ESRCH):
                return None
            events = ''
        try:
            fields = dict(line.split() for line in events.splitlines() if line.strip())
        except ValueError:
            return None
        if fields.get('populated') not in (None, '0', '1'):
            return None
        if fields.get('populated') == '1':
            populated = True
        return pids, populated

    def _cgroup_identity(self, directory: Path):
        """Return a stable identity for a real directory, or None if unusable.

        A symlink is not followed. The same identity seen twice is a cycle.
        """
        try:
            info = directory.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return None
        return (info.st_dev, info.st_ino)

    def _cgroup_children(self, directory: Path):
        """Return child directories, [] if this directory vanished, or None if unreadable.

        A symlink child fails the walk. Children are not descended into here.
        """
        try:
            entries = list(directory.iterdir())
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ESRCH):
                return []
            return None
        children = []
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ESRCH):
                    continue
                return None
            if stat.S_ISLNK(info.st_mode):
                return None
            if stat.S_ISDIR(info.st_mode) and entry.name not in ('.', '..'):
                children.append(entry)
        return children

    def _subtree(self, directory: Path):
        """Return (pids, any_populated) for this cgroup and its descendants.

        None means the walk could not be finished: a read error, a symlink, a
        cycle, or a tree past the depth or node bound. The walk is iterative.
        Delegate= lets a run create child cgroups, and a pid in one of those is
        still inside the unit.
        """
        stack = [(directory, 0)]
        seen = set()
        nodes = 0
        pids = []
        populated = False
        while stack:
            current, depth = stack.pop()
            nodes += 1
            if depth > CGROUP_WALK_MAX_DEPTH or nodes > CGROUP_WALK_MAX_NODES:
                return None
            identity = self._cgroup_identity(current)
            if identity is None or identity in seen:
                return None
            seen.add(identity)
            level = self._read_level(current)
            if level is None:
                return None
            level_pids, level_populated = level
            pids.extend(level_pids)
            populated = populated or level_populated
            children = self._cgroup_children(current)
            if children is None:
                return None
            for child in children:
                stack.append((child, depth + 1))
        return pids, populated

    def _process_still_live(self, props, group):
        """True when a listed pid is alive, False when all are gone, None if unknown.

        ``populated 0`` on the unit cgroup is not enough. Every descendant
        cgroup is walked, and each directory's populated bit is rechecked.
        An incomplete walk, a hierarchical populated bit with no confirmed
        death, or an unreadable /proc entry is None: not attested death.
        """
        pids = []
        subtree_populated = False
        if group:
            walked = self._subtree(self.cgroup_root / group.lstrip('/'))
            if walked is None:
                return None
            pids, subtree_populated = walked
        main = props.get('MainPID', '')
        if str(main).isdigit() and int(main) > 0:
            main_pid = int(main)
            if main_pid not in pids:
                pids.append(main_pid)
        unknown = False
        for pid in pids:
            alive = self.pid_alive(pid)
            if alive is True:
                return True
            if alive is None:
                unknown = True
        if unknown or subtree_populated:
            return None
        return False

    def processes_gone(self, unit):
        """Attest the unit's processes are gone. A scripted observe() string is not this."""
        try:
            return self.observe(unit) in ('empty', 'gone')
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def observe(self, unit):
        """Read systemd's view of the unit.

        systemd prunes a unit's cgroup once it is empty and the unit is inactive, failed
        or (RemainAfterExit) active/exited, so ControlGroup= is then blank: a loaded unit
        in one of those states with no cgroup is ended, but only with evidence that it
        ran (an InvocationID or a main-process start time), no queued job, and no live
        MainPID. A unit whose start job is still queued (systemd-run timed out, or the
        daemon died mid-launch) also reads loaded/inactive/dead with no cgroup; that is
        unknown. A cgroup that still exists is read directly, including every
        descendant cgroup (Delegate= child cgroups stay inside the unit).
        ``populated 0`` is ended only when the whole subtree was walked, no
        directory still says populated, and no listed pid was alive. Not-found
        is gone (garbage-collected or never launched).
        """
        try:
            text = self._call(['systemctl', '--user', 'show', '--property=LoadState,'
                               'ActiveState,SubState,ControlGroup,Job,InvocationID,'
                               'ExecMainStartTimestampMonotonic,MainPID', '--', unit])
            props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            group = props.get('ControlGroup', '')
            if not group:
                load, active = props.get('LoadState'), props.get('ActiveState')
                if load == 'not-found' and active == 'inactive':
                    # Not-found is "gone" only when no MainPID is still alive. A
                    # scripted not-found line with a live listener is not an end.
                    if self._process_still_live(props, '') is not False:
                        return 'unknown'
                    return 'gone'
                ran = (props.get('InvocationID', '').strip('0')
                       or props.get('ExecMainStartTimestampMonotonic', '0') not in ('', '0'))
                if (load == 'loaded' and ran and props.get('Job', '') in ('', '0')
                        and (active in ('inactive', 'failed')
                             or (active == 'active' and props.get('SubState') == 'exited'))):
                    if self._process_still_live(props, '') is not False:
                        return 'unknown'
                    return 'empty'
                return 'unknown'
            if not group.startswith('/') or '..' in Path(group).parts:
                return 'unknown'
            events = (self.cgroup_root / group.lstrip('/') / 'cgroup.events').read_text()
            populated = dict(line.split() for line in events.splitlines()).get('populated')
            if populated == '0':
                if self._process_still_live(props, group) is not False:
                    return 'unknown'
                return 'empty'
            if populated == '1' and props.get('ActiveState') == 'active':
                return 'running'
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        return 'unknown'

    def owned_units(self):
        text = self._call(['systemctl', '--user', 'list-units', '--all', '--plain',
                           '--no-legend', 'recursion-run-*.service'])
        return {line.split()[0] for line in text.splitlines() if line.split()}


def check_operator_linger(user, call=subprocess.run):
    """Read-only B9 check for the operator only; never enables or disables lingering.

    Operator Linger=yes keeps the operator's user manager, and so this daemon and its
    run units, alive after the last SSH session closes. It is never required of an
    engine user: a lingering engine user manager can start units outside an operator
    leaf (owner-reported host evidence on CSL-728/CSL-738, Nov 7), so the future
    cross-uid path requires the engine user to have no user manager during runs.
    """
    refusal = f'Linger=yes required for operator {user}; founder host setup needed'
    try:
        result = call(['loginctl', 'show-user', user, '--property=Linger', '--value'],
                      check=True, text=True, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        # logind unknown user, logind not running, loginctl missing or hung: fail closed
        # with the named refusal, not a raw traceback.
        raise RuntimeError(f'{refusal} (linger probe failed: {type(exc).__name__}: {exc})') from exc
    if result.stdout.strip() != 'yes':
        raise RuntimeError(refusal)


def refuse_engine_user(environ):
    """Nov 11 is same-uid only (CSL-728 B6). A configured engine user is refused, not checked."""
    engine = environ.get('RECURSION_ENGINE_USER', '').strip()
    if engine:
        raise RuntimeError(
            f'RECURSION_ENGINE_USER={engine} is not supported: the cross-uid path is not '
            'implemented (B7, CSL-734 picks its spawn path), and it will require the engine '
            'user to have no user manager (Linger=no, user@<engine uid>.service inactive, '
            'no /run/user/<uid>/systemd/private) during runs; see engine_user.py')
