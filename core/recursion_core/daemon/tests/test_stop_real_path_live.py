"""Stop on the path a person's Stop takes, live: the product's launcher, daemon and kill tree.

The control API is composed as ``daemon.control_adapter.compose_control`` composes it
(``ControlAPI(registry, UnitKillTree(daemon), starter, time.time, confirm_timeout_s=...)`` over
``DurableRunRegistry(daemon.registry, processes=pid_alive(manager))``). The run is launched by
``ProductionLauncher`` inside ``bound_run(RunContext(run_id))``, so ``engine_exec``, the unit's
properties and ``STOP_PROPERTIES`` are the product's own, in a transient unit of the caller's
systemd user manager (``EngineUnitManager``, ``KillMode=control-group``, ``Delegate=yes``).

The stand-in run: the main forks a child; the child forks a grandchild and exits; the grandchild
calls setsid(), ignores SIGINT and SIGTERM, writes its pid, session and process group, and ends by
itself after ``STANDIN_LIFE_S``. ``run.stop`` must answer ``stopped`` only once that grandchild is
gone, it must be gone within the unit's own stop bound, and the daemon's row must read terminated.
The unit's stop properties are checked against the values written out below, each with what a
person would meet if it were otherwise.

Two cases, one for each way the daemon can see the unit after the hard end:
* the person's case: the run unit is started exactly as the product starts it, with no property
  of the test's own. After the hard end systemd keeps it loaded and failed, and the daemon reads
  that branch. The values it must read are written out below (``AFTER_HARD_END``,
  ``OBSERVED_AFTER_HARD_END``);
* the unit-gone case: the run unit also carries the test's ``CollectMode=inactive-or-failed`` and
  bounds, so systemd unloads it at the stop and the daemon reads it as gone.

The cases start units in the caller's user manager, so they run only when ``LIVE_CELLS_ENV`` is
set to 1, a systemd user manager answers and its systemd-run can pass argv verbatim (the launcher
refuses an older one). Everywhere else they skip and say why.

They look for nothing of an earlier run: no step lists units or folders, reads a list file or
matches a token. A case ends only what this test process started, by the names it holds in its own
memory: its grandchild through a pidfd, after the stand-in's mark is read again, and its own units,
each name made here from a fixed prefix and random hex (the run unit's from the run id the control
API draws) and checked against ``UNIT_NAME`` before every call, which hands it to systemctl after
``--`` so that it is read neither as a pattern nor as an option. A cleanup that had to end a
survivor fails the case.

When the test process is ended from outside, what it started ends by itself, through properties
set on the case's own helper units when they start:
* the holder ``<HOLDER_PREFIX><hex>.service``: its runtime directory is the case's folder; its one
  process waits on a pidfd of the test process and then exits; ``--collect`` makes systemd unload
  it and remove the folder; ``RuntimeMaxSec`` bounds it;
* in the person's case, the run helper ``<HOLDER_PREFIX><hex>-run.service``, started before the run
  with ``PropagatesStopTo=`` the run unit: it waits the same way, and when it ends, systemd stops
  the run unit. The run unit itself carries nothing of the test's own; systemd shows the helper on
  it as ``StopPropagatedFrom``;
* in the unit-gone case, the run unit's own ``RuntimeMaxSec`` and ``CollectMode``, which that case
  sets anyway (a run helper there would keep the stopped unit loaded and hide the branch it drives).
What may stay behind: a run unit whose stop needed the hard end stays loaded and failed in the user
manager (the person's case; the unit-gone case's is unloaded). Each case prints its units' names
when the run starts, with the one command that removes such a unit:
``systemctl --user reset-failed -- recursion-run-<run id>.service``.
"""
import os
from pathlib import Path
import re
import secrets
import select
import signal
import subprocess
import sys
import threading
import time
import unittest

LIVE_CELLS_ENV = 'RECURSION_USER_MANAGER_CELLS'

# Test values (none is a figure of the product).
STANDIN_LIFE_S = 45          # every stand-in process ends by itself after this
UNIT_TASKS_MAX = 16          # TasksMax of the run unit in the unit-gone case
UNIT_RUNTIME_MAX_S = 25      # RuntimeMaxSec of the run unit in the unit-gone case
CASE_TIMEOUT_S = 90          # each stop case, start to cleanup
CONFIRM_TIMEOUT_S = 10.0     # confirm_timeout_s handed to the control API
STOP_MARGIN_S = 3.0          # scheduling slack on top of the unit's own TimeoutStopSec
WAIT_FILE_S = 15.0           # how long the stand-in's pid files are waited for
CALL_TIMEOUT_S = 20          # each systemctl or systemd-run call of the test
HOLDER_TASKS_MAX = 2         # TasksMax of each helper unit (its one waiting process)
HOLDER_MARGIN_S = 30         # a helper's RuntimeMaxSec is CASE_TIMEOUT_S plus this
DECOY_CASE_TIMEOUT_S = 300   # the decoy case, with its two runs of a stop case in a child process
PLANTED_LIFE_S = 330         # every decoy unit ends by itself after this
PLANTED_TASKS_MAX = 2        # TasksMax of each decoy unit
KILLED_END_S = UNIT_RUNTIME_MAX_S + 15.0  # after a unit-gone run is ended from outside, how long its units have

