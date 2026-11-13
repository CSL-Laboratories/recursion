"""Production engine launcher and cleanup layer for the daemon (CSL-738 composition).

This is the launcher the route adapters (``recursion_core.routes``) were written against
and until now only ever received as a fake. One engine child is one same-uid transient
``systemd-run --user`` unit in the operator's user manager (``engine_user.SPAWN_PATH``),
so the run lives outside the daemon's own unit and survives the daemon closing or
crashing (PRODUCT.md d84cdd26, CSLR-D-012, section 3.1 lines 459-469).

* The child environment is whatever ``child_env.build_child_env`` (B1, on master) accepts,
  with the adapter's environment as its only input. It reaches the child through a FIFO
  and ``engine_exec.py``, never through argv or a unit property.
* stdout and stderr are files in the run's private directory, not pipes owned by the
  daemon: a daemon that dies does not hand the engine a broken pipe (section 11.3 line
  3041: Codex panics on its next write after EPIPE). stdin is a FIFO; closing it is EOF
  for the engine and is never used as cancellation (section 11.3 line 3040).
* Stop is the #916 procedure (section 11.3 lines 3039-3041): the unit carries
  ``KillSignal=SIGINT``, ``TimeoutStopSec=2s`` and ``KillMode=control-group``, so one
  ``systemctl --user stop`` sends SIGINT to the whole cgroup, waits about two seconds and
  SIGKILLs whatever is left, including descendants in delegated child cgroups.
* ``UnitKillTree.confirm`` answers ``empty`` only when the daemon's sweep attested the
  termination (``Daemon._termination_attested``). Everything else is ``populated``,
  ``timeout`` or ``unknown``, which the control API reports as ``pending`` (section 3.4
  S6, line 888; section 3.3 lines 798-799).

Linux and same uid only. ``fence`` and Stop both go through ``Daemon.stop``, which records
the durable Stop intent before it touches the unit, so a daemon restart replays it.
"""
from __future__ import annotations

import errno
try:
    import fcntl
except ImportError:  # Windows: the launcher refuses in require_launcher_platform()
    fcntl = None
import json
import logging
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import sys
try:
    import termios
except ImportError:  # Windows, as above
    termios = None
import threading
import time

from recursion_core.child_env import ENGINE_PROFILES, ChildRoute, EnvRefused, build_child_env
from recursion_core.child_env import policy as _env_policy
from recursion_core.child_env.launch import _resolve_executable
from recursion_core.control.registry import EMPTY, POPULATED, TIMEOUT, UNKNOWN
from recursion_core.routes import contract as c
from recursion_core.routes.launch import ReadTimeout

from . import TERMINAL, UNIT, PlatformUnsupported, SystemdUserManager, require_posix
from .engine_exec import MAX_ENV_BYTES

log = logging.getLogger(__name__)

ENGINE_EXEC = Path(__file__).with_name('engine_exec.py')
# The #916 procedure as unit properties (PRODUCT.md d84cdd26 section 11.3, lines 3039-3041).
STOP_PROPERTIES = ('KillSignal=SIGINT', 'TimeoutStopSec=2s', 'SendSIGKILL=yes')
MAX_LINE_BYTES = 16 * 1024 * 1024
SEND_TIMEOUT_S = 30.0
# systemd-run expands $VAR, ${VAR} and $$ in the command line unless told not to, and it can be
# told only since v254 (--expand-environment=). An older systemd-run is refused, not worked around.
VERBATIM_ARGV_MIN_SYSTEMD = 254
_PATH_SAFE = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-+@,')

_current = threading.local()


def refused(detail):
    return c.RouteError(c.LAUNCH_REFUSED, detail)


def require_launcher_platform():
    """Refuse where the engine launcher's Linux calls are missing (Windows, macOS).

    A typed ``PlatformUnsupported`` before any directory, FIFO or unit exists, never an
    AttributeError or ImportError part-way through a launch.
    """
    require_posix()
    if (termios is None or not hasattr(termios, 'FIONREAD') or not hasattr(os, 'mkfifo')
            or not hasattr(os, 'pidfd_open') or not hasattr(os, 'geteuid')):
        raise PlatformUnsupported('platform_unsupported: the engine launcher needs Linux '
                                  '(FIFO stdio, FIONREAD, pidfd, systemd user units)')


