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

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from pathlib import Path

_FLAG = '--simulate-darwin-launch'
_MARK = 'SIMULATED_DARWIN_LAUNCH '
_SCRATCH = Path('/var/tmp/grok-darwin-launcher')
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

    scratch = Path(tempfile.mkdtemp(prefix='dl', dir=str(_SCRATCH)))
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
    _SCRATCH.mkdir(mode=0o700, exist_ok=True)
    sys.stdout.write('\n' + _MARK + json.dumps(_launch()) + '\n')
    return 0


def simulate() -> dict:
    _SCRATCH.mkdir(mode=0o700, exist_ok=True)
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['TMPDIR'] = str(_SCRATCH)
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


if __name__ == '__main__':
    if sys.argv[1:2] == [_FLAG]:
        raise SystemExit(_child_main())
    unittest.main()