# The names of this case's helper units and folders: <prefix><random hex>, and <...>-run for a run helper.
HOLDER_PREFIX = 'rc-stop-real-live-'
# The stand-in's mark: this prefix and random hex, drawn for each case.
MARK_PREFIX = 'rc-stop-real-'
# Every unit name the test hands to systemctl or systemd-run: letters, digits, dashes and dots only,
# a letter or digit first, ending in .service. No *, ?, [ or \ (a pattern), no leading dash (an option).
UNIT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*\.service')
# The line each stop case prints when its run starts: its units, its mark, and what to do after a kill.
NAMES_LINE = 'rc-stop-real-live units:'
_NAMES = re.compile(re.escape(NAMES_LINE) + r' (\S+) (\S+) mark: (\S+?)\.')

# What a person relies on when they press Stop, as systemd must report it for the run unit.
# Written out here, not read from the launcher, so that a change of the launcher's values turns
# this case red instead of moving the case with it.
#
# The first signal is SIGINT, the signal of Ctrl-C. The engines treat it as the person's own
# interrupt: they write out what they have (the session, the last turn) and end their own way. If
# the first signal were SIGTERM or SIGKILL, a person who pressed Stop could find the last turn lost
# and the session unable to resume.
EXPECTED_KILL_SIGNAL = signal.SIGINT
# The wait between that first signal and the hard end of whatever ignores it. It is what a person
# waits between pressing Stop and reading 'stopped' when a process will not end by itself; if it
# grew, Stop would seem to hang, and past the confirm window the person would read 'pending'.
# The value is a figure of the product (``launcher.STOP_PROPERTIES``), not yet a row of the
# person's settings; it is pinned here at the value the Stop procedure states.
EXPECTED_STOP_WAIT_S = 2.0
# The hard end follows the wait. Without it, a process that ignores SIGINT (as the grandchild here
# does) would go on running and spending after Stop, and the person would never read 'stopped'.
EXPECTED_SEND_SIGKILL = 'yes'
# Stop reaches the whole control group, not the main process alone: a detached grandchild is ended
# too. With KillMode=process it would outlive the Stop the person asked for.
EXPECTED_KILL_MODE = 'control-group'
# Child control groups an engine makes stay inside the unit, so Stop reaches them as well.
EXPECTED_DELEGATE = 'yes'

# The person's unit after a Stop that needed the hard end: systemd keeps it loaded and failed,
# because the stop timed out before the hard end. This is the branch a person's Stop takes.
AFTER_HARD_END = {'LoadState': 'loaded', 'ActiveState': 'failed', 'Result': 'timeout'}
# What the daemon must read for such a unit: its tree is empty. If it read 'unknown', the person
# would read 'pending' after Stop, though the run is over.
OBSERVED_AFTER_HARD_END = 'empty'
# systemd's defaults for what the unit-gone case sets of its own; the person's unit must carry them.
PERSON_COLLECT_MODE = 'inactive'
PERSON_BINDS_TO = ''
# The unit-gone case: systemd has unloaded the unit, and the daemon reads it as gone.
AFTER_COLLECT = {'LoadState': 'not-found'}
OBSERVED_AFTER_COLLECT = 'gone'

# The stand-in. argv: <pid file> <mark> <life seconds> <setsid 1|0>. The mark is only in argv, so
# every process of the tree carries it in /proc/<pid>/cmdline (fork without exec keeps it).
STANDIN = r'''
import os, signal, sys, time
out, mark, life, detach = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4] == "1"
pid = os.fork()
if pid == 0:
    g = os.fork()
    if g == 0:
        if detach:
            os.setsid()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(out + ".tmp", "w") as f:
            f.write("%d %d %d\n" % (os.getpid(), os.getsid(0), os.getpgrp()))
        os.rename(out + ".tmp", out)
        time.sleep(life)
        os._exit(0)
    os._exit(0)
os.waitpid(pid, 0)
with open(out + ".main.tmp", "w") as f:
    f.write("%d %d %d\n" % (os.getpid(), os.getsid(0), os.getpgrp()))
os.rename(out + ".main.tmp", out + ".main")
time.sleep(life)
'''