class RunContext:
    """One control-API run while its route request is in flight. Set by the starter, read by the launcher."""

    def __init__(self, run_id, *, breaker=None):
        self.run_id = run_id
        self.breaker = breaker
        self.admitted = None
        self.bound = False
        self.cgroup = None
        self.process = None
        self.result = None
        self.cancelled = False
        self.launches = 0
        self.changed = threading.Condition()

    def launched(self, cgroup, process):
        """Publish the launch. False when the start was abandoned first: nothing is published then.

        Publishing and abandoning are decided under one lock, so exactly one of them wins. A launch
        is never both answered as timed out and left running where no Stop can find it.
        """
        with self.changed:
            if self.cancelled:
                return False
            self.cgroup, self.process = cgroup, process
            self.changed.notify_all()
            return True

    def abandon(self):
        """Give the launch up, as a start that ran out of time does. False when the launch was
        published first: the run started and is not abandoned."""
        with self.changed:
            if self.cgroup is not None:
                return False
            self.cancelled = True
            return True

    def finished(self, result):
        with self.changed:
            self.result = result
            self.changed.notify_all()

    def wait_started(self, timeout_s):
        """True when the launch is recorded, False when the request ended or the wait ran out first."""
        deadline = time.monotonic() + timeout_s
        with self.changed:
            while self.cgroup is None and self.result is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.changed.wait(left)
            return self.cgroup is not None


class bound_run:
    """``with bound_run(ctx):`` makes ``ctx`` the run every launch on this thread belongs to."""

    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        _current.ctx = self.ctx
        return self.ctx

    def __exit__(self, *_):
        _current.ctx = None


def private_dir(path):
    """Create ``path`` (and missing parents) at 0700, or check an existing one is ours and 0700."""
    require_launcher_platform()
    path = Path(path)
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    for item in reversed(missing):
        try:
            os.mkdir(item, 0o700)
        except FileExistsError:
            pass
        os.chmod(item, 0o700)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise PermissionError(f'{path} must be an operator-owned directory')
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def _unit_path_ok(path):
    """A path that can sit in a unit property without quoting. Anything else is refused, not escaped."""
    text = str(path)
    return os.path.isabs(text) and all(ch in _PATH_SAFE for ch in text) and '..' not in Path(text).parts


class EngineUnitManager(SystemdUserManager):
    """``SystemdUserManager`` whose run units carry the #916 Stop properties and file stdio."""

    def __init__(self, cgroup_root=Path('/sys/fs/cgroup')):
        super().__init__(cgroup_root)
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._verbatim_argv = False

    def prepare(self, unit, *, properties=(), working_directory=None):
        with self._pending_lock:
            self._pending[unit] = (tuple(properties), working_directory)

    def discard(self, unit):
        with self._pending_lock:
            self._pending.pop(unit, None)

    def start(self, unit, argv):
        if not argv:
            raise ValueError('Empty launch')
        with self._pending_lock:
            properties, cwd = self._pending.pop(unit, ((), None))
        self.require_verbatim_argv()
        command = ['systemd-run', '--user', '--quiet', '--unit=' + unit, '--service-type=exec',
                   '--expand-environment=no',
                   '--property=KillMode=control-group', '--property=Delegate=yes',
                   '--property=RemainAfterExit=yes']
        command += ['--property=' + item for item in (*STOP_PROPERTIES, *properties)]
        if cwd is not None:
            command.append('--working-directory=' + str(cwd))
        self._call([*command, '--', *argv])

    def require_verbatim_argv(self):
        """Refuse the launch unless this systemd-run can hand the engine its argv unchanged (ADV1-11).

        Without ``--expand-environment=no`` an argument such as ``$HOME`` reaches the engine as the
        user manager's value for it. A systemd-run too old to know the flag, or one whose version
        cannot be read, gets a typed refusal; the flag is never dropped to make the launch work.
        """
        if self._verbatim_argv:
            return
        try:
            text = self._call(['systemd-run', '--version'])
        except (OSError, subprocess.SubprocessError):
            text = ''
        found = re.match(r'systemd (\d+)', text or '')
        if found is None or int(found.group(1)) < VERBATIM_ARGV_MIN_SYSTEMD:
            raise refused('systemd_run_cannot_pass_argv_verbatim')
        self._verbatim_argv = True

    def unit_identity(self, unit):
        """(cgroup path, main pid) as systemd reports them now; blank or 0 when it reports none."""
        text = self._call(['systemctl', '--user', 'show', '--property=ControlGroup,MainPID', '--', unit])
        props = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        group = props.get('ControlGroup', '')
        if not group.startswith('/') or '..' in Path(group).parts:
            group = ''
        main = props.get('MainPID', '')
        return group, int(main) if main.isdigit() else 0

    def open_main_pidfd(self, group, pid):
        """A pidfd for ``pid`` only while it is still a member of ``group``; otherwise None."""
        if not group or pid <= 0:
            return None
        try:
            fd = os.pidfd_open(pid)
        except OSError:
            return None
        try:
            lines = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
        except OSError:
            lines = []
        if not any(line.split(':', 2)[-1] == group for line in lines if line.startswith('0::')):
            os.close(fd)  # the pid was reused, or the main process already left the unit
            return None
        return fd


