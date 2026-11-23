"""Plugin hosts as run units: one socket as the channel, Stop by the system's kill, restart past a deadline.

The tests marked as needing a user manager start real ``systemd-run --user --pipe`` units named
``recursion-host-<16 hex>.service``. Each of those units carries ``TasksMax=`` and ``RuntimeMaxSec=`` so none can
grow or outlive the run, and the tests end only the units they started themselves. The rest run anywhere on Linux.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import venv

from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.limits import ARTIFACT
from recursion_core.plugin_security.tests.support import manifest_bytes

LINUX = sys.platform.startswith('linux')
CORE = Path(__file__).resolve().parents[3]
CGROUP_ROOT = Path('/sys/fs/cgroup')
# The guard on every unit these tests start: a hard count on its processes and a time it cannot outlive.
GUARD = ('TasksMax=16', 'RuntimeMaxSec=120')
# A free-threaded build runs the host with its GIL check on; a build with a GIL can only run it with the check off.
GIL_CHECK = 'on' if not getattr(sys, '_is_gil_enabled', lambda: True)() else 'off'

# A plugin that will not end by itself: every signal a polite stop sends is ignored, and its loop swallows every
# exception, SystemExit and KeyboardInterrupt included.
HOSTILE = b'''
import os, signal, stat, time
for _number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_number, signal.SIG_IGN)

def operator(command):
    if command == "ok":
        return "ok"
    if command == "exit":
        os._exit(3)
    if command == "exit0":
        os._exit(0)
    if command == "fds":
        found = []
        for name in sorted(os.listdir("/proc/self/fd"), key=int):
            try:
                info = os.fstat(int(name))
            except OSError:
                continue
            if stat.S_ISSOCK(info.st_mode):
                found.append([info.st_dev, info.st_ino, int(name)])
        return found
    while True:
        try:
            time.sleep(0.05)
        except BaseException:
            pass
'''
POLITE = b'def operator(command):\n    return {"echo": command}\n'


def manager_ready():
    """A running user manager whose systemd-run can pass an argv verbatim (version 254 or later)."""
    if not LINUX:
        return False
    try:
        state = subprocess.run(['systemctl', '--user', 'is-system-running'], capture_output=True, text=True,
                               timeout=15, check=False).stdout.strip()
        version = subprocess.run(['systemd-run', '--version'], capture_output=True, text=True, timeout=15,
                                 check=False).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    return state in ('running', 'degraded') and len(version) > 1 and version[1].isdigit() and int(version[1]) >= 254


class StandInCore:
    """The core service as a host channel needs it: one client per plugin, answering every request with {}."""

    def client_for(self, plugin_id):
        return lambda request: b'{}'


def package(source):
    return manifest_bytes('01' * 32, plugin_id='stand.in', source=source), {ARTIFACT: source}


def gone(group, timeout_s=5.0):
    """True once the cgroup directory of a unit is absent (the kernel removes it only when no process is left)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not (CGROUP_ROOT / group.lstrip('/')).exists():
            return True
        time.sleep(0.05)
    return False


def socket_inodes():
    """(device, inode) of every socket this test process holds."""
    found = set()
    for name in os.listdir('/proc/self/fd'):
        try:
            info = os.fstat(int(name))
        except OSError:
            continue
        if stat.S_ISSOCK(info.st_mode):
            found.add((info.st_dev, info.st_ino))
    return found


