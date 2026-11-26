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
refuses an older one). Everywhere else they skip and say why. They end only what they started:
their own grandchild, through a pidfd and after the stand-in's mark is read again, and their own
units by exact name. A cleanup that had to end a survivor fails the case.

Whatever ends a case, even a kill of the test run itself, it leaves nothing behind:
* each run of a case draws a token. Its folder is the runtime directory of a holder unit of its own
  (``HOLDER_PREFIX``), whose description carries the token; the folder holds a marker file with the
  same token and a list of the run units by exact name; the stand-in's mark ends with the token;
* the holder's one process waits on a pidfd of the test process. When the test process ends, the
  holder stops and resets each listed run unit whose command line carries the mark, by that exact
  name, then ends; systemd removes the holder's folder and unit (``--collect``);
* at the start, a holder of an earlier run whose test process is gone is removed only when its
  description and its folder's marker file carry the same token. Nothing is removed by a name
  alone: a unit or folder with a like name and no matching token is left and named in the output.
  The case never deletes a folder itself; systemd removes a holder's folder with the holder.
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
UNIT_RUNTIME_MAX_S = 50      # RuntimeMaxSec of the run unit in the unit-gone case
CASE_TIMEOUT_S = 90          # each case, start to cleanup
CONFIRM_TIMEOUT_S = 10.0     # confirm_timeout_s handed to the control API
STOP_MARGIN_S = 3.0          # scheduling slack on top of the unit's own TimeoutStopSec
WAIT_FILE_S = 15.0           # how long the stand-in's pid files are waited for
CALL_TIMEOUT_S = 20          # each systemctl or systemd-run call of the test and of the holder
HOLDER_TASKS_MAX = 4         # TasksMax of the holder unit (its waiting process and its systemctl calls)
HOLDER_MARGIN_S = 30         # the holder's RuntimeMaxSec is CASE_TIMEOUT_S plus this
PLANTED_LIFE_S = 60          # every unit the sweep case plants ends by itself after this
PLANTED_TASKS_MAX = 2        # TasksMax of each planted unit

# The name of this case's holder units and folders: <prefix><test pid>-<random hex>.
HOLDER_PREFIX = 'rc-stop-real-live-'
# The holder's description: these words, a space, and the run's token (32 hex digits).
HOLDER_DESCRIPTION = 'recursion live Stop case holder, token'
# In the holder's folder: the marker file with the token, and the run units by exact name.
OWNER_FILE = 'rc-stop-real-live.owner'
UNITS_FILE = 'rc-stop-real-live.units'
# The stand-in's mark: this prefix and the run's token.
MARK_PREFIX = 'rc-stop-real-'

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

# The holder's one process. argv: <test pid> <units file> <mark> <call timeout>. It waits until
# the test process has ended; then it stops and resets each run unit named in the units file, by
# that exact name, when the unit's command line carries the mark; then it exits.
HOLDER_WAIT = r'''
import os, select, subprocess, sys
test_pid, units_file, mark, timeout = int(sys.argv[1]), sys.argv[2], sys.argv[3], float(sys.argv[4])
fd = os.pidfd_open(test_pid)
select.select([fd], [], [])
def ctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
try:
    with open(units_file) as f:
        names = f.read().split()
except OSError:
    names = []
for unit in names:
    if mark in ctl("show", "-p", "ExecStart", "--value", "--", unit):
        ctl("stop", "--", unit)
        ctl("reset-failed", "--", unit)
'''

# A planted unit's one process: it sleeps argv[1] seconds; further arguments only mark it.
PLANTED_WAIT = 'import sys, time; time.sleep(float(sys.argv[1]))'