class UnitEngineProcess:
    """``routes.launch.EngineProcess`` over one run unit: FIFO stdin, a tailed stdout file, a pidfd."""

    def __init__(self, daemon, run_id, stdin_fd, stdout_path, pidfd, *, poll_s=0.05):
        require_launcher_platform()
        self._daemon, self.run_id = daemon, run_id
        self._stdin, self._pidfd, self._poll_s = stdin_fd, pidfd, poll_s
        self._out = os.open(stdout_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        self._buffer = b''
        self._liveness_at = 0.0
        self._ended_seen = False
        self.fenced = []
        self._lock = threading.Lock()

    def send(self, line):
        data = line.encode('utf-8') + b'\n'
        deadline = time.monotonic() + SEND_TIMEOUT_S
        with self._lock:
            if self._stdin is None:
                raise BrokenPipeError(errno.EPIPE, 'engine stdin is closed')
            while data:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(errno.ETIMEDOUT, 'engine did not read its stdin')
                if not select.select([], [self._stdin], [], min(left, 1.0))[1]:
                    continue
                try:
                    written = os.write(self._stdin, data)
                except BlockingIOError:
                    continue
                data = data[written:]

    def close_stdin(self):
        with self._lock:
            fd, self._stdin = self._stdin, None
        if fd is not None:
            os.close(fd)

    def _ended(self):
        """True once the unit's main process is gone (pidfd), or the sweep recorded a terminal state."""
        if self._ended_seen:
            return True
        if self._pidfd is not None:
            self._ended_seen = bool(select.select([self._pidfd], [], [], 0)[0])
            return self._ended_seen
        now = time.monotonic()
        if now - self._liveness_at < 0.5:
            return False
        self._liveness_at = now
        try:
            self._ended_seen = self._daemon.reconcile(self.run_id).state in TERMINAL
        except Exception:  # noqa: BLE001 - an unreadable registry is not an ended engine
            log.exception('liveness read for %s failed', self.run_id)
        return self._ended_seen

    def read_line(self, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            cut = self._buffer.find(b'\n')
            if cut >= 0:
                line, self._buffer = self._buffer[:cut], self._buffer[cut + 1:]
                return line.decode('utf-8', 'replace')
            if len(self._buffer) > MAX_LINE_BYTES:
                raise OSError(errno.EMSGSIZE, 'engine stdout line is too long')
            ended = self._ended()  # read liveness first, so output written before the exit is not missed
            chunk = os.read(self._out, 65536)
            if chunk:
                self._buffer += chunk
                continue
            if ended:
                if self._buffer:
                    line, self._buffer = self._buffer, b''
                    return line.decode('utf-8', 'replace')
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                raise ReadTimeout()
            time.sleep(min(self._poll_s, left))

    def fence(self, reason):
        """End the run's whole tree: durable Stop intent, then the #916 stop of the unit's cgroup."""
        self.fenced.append(reason)
        try:
            self._daemon.stop(self.run_id)
        except Exception:  # noqa: BLE001 - the Stop intent is replayed by the sweep when it was recorded
            log.exception('fence (%s) of %s did not finish', reason, self.run_id)

    def close(self):
        self.close_stdin()
        for name in ('_out', '_pidfd'):
            fd = getattr(self, name)
            setattr(self, name, None)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


class ProductionLauncher:
    """``launcher(argv, *, env, shell=False) -> EngineProcess`` for one engine's route adapter."""

    def __init__(self, engine, daemon, manager, state_dir, *, python=None, start_timeout_s=10.0):
        require_launcher_platform()
        if engine not in ENGINE_PROFILES:
            raise ValueError(engine)
        self.engine, self._daemon, self._manager = engine, daemon, manager
        self._state = Path(state_dir)
        self._python = python or sys.executable
        self._start_timeout_s = start_timeout_s

    # -- the pieces a test can check without systemd -------------------------------------------
    def child_environment(self, env):
        """The adapter's environment through the B1 policy. Returns a ``ValidatedEnv`` or refuses.

        The adapter's environment is the policy's ``parent``: a name reaches the child only when both
        the adapter and the policy allow it. The route's vendor key and cloud switches are lifted out
        and handed to the policy as the route (``ChildRoute``), which is the only way it sets them.
        A credential-like name the adapter selected and the policy would drop refuses the launch:
        the child would otherwise run on a different sign-in than the route names.
        """
        profile = ENGINE_PROFILES[self.engine]
        parent = {str(k): v for k, v in dict(env).items()}
        key = parent.pop(profile.key_name, None) if profile.key_name else None
        switches = tuple(name for name in profile.provider_switches if name in parent)
        for name in switches:
            parent.pop(name)
        if key is not None and switches:
            raise refused('child_env:route:key_and_cloud_provider')
        mode = 'api_key' if key is not None else 'cloud_provider' if switches else 'subscription'
        home = private_dir(self._state / 'homes' / self.engine)
        try:
            validated = build_child_env(self.engine, home=str(home), auth_mode=mode,
                                        route=ChildRoute(api_key=key, provider_switches=switches),
                                        parent=parent)
        except EnvRefused as exc:
            raise refused(f'child_env:{exc.name or "-"}:{exc.cause}') from None
        lost = sorted(name for name in parent
                      if name not in validated and name != 'HOME' and _env_policy._DENIED.search(name))
        if lost:
            raise refused(f'child_env:{lost[0]}:not_carried_by_policy')
        return validated

    def unit_argv(self, executable, argv, env_fifo):
        return [self._python, '-I', '-S', str(ENGINE_EXEC), str(env_fifo), '--', executable, *argv[1:]]

    def unit_properties(self, run_dir):
        return ('StandardInput=file:%s' % (run_dir / 'stdin.fifo'),
                'StandardOutput=append:%s' % (run_dir / 'stdout.jsonl'),
                'StandardError=append:%s' % (run_dir / 'stderr.log'))

    # -- launch --------------------------------------------------------------------------------
    def __call__(self, argv, *, env, shell=False, **options):
        if shell is not False or options:
            raise refused('launch_option_denied')
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and '\0' not in a for a in argv):
            raise refused('argv_malformed')
        ctx = getattr(_current, 'ctx', None)
        if ctx is None:
            raise refused('no_run_context')
        if ctx.cancelled:
            raise refused('start_abandoned')
        if ctx.launches:
            # One unit per run id. A second launch for the same run (a fallback route) needs its own
            # run record, which the control API does not have yet.
            raise refused('run_already_launched')
        if ctx.breaker is not None and ctx.admitted is not None and not ctx.bound:
            error = ctx.breaker.bind_run(ctx.run_id, ctx.admitted[0], self.engine, ctx.admitted[1])
            if error is not None:
                raise refused('breaker_bind_failed:' + error)
            ctx.bound = True
        validated = self.child_environment(env)
        try:
            executable = _resolve_executable(argv[0], validated)
        except EnvRefused as exc:
            raise refused(f'child_env:{exc.name or "-"}:{exc.cause}') from None
        payload = json.dumps(validated.to_dict()).encode('utf-8')
        if len(payload) > MAX_ENV_BYTES:
            raise refused('child_env:-:environment_too_large')
        run_dir = private_dir(self._state / 'runs' / ctx.run_id)
        home = self._state / 'homes' / self.engine
        if not _unit_path_ok(run_dir) or not _unit_path_ok(home):
            raise refused('state_path_not_unit_safe')
        unit = f'recursion-run-{ctx.run_id}.service'
        if not UNIT.fullmatch(unit):
            raise refused('run_id')
        stdin_fd = env_fd = None
        process = None
        try:
            for name in ('stdin.fifo', 'env.fifo'):
                os.mkfifo(run_dir / name, 0o600)
            for name in ('stdout.jsonl', 'stderr.log'):
                os.close(os.open(run_dir / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
            # O_RDWR: the open never blocks, and bytes written before the child opens its end are kept.
            flags = os.O_RDWR | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
            stdin_fd = os.open(run_dir / 'stdin.fifo', flags)
            env_fd = os.open(run_dir / 'env.fifo', flags)
            self._manager.prepare(unit, properties=self.unit_properties(run_dir), working_directory=home)
            ctx.launches += 1
            try:
                self._daemon.start(ctx.run_id, self.unit_argv(executable, argv, run_dir / 'env.fifo'))
            finally:
                self._manager.discard(unit)
            # engine_exec is now the unit's main process, blocked on the environment FIFO: the cgroup
            # is populated and systemd names it, before any engine code has run.
            group, main_pid = self._manager.unit_identity(unit)
            if not group:
                raise OSError(errno.ESRCH, 'unit has no control group after start')
            pidfd = self._manager.open_main_pidfd(group, main_pid)
            process = UnitEngineProcess(self._daemon, ctx.run_id, stdin_fd, run_dir / 'stdout.jsonl', pidfd)
            stdin_fd = None
            os.write(env_fd, b'%d\n' % len(payload) + payload)
            self._wait_env_read(env_fd)
            if ctx.cancelled:
                raise OSError(errno.ECANCELED, 'start was abandoned while the unit launched')
        except BaseException:
            # Anything after the registry row exists ends the tree: no half-launched engine is left.
            if process is not None:
                process.fence('launch_failed')
                process.close()
            else:
                self._fence_quietly(ctx.run_id)
            if stdin_fd is not None:
                os.close(stdin_fd)
            raise
        finally:
            if env_fd is not None:
                os.close(env_fd)
            try:
                os.unlink(run_dir / 'env.fifo')
            except OSError:
                pass
        if not ctx.launched(group, process):
            # The start was abandoned after the check above and has been answered as timed out. The
            # launch is not published, and its tree is ended like any other launch that failed.
            process.fence('launch_failed')
            process.close()
            raise OSError(errno.ECANCELED, 'start was abandoned as the unit launched')
        return process

    def _wait_env_read(self, fd):
        deadline = time.monotonic() + self._start_timeout_s
        unread = bytearray(4)
        while True:
            fcntl.ioctl(fd, termios.FIONREAD, unread)
            if int.from_bytes(unread, sys.byteorder) == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(errno.ETIMEDOUT, 'engine_exec did not read the environment')
            time.sleep(0.01)

    def _fence_quietly(self, run_id):
        try:
            self._daemon.registry.get(run_id)
        except KeyError:
            return  # the launch never reached the registry: there is no unit
        except Exception:  # noqa: BLE001
            log.exception('registry read for %s failed during launch cleanup', run_id)
        try:
            self._daemon.stop(run_id)
        except Exception:  # noqa: BLE001
            log.exception('cleanup of failed launch %s did not finish', run_id)


class UnitKillTree:
    """``control.registry.KillTree`` over the daemon: #916 stop of the unit, attested confirmation."""

    def __init__(self, daemon, *, poll_s=0.25, clock=time.monotonic, sleep=time.sleep):
        require_launcher_platform()
        self._daemon, self._poll_s, self._clock, self._sleep = daemon, poll_s, clock, sleep

    @staticmethod
    def run_id_of(cgroup):
        """The run a cgroup path names, by its unit. A path that names no run unit raises ValueError."""
        if not isinstance(cgroup, str) or not cgroup.startswith('/') or '..' in Path(cgroup).parts:
            raise ValueError('cgroup')
        unit = Path(cgroup).name
        if not UNIT.fullmatch(unit):
            raise ValueError('cgroup')
        return unit[len('recursion-run-'):-len('.service')]

    def request_stop(self, cgroup):
        # Durable Stop intent first, then systemctl stop (SIGINT, about 2 s, SIGKILL the rest). The
        # path goes with it: a launch whose control record was never written has no other one to walk.
        self._daemon.stop(self.run_id_of(cgroup), cgroup=cgroup)

    def confirm(self, cgroup, timeout_s):
        """``EMPTY`` only when the run's row is ``terminated`` and ``cgroup`` itself, walked in this
        call, is absent or empty.

        The row alone is not enough. It ends on what was seen of the unit and of the cgroup on the
        control record, and a sweep or a fence can end it for a run whose record carries no cgroup
        without ever looking at this path. The receipt names this path, so this path is walked.
        """
        run_id = self.run_id_of(cgroup)
        deadline = self._clock() + timeout_s
        while True:
            run = self._daemon.reconcile(run_id, cgroup=cgroup)
            clear = None
            if run.state == 'terminated':
                clear = self._daemon.cgroup_clear(run_id, cgroup)
                if clear is True:
                    return EMPTY  # attested: the unit ended, and this cgroup read absent or empty just now
            if run.state == 'lost_reboot':
                return UNKNOWN  # nothing saw the tree end; the registry does not call that terminated
            if self._clock() >= deadline:
                if run.state in ('running', 'stopping') or clear is False:
                    return POPULATED
                return TIMEOUT if run.state == 'starting' else UNKNOWN
            self._sleep(self._poll_s)
