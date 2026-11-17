"""DarwinRunManager: one session per run, behind the Linux manager interface.

``ProductionLauncher`` and ``UnitEngineProcess`` call ``prepare``, ``start``,
``discard``, ``unit_identity`` and ``open_main_pidfd`` as they do for an
``EngineUnitManager``. This module does not open the launcher gate. On a Mac
that gate still refuses, because ``os.pidfd_open`` is absent. A test that has
removed ``pidfd_open`` opens the gate in that process only.

The child environment is the FIFO ``ProductionLauncher`` writes after ``start``
returns. ``amend.py`` is the leader: it adds ``RECURSION_RUN_COOKIE`` and execs
the engine line, so the pid and the start time stay the leader's. The cookie
and that start time are written on the run's control record. A later adopt
accepts the pid only when the process table still reports that start time.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time

from recursion_core.daemon import RUN_ID, UNIT, UnitNotLoaded
from recursion_core.daemon.launcher import refused

_AMEND = Path(__file__).with_name('amend.py')
RUN_COOKIE = 'RECURSION_RUN_COOKIE'
_BOUNDARY = '/darwin-pgid/'


class _Leader:
    def __init__(self, unit, pid, group, start_time, cookie, kq_fd, popen, stdio):
        self.unit, self.pid, self.group = unit, pid, group
        self.start_time, self.cookie = start_time, cookie
        self.kq_fd, self.popen, self.stdio = kq_fd, popen, stdio
        self.stopped = False


def _gate() -> None:
    """The launcher gate, looked up on each call so a test can replace it."""

    from recursion_core.daemon import launcher as launcher_mod

    launcher_mod.require_launcher_platform()


def _parse_stdio(properties) -> dict:
    found = {}
    for item in properties:
        if not isinstance(item, str):
            continue
        if item.startswith('StandardInput=file:'):
            found['stdin'] = item[len('StandardInput=file:'):]
        elif item.startswith('StandardOutput=append:'):
            found['stdout'] = item[len('StandardOutput=append:'):]
        elif item.startswith('StandardError=append:'):
            found['stderr'] = item[len('StandardError=append:'):]
    return found


def _ensure_open(path: str, flags: int) -> None:
    fd = os.open(path, flags)
    os.close(fd)


def _ensure_stdio(stdio: dict) -> None:
    for name, path in stdio.items():
        if not isinstance(path, str) or not path or '\0' in path or not os.path.isabs(path):
            raise OSError(errno.EINVAL, 'stdio path')
        nofollow = os.O_NOFOLLOW | os.O_CLOEXEC
        if name == 'stdin':
            if not os.path.lexists(path):
                os.mkfifo(path, 0o600)
            _ensure_open(path, os.O_RDWR | os.O_NONBLOCK | nofollow)
        else:
            if not os.path.lexists(path):
                fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | nofollow, 0o600)
                os.close(fd)
            _ensure_open(path, os.O_RDWR | nofollow)


def _env_fifo(argv) -> str | None:
    try:
        dash = list(argv).index('--')
    except ValueError:
        return None
    if dash < 1:
        return None
    fifo = argv[dash - 1]
    if not isinstance(fifo, str) or not os.path.isabs(fifo) or '\0' in fifo:
        return None
    return fifo


def _run_id(unit: str) -> str:
    if not isinstance(unit, str) or not UNIT.fullmatch(unit):
        raise ValueError('unit')
    run_id = unit[len('recursion-run-'):-len('.service')]
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('unit')
    return run_id


def _boundary(unit: str) -> str:
    return _BOUNDARY + unit


class DarwinRunManager:
    """The service manager a Darwin daemon would hand to ``ProductionLauncher``.

    ``stop`` signals the leader's process group when the start time still
    matches. It does not scan for the cookie and it does not answer empty:
    that attestation is a later draft. ``open_main_pidfd`` returns the kqueue
    fd from ``start``, or None when the pid's start time no longer matches,
    which is the polling path ``UnitEngineProcess`` already has.
    """

    def __init__(self, registry, *, kqueue=None, processes=None):
        if registry is None:
            raise ValueError('registry')
        self._registry = registry
        if processes is None:
            from recursion_core.darwin.proc import ProcTable
            processes = ProcTable()
        if kqueue is None:
            import select
            if hasattr(select, 'kqueue'):
                from recursion_core.darwin.kq import XnuKqueue
                kqueue = XnuKqueue()
        self._kqueue = kqueue
        self._processes = processes
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._runs: dict = {}
        self._by_pid: dict = {}
        self._starting: set = set()

    def prepare(self, unit, *, properties=(), working_directory=None):
        _gate()
        _run_id(unit)
        stdio = _parse_stdio(properties)
        if set(stdio) != {'stdin', 'stdout', 'stderr'}:
            raise OSError(errno.EINVAL, 'stdio properties')
        _ensure_stdio(stdio)
        cwd = None if working_directory is None else str(working_directory)
        with self._lock:
            self._pending[unit] = {'stdio': stdio, 'cwd': cwd}

    def discard(self, unit):
        with self._lock:
            self._pending.pop(unit, None)

    def start(self, unit, argv):
        _gate()
        run_id = _run_id(unit)
        if not argv or not all(isinstance(item, str) and '\0' not in item for item in argv):
            raise ValueError('Empty launch')
        if self._kqueue is None:
            raise refused('kqueue_unavailable')
        with self._lock:
            if unit in self._runs or unit in self._starting:
                raise refused('run_already_launched')
            pending = self._pending.pop(unit, None)
            if pending is None:
                raise OSError(errno.EINVAL, 'stdio not prepared')
            self._starting.add(unit)
        proc = None
        try:
            fifo = _env_fifo(argv)
            if fifo is None:
                raise OSError(errno.EINVAL, 'env fifo missing')
            cookie = secrets.token_hex(16)
            proc = self._spawn(list(argv), pending, cookie)
            info = self._await_leader(proc)
            try:
                kq_fd = self._kqueue.watch_exit(proc.pid)
            except Exception as exc:
                raise refused('kqueue_unavailable') from exc
            stdio = dict(pending['stdio'])
            stdio['env'] = fifo
            # The launcher removes the env FIFO once the leader has read it.
            # Record the type now, while the inode is still there.
            env_is_fifo = stat.S_ISFIFO(os.lstat(fifo).st_mode)
            stdin_is_fifo = stat.S_ISFIFO(os.lstat(stdio['stdin']).st_mode)
            group = _boundary(unit)
            self._put_control(run_id, {
                'run_id': run_id,
                RUN_COOKIE: cookie,
                'cgroup': group,
                'leader_pid': proc.pid,
                'leader_start_time': info['start_time'],
            })
            self._processes.record_launch(
                pid=proc.pid, pgid=info['pgid'], sid=info['sid'],
                start_time=info['start_time'], cookie=cookie, stdio=stdio,
                env_is_fifo=env_is_fifo, stdin_is_fifo=stdin_is_fifo,
            )
            leader = _Leader(unit, proc.pid, group, info['start_time'], cookie, kq_fd, proc, stdio)
            with self._lock:
                self._runs[unit] = leader
                self._by_pid[proc.pid] = leader
            proc = None  # owned by the leader record; the except path must not kill it
        except BaseException:
            if proc is not None:
                self._stop_spawned(proc, proc.pid)
            raise
        finally:
            with self._lock:
                self._starting.discard(unit)

    def unit_identity(self, unit):
        _gate()
        with self._lock:
            leader = self._runs.get(unit)
        if leader is None:
            return '', 0
        return leader.group, leader.pid

    def open_main_pidfd(self, group, pid):
        """The leader's exit fd, or None when this pid is no longer that leader."""

        _gate()
        with self._lock:
            leader = self._by_pid.get(pid)
            if leader is None or leader.group != group:
                return None
            start, fd = leader.start_time, leader.kq_fd
        if self._processes.start_time(pid) != start:
            return None
        return fd

    def adopt(self, unit):
        """Take a run back after a restart. A different start time is ``pid_reused``."""

        _gate()
        run_id = _run_id(unit)
        record = self._read_control(run_id)
        cookie = record.get(RUN_COOKIE) if isinstance(record, dict) else None
        pid = record.get('leader_pid') if isinstance(record, dict) else None
        recorded = record.get('leader_start_time') if isinstance(record, dict) else None
        if not isinstance(cookie, str) or not cookie:
            raise refused('cookie_unrecorded')
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise refused('pid_reused')
        if isinstance(recorded, bool) or not isinstance(recorded, int):
            raise refused('pid_reused')
        if self._processes.start_time(pid) != recorded:
            raise refused('pid_reused')
        if self._kqueue is None:
            raise refused('kqueue_unavailable')
        try:
            kq_fd = self._kqueue.watch_exit(pid)
        except Exception as exc:
            raise refused('kqueue_unavailable') from exc
        group = record.get('cgroup') if isinstance(record.get('cgroup'), str) else _boundary(unit)
        leader = _Leader(unit, pid, group, recorded, cookie, kq_fd, None, {})
        with self._lock:
            self._runs[unit] = leader
            self._by_pid[pid] = leader
        return group, pid

    def stop(self, unit):
        """Signal the leader's group when the start time still matches.

        This is not the attested Stop. A cookie scan and an empty answer are
        a later draft. An unknown unit is ``UnitNotLoaded``, as systemd is.
        """

        _gate()
        _run_id(unit)
        with self._lock:
            leader = self._runs.get(unit)
        if leader is None:
            raise UnitNotLoaded(unit)
        if self._processes.start_time(leader.pid) != leader.start_time:
            raise refused('pid_reused')
        self._stop_spawned(leader.popen, leader.pid)
        leader.stopped = True

    def observe(self, unit):
        _gate()
        with self._lock:
            leader = self._runs.get(unit)
        if leader is None or leader.stopped:
            return 'unknown'
        if self._processes.start_time(leader.pid) != leader.start_time:
            return 'unknown'
        # Empty is the later Stop draft's answer. A live leader is running.
        return 'running'

    def processes_gone(self, unit):
        """False. This draft does not attest that the run is gone."""

        return False

    def recorded_cgroup_clear(self, unit, group):
        """None. This draft cannot tell, and does not call an empty cgroup gone."""

        return None

    def owned_units(self):
        with self._lock:
            return set(self._runs)

    def _await_leader(self, proc):
        deadline = time.monotonic() + 2.0
        info = None
        while time.monotonic() < deadline:
            info = self._processes.read(proc.pid)
            if (info and info.get('pgid') == proc.pid and info.get('sid') == proc.pid
                    and isinstance(info.get('start_time'), int) and not isinstance(info.get('start_time'), bool)):
                return info
            if proc.poll() is not None:
                break
            time.sleep(0.01)
        if info is None or not isinstance(info.get('start_time'), int):
            raise refused('start_time_unreadable')
        raise OSError(errno.EPERM, 'session was not split')

    def _spawn(self, argv, pending, cookie):
        stdio = pending['stdio']
        read_fd, write_fd = os.pipe()
        stdin_fd = stdout_fd = stderr_fd = None
        try:
            os.write(write_fd, cookie.encode('ascii'))
            os.close(write_fd)
            write_fd = -1
            nofollow = os.O_NOFOLLOW | os.O_CLOEXEC
            stdin_fd = os.open(stdio['stdin'], os.O_RDONLY | os.O_NONBLOCK | nofollow)
            stdout_fd = os.open(stdio['stdout'], os.O_WRONLY | os.O_APPEND | nofollow)
            stderr_fd = os.open(stdio['stderr'], os.O_WRONLY | os.O_APPEND | nofollow)
            command = [sys.executable, '-I', '-S', str(_AMEND), str(read_fd), RUN_COOKIE, *argv]
            return subprocess.Popen(
                command,
                stdin=stdin_fd,
                stdout=stdout_fd,
                stderr=stderr_fd,
                cwd=pending['cwd'],
                start_new_session=True,
                pass_fds=(read_fd,),
                env={'PATH': '/usr/bin:/bin'},
            )
        finally:
            for fd in (read_fd, write_fd, stdin_fd, stdout_fd, stderr_fd):
                if isinstance(fd, int) and fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def _put_control(self, run_id, fields):
        current = self._read_control(run_id)
        if not isinstance(current, dict):
            current = {}
        current.update(fields)
        current['run_id'] = run_id
        try:
            self._registry.control_put(run_id, json.dumps(current, sort_keys=True))
            written = self._read_control(run_id)
        except Exception as exc:
            raise refused('cookie_unrecorded') from exc
        if not isinstance(written, dict) or written.get(RUN_COOKIE) != fields[RUN_COOKIE]:
            raise refused('cookie_unrecorded')
        if written.get('leader_pid') != fields['leader_pid']:
            raise refused('cookie_unrecorded')
        if written.get('leader_start_time') != fields['leader_start_time']:
            raise refused('cookie_unrecorded')

    def _read_control(self, run_id):
        try:
            rows = self._registry.control_all()
        except Exception as exc:
            raise refused('cookie_unrecorded') from exc
        for stored_id, text in rows:
            if stored_id != run_id:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError) as exc:
                raise refused('cookie_unrecorded') from exc
            if not isinstance(parsed, dict):
                raise refused('cookie_unrecorded')
            return parsed
        return {}

    def _stop_spawned(self, popen, pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            if popen is not None:
                try:
                    popen.kill()
                except OSError:
                    pass
        if popen is None:
            return
        try:
            popen.wait(timeout=2)
        except Exception:
            pass