@unittest.skipUnless(LINUX, 'plugin host units are Linux only')
class StartAndStopWithoutAManagerTest(unittest.TestCase):
    """What the launcher asks of systemd-run, and what Stop does, read without starting anything."""

    def test_a_host_unit_is_the_launchers_unit_path_with_pipe_and_no_remain_after_exit(self):
        from recursion_core.daemon import launcher

        manager = launcher.EngineUnitManager()
        manager._verbatim_argv = True
        one, other = socket.socketpair()
        self.addCleanup(one.close)
        self.addCleanup(other.close)
        with unittest.mock.patch.object(launcher.subprocess, 'Popen') as popen:
            process = manager.start_piped('recursion-host-0123456789abcdef.service', ['python', '-m', 'x'], one,
                                          properties=('TasksMax=8',), working_directory='/w')
        self.assertIs(process, popen.return_value)
        (command,), options = popen.call_args
        cut = command.index('--')
        head, argv = command[:cut], command[cut + 1:]
        self.assertEqual(argv, ['python', '-m', 'x'])
        self.assertEqual(head[:3], ['systemd-run', '--user', '--quiet'])
        for item in ('--unit=recursion-host-0123456789abcdef.service', '--service-type=exec', '--pipe',
                     '--expand-environment=no', '--collect', '--property=KillMode=control-group',
                     '--property=Delegate=yes', '--property=TasksMax=8', '--working-directory=/w',
                     *('--property=' + item for item in launcher.STOP_PROPERTIES)):
            self.assertIn(item, head)
        self.assertFalse([item for item in head if 'RemainAfterExit' in item], head)
        self.assertIs(options['stdin'], one)
        self.assertIs(options['stdout'], one)
        self.assertTrue(options['close_fds'])

    def test_the_engine_unit_path_keeps_every_property_it_had(self):
        from recursion_core.daemon import launcher

        manager = launcher.EngineUnitManager()
        manager._verbatim_argv = True
        manager.prepare('recursion-run-r1.service', properties=('StandardOutput=append:/x',))
        with unittest.mock.patch.object(manager, '_call') as call:
            manager.start('recursion-run-r1.service', ['engine'])
        (command,), _ = call.call_args
        for item in ('--property=KillMode=control-group', '--property=Delegate=yes',
                     '--property=RemainAfterExit=yes', '--property=StandardOutput=append:/x',
                     *('--property=' + item for item in launcher.STOP_PROPERTIES)):
            self.assertIn(item, command)
        self.assertNotIn('--pipe', command)

    def test_stop_is_the_cgroup_kill_of_the_unit_and_sends_the_host_nothing(self):
        from recursion_core.daemon import host_units, launcher

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            unit = 'recursion-host-0123456789abcdef.service'
            group = '/user.slice/app.slice/' + unit
            (root / group.lstrip('/')).mkdir(parents=True)
            (root / group.lstrip('/') / 'cgroup.kill').write_bytes(b'')
            manager = launcher.EngineUnitManager(cgroup_root=root)
            units = host_units.PluginHostUnits(manager, root / 'state', StandInCore())
            daemon_end, host_end = socket.socketpair()
            self.addCleanup(host_end.close)
            process = unittest.mock.Mock()
            process.poll.return_value = None
            host = host_units.HostUnit('stand.in', unit, daemon_end, process, None)
            host.group = group
            units._units['stand.in'] = host
            began = time.monotonic()
            units.stop('stand.in')
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertEqual((root / group.lstrip('/') / 'cgroup.kill').read_bytes(), b'1')
            host_end.setblocking(False)
            with self.assertRaises(BlockingIOError):
                host_end.recv(1)  # not a byte was written to the host: Stop is never a message
            process.kill.assert_not_called()
            with self.assertRaises(host_units.HostUnavailable) as caught:
                units.call('stand.in', 'operator', {'command': 'ok'})
            self.assertEqual(caught.exception.reason, 'stopped')
            units.close()

    def test_every_deadline_takes_a_positive_number_or_none(self):
        from recursion_core.daemon import host_units, launcher

        with tempfile.TemporaryDirectory() as scratch:
            for bad in (0, -1, True, '5', float('nan')):
                for name in ('call_deadline_s', 'start_deadline_s'):
                    with self.assertRaises(ValueError, msg=(name, bad)):
                        host_units.PluginHostUnits(launcher.EngineUnitManager(), Path(scratch), StandInCore(),
                                                   **{name: bad})
            for good in (None, 0.5, 30):
                host_units.PluginHostUnits(launcher.EngineUnitManager(), Path(scratch), StandInCore(),
                                           call_deadline_s=good, start_deadline_s=good)


