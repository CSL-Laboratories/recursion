"""Launch a run through DarwinRunManager on a simulated Darwin host.

The child sets ``sys.platform`` to ``darwin`` and removes ``os.pidfd_open``, the
same simulation the host start-path harness uses. Path syntax and ``/proc`` stay
those of this Linux host. The child does not open a real kqueue and does not
talk to launchd.

Today the launcher gate raises ``PlatformUnsupported`` before any run exists.
After the manager is in place, this test opens that gate in the child only.
The fakes must then see a new session, the stdio paths, the run cookie and the
leader's start time. A pid whose start time changed is not adopted.
"""

from __future__ import annotations

import errno
try:
    import fcntl
except ImportError:  # Windows: the simulated Darwin launch needs POSIX calls, and its tests skip
    fcntl = None
import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from pathlib import Path

_FLAG = '--simulate-darwin-launch'
_ANSWER = '--simulate-darwin-answer'
_MARK = 'SIMULATED_DARWIN_LAUNCH '
_LAUNCHER = (
    'platform_unsupported: the engine launcher needs Linux '
    '(FIFO stdio, FIONREAD, pidfd, systemd user units)'
)
_RUN_ID = 'dlaunch1'


def _apply() -> None:
    # Cache the host build data first. sysconfig picks its data file from sys.platform,
    # and a darwin name on this host has no such file.
    sysconfig.get_config_vars()
    sys.platform = 'darwin'
    if hasattr(os, 'pidfd_open'):
        delattr(os, 'pidfd_open')


def _refuse_at_the_gate() -> dict:
    from recursion_core.daemon.launcher import require_launcher_platform

    try:
        require_launcher_platform()
    except Exception as exc:  # noqa: BLE001 - the gate's refusal is the result
        return {'ok': False, 'exc_type': type(exc).__name__, 'message': str(exc)}
    return {'ok': False, 'exc_type': 'missing', 'message': 'the launcher gate did not refuse'}


def _launch() -> dict:
    import stat

    _apply()
    try:
        from recursion_core.darwin.kq import EVFILT_PROC, NOTE_EXIT, NOTE_TRACK, FakeKqueue, exit_watch
        from recursion_core.darwin.launcher import RUN_COOKIE, DarwinRunManager
        from recursion_core.darwin.proc import FakeProcessTable
    except ImportError:
        return _refuse_at_the_gate()

    from recursion_core.daemon import Daemon, Registry
    from recursion_core.daemon.launcher import (
        PlatformUnsupported, ProductionLauncher, RunContext, UnitKillTree, bound_run,
    )
    import recursion_core.daemon.launcher as launcher_mod

    scratch = Path(tempfile.mkdtemp(prefix='dl'))
    os.chmod(scratch, 0o700)
    state = scratch / 'state'
    out = scratch / 'child.json'
    standin = scratch / 'standin.py'
    standin.write_text(
        '#!' + sys.executable + '\n'
        'import json, os, sys\n'
        'with open(sys.argv[1], "w") as handle:\n'
        '    json.dump({"cookie": os.environ.get("RECURSION_RUN_COOKIE"),\n'
        '               "pid": os.getpid(), "pgid": os.getpgid(0), "sid": os.getsid(0)}, handle)\n'
    )
    os.chmod(standin, 0o700)
    process = daemon = None
    try:
        registry = Registry(state)
        kqueue = FakeKqueue()
        processes = FakeProcessTable()
        manager = DarwinRunManager(registry, kqueue=kqueue, processes=processes)
        try:
            manager.start('recursion-run-%s.service' % _RUN_ID, ['/bin/true'])
        except PlatformUnsupported as exc:
            gate_type, gate_message = type(exc).__name__, str(exc)
        else:
            return {'ok': False, 'exc_type': 'missing', 'message': 'the manager launched with the gate shut'}

        # This process only. Production still calls the real gate.
        launcher_mod.require_launcher_platform = lambda: None
        daemon = Daemon(registry, manager, boot_id=lambda: 'boot-darwin')
        ctx = RunContext(_RUN_ID)
        with bound_run(ctx):
            process = ProductionLauncher(
                'claude_code', daemon, manager, state, start_timeout_s=8,
            )([str(standin), str(out)], env={'PATH': '/usr/bin:/bin'})
        deadline = time.monotonic() + 5
        while not out.exists():
            if time.monotonic() >= deadline:
                return {'ok': False, 'exc_type': 'Timeout', 'message': 'child did not write its environment'}
            time.sleep(0.05)
        child = json.loads(out.read_text())
        run_dir = state / 'runs' / _RUN_ID
        run_dir_fifos = sorted(name for name in os.listdir(run_dir)
                               if stat.S_ISFIFO(os.lstat(run_dir / name).st_mode))
        rows = dict(registry.control_all())
        record = json.loads(rows[_RUN_ID])
        launch = processes.launches[0]
        watch = kqueue.watches[0]
        group, pid = manager.unit_identity('recursion-run-%s.service' % _RUN_ID)
        fd = manager.open_main_pidfd(group, pid)
        import select
        kqueue.report_exit(pid)
        readable = bool(select.select([fd], [], [], 0)[0]) if isinstance(fd, int) else False
        same = DarwinRunManager(registry, kqueue=FakeKqueue(), processes=processes)
        readopted = same.adopt('recursion-run-%s.service' % _RUN_ID)
        unit = 'recursion-run-%s.service' % _RUN_ID
        try:
            manager.start(unit, ['/bin/true'])
        except Exception as exc:  # noqa: BLE001 - a second caller is refused
            second_detail = getattr(exc, 'detail', '')
        else:
            second_detail = 'launched'
        nok = DarwinRunManager(registry, kqueue=None, processes=processes)
        other = 'recursion-run-dlaunch2.service'
        paths = launch['stdio']
        nok.prepare(other, properties=(
            'StandardInput=file:' + paths['stdin'],
            'StandardOutput=append:' + paths['stdout'],
            'StandardError=append:' + paths['stderr'],
        ))
        try:
            nok.start(other, ['/bin/true'])
        except Exception as exc:  # noqa: BLE001 - no kqueue is a refusal, and nothing is spawned
            kqueue_detail = getattr(exc, 'detail', '')
        else:
            kqueue_detail = 'launched'
        processes.spoof_start_time(pid, launch['start_time'] + 1)
        reused_detail = ''
        try:
            DarwinRunManager(registry, kqueue=FakeKqueue(), processes=processes).adopt(
                'recursion-run-%s.service' % _RUN_ID)
        except Exception as exc:  # noqa: BLE001 - the refusal is the result
            reused_detail = getattr(exc, 'detail', '') or str(exc)
            reused_adopted = False
        else:
            reused_adopted = True
        stdio = launch['stdio']
        return {
            'ok': True,
            'gate_type': gate_type,
            'gate_message': gate_message,
            'session_split': launch['pgid'] == launch['pid'] and launch['sid'] == launch['pid'],
            'child_session': child['pgid'] == child['pid'] and child['sid'] == child['pid']
            and child['pid'] == pid,
            'stdin_fifo': stat.S_ISFIFO(os.lstat(stdio['stdin']).st_mode) and launch.get('stdin_is_fifo') is True,
            'env_fifo': launch.get('env_is_fifo') is True,
            'stdout_file': stat.S_ISREG(os.lstat(stdio['stdout']).st_mode),
            'stderr_file': stat.S_ISREG(os.lstat(stdio['stderr']).st_mode),
            'cookie': record.get(RUN_COOKIE),
            'cookie_in_child': child.get('cookie') == record.get(RUN_COOKIE) == launch.get('cookie'),
            'cookie_shape': isinstance(record.get(RUN_COOKIE), str)
            and len(record.get(RUN_COOKIE)) == 32
            and all(ch in '0123456789abcdef' for ch in record.get(RUN_COOKIE)),
            'start_time': launch.get('start_time'),
            'start_recorded': record.get('leader_start_time') == launch.get('start_time'),
            'filter': watch['filter'],
            'fflags': watch['fflags'],
            'exit_watch': list(exit_watch()),
            'note_exit': watch['fflags'] == NOTE_EXIT and watch['filter'] == EVFILT_PROC,
            'note_track': bool(watch['fflags'] & NOTE_TRACK),
            'exit_wakes': readable,
            'cgroup': group,
            'run_id_of': UnitKillTree.run_id_of(group),
            'pid': pid,
            'readopted_pid': readopted[1],
            'pidfd_is_int': isinstance(fd, int),
            'pidfd_after_reuse': manager.open_main_pidfd(group, pid) is None,
            'reused_adopted': reused_adopted,
            'reuse_detail': reused_detail,
            'second_detail': second_detail,
            'kqueue_detail': kqueue_detail,
            'real_kqueue': hasattr(__import__('select'), 'kqueue'),
            'run_dir_fifos': run_dir_fifos,
        }
    except Exception as exc:  # noqa: BLE001 - the child reports the failure instead of a traceback alone
        import traceback
        return {'ok': False, 'exc_type': type(exc).__name__, 'message': str(exc),
                'trace': traceback.format_exc()[-2500:]}
    finally:
        if process is not None:
            process.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(scratch, ignore_errors=True)