# A helper unit's one process. argv: <test pid>. It waits until the test process has ended, then
# exits; what follows (the folder removed, the run unit stopped) is systemd's, from the properties
# the helper was started with. It reads no file and calls nothing.
HOLDER_WAIT = r'''
import os, select, sys
try:
    fd = os.pidfd_open(int(sys.argv[1]))
except OSError:
    sys.exit(0)
select.select([fd], [], [])
'''

# A decoy unit's one process: it sleeps argv[1] seconds; further arguments only mark it.
PLANTED_WAIT = 'import sys, time; time.sleep(float(sys.argv[1]))'

_DURATION = re.compile(r'(\d+(?:\.\d+)?)(us|ms|min|s)')
_SCALE = {'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'min': 60.0}


def systemd_seconds(text):
    """'2s', '500ms', '1min 30s' as systemctl show prints them, in seconds. None if unreadable."""
    parts = _DURATION.findall(text.replace(' ', ''))
    if not parts or ''.join(n + u for n, u in parts) != text.replace(' ', ''):
        return None
    return sum(float(n) * _SCALE[u] for n, u in parts)


def checked_unit(unit):
    """``unit`` when it is a plain unit name of this test (``UNIT_NAME``); otherwise ValueError."""
    if not isinstance(unit, str) or not UNIT_NAME.fullmatch(unit):
        raise ValueError(f'refused to hand {unit!r} to systemctl: a unit name of this test has only letters, '
                         'digits, dashes and dots, begins with a letter or digit and ends in .service')
    return unit


def _user_systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True,
                          timeout=CALL_TIMEOUT_S, check=False)


def unit_call(verb, unit, *options):
    """``systemctl --user <verb> <options> -- <unit>`` for one plain unit name, checked first."""
    return _user_systemctl(verb, *options, '--', checked_unit(unit))


def runtime_dir():
    """The folder under which the user manager makes a unit's RuntimeDirectory."""
    return Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')


def live_gate():
    """None when the live case can run here; otherwise why it cannot, in words a person can act on."""
    if not sys.platform.startswith('linux'):
        return 'needs Linux: Stop ends a run through a systemd user unit and its control group'
    if os.environ.get(LIVE_CELLS_ENV) != '1':
        return (f'starts transient units in your systemd user manager; set {LIVE_CELLS_ENV}=1 to run it '
                '(the hosted checks do not set it)')
    if not hasattr(os, 'pidfd_open'):
        return 'needs os.pidfd_open (Linux 5.3 or later) to end only its own stand-in'
    try:
        state = _user_systemctl('is-system-running').stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f'no systemd user manager answered ({type(exc).__name__}); start one with a login session or lingering'
    if state not in ('running', 'degraded'):
        return f'the systemd user manager is {state or "not answering"}; it must be running to start transient units'
    if not runtime_dir().is_dir():
        return (f'the user manager\'s runtime folder {runtime_dir()} is missing; set XDG_RUNTIME_DIR to it '
                '(this case keeps its files there so that they go when the case ends)')
    # The launcher's floor (254) is above the 249 that PropagatesStopTo needs.
    from recursion_core.daemon.launcher import VERBATIM_ARGV_MIN_SYSTEMD
    try:
        text = subprocess.run(['systemd-run', '--version'], capture_output=True, text=True,
                              timeout=CALL_TIMEOUT_S, check=False).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f'systemd-run could not be read ({type(exc).__name__}); install systemd {VERBATIM_ARGV_MIN_SYSTEMD} or later'
    found = re.match(r'systemd (\d+)', text or '')
    if found is None or int(found.group(1)) < VERBATIM_ARGV_MIN_SYSTEMD:
        return (f'systemd-run is older than {VERBATIM_ARGV_MIN_SYSTEMD} (or unreadable); the launcher refuses it, '
                'so the real path cannot start a run here')
    return None