_DURATION = re.compile(r'(\d+(?:\.\d+)?)(us|ms|min|s)')
_SCALE = {'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'min': 60.0}
_HOLDER_NAME = re.compile(re.escape(HOLDER_PREFIX) + r'(\d+)-[0-9a-f]+')
_TOKEN = re.compile(r'[0-9a-f]{32}')


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


def holder_description(token):
    return f'{HOLDER_DESCRIPTION} {token}'


def holder_token(unit):
    """The token in a holder unit's description, or None when it carries none."""
    text = unit_props(unit, ['Description']).get('Description', '')
    head, _, token = text.rpartition(' ')
    return token if head == HOLDER_DESCRIPTION and _TOKEN.fullmatch(token) else None


def own_folder(folder):
    """True when ``folder`` is a real folder (no link) of this user that no one else may enter."""
    try:
        info = os.lstat(folder)
    except OSError:
        return False
    return (not os.path.islink(folder) and os.path.isdir(folder) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def read_own_file(folder, name, limit=4096):
    """A regular file of an own folder, read without following a link; None when there is none."""
    if not own_folder(folder):
        return None
    try:
        fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not (info.st_mode & 0o170000 == 0o100000) or info.st_uid != os.getuid():
            return None
        return os.read(fd, limit).decode('utf-8', 'replace')
    finally:
        os.close(fd)


def write_own_file(folder, name, text, append=False):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_EXCL)
    fd = os.open(folder / name, flags, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def remove_leftovers():
    """Remove what an earlier run of this case left when its test process was ended from outside.

    A holder unit counts as this case's own only when its name has the holder form, its test
    process is gone, its description carries a token and its folder (the holder's runtime
    directory, a private folder of this user) holds the marker file with that same token. Then the
    run units its folder lists are stopped and reset by exact name, each only when its command line
    carries the stand-in mark with that token, and the holder unit is stopped and reset, which makes
    systemd remove its folder. Everything else with a like name is left as it is.

    Returns (removed, left): the names removed, and each name left with the reason.
    """
    removed, left, seen = [], [], set()
    listing = _user_systemctl('list-units', '--all', '--plain', '--no-legend', '--', HOLDER_PREFIX + '*').stdout
    for line in listing.splitlines():
        unit = (line.split() or [''])[0]
        name = unit[:-len('.service')] if unit.endswith('.service') else unit
        seen.add(name)
        found = _HOLDER_NAME.fullmatch(name) if unit.endswith('.service') else None
        if found is None:
            left.append(f'{unit}: not a name of this case\'s holders')
            continue
        pid = int(found.group(1))
        if pid == os.getpid() or alive(pid):
            left.append(f'{unit}: its test process {pid} is still running')
            continue
        token = holder_token(unit)
        folder = runtime_dir() / name
        if token is None:
            left.append(f'{unit}: its description carries no token of this case')
            continue
        if (read_own_file(folder, OWNER_FILE) or '').strip() != token:
            left.append(f'{unit}: its folder holds no marker file with its token')
            continue
        mark = MARK_PREFIX + token
        for run_unit in (read_own_file(folder, UNITS_FILE) or '').split():
            command = _user_systemctl('show', '-p', 'ExecStart', '--value', '--', run_unit).stdout
            if mark not in command:
                left.append(f'{run_unit}: listed by {unit} but its command line carries no mark of it')
                continue
            _user_systemctl('stop', '--', run_unit)
            _user_systemctl('reset-failed', '--', run_unit)
            removed.append(run_unit)
        _user_systemctl('stop', '--', unit)
        _user_systemctl('reset-failed', '--', unit)
        removed.append(unit)
    for entry in sorted(runtime_dir().glob(HOLDER_PREFIX + '*')):
        if entry.name not in seen:
            left.append(f'{entry}: a folder with no holder unit of this case, so no token to match')
    return removed, left


def dead_pid():
    """The pid of a process of this test that has ended and been reaped."""
    done = subprocess.Popen(['true'])
    done.wait(timeout=CALL_TIMEOUT_S)
    return done.pid


class _CaseTimeout(Exception):
    pass


GATE = live_gate()


def _arm_case_timeout(case):
    def timed_out(signum, frame):
        raise _CaseTimeout(f'the live case passed {CASE_TIMEOUT_S} s')
    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, CASE_TIMEOUT_S)
    case.addCleanup(signal.signal, signal.SIGALRM, previous)
    case.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)