def _child_main() -> int:
    sys.stdout.write('\n' + _MARK + json.dumps(_launch()) + '\n')
    return 0


def _unit_safe_or_skip(scratch: str) -> None:
    """Skip, with the reason, when ProductionLauncher would refuse state under ``scratch``."""

    from recursion_core.daemon.launcher import _unit_path_ok

    for path in (scratch, os.path.realpath(scratch)):
        if not _unit_path_ok(path):
            raise unittest.SkipTest('the scratch root %r is not unit-safe: the launcher refuses state there'
                                    % path)


def simulate() -> dict:
    # The child makes every path with tempfile under this directory, which it gets as TMPDIR.
    with tempfile.TemporaryDirectory(prefix='dl-') as scratch:
        _unit_safe_or_skip(scratch)
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['TMPDIR'] = scratch
        proc = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()), _FLAG],
            capture_output=True, text=True, timeout=40, env=env, check=False,
        )
    line = next((item for item in reversed(proc.stdout.splitlines()) if item.startswith(_MARK)), None)
    if line is None:
        raise AssertionError(
            'simulated-darwin launch gave no result (exit %s):\n%s\n%s'
            % (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:]))
    return json.loads(line[len(_MARK):])


def _proc_state(pid: int) -> str:
    try:
        text = Path('/proc/%d/stat' % pid).read_text()
    except OSError as exc:
        return 'gone:%s' % exc.errno
    try:
        return text[text.rindex(')') + 2:].split()[0]
    except (ValueError, IndexError):
        return 'bad'


def _child_pids(pid: int) -> list[dict]:
    path = Path('/proc/%d/task/%d/children' % (pid, pid))
    try:
        raw = path.read_text().split()
    except OSError as exc:
        return [{'error': exc.errno}]
    found = []
    for item in raw:
        try:
            cpid = int(item)
        except ValueError:
            continue
        found.append({'pid': cpid, 'state': _proc_state(cpid)})
    return found


def _fd_open(fd: int) -> bool:
    try:
        fcntl.fcntl(fd, fcntl.F_GETFD)
    except OSError:
        return False
    return True