def alive(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
    except OSError:
        return False
    return text[text.rindex(')') + 2:].split()[0] not in ('Z', 'X')


def cgroup_of(pid):
    try:
        lines = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    except OSError:
        return None
    paths = [line.split(':', 2)[2] for line in lines if line.startswith('0::')]
    return paths[0] if paths else None


def carries_mark(pid, mark):
    try:
        return mark.encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    except OSError:
        return False


def pidfd_ended(fd):
    return bool(select.select([fd], [], [], 0)[0])


def unit_props(unit, names):
    text = unit_call('show', unit, '-p', ','.join(names)).stdout
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def own_folder(folder):
    """True when ``folder`` is a real folder (no link) of this user that no one else may enter."""
    try:
        info = os.lstat(folder)
    except OSError:
        return False
    return (not os.path.islink(folder) and os.path.isdir(folder) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def write_own_file(folder, name, text):
    fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def start_helper(unit, *, folder=None, propagates_stop_to=None):
    """Start one helper unit of this test; it ends when this test process ends, or at its RuntimeMaxSec."""
    command = ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + checked_unit(unit),
               '--description=recursion live Stop case helper', '--service-type=exec',
               f'--property=TasksMax={HOLDER_TASKS_MAX}',
               f'--property=RuntimeMaxSec={CASE_TIMEOUT_S + HOLDER_MARGIN_S}']
    if folder is not None:
        command += ['--property=RuntimeDirectory=' + folder, '--property=RuntimeDirectoryMode=0700']
    if propagates_stop_to is not None:
        command.append('--property=PropagatesStopTo=' + checked_unit(propagates_stop_to))
    return subprocess.run([*command, '--', sys.executable, '-I', '-c', HOLDER_WAIT, str(os.getpid())],
                          capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False)


def dead_pid():
    """The pid of a process of this test that has ended and been reaped."""
    done = subprocess.Popen(['true'])
    done.wait(timeout=CALL_TIMEOUT_S)
    return done.pid


def child_python():
    """This interpreter with the flags it runs under, for a test run in a child process."""
    flags = ['-B']
    if sys.flags.utf8_mode:
        flags += ['-X', 'utf8']
    if getattr(sys, '_is_gil_enabled', None) is not None and not sys._is_gil_enabled():
        flags += ['-X', 'gil=0']
    return [sys.executable, *flags]


class _CaseTimeout(Exception):
    pass


GATE = live_gate()


def _arm_case_timeout(case, seconds=CASE_TIMEOUT_S):
    def timed_out(signum, frame):
        raise _CaseTimeout(f'the live case passed {seconds} s')
    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    case.addCleanup(signal.signal, signal.SIGALRM, previous)
    case.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)