@unittest.skipIf(GATE is not None, GATE or '')
class StopOnTheRealPathTests(unittest.TestCase):
    def setUp(self):
        from recursion_core.daemon import launcher

        _arm_case_timeout(self)
        removed, left = remove_leftovers()
        self.leftovers_removed = removed
        if left:
            print('\nleft as they are (not this case\'s): ' + '; '.join(left), file=sys.stderr)
        self.token = secrets.token_hex(16)
        self.mark = MARK_PREFIX + self.token
        self.tmp = self._start_holder()
        self.units, self.pidfds, self.processes = [], [], []
        self.addCleanup(self._end_own)
        self.launcher_module = launcher
        self.detach = '1'

    def compose(self, manager):
        """The daemon and the control API over ``manager``, as the product composes them."""
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
        name = f'{HOLDER_PREFIX}{os.getpid()}-{secrets.token_hex(6)}'
        self.holder_unit = name + '.service'
        folder = runtime_dir() / name
        done = subprocess.run(
            ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + self.holder_unit,
             '--description=' + holder_description(self.token),
             '--service-type=exec', '--property=RuntimeDirectory=' + name,
             '--property=RuntimeDirectoryMode=0700', f'--property=TasksMax={HOLDER_TASKS_MAX}',
             f'--property=RuntimeMaxSec={CASE_TIMEOUT_S + HOLDER_MARGIN_S}',
             '--', sys.executable, '-I', '-c', HOLDER_WAIT, str(os.getpid()), str(folder / UNITS_FILE),
             self.mark, str(CALL_TIMEOUT_S)],
            capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False)
        self.addCleanup(self._stop_holder, folder)
        if done.returncode != 0 or not folder.is_dir():
            self.fail(f'the holder unit {self.holder_unit} did not start with its folder {folder}: '
                      f'{done.stderr.strip() or done.returncode}')
        if not own_folder(folder):
            self.fail(f'{folder} is not a private folder of this user')
        write_own_file(folder, OWNER_FILE, self.token + '\n')
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
        unit = f'recursion-run-{run_id}.service'
        self.units.append(unit)
        write_own_file(self.tmp, UNITS_FILE, unit + '\n', append=True)
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
        """End what this case started and is still there, by exact name. Ending a survivor fails the case."""
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

    def drive_stop(self):
        """Start the stand-in through the control API, press Stop, and check what every branch shares.

        Returns the run unit's name and its properties as systemd reported them before Stop.
        """
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
        return unit, props

    def test_stop_answers_stopped_only_once_a_detached_grandchild_is_gone(self):
        # The person's branch: the run unit exactly as the product starts it, loaded and failed after Stop.
        self.compose(self.launcher_module.EngineUnitManager())
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

        self.compose(CollectedUnitManager())
        unit, props = self.drive_stop()
        self.assertEqual(props.get('CollectMode'), 'inactive-or-failed', props)
        after = unit_props(unit, list(AFTER_COLLECT))
        self.assertEqual(after, AFTER_COLLECT, f'systemd must have unloaded the collected unit: {after}')
        self.assertEqual(self.manager.observe(unit), OBSERVED_AFTER_COLLECT,
                         'the daemon must read the unloaded unit as gone')