class _Held:
    """One simulated launch. ``close`` reaps only the child this launch created."""

    def __init__(self) -> None:
        self.scratch: Path | None = None
        self.popen = None
        self.kqueue = None
        self.fds: list[int] = []

    def close(self) -> None:
        for fd in self.fds:
            if _fd_open(fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.fds.clear()
        popen = self.popen
        if popen is not None:
            if popen.poll() is None:
                popen.kill()
            try:
                popen.wait(timeout=2)
            except Exception:
                pass
        if self.scratch is not None:
            shutil.rmtree(self.scratch, ignore_errors=True)


def _bind() -> tuple:
    """Open the gate in this process only, the same way the launch test does."""

    _apply()
    import recursion_core.daemon.launcher as gate_mod
    import recursion_core.darwin.launcher as launcher_mod
    from recursion_core.daemon import Registry, engine_exec
    from recursion_core.darwin.kq import FakeKqueue
    from recursion_core.darwin.launcher import RUN_COOKIE, DarwinRunManager
    from recursion_core.darwin.proc import ProcTable

    gate_mod.require_launcher_platform = lambda: None
    return launcher_mod, Registry, engine_exec, FakeKqueue, RUN_COOKIE, DarwinRunManager, ProcTable


def _begin(run_id: str, kqueue=None, processes=None) -> tuple:
    launcher_mod, Registry, engine_exec, FakeKqueue, RUN_COOKIE, DarwinRunManager, ProcTable = _bind()
    held = _Held()
    held.scratch = Path(tempfile.mkdtemp(prefix='ans-'))
    os.chmod(held.scratch, 0o700)
    scratch = held.scratch
    stdin, stdout, stderr = scratch / 'stdin.fifo', scratch / 'stdout.log', scratch / 'stderr.log'
    envfifo = scratch / 'env.fifo'
    os.mkfifo(envfifo, 0o600)
    registry = Registry(scratch / 'state')
    if kqueue is None:
        kqueue = FakeKqueue()
    held.kqueue = kqueue
    manager = DarwinRunManager(registry, kqueue=kqueue,
                               processes=ProcTable() if processes is None else processes)
    unit = 'recursion-run-%s.service' % run_id
    manager.prepare(unit, properties=(
        'StandardInput=file:' + str(stdin),
        'StandardOutput=append:' + str(stdout),
        'StandardError=append:' + str(stderr),
    ))
    return held, manager, unit, envfifo, registry, RUN_COOKIE, engine_exec, launcher_mod


def _publish_env(path: Path) -> None:
    payload = json.dumps({'PATH': '/usr/bin:/bin'}).encode()
    blob = ('%d\n' % len(payload)).encode() + payload
    deadline = time.monotonic() + 2
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno == errno.ENXIO and time.monotonic() < deadline:
                time.sleep(0.02)
                continue
            raise
        break
    try:
        while blob:
            wrote = os.write(fd, blob)
            if wrote <= 0:
                raise OSError(errno.EIO, 'short env write')
            blob = blob[wrote:]
    finally:
        os.close(fd)


def _wait_path(path: Path, popen, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if popen is not None and popen.poll() is not None:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _standin(scratch: Path, body: str) -> Path:
    path = scratch / 'standin.py'
    path.write_text('#!' + sys.executable + '\n' + body)
    os.chmod(path, 0o700)
    return path


def _argv(engine_exec, envfifo: Path, standin: Path, args: list[str]) -> list[str]:
    return [sys.executable, '-I', '-S', str(Path(engine_exec.__file__)), str(envfifo),
            '--', str(standin), *args]


def _answer_stdin() -> dict:
    held, manager, unit, envfifo, registry, cookie_name, engine_exec, _launcher = _begin('stdin1')
    try:
        out = held.scratch / 'child.json'
        ready = held.scratch / 'ready'
        standin = _standin(held.scratch, (
            'import fcntl, json, os, sys\n'
            'out, ready = sys.argv[1], sys.argv[2]\n'
            'flags = fcntl.fcntl(0, fcntl.F_GETFL)\n'
            'fdinfo = open("/proc/self/fdinfo/0").read()\n'
            'line = ""\n'
            'for item in fdinfo.splitlines():\n'
            '    if item.startswith("flags:"):\n'
            '        line = item\n'
            'json.dump({"nonblock": bool(flags & os.O_NONBLOCK), "flags": flags,\n'
            '           "fdinfo_flags": line}, open(out, "w"))\n'
            'open(ready, "w").close()\n'
        ))
        manager.start(unit, _argv(engine_exec, envfifo, standin, [str(out), str(ready)]))
        leader = manager._runs[unit]
        held.popen = leader.popen
        _publish_env(envfifo)
        if not _wait_path(ready, held.popen, 5):
            return {'ok': False, 'message': 'stdin child did not report',
                    'state': _proc_state(leader.pid), 'code': held.popen.returncode}
        child = json.loads(out.read_text())
        child['ok'] = True
        return child
    finally:
        held.close()


def _answer_exitfd() -> dict:
    held, manager, unit, envfifo, _registry, _cookie, engine_exec, _launcher = _begin('exitfd1')
    try:
        standin = _standin(held.scratch, 'import time\ntime.sleep(30)\n')
        manager.start(unit, _argv(engine_exec, envfifo, standin, []))
        leader = manager._runs[unit]
        held.popen = leader.popen
        group, pid = manager.unit_identity(unit)
        fd_a = manager.open_main_pidfd(group, pid)
        fd_b = manager.open_main_pidfd(group, pid)
        kq = held.kqueue.watches[0]['fd']
        held.fds.extend(fd for fd in (fd_a, fd_b) if isinstance(fd, int))
        distinct = isinstance(fd_a, int) and isinstance(fd_b, int) and fd_a != fd_b and fd_a != kq and fd_b != kq
        if isinstance(fd_a, int):
            os.close(fd_a)
            held.fds.remove(fd_a)
        other_alive = isinstance(fd_b, int) and _fd_open(fd_b)
        kq_alive = _fd_open(kq)
        marker = None
        marker_alive = False
        if isinstance(fd_a, int):
            held_null = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
            if held_null == fd_a:
                marker = held_null
            else:
                os.dup2(held_null, fd_a)
                os.close(held_null)
                marker = fd_a
            held.fds.append(marker)
            held.kqueue.close()
            held.kqueue = None
            marker_alive = _fd_open(marker)
        return {
            'ok': True,
            'distinct': distinct,
            'other_alive': other_alive,
            'kq_alive': kq_alive,
            'marker_alive': marker_alive,
        }
    finally:
        held.close()


def _answer_stop() -> dict:
    held, manager, unit, envfifo, _registry, cookie_name, engine_exec, launcher_mod = _begin('ours1')
    calls: list[list[int]] = []
    real = launcher_mod.os.killpg

    def record(pid, sig):
        calls.append([int(pid), int(sig)])
        raise OSError(errno.EPERM, 'recorded, not sent')

    launcher_mod.os.killpg = record
    try:
        def adopt_stop(run_id: str, target: int) -> dict:
            from recursion_core.daemon import Registry
            from recursion_core.darwin.kq import FakeKqueue
            from recursion_core.darwin.launcher import DarwinRunManager
            from recursion_core.darwin.proc import ProcTable

            reg = Registry(held.scratch / run_id)
            table = ProcTable()
            mgr = DarwinRunManager(reg, kqueue=FakeKqueue(), processes=table)
            start = table.start_time(target)
            body = {
                'run_id': run_id,
                cookie_name: 'ab' * 16,
                'cgroup': '/darwin-pgid/recursion-run-%s.service' % run_id,
                'leader_pid': target,
                'leader_start_time': start,
            }
            reg.control_put(run_id, json.dumps(body))
            before = len(calls)
            adopted = None
            detail = None
            try:
                adopted = mgr.adopt('recursion-run-%s.service' % run_id)
                mgr.stop('recursion-run-%s.service' % run_id)
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                detail = getattr(exc, 'detail', None) or type(exc).__name__
            return {
                'target': target,
                'start': start,
                'adopted': list(adopted) if adopted else None,
                'detail': detail,
                'calls': calls[before:],
            }

        pid1 = adopt_stop('pidone', 1)
        pgrp_id = os.getpgrp()
        pgrp = adopt_stop('pgroup', pgrp_id)
        standin = _standin(held.scratch, 'import time\ntime.sleep(30)\n')
        manager.start(unit, _argv(engine_exec, envfifo, standin, []))
        leader = manager._runs[unit]
        held.popen = leader.popen
        before = len(calls)
        own_detail = None
        try:
            manager.stop(unit)
        except Exception as exc:  # noqa: BLE001 - a refusal here is the result
            own_detail = getattr(exc, 'detail', None) or type(exc).__name__
        return {
            'ok': True,
            'pid1': pid1,
            'pgrp': pgrp,
            'own_pid': leader.pid,
            'own_pgrp': pgrp_id,
            'own_detail': own_detail,
            'own_calls': calls[before:],
        }
    finally:
        launcher_mod.os.killpg = real
        held.close()


def _answer_reap() -> dict:
    held, manager, unit, envfifo, _registry, _cookie, engine_exec, _launcher = _begin('reap1')
    try:
        ready = held.scratch / 'ready'
        release = held.scratch / 'release'
        standin = _standin(held.scratch, (
            'import os, sys, time\n'
            'ready, release = sys.argv[1], sys.argv[2]\n'
            'open(ready, "w").close()\n'
            'deadline = time.time() + 15\n'
            'while not os.path.exists(release):\n'
            '    if time.time() > deadline:\n'
            '        break\n'
            '    time.sleep(0.02)\n'
        ))
        manager.start(unit, _argv(engine_exec, envfifo, standin, [str(ready), str(release)]))
        leader = manager._runs[unit]
        held.popen = leader.popen
        _publish_env(envfifo)
        if not _wait_path(ready, held.popen, 5):
            return {'ok': False, 'message': 'reap child did not become ready',
                    'state': _proc_state(leader.pid), 'code': held.popen.returncode}
        kids = _child_pids(leader.pid)
        zombies = [item for item in kids if item.get('state') == 'Z']
        release.write_text('go')
        deadline = time.monotonic() + 1.0
        state = _proc_state(leader.pid)
        code = held.popen.returncode
        while time.monotonic() < deadline:
            state = _proc_state(leader.pid)
            code = held.popen.returncode
            if code is not None and state != 'Z':
                break
            time.sleep(0.02)
        return {
            'ok': True,
            'zombie_children': zombies,
            'leader_state': state,
            'returncode': code,
            'observe_after': manager.observe(unit),
        }
    finally:
        held.close()


def _answer_rules() -> dict:
    held, manager, unit, envfifo, registry, cookie_name, engine_exec, _launcher = _begin('rules1')
    try:
        out = held.scratch / 'child.json'
        ready = held.scratch / 'ready'
        release = held.scratch / 'release'
        standin = _standin(held.scratch, (
            'import json, os, sys, time\n'
            'out, ready, release = sys.argv[1:4]\n'
            'cmd = open("/proc/self/cmdline", "rb").read().decode("latin1")\n'
            'json.dump({"cmd": cmd}, open(out, "w"))\n'
            'open(ready, "w").close()\n'
            'deadline = time.time() + 15\n'
            'while not os.path.exists(release):\n'
            '    if time.time() > deadline:\n'
            '        break\n'
            '    time.sleep(0.02)\n'
        ))
        manager.start(unit, _argv(engine_exec, envfifo, standin, [str(out), str(ready), str(release)]))
        leader = manager._runs[unit]
        held.popen = leader.popen
        amend_cmd = Path('/proc/%d/cmdline' % leader.pid).read_bytes()
        rows = dict(registry.control_all())
        record = json.loads(rows['rules1'])
        cookie = record.get(cookie_name)
        _publish_env(envfifo)
        if not _wait_path(ready, held.popen, 5):
            return {'ok': False, 'message': 'rules child did not become ready',
                    'state': _proc_state(leader.pid)}
        child = json.loads(out.read_text())
        group, _pid = manager.unit_identity(unit)
        # Sample while the leader is still alive. A later wait must not
        # change what these rules said about the live run.
        gone = manager.processes_gone(unit)
        clear = manager.recorded_cgroup_clear(unit, group)
        observed = manager.observe(unit)
        cookie_bytes = cookie.encode('ascii') if isinstance(cookie, str) else b'\x00none'
        release.write_text('go')
        return {
            'ok': True,
            'gone': gone,
            'clear': clear,
            'observe': observed,
            'cookie_len': len(cookie) if isinstance(cookie, str) else None,
            'cookie_on_amend': isinstance(cookie, str) and cookie_bytes in amend_cmd,
            'cookie_on_engine': isinstance(cookie, str) and cookie in child.get('cmd', ''),
        }
    finally:
        held.close()


class _WatchRefused:
    """A kqueue whose watch fails. ``OSError(ESRCH)`` is XNU's answer for a pid that already exited."""

    def __init__(self, err) -> None:
        self.err = err
        self.calls: list[int] = []

    def watch_exit(self, pid: int) -> int:
        self.calls.append(pid)
        if self.err is None:
            raise RuntimeError('watch failed')
        raise OSError(self.err, os.strerror(self.err))


class _BlindTable:
    """A process table that cannot read the leader: the start-time check never passes."""

    def read(self, pid: int):
        return None

    def start_time(self, pid: int):
        return None

    def record_launch(self, **fields) -> None:
        raise AssertionError('recorded a launch the start-time check did not pass')


def _readable(fd) -> bool:
    import select

    return isinstance(fd, int) and bool(select.select([fd], [], [], 0)[0])


def _start_with(run_id: str, kqueue, processes=None) -> dict:
    """Start one sleeping leader through ``kqueue``; what a caller of the manager sees."""

    held, manager, unit, envfifo, registry, cookie_name, engine_exec, _launcher = _begin(
        run_id, kqueue=kqueue, processes=processes)
    try:
        standin = _standin(held.scratch, 'import time\ntime.sleep(30)\n')
        detail = None
        try:
            manager.start(unit, _argv(engine_exec, envfifo, standin, []))
        except Exception as exc:  # noqa: BLE001 - a refusal is the result
            detail = getattr(exc, 'detail', None) or type(exc).__name__
        leader = manager._runs.get(unit)
        if leader is not None:
            held.popen = leader.popen
        group, pid = manager.unit_identity(unit)
        fd = manager.open_main_pidfd(group, pid) if pid else None
        if isinstance(fd, int):
            held.fds.append(fd)
        record = json.loads(dict(registry.control_all()).get(run_id, '{}'))
        cookie = record.get(cookie_name)
        spawned = kqueue.calls[0] if kqueue.calls else None
        return {
            'detail': detail,
            'calls': list(kqueue.calls),
            'pid': pid,
            'group': group,
            'exit_delivered': _readable(fd),
            'cookie_recorded': isinstance(cookie, str) and len(cookie) == 32,
            'start_recorded': leader is not None and record.get('leader_pid') == pid
            and record.get('leader_start_time') == leader.start_time,
            'owned': sorted(manager.owned_units()),
            'spawned_state': _proc_state(spawned) if isinstance(spawned, int) else None,
        }
    finally:
        held.close()


def _answer_esrch_start() -> dict:
    result = _start_with('esrch1', _WatchRefused(errno.ESRCH))
    result['ok'] = True
    return result


def _answer_esrch_adopt() -> dict:
    _launcher_mod, Registry, _exec, _fake, cookie_name, DarwinRunManager, ProcTable = _bind()
    scratch = Path(tempfile.mkdtemp(prefix='adopt-'))
    target = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        table = ProcTable()
        start = table.start_time(target.pid)

        def adopt(run_id: str, err, recorded, cookie=True) -> dict:
            reg = Registry(scratch / run_id)
            body = {
                'run_id': run_id,
                'cgroup': '/darwin-pgid/recursion-run-%s.service' % run_id,
                'leader_pid': target.pid,
                'leader_start_time': recorded,
            }
            if cookie:
                body[cookie_name] = 'cd' * 16
            reg.control_put(run_id, json.dumps(body))
            kq = _WatchRefused(err)
            mgr = DarwinRunManager(reg, kqueue=kq, processes=table)
            adopted = detail = None
            try:
                adopted = mgr.adopt('recursion-run-%s.service' % run_id)
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                detail = getattr(exc, 'detail', None) or type(exc).__name__
            fd = mgr.open_main_pidfd(*adopted) if adopted else None
            delivered = _readable(fd)
            if isinstance(fd, int):
                os.close(fd)
            return {'adopted': list(adopted) if adopted else None, 'detail': detail,
                    'calls': list(kq.calls), 'exit_delivered': delivered}

        later = start + 1 if isinstance(start, int) else 1
        return {
            'ok': True,
            'pid': target.pid,
            'start': start,
            'esrch': adopt('gone1', errno.ESRCH, start),
            'esrch_reused': adopt('gone2', errno.ESRCH, later),
            'esrch_no_cookie': adopt('gone3', errno.ESRCH, start, cookie=False),
            'eperm': adopt('gone4', errno.EPERM, start),
            'other': adopt('gone5', None, start),
        }
    finally:
        target.kill()
        try:
            target.wait(timeout=2)
        except Exception:
            pass
        shutil.rmtree(scratch, ignore_errors=True)


def _answer_watch_refused() -> dict:
    return {
        'ok': True,
        'eperm': _start_with('eperm1', _WatchRefused(errno.EPERM)),
        'other': _start_with('other1', _WatchRefused(None)),
        'esrch_unchecked': _start_with('blind1', _WatchRefused(errno.ESRCH), processes=_BlindTable()),
    }


class _Forgetful:
    """A process table that loses every row once the exit watch is asked: the leader was reaped since its check."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.forgot = False

    def read(self, pid: int):
        return None if self.forgot else self.inner.read(pid)

    def start_time(self, pid: int):
        got = self.read(pid)
        return got.get('start_time') if got else None

    def record_launch(self, **fields) -> None:
        pass


class _ForgetThenEsrch(_WatchRefused):
    """ESRCH from a watch asked after the start-time check, with the table's row gone by then."""

    def __init__(self, table) -> None:
        super().__init__(errno.ESRCH)
        self.table = table

    def watch_exit(self, pid: int) -> int:
        self.table.forgot = True
        return super().watch_exit(pid)


class _ExitsThenEsrch(_WatchRefused):
    """A leader that exits before the watch is asked: this test ends its own child, then ESRCH is XNU's answer."""

    def __init__(self) -> None:
        super().__init__(errno.ESRCH)

    def watch_exit(self, pid: int) -> int:
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = _proc_state(pid)
            if state == 'Z' or state.startswith('gone'):
                break
            time.sleep(0.01)
        return super().watch_exit(pid)


def _answer_esrch_forgotten() -> dict:
    _launcher_mod, _registry, _exec, _fake, _cookie, _manager, ProcTable = _bind()
    table = _Forgetful(ProcTable())
    result = _start_with('forgot1', _ForgetThenEsrch(table), processes=table)
    result['ok'] = True
    result['forgot'] = table.forgot
    return result


def _answer_esrch_owner() -> dict:
    import gc

    _launcher_mod, Registry, _exec, _fake, cookie_name, DarwinRunManager, ProcTable = _bind()
    scratch = Path(tempfile.mkdtemp(prefix='own-'))
    target = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        table = ProcTable()
        start = table.start_time(target.pid)
        run_id = 'owned1'
        unit = 'recursion-run-%s.service' % run_id
        reg = Registry(scratch / run_id)
        reg.control_put(run_id, json.dumps({
            'run_id': run_id,
            cookie_name: 'ef' * 16,
            'cgroup': '/darwin-pgid/%s' % unit,
            'leader_pid': target.pid,
            'leader_start_time': start,
        }))
        mgr = DarwinRunManager(reg, kqueue=_WatchRefused(errno.ESRCH), processes=table)
        group, pid = mgr.adopt(unit)
        original = mgr._runs[unit].kq_fd
        dup = mgr.open_main_pidfd(group, pid)
        before = _readable(original) and _readable(dup)
        del mgr
        gc.collect()
        try:
            os.fstat(original)
        except OSError as exc:
            original_shut = exc.errno == errno.EBADF
        else:
            original_shut = False
        dup_readable = _readable(dup)
        if isinstance(dup, int):
            os.close(dup)
        return {
            'ok': True,
            'before': before,
            'distinct': isinstance(dup, int) and dup != original,
            'original_shut': original_shut,
            'dup_readable': dup_readable,
        }
    finally:
        target.kill()
        try:
            target.wait(timeout=2)
        except Exception:
            pass
        shutil.rmtree(scratch, ignore_errors=True)


def _answer_stop_survivors() -> dict:
    held, manager, unit, envfifo, _registry, _cookie, engine_exec, _launcher = _begin('surv1')
    survivor = None
    try:
        out = held.scratch / 'survivor'
        standin = _standin(held.scratch, (
            'import os, subprocess, sys\n'
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])\n'
            'with open(sys.argv[1] + ".part", "w") as handle:\n'
            '    handle.write(str(child.pid))\n'
            'os.replace(sys.argv[1] + ".part", sys.argv[1])\n'
        ))
        manager.start(unit, _argv(engine_exec, envfifo, standin, [str(out)]))
        leader = manager._runs[unit]
        held.popen = leader.popen
        _publish_env(envfifo)
        if not _wait_path(out, None, 5):
            return {'ok': False, 'message': 'the leader did not name its child', 'state': _proc_state(leader.pid)}
        survivor = int(out.read_text())
        deadline = time.monotonic() + 5
        while not _proc_state(leader.pid).startswith('gone') and time.monotonic() < deadline:
            time.sleep(0.02)
        leader_state = _proc_state(leader.pid)
        before = _proc_state(survivor)
        detail = None
        try:
            manager.stop(unit)
        except Exception as exc:  # noqa: BLE001 - a refusal is the result
            detail = getattr(exc, 'detail', None) or type(exc).__name__
        deadline = time.monotonic() + 3
        after = _proc_state(survivor)
        while not (after == 'Z' or after.startswith('gone')) and time.monotonic() < deadline:
            time.sleep(0.02)
            after = _proc_state(survivor)
        return {
            'ok': True,
            'leader_state': leader_state,
            'before': before,
            'detail': detail,
            'after': after,
            'observe': manager.observe(unit),
        }
    finally:
        # The survivor is this test's own grandchild: end it if the stop did not.
        if survivor is not None and not _proc_state(survivor).startswith('gone'):
            try:
                os.kill(survivor, signal.SIGKILL)
            except OSError:
                pass
        held.close()


