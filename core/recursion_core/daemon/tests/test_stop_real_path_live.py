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

The case starts units in the caller's user manager, so it runs only when ``LIVE_CELLS_ENV`` is set
to 1, a systemd user manager answers and its systemd-run can pass argv verbatim (the launcher
refuses an older one). Everywhere else it skips and says why. It ends only what it started: its
own grandchild, through a pidfd and after the stand-in's mark is read again, and its own units by
name. A cleanup that had to end a survivor fails the case.

Whatever ends the case, even a kill of the test run itself, it leaves nothing behind:
* its folder is the runtime directory of a holder unit of its own (``HOLDER_PREFIX``), a unit
  whose one process waits on a pidfd of the test process; when the test process ends, the holder
  ends, and systemd removes the folder and the unit (``--collect``);
* the run unit is bound to the holder (``BindsTo=``), so it is stopped when the holder ends, and
  carries ``CollectMode=inactive-or-failed``, so systemd unloads it once it is inactive or failed;
* at the start, a holder unit or folder of an earlier run of this case whose test process is gone
  is removed, found by ``HOLDER_PREFIX``, a name only this case uses.
"""
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest

LIVE_CELLS_ENV = 'RECURSION_USER_MANAGER_CELLS'

# Test values (none is a figure of the product).
STANDIN_LIFE_S = 45          # every stand-in process ends by itself after this
UNIT_TASKS_MAX = 16          # TasksMax of the run unit
UNIT_RUNTIME_MAX_S = 50      # RuntimeMaxSec of the run unit
CASE_TIMEOUT_S = 90          # the whole case, start to cleanup
CONFIRM_TIMEOUT_S = 10.0     # confirm_timeout_s handed to the control API
STOP_MARGIN_S = 3.0          # scheduling slack on top of the unit's own TimeoutStopSec
WAIT_FILE_S = 15.0           # how long the stand-in's pid files are waited for
CALL_TIMEOUT_S = 20          # each systemctl or systemd-run call of the test
HOLDER_TASKS_MAX = 2         # TasksMax of the holder unit (its one waiting process)
HOLDER_MARGIN_S = 30         # the holder's RuntimeMaxSec is CASE_TIMEOUT_S plus this

# The name of this case's holder units and folders: <prefix><test pid>-<random hex>.
HOLDER_PREFIX = 'rc-stop-real-live-'

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

# The holder's one process: it waits until the test process (argv[1]) has ended, then exits.
HOLDER_WAIT = r'''
import os, select, sys
fd = os.pidfd_open(int(sys.argv[1]))
select.select([fd], [], [])
'''

_DURATION = re.compile(r'(\d+(?:\.\d+)?)(us|ms|min|s)')
_SCALE = {'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'min': 60.0}
_HOLDER_NAME = re.compile(re.escape(HOLDER_PREFIX) + r'(\d+)-[0-9a-f]+')


def systemd_seconds(text):
    """'2s', '500ms', '1min 30s' as systemctl show prints them, in seconds. None if unreadable."""
    parts = _DURATION.findall(text.replace(' ', ''))
    if not parts or ''.join(n + u for n, u in parts) != text.replace(' ', ''):
        return None
    return sum(float(n) * _SCALE[u] for n, u in parts)


def _user_systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], capture_output=True, text=True,
                          timeout=CALL_TIMEOUT_S, check=False)


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
    text = _user_systemctl('show', '-p', ','.join(names), '--', unit).stdout
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def remove_leftovers():
    """Remove what an earlier run of this case left when its test process was ended from outside.

    Only names of this case's holders count (``HOLDER_PREFIX``, the test pid, random hex), and only
    when that test process is gone: a holder of a run of this case that is still going is left
    alone. Returns the names removed.
    """
    removed = []
    listing = _user_systemctl('list-units', '--all', '--plain', '--no-legend', '--', HOLDER_PREFIX + '*').stdout
    for line in listing.splitlines():
        unit = (line.split() or [''])[0]
        found = _HOLDER_NAME.fullmatch(unit[:-len('.service')] if unit.endswith('.service') else '')
        if found is None:
            continue
        pid = int(found.group(1))
        if pid == os.getpid() or alive(pid):
            continue
        _user_systemctl('stop', '--', unit)
        _user_systemctl('reset-failed', '--', unit)
        removed.append(unit)
    for entry in runtime_dir().glob(HOLDER_PREFIX + '*'):
        found = _HOLDER_NAME.fullmatch(entry.name)
        if found is None or entry.is_symlink() or not entry.is_dir() or entry.stat().st_uid != os.getuid():
            continue
        pid = int(found.group(1))
        if pid == os.getpid() or alive(pid):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(str(entry))
    return removed


class _CaseTimeout(Exception):
    pass


GATE = live_gate()


@unittest.skipIf(GATE is not None, GATE or '')
class StopOnTheRealPathTests(unittest.TestCase):
    def setUp(self):
        from recursion_core.daemon import Daemon, Registry
        from recursion_core.daemon import launcher
        from recursion_core.daemon.control_adapter import DurableRunRegistry, pid_alive
        from recursion_core.control.api import ControlAPI

        previous = signal.signal(signal.SIGALRM, self._timed_out)
        signal.setitimer(signal.ITIMER_REAL, CASE_TIMEOUT_S)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)

        self.leftovers_removed = remove_leftovers()
        self.tmp = self._start_holder()
        self.mark = 'rc-stop-real-' + secrets.token_hex(8)
        self.units, self.pidfds, self.processes = [], [], []
        self.addCleanup(self._end_own)

        holder_unit = self.holder_unit

        class BoundedUnitManager(launcher.EngineUnitManager):
            """The product's manager; every run unit also gets the test's bounds and is bound to the holder."""

            def prepare(self, unit, *, properties=(), working_directory=None):
                super().prepare(unit, properties=(*properties, f'TasksMax={UNIT_TASKS_MAX}',
                                                  f'RuntimeMaxSec={UNIT_RUNTIME_MAX_S}',
                                                  'CollectMode=inactive-or-failed',
                                                  f'BindsTo={holder_unit}'),
                                working_directory=working_directory)

        self.launcher_module = launcher
        self.manager = BoundedUnitManager()
        self.daemon = Daemon(Registry(self.tmp / 'reg'), self.manager)
        self.addCleanup(self.daemon.close)
        self.launch = launcher.ProductionLauncher('claude_code', self.daemon, self.manager, self.tmp / 'state')
        registry = DurableRunRegistry(self.daemon.registry, processes=pid_alive(self.manager))
        self.registry = registry
        self.api = ControlAPI(registry, launcher.UnitKillTree(self.daemon), self.start_run, time.time,
                              confirm_timeout_s=CONFIRM_TIMEOUT_S)
        self.detach = '1'

    def _timed_out(self, signum, frame):
        raise _CaseTimeout(f'the live Stop case passed {CASE_TIMEOUT_S} s')

    def _start_holder(self):
        """Start this case's holder unit; its runtime directory is the case's folder, gone with the unit."""
        name = f'{HOLDER_PREFIX}{os.getpid()}-{secrets.token_hex(6)}'
        self.holder_unit = name + '.service'
        folder = runtime_dir() / name
        done = subprocess.run(
            ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + self.holder_unit,
             '--service-type=exec', '--property=RuntimeDirectory=' + name,
             '--property=RuntimeDirectoryMode=0700', f'--property=TasksMax={HOLDER_TASKS_MAX}',
             f'--property=RuntimeMaxSec={CASE_TIMEOUT_S + HOLDER_MARGIN_S}',
             '--', sys.executable, '-I', '-c', HOLDER_WAIT, str(os.getpid())],
            capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False)
        self.addCleanup(self._stop_holder, folder)
        if done.returncode != 0 or not folder.is_dir():
            self.fail(f'the holder unit {self.holder_unit} did not start with its folder {folder}: '
                      f'{done.stderr.strip() or done.returncode}')
        info = folder.stat()
        if folder.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            self.fail(f'{folder} is not a private folder of this user')
        return folder

    def _stop_holder(self, folder):
        _user_systemctl('stop', '--', self.holder_unit)
        _user_systemctl('reset-failed', '--', self.holder_unit)
        left = unit_props(self.holder_unit, ['LoadState']).get('LoadState')
        if left != 'not-found' or folder.exists():
            self.fail(f'after the case the holder unit reads {left} and its folder is '
                      f'{"still there" if folder.exists() else "gone"}')

    def start_run(self, params):
        """The control API's starter: one launch through ProductionLauncher, bound to the API's run id."""
        launcher = self.launcher_module
        run_id = params['run_id']
        self.units.append(f'recursion-run-{run_id}.service')
        out = self.tmp / 'standin.pid'
        argv = [sys.executable, '-I', '-c', STANDIN, str(out), self.mark, str(STANDIN_LIFE_S), self.detach]
        ctx = launcher.RunContext(run_id)
        with launcher.bound_run(ctx):
            process = self.launch(argv, env={'PATH': '/usr/bin:/bin'})
        self.processes.append(process)
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
        """End what this case started and is still there. Ending a survivor fails the case."""
        ended = []
        for pid, fd in self.pidfds:
            if not pidfd_ended(fd) and carries_mark(pid, self.mark):
                signal.pidfd_send_signal(fd, signal.SIGKILL)
                ended.append(pid)
            os.close(fd)
        for process in self.processes:
            process.close()
        for unit in self.units:
            _user_systemctl('stop', '--', unit)
            _user_systemctl('reset-failed', '--', unit)
        left = [unit for unit in self.units if unit_props(unit, ['LoadState']).get('LoadState') != 'not-found']
        if ended or left:
            self.fail(f'cleanup had to end stand-in pids {ended}; units still loaded: {left}')

    def test_stop_answers_stopped_only_once_a_detached_grandchild_is_gone(self):
        started = self.api.call('run.start', {'tenant': 'live-stop', 'engine': 'claude_code', 'prompt': 'stand-in'})
        self.assertEqual(started.get('code'), 'started', started)
        run_id = started['run_id'] if 'run_id' in started else started['run']['run_id']
        unit = f'recursion-run-{run_id}.service'
        group, _main = self.manager.unit_identity(unit)
        self.assertTrue(group, 'systemd names no control group for the run unit')
        self.assertEqual(self.registry.get(run_id).get('cgroup'), group,
                         'the control record must carry the cgroup systemd reports for the unit')

        gpid, gsid, gpgrp = self.wait_file(self.tmp / 'standin.pid')
        mpid, msid, _mpgrp = self.wait_file(self.tmp / 'standin.pid.main')
        gfd = self.own_pidfd(gpid)
        self.own_pidfd(mpid)
        props = unit_props(unit, ['KillMode', 'KillSignal', 'TimeoutStopUSec', 'SendSIGKILL', 'Delegate',
                                  'CollectMode', 'BindsTo'])

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
        # The case's own hygiene: the run unit goes when it ends or fails, and with the case.
        self.assertEqual(props.get('CollectMode'), 'inactive-or-failed', props)
        self.assertIn(self.holder_unit, props.get('BindsTo', '').split(), props)


if __name__ == '__main__':
    unittest.main()
