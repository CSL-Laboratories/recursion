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
* the holder ``<HOLDER_PREFIX><hex>.service``: its runtime directory is the case's folder, which
  only the test process writes (the daemon's registry); its one process waits on a pidfd of the
  test process and then exits; ``--collect`` makes systemd unload it and remove the folder;
  ``RuntimeMaxSec`` bounds it;
* the run folder ``<HOLDER_PREFIX><hex>-files.service``: its runtime directory holds everything the
  run unit writes (the launcher's state, the run's output files, the stand-in's pid files). Its one
  process waits until systemd stops it, and systemd stops it only after the run unit has stopped:
  in the person's case it carries ``Before=`` the run unit, in the unit-gone case the run unit
  carries ``After=`` it. So no process of the run is left to write into the folder while systemd
  removes it;
* the run helper ``<HOLDER_PREFIX><hex>-run.service``: it waits on a pidfd of the test process as
  the holder does, and when it ends, systemd stops the run unit and then the run folder. In the
  person's case the helper carries ``PropagatesStopTo=`` the run unit and the run folder, and the
  run unit itself carries nothing of the test's own: systemd shows the helper on it as
  ``StopPropagatedFrom`` and the run folder in its ``After``. In the unit-gone case the run unit
  carries ``StopPropagatedFrom=`` the helper among the test's own properties (a ``PropagatesStopTo=``
  the run unit on the helper would keep the stopped unit loaded and hide the branch the case
  drives), and the helper carries ``PropagatesStopTo=`` the run folder.
Each case prints the names of these units and the whole paths of both folders before it starts the
run helper and before the launch begins, and a second line once the launch has returned.

What may stay behind after the test process is ended from outside:
* the person's run unit, loaded and failed, with ``Result`` timeout (its stop needed the hard end)
  or exit-code (the end came while the launch was in flight: the run unit started after its run
  helper had ended, and its run ended by itself). Remove it with the command the case prints:
  ``systemctl --user reset-failed -- recursion-run-<run id>.service``;
* after an end while the run unit was starting (the launch in flight), a run unit that reads
  activating until systemd's own start timeout ends it. That timeout is systemd's
  (``DefaultTimeoutStartSec``, ninety seconds on the build host), not a value of this test, and
  the unit-gone case's ``RuntimeMaxSec`` does not count while a unit is activating. The unit-gone
  case's unit is then unloaded; a person's unit that stays failed goes with the same command;
* after an end while the launch was in flight, in a narrow race, the run folder: the run unit can
  start after its run folder was stopped, and the stand-in can then write while systemd removes
  the folder. Remove it with ``rm -r -- '<path>'``, the path the case prints. After an end at any
  other point, the run folder goes after the run unit, as above.
"""
import json
import os
from pathlib import Path
import re
import secrets
import select
import signal
import subprocess
import sys
import tempfile
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
CALL_TIMEOUT_S = 20          # each systemctl or systemd-run call of the test (each step of a cleanup is bounded by it)
HOLDER_TASKS_MAX = 2         # TasksMax of each helper unit (its one waiting process)
HOLDER_MARGIN_S = 30         # a helper's RuntimeMaxSec is CASE_TIMEOUT_S plus this
DECOY_CASE_TIMEOUT_S = 300   # the decoy case, with its two runs of a stop case in a child process
PLANTED_LIFE_S = 330         # every decoy unit ends by itself after this
PLANTED_TASKS_MAX = 2        # TasksMax of each decoy unit
KILLED_END_S = UNIT_RUNTIME_MAX_S + 15.0  # after a unit-gone run is ended from outside, how long its units have
CHILD_RUN_TIMEOUT_S = 60     # each process of the child-name case (a load of this file or a child run)

# The names of this case's helper units and folders: <prefix><random hex> for the holder, and for a run
# <prefix><random hex>-run (the run helper) and <prefix><random hex>-files (the run folder).
HOLDER_PREFIX = 'rc-stop-real-live-'
# The stand-in's mark: this prefix and random hex, drawn for each case.
MARK_PREFIX = 'rc-stop-real-'
# Every unit name the test hands to systemctl or systemd-run: letters, digits, dashes and dots only,
# a letter or digit first, ending in .service. No *, ?, [ or \ (a pattern), no leading dash (an option).
UNIT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]*\.service')
# The line each stop case prints before it starts anything of its run: its units (holder, run unit, run
# helper, run folder), its mark, both folders' whole paths, and what to do after a kill.
NAMES_LINE = 'rc-stop-real-live units:'
_NAMES = re.compile(re.escape(NAMES_LINE) + r' (\S+) (\S+) (\S+) (\S+) mark: (\S+?)\. Folders: (\S+) (\S+?)\. ')
# The line each stop case prints once the launch has returned.
STARTED_LINE = 'rc-stop-real-live run started:'
_STARTED = re.compile(re.escape(STARTED_LINE) + r' (\S+)')

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

# The run folder's one process. argv: <test pid>. If the test process has already ended (the folder was
# started after it), it exits at once; otherwise it waits until systemd stops the unit, which happens
# only after the run unit has stopped (see the module text). It reads no file and calls nothing.
FILES_WAIT = r'''
import os, select, signal, sys
try:
    fd = os.pidfd_open(int(sys.argv[1]))
except OSError:
    sys.exit(0)
if select.select([fd], [], [], 0)[0]:
    sys.exit(0)
os.close(fd)
signal.pause()
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


def pasteable(path):
    """``path`` in single quotes, as a shell reads it back unchanged."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def start_helper(unit, *, folder=None, propagates_stop_to=(), before=None, wait=HOLDER_WAIT):
    """Start one helper unit of this test; it ends when this test process ends (``wait``), when systemd
    stops it, or at its RuntimeMaxSec."""
    command = ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + checked_unit(unit),
               '--description=recursion live Stop case helper', '--service-type=exec',
               f'--property=TasksMax={HOLDER_TASKS_MAX}',
               f'--property=RuntimeMaxSec={CASE_TIMEOUT_S + HOLDER_MARGIN_S}']
    if folder is not None:
        command += ['--property=RuntimeDirectory=' + folder, '--property=RuntimeDirectoryMode=0700']
    if propagates_stop_to:
        command.append('--property=PropagatesStopTo=' + ' '.join(checked_unit(u) for u in propagates_stop_to))
    if before is not None:
        command.append('--property=Before=' + checked_unit(before))
    return subprocess.run([*command, '--', sys.executable, '-I', '-c', wait, str(os.getpid())],
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


def importable_name(path):
    """The folder ``path`` imports from and its dotted module name: the folders above it that hold an
    ``__init__.py`` are its packages."""
    path = Path(path).resolve()
    parts, folder = [path.stem], path.parent
    while (folder / '__init__.py').is_file():
        parts.insert(0, folder.name)
        folder = folder.parent
    return folder, '.'.join(parts)


# This file's dotted name and the folder it imports from, read from where the file lies, so that a child
# process imports it however this process loaded it: as a script (where its name is __main__), through the
# gate (which runs a file with a __main__ guard by its path), by its dotted name or by discovery.
IMPORT_ROOT, MODULE_NAME = importable_name(__file__)
# A test run in a child process by the dotted name of its case. argv: <import folder> <test name>. The
# folder goes first on the path, so the child imports this file even where neither PYTHONPATH nor the
# folder the child runs in leads to it.
CHILD_RUN = ('import sys, unittest\n'
             'sys.path.insert(0, sys.argv[1])\n'
             "unittest.main(module=None, argv=['python -m unittest', sys.argv[2]])\n")


class _CaseTimeout(Exception):
    pass


class _TimerOffInCleanup:
    """The case timer is cancelled before the first cleanup, so that it cannot fire inside one and cut
    the ending of the case's own units short; each cleanup step is bounded by its own call timeout."""

    def doCleanups(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        return super().doCleanups()


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
class StopOnTheRealPathTests(_TimerOffInCleanup, unittest.TestCase):
    def setUp(self):
        from recursion_core.daemon import launcher

        _arm_case_timeout(self)
        self.mark = MARK_PREFIX + secrets.token_hex(16)
        self.units, self.helpers, self.pidfds, self.processes, self.folders = [], [], [], [], []
        self.tmp = self._start_holder()
        self.addCleanup(self._end_own)
        self.launcher_module = launcher
        self.detach = '1'

    def compose(self, manager, *, person):
        """The daemon and the control API over ``manager``, as the product composes them.

        ``person``: the run unit carries nothing of the test's own (see the module text). The names of
        the run helper and the run folder are drawn here; the launcher keeps its state in the run folder.
        """
        self.person = person
        stem = f'{HOLDER_PREFIX}{secrets.token_hex(8)}'
        self.helper_unit = checked_unit(stem + '-run.service')
        self.files_unit = checked_unit(stem + '-files.service')
        self.run_folder = runtime_dir() / (stem + '-files')
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon.control_adapter import DurableRunRegistry, pid_alive
        from recursion_core.control.api import ControlAPI
        launcher = self.launcher_module
        self.manager = manager
        self.daemon = Daemon(Registry(self.tmp / 'reg'), manager)
        self.addCleanup(self.daemon.close)
        self.launch = launcher.ProductionLauncher('claude_code', self.daemon, manager, self.run_folder / 'state')
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

        It first prints the names of everything the run can leave, then starts the run helper and the
        run folder (see the module text), and only then launches.
        """
        launcher = self.launcher_module
        if self.units:
            raise RuntimeError('this case starts one run; a second start was asked for')
        run_id = params['run_id']
        unit = checked_unit(f'recursion-run-{run_id}.service')
        helper, files, folder = self.helper_unit, self.files_unit, self.run_folder
        print(f'\n{NAMES_LINE} {self.holder_unit} {unit} {helper} {files} mark: {self.mark}. Folders: {self.tmp} '
              f'{folder}. If this test run is ended from outside, these units and folders end by themselves, '
              'except: the run unit may stay failed in your user manager (remove it with '
              f'systemctl --user reset-failed -- {unit}), or read activating until systemd\'s own start timeout '
              'ends it if the end came while it was starting; and if the end came during the launch, the run '
              f'folder may stay (remove it with rm -r -- {pasteable(folder)}).', file=sys.stderr, flush=True)
        self.units.append(unit)
        self.helpers.append(helper)
        done = start_helper(helper, propagates_stop_to=(unit, files) if self.person else (files,))
        if done.returncode != 0:
            raise RuntimeError(f'the run helper {helper} did not start: {done.stderr.strip() or done.returncode}')
        self.helpers.append(files)
        self.folders.append(folder)
        done = start_helper(files, folder=folder.name, before=unit if self.person else None, wait=FILES_WAIT)
        if done.returncode != 0 or not own_folder(folder):
            raise RuntimeError(f'the run folder {files} did not start with a private folder {folder}: '
                               f'{done.stderr.strip() or done.returncode}')
        out = folder / 'standin.pid'
        argv = [sys.executable, '-I', '-c', STANDIN, str(out), self.mark, str(STANDIN_LIFE_S), self.detach]
        ctx = launcher.RunContext(run_id)
        with launcher.bound_run(ctx):
            process = self.launch(argv, env={'PATH': '/usr/bin:/bin'})
        self.processes.append(process)
        print(f'{STARTED_LINE} {unit}', file=sys.stderr, flush=True)
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
        folders = [str(folder) for folder in self.folders if folder.exists()]
        if ended or left or folders:
            self.fail(f'cleanup had to end stand-in pids {ended}; units still loaded: {left}; folders still there: {folders}')

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

        gpid, gsid, gpgrp = self.wait_file(self.run_folder / 'standin.pid')
        mpid, msid, _mpgrp = self.wait_file(self.run_folder / 'standin.pid.main')
        gfd = self.own_pidfd(gpid)
        self.own_pidfd(mpid)
        props = unit_props(unit, ['KillMode', 'KillSignal', 'TimeoutStopUSec', 'SendSIGKILL', 'Delegate',
                                  'CollectMode', 'BindsTo', 'StopPropagatedFrom', 'After'])

        self.assertEqual(props.get('StopPropagatedFrom'), self.helper_unit,
                         'the run unit must be stopped by systemd when the case\'s run helper ends, and be '
                         f'bound from nothing else: {props}')
        self.assertIn(self.files_unit, props.get('After', '').split(),
                      'the run unit must stop before systemd removes the run folder, so that no process of the '
                      f'run writes into the folder while it is removed: {props}')
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
        self.compose(self.launcher_module.EngineUnitManager(), person=True)
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
        # The unit-gone branch: the run unit also carries the test's CollectMode and bounds, is stopped
        # when the run helper ends, and stops before the run folder.
        launcher = self.launcher_module
        case = self

        class CollectedUnitManager(launcher.EngineUnitManager):
            """The product's manager; every run unit also gets the test's bounds and is unloaded at its end."""

            def prepare(self, unit, *, properties=(), working_directory=None):
                super().prepare(unit, properties=(*properties, f'TasksMax={UNIT_TASKS_MAX}',
                                                  f'RuntimeMaxSec={UNIT_RUNTIME_MAX_S}',
                                                  'CollectMode=inactive-or-failed',
                                                  'StopPropagatedFrom=' + checked_unit(case.helper_unit),
                                                  'After=' + checked_unit(case.files_unit)),
                                working_directory=working_directory)

        self.compose(CollectedUnitManager(), person=False)
        unit, props = self.drive_stop()
        self.assertEqual(props.get('CollectMode'), 'inactive-or-failed', props)
        after = unit_props(unit, list(AFTER_COLLECT))
        self.assertEqual(after, AFTER_COLLECT, f'systemd must have unloaded the collected unit: {after}')
        self.assertEqual(self.manager.observe(unit), OBSERVED_AFTER_COLLECT,
                         'the daemon must read the unloaded unit as gone')


@unittest.skipIf(GATE is not None, GATE or '')
class LikeNamedDecoyTests(_TimerOffInCleanup, unittest.TestCase):
    """Units and folders with like names, copied marks and a pattern in a list file are left alone.

    The decoys are this case's own units, each one sleeping process that ends by itself. A stop case
    runs in a child process twice: once to its end, once ended from outside (SIGKILL) as soon as its
    run has started (the unit-gone case: systemd stops its run unit when its run helper ends, and its
    run folder after that). After each, every decoy must still be active with its files as they were;
    after the kill, the child's own units and both its folders must be gone by themselves within
    ``KILLED_END_S``. The child imports this file by its dotted name (``child_case``), whatever way this
    process loaded it.
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
        return [*child_python(), '-c', CHILD_RUN, str(IMPORT_ROOT), f'{MODULE_NAME}.StopOnTheRealPathTests.{test}']

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
        ended_holder, _unit, _helper, _files, ended_mark, _folder, _run_folder = names.groups()
        copy_of = f'{HOLDER_PREFIX}{secrets.token_hex(8)}'
        self.plant(copy_of + '.service', [ended_mark], description=f'recursion live Stop case helper {ended_mark}',
                   folder=copy_of, files=[('rc-stop-real-live.owner', ended_mark + '\n'),
                                          ('rc-stop-real-live.units', f'{stem}*\n{ended_holder}\n'),
                                          ('data', 'someone else\'s data\n')])

        child = subprocess.Popen(self.child_case('test_stop_answers_stopped_when_systemd_has_unloaded_the_unit'),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        said, deadline, started, running, at_kill = b'', time.monotonic() + CASE_TIMEOUT_S, None, False, None
        try:
            while started is None and time.monotonic() < deadline and child.poll() is None:
                if select.select([child.stderr], [], [], 1.0)[0]:
                    said += os.read(child.stderr.fileno(), 65536)
                    started = _STARTED.search(said.decode('utf-8', 'replace'))
            if started is not None:
                # Read just before the kill: it must land while the run is up and before its Stop, so
                # that what ends the run is the run helper's end.
                at_kill = unit_props(checked_unit(started.group(1)), ['ActiveState']).get('ActiveState')
        finally:
            running = child.poll() is None
            child.kill()
            child.communicate(timeout=CALL_TIMEOUT_S)
        said = said.decode('utf-8', 'replace')
        found = _NAMES.search(said)
        self.assertIsNotNone(found, f'the killed run never printed its units: {said[-2000:]}')
        self.assertIsNotNone(started, f'the killed run never said that its run started: {said[-2000:]}')
        self.assertTrue(running, f'the child run ended before it could be ended from outside: {said[-2000:]}')
        holder, unit, helper, files, _mark, folder, run_folder = found.groups()
        self.assertEqual(started.group(1), unit, said[-2000:])
        own = [checked_unit(holder), checked_unit(unit), checked_unit(helper), checked_unit(files)]
        folders = [Path(folder), Path(run_folder)]
        self.assertEqual(at_kill, 'active', f'the run unit read {at_kill} just before the kill: {said[-2000:]}')
        deadline = time.monotonic() + KILLED_END_S
        while time.monotonic() < deadline:
            states = {u: unit_props(u, ['LoadState']).get('LoadState') for u in own}
            if set(states.values()) == {'not-found'} and not any(f.exists() for f in folders):
                break
            time.sleep(0.2)
        self.assertEqual(set(states.values()), {'not-found'},
                         f'after the test run was ended from outside, its units did not end by themselves: {states}')
        self.assertEqual([str(f) for f in folders if f.exists()], [],
                         'the killed run\'s folders are still there')
        self.assert_untouched('after a run that was ended from outside')


# The decoy case's child command, built in a process that loaded this file one way. argv: <way> <args>.
# It prints CHILD <json> (the command, the folder it runs in, its environment) and exits. For the way
# 'gate' it prints GATE <json>: the command, folder and environment the gate's run_test_file would run
# this file with; it runs nothing.
CHILD_PROBE = r'''
import importlib, json, os, runpy, subprocess, sys, unittest
DECOY = 'test_like_named_units_and_folders_are_left_alone_by_a_run_and_by_a_killed_run'
TEST = 'test_stop_answers_stopped_only_once_a_detached_grandchild_is_gone'
def say(cls):
    command = cls(DECOY).child_case(TEST)
    print('CHILD ' + json.dumps({'cmd': command, 'cwd': os.getcwd(), 'env': dict(os.environ)}), flush=True)
    raise SystemExit(0)
def walk(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from walk(test)
        else:
            yield test
way, args = sys.argv[1], sys.argv[2:]
if way == 'gate':
    sys.path.insert(0, args[0])
    import ci_gate
    def capture(command, cwd=None, env=None, **_):
        print('GATE ' + json.dumps({'cmd': [str(c) for c in command], 'cwd': str(cwd), 'env': env}), flush=True)
        return subprocess.CompletedProcess(command, 0)
    ci_gate.subprocess.run = capture
    ci_gate.run_test_file(args[1])
    raise SystemExit(0)
if way == 'script':
    unittest.main = lambda *a, **k: say(sys._getframe(1).f_globals['LikeNamedDecoyTests'])
    runpy.run_path(args[0], run_name='__main__')
elif way == 'runner':
    unittest.TestLoader.loadTestsFromModule = lambda self, module, *a, **k: say(module.LikeNamedDecoyTests)
    sys.argv = ['-c', args[1]]
    exec(args[0], {'__name__': '__main__'})
elif way == 'module':
    say(importlib.import_module(args[0]).LikeNamedDecoyTests)
elif way == 'discover':
    for test in walk(unittest.defaultTestLoader.discover(args[0], pattern=args[1], top_level_dir=args[2] or None)):
        if type(test).__name__ == 'LikeNamedDecoyTests':
            say(type(test))
raise SystemExit('the decoy case was not reached this way')
'''


class DecoyChildNameTests(unittest.TestCase):
    """The decoy case's child names a case it can import, however this file is run.

    This file runs as a script, through the gate's ``run_test_file`` (which runs a file with a
    ``__main__`` guard by its path), by its dotted name, and by discovery with a top-level folder or
    from its own folder. For each way a process loads this file that way and builds the decoy case's
    child command; the child then runs with ``LIVE_CELLS_ENV`` unset and must run its one case and skip
    it. Nothing here starts a unit or asks the user manager anything, so this case runs on every host.
    """

    def probe(self, python, env, cwd, args, line='CHILD '):
        done = subprocess.run([*python, '-c', CHILD_PROBE, *args], capture_output=True, text=True, env=env,
                              cwd=cwd, timeout=CHILD_RUN_TIMEOUT_S, check=False)
        for text in done.stdout.splitlines():
            if text.startswith(line):
                return json.loads(text[len(line):])
        self.fail(f'loading this file ({args[0]}) gave no {line.strip()} line (exit {done.returncode}): '
                  f'{done.stderr[-1500:]}')

    def assert_child_skips(self, way, said):
        env = {k: v for k, v in said['env'].items() if k != LIVE_CELLS_ENV}
        done = subprocess.run(said['cmd'], capture_output=True, text=True, env=env, cwd=said['cwd'],
                              timeout=CHILD_RUN_TIMEOUT_S, check=False)
        why = (f'run {way}, the decoy case\'s child asks for {said["cmd"][-1]!r}; it must import that case '
               f'and skip it (exit {done.returncode}): {done.stderr[-1500:]}')
        self.assertEqual(done.returncode, 0, why)
        self.assertRegex(done.stderr, r'(?m)^Ran 1 test in ', why)
        self.assertRegex(done.stderr, r'(?m)^OK \(skipped=1\)$', why)

    def test_the_decoy_cases_child_skips_its_case_however_this_file_is_run(self):
        import recursion_core

        core = Path(recursion_core.__file__).resolve().parent.parent
        this = Path(__file__).resolve()
        dotted = '.'.join(this.relative_to(core).with_suffix('').parts)
        bare = {k: v for k, v in os.environ.items() if k not in (LIVE_CELLS_ENV, 'PYTHONPATH')}
        with_path = {**bare, 'PYTHONPATH': str(core)}
        with tempfile.TemporaryDirectory(prefix='rc-stop-child-name-') as tmp:
            ways = (
                ('as a script', with_path, ['script', str(this)]),
                ('by its dotted name', with_path, ['module', dotted]),
                ('by discovery with a top-level folder', bare, ['discover', str(this.parent), this.name, str(core)]),
                ('by discovery from its own folder', with_path, ['discover', str(this.parent), this.name, '']),
            )
            for way, env, args in ways:
                with self.subTest(way=way):
                    self.assert_child_skips(way, self.probe(child_python(), env, tmp, args))
            with self.subTest(way='through the gate'):
                gate = core.parent / 'tools' / 'ci_gate.py'
                if not gate.is_file():
                    self.skipTest(f'the gate is not at {gate}')
                rel = this.relative_to(core.parent).as_posix()
                run = self.probe(child_python(), with_path, tmp, ['gate', str(gate.parent), rel], line='GATE ')
                command = run['cmd']
                env = {k: v for k, v in (run['env'] or with_path).items() if k != LIVE_CELLS_ENV}
                if command[-1:] == [rel] and command[-3:-2] == ['-c']:
                    python, args = command[:-3], ['runner', command[-2], rel]
                elif command[-1:] == [rel]:
                    python, args = command[:-1], ['script', rel]
                else:
                    self.fail(f'the gate runs this file in a way this case does not know: {command}')
                self.assert_child_skips('through the gate', self.probe(python, env, run['cwd'], args))


if __name__ == '__main__':
    unittest.main()