def _answer_esrch_launch() -> dict:
    _apply()
    from recursion_core.daemon import Daemon, Registry
    from recursion_core.daemon.launcher import ProductionLauncher, RunContext, bound_run
    import recursion_core.daemon.launcher as gate_mod
    from recursion_core.darwin.launcher import DarwinRunManager
    from recursion_core.darwin.proc import FakeProcessTable

    gate_mod.require_launcher_platform = lambda: None
    scratch = Path(tempfile.mkdtemp(prefix='esl'))
    os.chmod(scratch, 0o700)
    state = scratch / 'state'
    standin = _standin(scratch, 'import time\ntime.sleep(30)\n')
    kqueue = _ExitsThenEsrch()
    daemon = process = None
    run_id = 'esrchl1'
    try:
        registry = Registry(state)
        manager = DarwinRunManager(registry, kqueue=kqueue, processes=FakeProcessTable())
        daemon = Daemon(registry, manager, boot_id=lambda: 'boot-darwin')
        ctx = RunContext(run_id)
        detail = None
        began = time.monotonic()
        try:
            with bound_run(ctx):
                process = ProductionLauncher(
                    'claude_code', daemon, manager, state, start_timeout_s=1,
                )([str(standin)], env={'PATH': '/usr/bin:/bin'})
        except Exception as exc:  # noqa: BLE001 - the refusal is the result
            detail = '%s: %s' % (type(exc).__name__, getattr(exc, 'detail', None) or exc)
        elapsed = time.monotonic() - began
        leader = kqueue.calls[0] if kqueue.calls else None
        return {
            'ok': True,
            'launched': process is not None,
            'detail': detail,
            'elapsed': elapsed,
            'calls': list(kqueue.calls),
            'leader_state': _proc_state(leader) if isinstance(leader, int) else None,
            'observe': manager.observe('recursion-run-%s.service' % run_id),
        }
    finally:
        if process is not None:
            process.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(scratch, ignore_errors=True)