@unittest.skipIf(GATE is not None, GATE or '')
class LeftoverSweepTests(unittest.TestCase):
    """The start sweep removes a leftover only when it carries this case's token, nothing by its name alone."""

    def setUp(self):
        _arm_case_timeout(self)
        self.planted = []
        self.addCleanup(self._end_planted)

    def plant(self, unit, argv, *, description=None, folder=None, files=()):
        """Start a unit of this case that ends by itself, with a private runtime folder holding ``files``."""
        command = ['systemd-run', '--user', '--quiet', '--collect', '--unit=' + unit, '--service-type=exec',
                   f'--property=TasksMax={PLANTED_TASKS_MAX}',
                   f'--property=RuntimeMaxSec={PLANTED_LIFE_S + HOLDER_MARGIN_S}']
        if description is not None:
            command.append('--description=' + description)
        if folder is not None:
            command += ['--property=RuntimeDirectory=' + folder, '--property=RuntimeDirectoryMode=0700']
        done = subprocess.run([*command, '--', sys.executable, '-I', '-c', PLANTED_WAIT, str(PLANTED_LIFE_S), *argv],
                              capture_output=True, text=True, timeout=CALL_TIMEOUT_S, check=False)
        self.planted.append((unit, folder))
        if done.returncode != 0:
            self.fail(f'could not plant {unit}: {done.stderr.strip() or done.returncode}')
        for name, text in files:
            write_own_file(runtime_dir() / folder, name, text)

    def _end_planted(self):
        for unit, _folder in self.planted:
            _user_systemctl('stop', '--', unit)
            _user_systemctl('reset-failed', '--', unit)
        left = [unit for unit, _f in self.planted if unit_props(unit, ['LoadState']).get('LoadState') != 'not-found']
        folders = [f for _u, f in self.planted if f is not None and (runtime_dir() / f).exists()]
        if left or folders:
            self.fail(f'after the case these planted units are still loaded: {left}; folders still there: {folders}')

    def active(self, unit):
        return unit_props(unit, ['ActiveState']).get('ActiveState') == 'active'

    def test_the_start_sweep_removes_only_what_carries_this_cases_token(self):
        dead = dead_pid()
        self.assertFalse(alive(dead))
        own_token, wrong_token, lone_token = (secrets.token_hex(16) for _ in range(3))
        name = lambda: f'{HOLDER_PREFIX}{dead}-{secrets.token_hex(6)}'
        own, no_mark, wrong, lone = name(), name(), name(), name()
        own_run = f'rc-stop-planted-run-{secrets.token_hex(6)}.service'
        foreign_run = f'rc-stop-planted-run-{secrets.token_hex(6)}.service'
        lone_unit = f'rc-stop-planted-{secrets.token_hex(6)}.service'

        # A leftover of this case: holder name, dead test pid, description and marker with one token,
        # and a listed run unit that carries the mark. Beside it a listed run unit with no mark.
        self.plant(own_run, [MARK_PREFIX + own_token])
        self.plant(foreign_run, ['not-marked'])
        self.plant(own + '.service', [], description=holder_description(own_token), folder=own,
                   files=[(OWNER_FILE, own_token + '\n'), (UNITS_FILE, f'{own_run}\n{foreign_run}\n'),
                          ('data', 'leftover data\n')])
        # A like name and no mark at all.
        self.plant(no_mark + '.service', [], folder=no_mark, files=[('data', 'someone else\'s data\n')])
        # A like name whose description token and folder marker differ.
        self.plant(wrong + '.service', [], description=holder_description(wrong_token), folder=wrong,
                   files=[(OWNER_FILE, own_token + '\n'), ('data', 'someone else\'s data\n')])
        # A folder with a like name and a marker, but no unit of that name (its unit has another name).
        self.plant(lone_unit, [], description=holder_description(lone_token), folder=lone,
                   files=[(OWNER_FILE, lone_token + '\n'), ('data', 'someone else\'s data\n')])

        removed, left = remove_leftovers()
        said = '\n'.join(left)

        self.assertEqual(unit_props(own + '.service', ['LoadState']).get('LoadState'), 'not-found', removed)
        self.assertFalse((runtime_dir() / own).exists(), 'the leftover\'s folder must go with its holder')
        self.assertEqual(unit_props(own_run, ['LoadState']).get('LoadState'), 'not-found', removed)

        self.assertTrue(self.active(foreign_run), f'a listed run unit without the mark was stopped: {removed}')
        self.assertIn(foreign_run, said)
        for decoy in (no_mark, wrong):
            self.assertTrue(self.active(decoy + '.service'), f'{decoy} was stopped: {removed}')
            self.assertEqual((runtime_dir() / decoy / 'data').read_text(), 'someone else\'s data\n')
            self.assertIn(decoy + '.service', said)
            self.assertNotIn(decoy + '.service', removed)
        self.assertTrue(self.active(lone_unit))
        self.assertEqual((runtime_dir() / lone / 'data').read_text(), 'someone else\'s data\n')
        self.assertIn(str(runtime_dir() / lone), said)


if __name__ == '__main__':
    unittest.main()
