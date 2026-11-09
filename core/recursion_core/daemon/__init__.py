"""Linux user daemon: durable intent and conservative reconciliation (CSL-738)."""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import sysconfig
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Protocol

log = logging.getLogger(__name__)

RUN_ID = re.compile(r'[a-zA-Z0-9_-]{1,64}')
UNIT = re.compile(r'recursion-run-[a-zA-Z0-9_-]{1,64}\.service')
# Terminal states are sticky: once recorded they are never re-observed or re-adopted.
# terminated: systemd positively saw the run unit end in this boot: its cgroup read
# empty, the loaded unit reached inactive/failed/exited with its cgroup pruned, or a
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


class Registry:
    """SQLite FULL transactions: intent commits before any external side effect."""
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (not directory.is_dir() or directory.is_symlink() or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise PermissionError('Registry directory must be operator-owned mode 0700')
        path = directory / 'runs.sqlite3'
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.lock = threading.RLock()
        self.closed = False
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA journal_mode=WAL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.db.close()
            raise RuntimeError(f'Unknown registry schema {version}; refusing to guess')
        with self._tx():
            self.db.execute('CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, '
                            'unit TEXT UNIQUE NOT NULL, stop INTEGER NOT NULL, '
                            'state TEXT NOT NULL, boot_id TEXT, detail TEXT NOT NULL, '
                            'created REAL NOT NULL, updated REAL NOT NULL)')
            self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    @contextmanager
    def _tx(self):
        with self.lock:
            if self.closed:
                raise RuntimeError('Registry closed')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            self.db.execute('COMMIT')

    def add(self, run_id, boot_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ValueError('Invalid run id')
        now = time.time()
        with self._tx():
            self.db.execute('INSERT INTO runs VALUES (?, ?, 0, ?, ?, ?, ?, ?)',
                            (run_id, f'recursion-run-{run_id}.service', 'starting',
                             boot_id, '', now, now))
        return self.get(run_id)

    def all(self):
        with self._tx():
            rows = self.db.execute('SELECT run_id, unit, stop, state, boot_id, detail '
                                   'FROM runs ORDER BY run_id').fetchall()
        return [Run(a, b, bool(c), d, e, f) for a, b, c, d, e, f in rows]

    def get(self, run_id):
        with self._tx():
            row = self.db.execute('SELECT run_id, unit, stop, state, boot_id, detail '
                                  'FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        a, b, c, d, e, f = row
        return Run(a, b, bool(c), d, e, f)

    def request_stop(self, run_id):
        """Durable Stop intent. Terminal runs keep their state; the intent is still recorded."""
        with self._tx():
            cursor = self.db.execute(
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
        with self._tx():
            self.db.execute("UPDATE runs SET state=CASE WHEN stop=1 AND ?='running' "
                            "THEN 'stopping' ELSE ? END, detail=?, updated=? WHERE run_id=? "
                            "AND state NOT IN ('terminated', 'lost_reboot')",
                            (value, value, detail[:500], time.time(), run_id))

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.db.close()


class ServiceManager(Protocol):
    def start(self, unit: str, argv: list[str]) -> None: ...
    def stop(self, unit: str) -> None: ...
    def observe(self, unit: str) -> str:
        """Return running, empty (positively observed ended), gone (not loaded), or unknown."""
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
            stopped = False
            if run.stop_requested:
                self.manager.stop(run.unit)
                stopped = True
            observation = self.manager.observe(run.unit)
            # A gone (not-found) unit is terminated only right after this reconcile's
            # stop returned: systemctl stop waits for the unit to go inactive, then systemd
            # garbage-collects it. A unit that is merely absent may never have launched.
            if observation == 'gone' and stopped:
                observation = 'empty'
            state = {'running': 'stopping' if run.stop_requested else 'running',
                     'empty': 'terminated'}.get(observation, 'unknown')
        except (OSError, subprocess.SubprocessError) as exc:
            state, detail = 'unknown', detail or f'{type(exc).__name__}: {exc}'
        self.registry.state(run.run_id, state, detail)

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
        self._call(['systemctl', '--user', 'stop', '--', unit])

    def observe(self, unit):
        """Read systemd's view of the unit.

        systemd prunes a unit's cgroup once it is empty and the unit is inactive, failed
        or (RemainAfterExit) active/exited, so ControlGroup= is then blank: a loaded unit
        in one of those states with no cgroup is ended. A cgroup that still exists is
        read directly. Not-found is gone (garbage-collected or never launched).
        """
        try:
            text = self._call(['systemctl', '--user', 'show', '--property=LoadState,'
                               'ActiveState,SubState,ControlGroup', '--', unit])
            props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
            group = props.get('ControlGroup', '')
            if not group:
                load, active = props.get('LoadState'), props.get('ActiveState')
                if load == 'not-found' and active == 'inactive':
                    return 'gone'
                if load == 'loaded' and (active in ('inactive', 'failed')
                                         or (active == 'active'
                                             and props.get('SubState') == 'exited')):
                    return 'empty'
                return 'unknown'
            if not group.startswith('/') or '..' in Path(group).parts:
                return 'unknown'
            events = (self.cgroup_root / group.lstrip('/') / 'cgroup.events').read_text()
            populated = dict(line.split() for line in events.splitlines()).get('populated')
            if populated == '0':
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