def _answer(mode: str) -> dict:
    modes = {
        'stdin': _answer_stdin,
        'exitfd': _answer_exitfd,
        'stop': _answer_stop,
        'reap': _answer_reap,
        'rules': _answer_rules,
        'esrch_start': _answer_esrch_start,
        'esrch_adopt': _answer_esrch_adopt,
        'watch_refused': _answer_watch_refused,
        'esrch_forgotten': _answer_esrch_forgotten,
        'esrch_owner': _answer_esrch_owner,
        'stop_survivors': _answer_stop_survivors,
        'esrch_launch': _answer_esrch_launch,
    }
    fn = modes.get(mode)
    if fn is None:
        return {'ok': False, 'message': 'unknown mode'}
    return fn()


def _answer_main() -> int:
    mode = sys.argv[2] if len(sys.argv) > 2 else ''
    try:
        payload = _answer(mode)
    except Exception as exc:  # noqa: BLE001 - the child reports the failure
        import traceback
        payload = {'ok': False, 'exc_type': type(exc).__name__, 'message': str(exc),
                   'trace': traceback.format_exc()[-2500:]}
    sys.stdout.write('\n' + _MARK + json.dumps(payload) + '\n')
    return 0


def run_answer(mode: str) -> dict:
    # The child makes every path with tempfile under this directory, which it gets as TMPDIR.
    with tempfile.TemporaryDirectory(prefix='ans-') as scratch:
        _unit_safe_or_skip(scratch)
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env['TMPDIR'] = scratch
        proc = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()), _ANSWER, mode],
            capture_output=True, text=True, timeout=40, env=env, check=False,
        )
    line = next((item for item in reversed(proc.stdout.splitlines()) if item.startswith(_MARK)), None)
    if line is None:
        raise AssertionError(
            'simulated-darwin answer %s gave no result (exit %s):\n%s\n%s'
            % (mode, proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:]))
    return json.loads(line[len(_MARK):])