class UnitNameRuleTests(unittest.TestCase):
    """A name that systemctl could read as a pattern or an option never reaches it."""

    def test_only_plain_unit_names_are_handed_on(self):
        for good in ('rc-stop-real-live-0a1b2c3d4e5f6071.service', 'recursion-run-run-0f1e2d3c.service',
                     'rc-stop-real-live-0a1b2c3d4e5f6071-run.service', 'a.b-c.service'):
            self.assertEqual(checked_unit(good), good)
        for bad in ('*', 'recursion-run-*', 'recursion-run-*.service', 'rc-stop-?.service', 'rc-[ab].service',
                    'rc\\x2d.service', '-H.service', '--all.service', 'a b.service', 'a_b.service', 'a/b.service',
                    'rc-stop-real-live-0a1b', '.service', '', None, 'a.service\n', 'a.service b.service'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                checked_unit(bad)


@unittest.skipIf(GATE is not None, GATE or '')
class StopOnTheRealPathTests(unittest.TestCase):
    def setUp(self):
        from recursion_core.daemon import launcher

        _arm_case_timeout(self)
        self.mark = MARK_PREFIX + secrets.token_hex(16)
        self.units, self.helpers, self.pidfds, self.processes = [], [], [], []
        self.tmp = self._start_holder()
        self.addCleanup(self._end_own)
        self.launcher_module = launcher
        self.detach = '1'

    def compose(self, manager, *, run_helper):
        """The daemon and the control API over ``manager``, as the product composes them.

        ``run_helper``: each run gets a run helper whose end stops the run unit (see the module text).
        """
        self.run_helper = run_helper
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon.control_adapter import DurableRunRegistry, pid_alive
        from recursion_core.control.api import ControlAPI
        launcher = self.launcher_module
        self.manager = manager
        self.daemon = Daemon(Registry(self.tmp / 'reg'), manager)
        self.addCleanup(self.daemon.close)
        self.launch = launcher.ProductionLauncher('claude_code', self.daemon, manager, self.tmp / 'state')
        self.registry = DurableRunRegistry(self.daemon.registry, processes=pid_alive(manager))
        self.api = ControlAPI(self.registry, launcher.UnitKillTree(self.daemon), self.start_run, time.time,
                              confirm_timeout_s=CONFIRM_TIMEOUT_S)

    def _start_holder(self):
        """Start this case's holder unit; its runtime directory is the case's folder, gone with the unit."""
        name = f'{HOLDER_PREFIX}{secrets.token_hex(8)}'
        self.holder_unit = checked_unit(name + '.service')
        folder = runtime_dir() / name
        self.addCleanup(self._stop_holder, folder)
        done = start_helper(self.holder_unit, folder=name)
        if done.returncode != 0 or not folder.is_dir():
            self.fail(f'the holder unit {self.holder_unit} did not start with its folder {folder}: '
                      f'{done.stderr.strip() or done.returncode}')
        if not own_folder(folder):
            self.fail(f'{folder} is not a private folder of this user')
        return folder

    def _stop_holder(self, folder):
        unit_call('stop', self.holder_unit)
        unit_call('reset-failed', self.holder_unit)
        left = unit_props(self.holder_unit, ['LoadState']).get('LoadState')
        if left != 'not-found' or folder.exists():
            self.fail(f'after the case the holder unit reads {left} and its folder is '
                      f'{"still there" if folder.exists() else "gone"}')

    def start_run(self, params):
        """The control API's starter: one launch through ProductionLauncher, bound to the API's run id.

        Before the launch it starts the run helper (when the case has one), whose PropagatesStopTo
        names the run unit, so that systemd stops the run unit if this test process ends first.
        """
        launcher = self.launcher_module
        run_id = params['run_id']
        unit = checked_unit(f'recursion-run-{run_id}.service')
        helper = 'none'
        if self.run_helper:
            helper = checked_unit(f'{HOLDER_PREFIX}{secrets.token_hex(8)}-run.service')
            self.helpers.append(helper)
            done = start_helper(helper, propagates_stop_to=unit)
            if done.returncode != 0:
                raise RuntimeError(f'the run helper {helper} did not start: {done.stderr.strip() or done.returncode}')
        self.units.append(unit)
        out = self.tmp / 'standin.pid'
        argv = [sys.executable, '-I', '-c', STANDIN, str(out), self.mark, str(STANDIN_LIFE_S), self.detach]
        ctx = launcher.RunContext(run_id)
        with launcher.bound_run(ctx):
            process = self.launch(argv, env={'PATH': '/usr/bin:/bin'})
        self.processes.append(process)
        print(f'\n{NAMES_LINE} {self.holder_unit} {unit} mark: {self.mark}. Run helper: {helper}. If this test '
              'run is ended from outside, these units end by themselves; the run unit may then stay failed in your '
              f'user manager: remove it with systemctl --user reset-failed -- {unit}', file=sys.stderr, flush=True)
        return {'cgroup': ctx.cgroup}

    def wait_file(self, path):
        deadline = time.monotonic() + WAIT_FILE_S
        while time.monotonic() < deadline:
            if path.exists():
                return [int(x) for x in path.read_text().split()]
            time.sleep(0.05)
        self.fail(f'the stand-in never wrote {path.name} within {WAIT_FILE_S} s')

    def own_pidfd(self, pid):
        """A pidfd for ``pid`` only when that process carries this case's mark; the mark is read after the open."""
        fd = os.pidfd_open(pid)
        if not carries_mark(pid, self.mark):
            os.close(fd)
            self.fail(f'pid {pid} does not carry the stand-in mark; it is not this case\'s process')
        self.pidfds.append((pid, fd))
        return fd

    def _end_own(self):
        """End what this case started and is still there, by the names it holds. Ending a survivor fails the case."""
        ended = []
        for pid, fd in self.pidfds:
            if not pidfd_ended(fd) and carries_mark(pid, self.mark):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                ended.append(pid)
            os.close(fd)
        for process in self.processes:
            process.close()
        for unit in (*self.units, *self.helpers):
            unit_call('stop', unit)
            unit_call('reset-failed', unit)
        left = [unit for unit in (*self.units, *self.helpers)
                if unit_props(unit, ['LoadState']).get('LoadState') != 'not-found']
        if ended or left:
            self.fail(f'cleanup had to end stand-in pids {ended}; units still loaded: {left}')

    def drive_stop(self):
        """Start the stand-in through the control API, press Stop, and check what every branch shares.

        Returns the run unit's name and its properties as systemd reported them before Stop.
        """
        started = self.api.call('run.start', {'tenant': 'live-stop', 'engine': 'claude_code', 'prompt': 'stand-in'})
        self.assertEqual(started.get('code'), 'started', started)
        run_id = started['run_id'] if 'run_id' in started else started['run']['run_id']
        unit = checked_unit(f'recursion-run-{run_id}.service')
        group, _main = self.manager.unit_identity(unit)
        self.assertTrue(group, 'systemd names no control group for the run unit')
        self.assertEqual(self.registry.get(run_id).get('cgroup'), group,
                         'the control record must carry the cgroup systemd reports for the unit')

        gpid, gsid, gpgrp = self.wait_file(self.tmp / 'standin.pid')
        mpid, msid, _mpgrp = self.wait_file(self.tmp / 'standin.pid.main')
        gfd = self.own_pidfd(gpid)
        self.own_pidfd(mpid)
        props = unit_props(unit, ['KillMode', 'KillSignal', 'TimeoutStopUSec', 'SendSIGKILL', 'Delegate',
                                  'CollectMode', 'BindsTo', 'StopPropagatedFrom'])

        self.assertEqual(props.get('StopPropagatedFrom'), self.helpers[-1] if self.run_helper else '',
                         'the run unit must be stopped by systemd when the case\'s run helper ends, and be '
                         f'bound from nothing else: {props}')
        self.assertTrue(alive(gpid), 'the grandchild must be alive before Stop')
        self.assertEqual((gsid, gpgrp), (gpid, gpid), 'the grandchild must lead a session of its own')
        self.assertNotEqual(gsid, msid, 'the grandchild must be outside the main\'s session')
        inside = cgroup_of(gpid)
        self.assertTrue(inside == group or (inside or '').startswith(group + '/'),
                        f'the grandchild must be inside the unit\'s cgroup before Stop: {inside} vs {group}')
        bound = systemd_seconds(props.get('TimeoutStopUSec', ''))
        self.assertIsNotNone(bound, f'TimeoutStopUSec unreadable: {props}')

        gone_at = []
        watcher = threading.Thread(target=lambda: select.select([gfd], [], [], CASE_TIMEOUT_S)[0]
                                   and gone_at.append(time.monotonic()), daemon=True)
        watcher.start()
        asked = time.monotonic()
        stopped = self.api.call('run.stop', {'run_id': run_id})
        gone_at_answer = pidfd_ended(gfd)
        survivor_alive = alive(gpid)

        self.assertEqual((stopped.get('code'), stopped.get('state')), ('stopped', 'stopped'), stopped)
        self.assertTrue(gone_at_answer and not survivor_alive,
                        f'run.stop answered {stopped} while the grandchild {gpid} was still alive')
        watcher.join(WAIT_FILE_S)
        self.assertTrue(gone_at, 'the grandchild\'s pidfd never reported its end')
        self.assertLessEqual(gone_at[0] - asked, bound + STOP_MARGIN_S,
                             f'the grandchild outlived the unit\'s stop bound ({bound} s + {STOP_MARGIN_S} s)')
        record = self.registry.get(run_id)
        self.assertEqual(record.get('state'), 'stopped', record)
        self.assertEqual((record.get('stop_attestation') or {}).get('proof'), 'empty', record)
        self.assertEqual((record.get('stop_attestation') or {}).get('cgroup'), group, record)
        self.assertEqual(self.daemon.registry.get(run_id).state, 'terminated')

        # What systemd reports for the run unit, against the values written out at the top.
        self.assertEqual(props.get('KillSignal'), str(int(EXPECTED_KILL_SIGNAL)),
                         f'the first signal of Stop must be SIGINT: {props}')
        self.assertEqual(bound, EXPECTED_STOP_WAIT_S,
                         f'the wait before the hard end must be {EXPECTED_STOP_WAIT_S} s: {props}')
        self.assertEqual(props.get('SendSIGKILL'), EXPECTED_SEND_SIGKILL,
                         f'the hard end must follow the wait: {props}')
        self.assertEqual(props.get('KillMode'), EXPECTED_KILL_MODE, props)
        self.assertEqual(props.get('Delegate'), EXPECTED_DELEGATE, props)
        return unit, props

    def test_stop_answers_stopped_only_once_a_detached_grandchild_is_gone(self):
        # The person's branch: the run unit exactly as the product starts it, loaded and failed after Stop.
        self.compose(self.launcher_module.EngineUnitManager(), run_helper=True)
        unit, props = self.drive_stop()
        after = unit_props(unit, list(AFTER_HARD_END))
        self.assertEqual(after, AFTER_HARD_END,
                         'after a Stop that needed the hard end, systemd must keep the person\'s unit loaded '
                         f'and failed: {after}')
        self.assertEqual(self.manager.observe(unit), OBSERVED_AFTER_HARD_END,
                         'the daemon must read the loaded and failed unit as ended')
        # The unit carries none of the properties the unit-gone case adds of its own.
        self.assertEqual(props.get('CollectMode'), PERSON_COLLECT_MODE, props)
        self.assertEqual(props.get('BindsTo'), PERSON_BINDS_TO, props)

    def test_stop_answers_stopped_when_systemd_has_unloaded_the_unit(self):
        # The unit-gone branch: the run unit also carries the test's CollectMode and bounds.
        launcher = self.launcher_module

        class CollectedUnitManager(launcher.EngineUnitManager):
            """The product's manager; every run unit also gets the test's bounds and is unloaded at its end."""

            def prepare(self, unit, *, properties=(), working_directory=None):
                super().prepare(unit, properties=(*properties, f'TasksMax={UNIT_TASKS_MAX}',
                                                  f'RuntimeMaxSec={UNIT_RUNTIME_MAX_S}',
                                                  'CollectMode=inactive-or-failed'),
                                working_directory=working_directory)

        self.compose(CollectedUnitManager(), run_helper=False)
        unit, props = self.drive_stop()
        self.assertEqual(props.get('CollectMode'), 'inactive-or-failed', props)
        after = unit_props(unit, list(AFTER_COLLECT))
        self.assertEqual(after, AFTER_COLLECT, f'systemd must have unloaded the collected unit: {after}')
        self.assertEqual(self.manager.observe(unit), OBSERVED_AFTER_COLLECT,
                         'the daemon must read the unloaded unit as gone')


@unittest.skipIf(GATE is not None, GATE or '')
class LikeNamedDecoyTests(unittest.TestCase):
    """Units and folders with like names, copied marks and a pattern in a list file are left alone.

    The decoys are this case's own units, each one sleeping process that ends by itself. A stop case
    runs in a child process twice: once to its end, once ended from outside (SIGKILL) as soon as its
    run has started (the unit-gone case, whose run unit nothing of the test is left to stop but its own
    bounds). After each, every decoy must still be active with its files as they were; after the
    kill, the child's own units must end by themselves within ``KILLED_END_S``.
    """

    def setUp(self):
        _arm_case_timeout(self, DECOY_CASE_TIMEOUT_S)
        self.planted, self.decoys = [], []
        self.addCleanup(self._end_planted)

    def plant(self, unit, argv, *, description=None, folder=None, files=()):
        """Start a decoy unit that ends by itself, with a private runtime folder holding ``files``."""
        command = ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + checked_unit(unit),
                   '--service-type=exec', f'--property=TasksMax={PLANTED_TASKS_MAX}',
                   f'--property=RuntimeMaxSec={PLANTED_LIFE_S}']
        if description is not None:
            command.append('--description=' + description)
        if folder is not None:
            command += ['--property=RuntimeDirectory=' + folder, '--property=RuntimeDirectoryMode=0700']
        self.planted.append((unit, folder))
        done = subprocess.run([*command, '--', sys.executable, '-I', '-c', PLANTED_WAIT, str(PLANTED_LIFE_S), *argv],
                              capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False)
        if done.returncode != 0:
            self.fail(f'could not plant {unit}: {done.stderr.strip() or done.returncode}')
        for name, text in files:
            write_own_file(runtime_dir() / folder, name, text)
        self.decoys.append((unit, folder, tuple(files)))

    def _end_planted(self):
        for unit, _folder in self.planted:
            unit_call('stop', unit)
            unit_call('reset-failed', unit)
        left = [unit for unit, _f in self.planted if unit_props(unit, ['LoadState']).get('LoadState') != 'not-found']
        folders = [f for _u, f in self.planted if f is not None and (runtime_dir() / f).exists()]
        if left or folders:
            self.fail(f'after the case these decoy units are still loaded: {left}; folders still there: {folders}')

    def assert_untouched(self, when):
        for unit, folder, files in self.decoys:
            state = unit_props(unit, ['ActiveState']).get('ActiveState')
            self.assertEqual(state, 'active', f'{when}: the decoy {unit} reads {state}')
            for name, text in files:
                self.assertEqual((runtime_dir() / folder / name).read_text(), text,
                                 f'{when}: the file {name} of the decoy {unit} changed')

    def child_case(self, test):
        module = getattr(sys.modules[__name__], '__spec__', None)
        module = module.name if module is not None else __name__
        return [*child_python(), '-m', 'unittest', f'{module}.StopOnTheRealPathTests.{test}']

    def test_like_named_units_and_folders_are_left_alone_by_a_run_and_by_a_killed_run(self):
        dead = dead_pid()
        self.assertFalse(alive(dead))
        # Every decoy name begins with a stem drawn here; the pattern in the list file below covers only
        # these decoys, so that even a code that read it as a pattern could reach nothing else.
        stem = f'{HOLDER_PREFIX}{dead}-{secrets.token_hex(4)}'
        copied = secrets.token_hex(16)
        copied_mark = MARK_PREFIX + copied
        like_run = f'recursion-run-run-{secrets.token_hex(16)}.service'
        holder_form = stem + '01'
        # The form a holder had in earlier versions of this case: holder name with an ended pid, its
        # description and its folder's marker with one token, a list file naming a pattern and a run unit.
        self.plant(holder_form + '.service', [copied_mark], description=f'recursion live Stop case holder, token {copied}',
                   folder=holder_form,
                   files=[('rc-stop-real-live.owner', copied + '\n'),
                          ('rc-stop-real-live.units', f'{stem}*\n{like_run}\n'),
                          ('data', 'someone else\'s data\n')])
        self.plant(stem + '02.service', [copied_mark])
        self.plant(stem + '03.service', ['not-marked'], folder=stem + '03', files=[('data', 'someone else\'s data\n')])
        # A unit named like a run unit, and one named like a run helper, with folders and data.
        self.plant(like_run, [copied_mark])
        like_helper = f'{HOLDER_PREFIX}{secrets.token_hex(8)}-run'
        self.plant(like_helper + '.service', [copied_mark], description='recursion live Stop case helper',
                   folder=like_helper, files=[('data', 'someone else\'s data\n')])
        # A folder with a like name under a unit of another name.
        lone = f'{HOLDER_PREFIX}{secrets.token_hex(8)}'
        self.plant(f'rc-stop-planted-{secrets.token_hex(6)}.service', [], folder=lone,
                   files=[('rc-stop-real-live.owner', copied + '\n'), ('data', 'someone else\'s data\n')])

        env = dict(os.environ)
        finished = subprocess.run(self.child_case('test_stop_answers_stopped_only_once_a_detached_grandchild_is_gone'),
                                  capture_output=True, text=True, env=env, timeout=CASE_TIMEOUT_S + HOLDER_MARGIN_S,
                                  check=False)
        self.assertEqual(finished.returncode, 0, finished.stderr[-2000:])
        self.assert_untouched('after a run of the person\'s case')

        # Decoys with the finished run's own mark, holder name form and folder marker copied.
        names = _NAMES.search(finished.stderr)
        self.assertIsNotNone(names, f'the case did not print its units: {finished.stderr[-2000:]}')
        ended_holder, _unit, ended_mark = names.groups()
        copy_of = f'{HOLDER_PREFIX}{secrets.token_hex(8)}'
        self.plant(copy_of + '.service', [ended_mark], description=f'recursion live Stop case helper {ended_mark}',
                   folder=copy_of, files=[('rc-stop-real-live.owner', ended_mark + '\n'),
                                          ('rc-stop-real-live.units', f'{stem}*\n{ended_holder}\n'),
                                          ('data', 'someone else\'s data\n')])

        child = subprocess.Popen(self.child_case('test_stop_answers_stopped_when_systemd_has_unloaded_the_unit'),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        said, deadline, found, running = b'', time.monotonic() + CASE_TIMEOUT_S, None, False
        try:
            while found is None and time.monotonic() < deadline and child.poll() is None:
                if select.select([child.stderr], [], [], 1.0)[0]:
                    said += os.read(child.stderr.fileno(), 65536)
                    found = _NAMES.search(said.decode('utf-8', 'replace'))
        finally:
            running = child.poll() is None
            child.kill()
            child.communicate(timeout=CALL_TIMEOUT_S)
        said = said.decode('utf-8', 'replace')
        self.assertIsNotNone(found, f'the killed run never printed its units: {said[-2000:]}')
        self.assertTrue(running, f'the child run ended before it could be ended from outside: {said[-2000:]}')
        holder, unit, _mark = found.groups()
        own = [checked_unit(holder), checked_unit(unit)]
        # The kill must land while the run is up and before its Stop, so that what ends it is its own bounds.
        at_kill = unit_props(unit, ['ActiveState']).get('ActiveState')
        self.assertEqual(at_kill, 'active', f'the run unit read {at_kill} right after the kill: {said[-2000:]}')
        deadline = time.monotonic() + KILLED_END_S
        while time.monotonic() < deadline:
            states = {u: unit_props(u, ['LoadState']).get('LoadState') for u in own}
            if set(states.values()) == {'not-found'} and not (runtime_dir() / holder[:-len('.service')]).exists():
                break
            time.sleep(0.2)
        self.assertEqual(set(states.values()), {'not-found'},
                         f'after the test run was ended from outside, its units did not end by themselves: {states}')
        self.assertFalse((runtime_dir() / holder[:-len('.service')]).exists(),
                         'the killed run\'s folder is still there')
        self.assert_untouched('after a run that was ended from outside')


if __name__ == '__main__':
    unittest.main()
