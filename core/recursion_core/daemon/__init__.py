"""Linux user daemon: durable intent and conservative reconciliation (CSL-738)."""
from __future__ import annotations

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
# cgroup read empty and no listed pid was alive; the loaded unit reached
# inactive/failed/exited with its cgroup pruned and its main pid gone; or a
# synchronous systemctl stop returned and the unit was then garbage-collected.
# lost_reboot: the run belongs to an earlier boot (boot_id changed). Its processes
# cannot exist in this kernel, but nothing saw them end, so it is not "terminated".
# Nothing is relaunched: workload restart after reboot needs a founder policy.
TERMINAL = frozenset({'terminated', 'lost_reboot'})
SCHEMA_VERSION = 1


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
    An existing directory is not chmod'd: it must already be operator-owned 0700.
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
    info = directory.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise PermissionError('Registry directory must be operator-owned mode 0700')


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


_RUNS_TABLE = (
    'CREATE TABLE runs (run_id TEXT PRIMARY KEY, '
    'unit TEXT UNIQUE NOT NULL, stop INTEGER NOT NULL, '
    'state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, '
    'created REAL NOT NULL, updated REAL NOT NULL)')


class Registry:
    """Durable run records. A commit is a temp file, fsync, and rename.

    The live ``runs.sqlite3`` is replaced only after the new bytes pass
    ``integrity_check``. A crash mid-write leaves the previous file. A zero-length
    or truncated file is refused, not opened as an empty registry.
    """
    def __init__(self, directory: Path):
        directory = Path(directory)
        _require_private_dir(directory)
        self.directory = directory
        self.path = directory / 'runs.sqlite3'
        self.lock = threading.RLock()
        self.closed = False
        self.db = None
        try:
            self.path.lstat()
        except FileNotFoundError:
            self._publish_new()
        else:
            self._assert_sound(self.path)
        self._reopen()
        self._seal_files()

    def _temp_path(self):
        return self.directory / f'.runs.sqlite3.tmp-{os.getpid()}-{threading.get_ident()}'

    def _discard(self, tmp: Path):
        for suffix in ('', '-journal', '-wal', '-shm'):
            try:
                os.unlink(f'{tmp}{suffix}')
            except FileNotFoundError:
                pass

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

        The directory fsync is the caller's, after it has reopened the new
        file: a failure there must not discard bytes the rename already published.
        """
        _fsync_file(tmp)
        os.replace(tmp, self.path)

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
            self._publish(tmp)
            published = True
            self._drop_live_sidecars()
            _fsync_dir(self.directory)
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
        """Remove a journal left beside a file we just replaced with a full copy."""
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                os.unlink(self.directory / f'runs.sqlite3{suffix}')
            except FileNotFoundError:
                pass

    def _reopen(self, drop_sidecars=False):
        previous = self.db
        if previous is not None:
            previous.close()
            self.db = None
        if drop_sidecars:
            # Close the connection that held the old inode before unlinking its journal.
            self._drop_live_sidecars()
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA query_only=ON')
        self.db = conn

    @contextmanager
    def _write(self):
        """Apply one change on a copy, then publish it with fsync and rename."""
        with self.lock:
            if self.closed:
                raise RuntimeError('Registry closed')
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
                self._publish(tmp)
                published = True
                self._reopen(drop_sidecars=True)
                self._seal_files()
                _fsync_dir(self.directory)
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
            if self.closed:
                raise RuntimeError('Registry closed')
            rows = self.db.execute('SELECT run_id, unit, stop, state, boot_id, detail '
                                   'FROM runs ORDER BY run_id').fetchall()
        return [Run(a, b, bool(c), d, e, f) for a, b, c, d, e, f in rows]

    def get(self, run_id):
        with self.lock:
            if self.closed:
                raise RuntimeError('Registry closed')
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
        """True when ``/proc/<pid>`` is a live process. A zombie is not alive."""
        try:
            text = Path(f'/proc/{int(pid)}/stat').read_text()
        except (OSError, ValueError):
            return False
        try:
            state = text[text.rindex(')') + 2:].split()[0]
        except (ValueError, IndexError):
            return True
        return state not in ('Z', 'X')

    def _listed_pids(self, group):
        path = self.cgroup_root / group.lstrip('/') / 'cgroup.procs'
        try:
            text = path.read_text()
        except OSError:
            return []
        pids = []
        for token in text.split():
            if not token.isdigit():
                return None
            pids.append(int(token))
        return pids

    def _process_still_live(self, props, group):
        """True when a pid we can see is alive, or the cgroup pid list is unreadable.

        ``populated 0`` or an inactive show line is a scripted observation unless
        every listed pid, including MainPID, is actually gone.
        """
        pids = []
        if group:
            listed = self._listed_pids(group)
            if listed is None:
                return True
            pids.extend(listed)
        main = props.get('MainPID', '')
        if str(main).isdigit() and int(main) > 0:
            pids.append(int(main))
        return any(self.pid_alive(pid) for pid in pids)

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
        unknown. A cgroup that still exists is read directly, and ``populated 0`` is
        ended only when ``cgroup.procs`` has no live pid. Not-found is gone
        (garbage-collected or never launched).
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
                    if self._process_still_live(props, ''):
                        return 'unknown'
                    return 'gone'
                ran = (props.get('InvocationID', '').strip('0')
                       or props.get('ExecMainStartTimestampMonotonic', '0') not in ('', '0'))
                if (load == 'loaded' and ran and props.get('Job', '') in ('', '0')
                        and (active in ('inactive', 'failed')
                             or (active == 'active' and props.get('SubState') == 'exited'))):
                    if self._process_still_live(props, ''):
                        return 'unknown'
                    return 'empty'
                return 'unknown'
            if not group.startswith('/') or '..' in Path(group).parts:
                return 'unknown'
            events = (self.cgroup_root / group.lstrip('/') / 'cgroup.events').read_text()
            populated = dict(line.split() for line in events.splitlines()).get('populated')
            if populated == '0':
                if self._process_still_live(props, group):
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