@unittest.skipUnless(fcntl is not None and Path('/proc/self/stat').exists(),
                     'the simulated Darwin launch runs on a Linux host: it needs fcntl and /proc')
class DarwinLauncherTests(unittest.TestCase):
    def test_a_launch_records_the_session_the_fifos_the_cookie_and_the_start_time(self) -> None:
        result = simulate()
        self.assertTrue(result['ok'], result)
        self.assertEqual('PlatformUnsupported', result['gate_type'])
        self.assertEqual(_LAUNCHER, result['gate_message'])
        self.assertTrue(result['session_split'], result)
        self.assertTrue(result['child_session'], result)
        self.assertTrue(result['stdin_fifo'] and result['env_fifo'], result)
        self.assertTrue(result['stdout_file'] and result['stderr_file'], result)
        self.assertTrue(result['cookie_in_child'], result)
        self.assertTrue(result['cookie_shape'], result)
        self.assertIsInstance(result['start_time'], int)
        self.assertGreater(result['start_time'], 0)
        self.assertTrue(result['start_recorded'], result)
        self.assertEqual([-5, 0x80000000], result['exit_watch'])
        self.assertTrue(result['note_exit'], result)
        self.assertFalse(result['note_track'], result)
        self.assertEqual(-5, result['filter'])
        self.assertTrue(result['exit_wakes'], result)
        self.assertEqual('/darwin-pgid/recursion-run-%s.service' % _RUN_ID, result['cgroup'])
        self.assertEqual(_RUN_ID, result['run_id_of'])
        self.assertEqual(result['pid'], result['readopted_pid'])
        self.assertGreater(result['readopted_pid'], 0)
        self.assertTrue(result['pidfd_is_int'], result)
        self.assertTrue(result['pidfd_after_reuse'], result)
        self.assertFalse(result['reused_adopted'], result)
        self.assertEqual('pid_reused', result['reuse_detail'])
        self.assertEqual('run_already_launched', result['second_detail'])
        self.assertEqual('kqueue_unavailable', result['kqueue_detail'])
        self.assertFalse(result['real_kqueue'])

    def test_stdin_of_the_engine_is_blocking(self) -> None:
        result = run_answer('stdin')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['nonblock'], result)

    def test_exit_fd_is_a_duplicate_the_caller_owns(self) -> None:
        result = run_answer('exitfd')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['distinct'], result)
        self.assertTrue(result['other_alive'], result)
        self.assertTrue(result['kq_alive'], result)
        self.assertTrue(result['marker_alive'], result)

    def test_stop_refuses_pid_one_and_this_group(self) -> None:
        result = run_answer('stop')
        self.assertTrue(result['ok'], result)
        self.assertEqual([], result['pid1']['calls'], result)
        self.assertEqual('pid_unreachable', result['pid1']['detail'], result)
        self.assertEqual([], result['pgrp']['calls'], result)
        self.assertEqual('pid_unreachable', result['pgrp']['detail'], result)
        self.assertGreater(result['own_pid'], 1)
        self.assertNotEqual(result['own_pgrp'], result['own_pid'])
        self.assertEqual([[result['own_pid'], signal.SIGKILL]], result['own_calls'], result)
        self.assertIsNone(result['own_detail'], result)

    def test_finished_leader_is_waited_and_writer_is_not_its_child(self) -> None:
        result = run_answer('reap')
        self.assertTrue(result['ok'], result)
        self.assertEqual([], result['zombie_children'], result)
        self.assertNotEqual('Z', result['leader_state'], result)
        self.assertIsNotNone(result['returncode'], result)
        self.assertEqual('unknown', result['observe_after'], result)

    def test_stated_rules_have_a_witness(self) -> None:
        result = run_answer('rules')
        self.assertTrue(result['ok'], result)
        self.assertIs(result['gone'], False, result)
        self.assertIsNone(result['clear'], result)
        self.assertEqual('running', result['observe'], result)
        self.assertEqual(32, result['cookie_len'], result)
        self.assertFalse(result['cookie_on_amend'], result)
        self.assertFalse(result['cookie_on_engine'], result)

    def test_esrch_at_start_is_an_exit_already_delivered(self) -> None:
        result = run_answer('esrch_start')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['detail'], result)
        self.assertGreater(result['pid'], 1)
        self.assertEqual([result['pid']], result['calls'], result)
        self.assertEqual('/darwin-pgid/recursion-run-esrch1.service', result['group'])
        self.assertTrue(result['exit_delivered'], result)
        self.assertTrue(result['cookie_recorded'], result)
        self.assertTrue(result['start_recorded'], result)
        self.assertEqual(['recursion-run-esrch1.service'], result['owned'], result)

    def test_esrch_at_adopt_is_an_exit_already_delivered(self) -> None:
        result = run_answer('esrch_adopt')
        self.assertTrue(result['ok'], result)
        self.assertIsInstance(result['start'], int)
        pid = result['pid']
        gone = result['esrch']
        self.assertIsNone(gone['detail'], result)
        self.assertEqual(['/darwin-pgid/recursion-run-gone1.service', pid], gone['adopted'], result)
        self.assertEqual([pid], gone['calls'], result)
        self.assertTrue(gone['exit_delivered'], result)
        # ESRCH before the start-time check or the cookie has passed is never asked for.
        for name, detail in (('esrch_reused', 'pid_reused'), ('esrch_no_cookie', 'cookie_unrecorded')):
            self.assertEqual(detail, result[name]['detail'], result)
            self.assertIsNone(result[name]['adopted'], result)
            self.assertEqual([], result[name]['calls'], result)
        for name in ('eperm', 'other'):
            self.assertEqual('kqueue_unavailable', result[name]['detail'], result)
            self.assertIsNone(result[name]['adopted'], result)
            self.assertEqual([pid], result[name]['calls'], result)

    def test_other_watch_errors_still_refuse_kqueue_unavailable(self) -> None:
        result = run_answer('watch_refused')
        self.assertTrue(result['ok'], result)
        for name in ('eperm', 'other'):
            row = result[name]
            self.assertEqual('kqueue_unavailable', row['detail'], result)
            self.assertEqual(1, len(row['calls']), result)
            self.assertEqual([], row['owned'], result)
            self.assertEqual(0, row['pid'], result)
            self.assertTrue(row['spawned_state'].startswith('gone'), result)
        unchecked = result['esrch_unchecked']
        self.assertEqual('start_time_unreadable', unchecked['detail'], result)
        self.assertEqual([], unchecked['calls'], result)
        self.assertEqual([], unchecked['owned'], result)

    def test_cookie_fifo_name_is_gone_once_the_engine_has_its_environment(self) -> None:
        # The same launch as the first test; this one reads only the run directory.
        result = simulate()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['cookie_in_child'], result)
        self.assertEqual(['stdin.fifo'], result['run_dir_fifos'], result)

    def test_a_scratch_root_the_launcher_refuses_is_a_skip_with_its_reason(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            spaced = Path(root) / 'my dir'
            spaced.mkdir()
            saved = tempfile.tempdir
            tempfile.tempdir = str(spaced)
            try:
                with self.assertRaises(unittest.SkipTest) as caught:
                    simulate()
            finally:
                tempfile.tempdir = saved
        self.assertIn('not unit-safe', str(caught.exception))

    def test_stop_after_the_leader_was_reaped_ends_its_surviving_group(self) -> None:
        result = run_answer('stop_survivors')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['leader_state'].startswith('gone'), result)
        self.assertFalse(result['before'] == 'Z' or result['before'].startswith('gone'), result)
        self.assertIsNone(result['detail'], result)
        self.assertTrue(result['after'] == 'Z' or result['after'].startswith('gone'), result)
        self.assertEqual('unknown', result['observe'], result)

    def test_esrch_exit_reaches_the_caller_after_the_row_is_gone(self) -> None:
        result = run_answer('esrch_forgotten')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['forgot'], result)
        self.assertIsNone(result['detail'], result)
        self.assertEqual([result['pid']], result['calls'], result)
        self.assertTrue(result['exit_delivered'], result)

    def test_esrch_exit_fd_is_shut_with_its_manager_and_duplicates_stay(self) -> None:
        result = run_answer('esrch_owner')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['before'], result)
        self.assertTrue(result['distinct'], result)
        self.assertTrue(result['original_shut'], result)
        self.assertTrue(result['dup_readable'], result)

    def test_esrch_leader_gone_before_its_environment_times_out_and_is_fenced(self) -> None:
        # Pinned: the launcher waits its whole start timeout (1 s here) on the env FIFO, then refuses.
        result = run_answer('esrch_launch')
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['launched'], result)
        self.assertEqual(1, len(result['calls']), result)
        self.assertTrue(result['detail'].startswith('TimeoutError'), result)
        self.assertGreaterEqual(result['elapsed'], 1.0, result)
        self.assertLess(result['elapsed'], 15.0, result)
        self.assertTrue(result['leader_state'] == 'Z' or result['leader_state'].startswith('gone'), result)
        self.assertEqual('unknown', result['observe'], result)


if __name__ == '__main__':
    if sys.argv[1:2] == [_FLAG]:
        raise SystemExit(_child_main())
    if sys.argv[1:2] == [_ANSWER]:
        raise SystemExit(_answer_main())
    unittest.main()