@unittest.skipUnless(manager_ready(), 'needs a running systemd --user manager with systemd-run 254 or later')
class HostUnitsInTheUserManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.root = Path(scratch.name)
        # The host runs isolated (-I), which ignores PYTHONPATH: a venv whose .pth names this checkout stands in
        # for an installed package.
        venv.EnvBuilder(with_pip=False, symlinks=True).create(cls.root / 'python')
        cls.python = str(cls.root / 'python' / 'bin' / 'python')
        site = subprocess.check_output([cls.python, '-c', "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                       text=True).strip()
        Path(site, 'recursion_checkout.pth').write_text(str(CORE) + '\n', encoding='utf-8')

    def setUp(self):
        from recursion_core.daemon import host_units, launcher

        self.host_units = host_units
        self.state = Path(tempfile.mkdtemp(dir=self.root))
        self.started = []
        manager = launcher.EngineUnitManager()
        real = manager.start_piped

        def recorded(unit, *args, **kwargs):
            self.started.append(unit)
            return real(unit, *args, **kwargs)

        manager.start_piped = recorded
        self.manager = manager
        self.addCleanup(self.end_own_units)

    def units(self, **options):
        units = self.host_units.PluginHostUnits(self.manager, self.state, StandInCore(), python=self.python,
                                                gil_check=GIL_CHECK, properties=GUARD, **options)
        self.addCleanup(units.close)
        return units

    def end_own_units(self):
        """Stop and forget each unit this test started that is still loaded; no other unit is touched."""
        for unit in self.started:
            loaded = subprocess.run(['systemctl', '--user', 'show', '--property=LoadState', '--value', '--', unit],
                                    capture_output=True, text=True, timeout=15).stdout.strip()
            if loaded == 'loaded':
                subprocess.run(['systemctl', '--user', 'stop', '--', unit], capture_output=True, timeout=15)
                subprocess.run(['systemctl', '--user', 'reset-failed', '--', unit], capture_output=True, timeout=15)

    def call_in_thread(self, units, plugin_id, command):
        box = {}

        def run():
            try:
                box['answer'] = units.call(plugin_id, 'operator', {'command': command})
            except BaseException as exc:  # noqa: BLE001 - the test reads what the call ended with
                box['error'] = exc
            box['at'] = time.monotonic()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, box

    def test_the_host_sees_its_standard_input_and_output_as_one_socket_and_the_daemon_keeps_no_copy(self):
        units = self.units()
        units.start('hostile', *package(HOSTILE))
        answer = units.call('hostile', 'operator', {'command': 'fds'})
        self.assertEqual(answer['type'], 'result', answer)
        sockets = answer['value']
        # The host reads and writes its channel on two descriptors (its copies of fd 0 and fd 1); both are one socket.
        self.assertEqual(len(sockets), 2, sockets)
        self.assertEqual(sockets[0][:2], sockets[1][:2], sockets)
        self.assertNotIn(tuple(sockets[0][:2]), socket_inodes())

    def test_a_host_that_ends_makes_the_next_send_fail_at_once(self):
        # Status 3 fails the unit; status 0 is a clean end, after which a unit that remained active would keep
        # systemd-run, and its copy of the channel, alive.
        units = self.units(call_deadline_s=60)
        for plugin_id, command in (('hostile', 'exit'), ('clean', 'exit0')):
            with self.subTest(command=command):
                host = units.start(plugin_id, *package(HOSTILE))
                host.send({'type': 'call', 'id': 1, 'name': 'operator', 'args': {'command': command}})
                began = time.monotonic()
                self.assertIsNone(host.receive(time.monotonic() + 10))  # the end of the channel, not a deadline
                self.assertLess(time.monotonic() - began, 5)
                began = time.monotonic()
                with self.assertRaises(BrokenPipeError):
                    host.send({'type': 'call', 'id': 2, 'name': 'operator', 'args': {'command': 'ok'}})
                self.assertLess(time.monotonic() - began, 0.5)
                self.assertTrue(gone(host.group), host.group)

    def test_a_host_that_ends_in_a_call_is_marked_degraded_restarted_and_cleared_when_it_answers(self):
        units = self.units(call_deadline_s=60)
        first = units.start('hostile', *package(HOSTILE))
        began = time.monotonic()
        with self.assertRaises(self.host_units.HostUnavailable) as caught:
            units.call('hostile', 'operator', {'command': 'exit'})
        self.assertEqual(caught.exception.reason, 'ended')
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(units.degraded, {'hostile': 1})
        second = units.unit_of('hostile')
        self.assertIsNotNone(second)
        self.assertNotEqual(second.unit, first.unit)
        self.assertEqual(units.call('hostile', 'operator', {'command': 'ok'})['value'], 'ok')
        self.assertEqual(units.degraded, {})

    def test_stop_ends_a_host_that_loops_and_swallows_system_exit_and_the_other_host_keeps_answering(self):
        units = self.units(call_deadline_s=None)
        hostile = units.start('hostile', *package(HOSTILE))
        units.start('polite', *package(POLITE))
        thread, box = self.call_in_thread(units, 'hostile', 'loop')
        time.sleep(1.0)
        self.assertTrue(thread.is_alive())  # no deadline: only Stop can end this call
        began = time.monotonic()
        units.stop('hostile')
        self.assertLess(time.monotonic() - began, 1.0)  # Stop waits on nothing the host does
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(box.get('error'), self.host_units.HostUnavailable, box)
        self.assertEqual(box['error'].reason, 'stopped')
        self.assertTrue(gone(hostile.group), hostile.group)
        self.assertEqual(units.call('polite', 'operator', {'command': 'still here'})['value'],
                         {'echo': 'still here'})
        with self.assertRaises(self.host_units.HostUnavailable) as caught:
            units.call('hostile', 'operator', {'command': 'ok'})
        self.assertEqual(caught.exception.reason, 'stopped')  # a stopped host is not started again
        self.assertEqual(units.degraded, {})
        self.assertEqual(len(self.started), 2)

    def test_stop_by_the_systems_kill_releases_a_queued_message(self):
        units = self.units(call_deadline_s=None)
        host = units.start('hostile', *package(HOSTILE))
        host.send({'type': 'call', 'id': 1, 'name': 'operator', 'args': {'command': 'loop'}})
        # The host is in its loop and reads nothing more: these frames fill the socket and the sender waits.
        big = {'type': 'call', 'id': 2, 'name': 'operator', 'args': {'command': 'x' * (900 * 1024)}}
        box = {}

        def flood():
            try:
                for _ in range(16):
                    host.send(big)
                box['sent'] = True
            except OSError as exc:
                box['error'] = exc
            box['at'] = time.monotonic()

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        time.sleep(1.0)
        self.assertTrue(thread.is_alive(), box)
        stopped = time.monotonic()
        units.stop('hostile')
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(box.get('error'), BrokenPipeError, box)
        self.assertLess(box['at'] - stopped, 2.0)
        self.assertTrue(gone(host.group), host.group)

    def test_a_host_that_hangs_past_its_deadline_is_ended_restarted_and_degraded_until_it_answers(self):
        units = self.units(call_deadline_s=2.0)
        first = units.start('hostile', *package(HOSTILE))
        began = time.monotonic()
        with self.assertRaises(self.host_units.HostUnavailable) as caught:
            units.call('hostile', 'operator', {'command': 'loop'})
        took = time.monotonic() - began
        self.assertEqual(caught.exception.reason, 'timed_out')
        self.assertGreaterEqual(took, 2.0)
        self.assertLess(took, 12.0)
        self.assertTrue(gone(first.group), first.group)
        self.assertEqual(units.degraded, {'hostile': 1})
        second = units.unit_of('hostile')
        self.assertIsNotNone(second)
        self.assertNotEqual(second.unit, first.unit)
        self.assertFalse(gone(second.group, timeout_s=0.1))
        self.assertEqual(units.call('hostile', 'operator', {'command': 'ok'})['value'], 'ok')
        self.assertEqual(units.degraded, {})

    def test_the_daemons_close_ends_every_host_and_waits_on_none(self):
        units = self.units(call_deadline_s=None)
        hostile = units.start('hostile', *package(HOSTILE))
        polite = units.start('polite', *package(POLITE))
        thread, box = self.call_in_thread(units, 'hostile', 'loop')
        time.sleep(1.0)
        began = time.monotonic()
        units.close()
        self.assertLess(time.monotonic() - began, 1.0)
        thread.join(10)
        self.assertEqual(getattr(box.get('error'), 'reason', None), 'stopped', box)
        self.assertTrue(gone(hostile.group), hostile.group)
        self.assertTrue(gone(polite.group), polite.group)
        with self.assertRaises(self.host_units.HostUnavailable):
            units.start('polite', *package(POLITE))


if __name__ == '__main__':
    unittest.main()
