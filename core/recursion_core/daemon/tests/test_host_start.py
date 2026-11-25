"""The plugin host's start as the daemon makes it, and the daemon's side of a host that gives no answer.

The core's folder is handed to a host as an argument and never taken from its environment; a restart that could not
find the host's own code, or keep its channel, says so on the channel; inherited descriptors are closed whatever the
limit; a call that gets no answer returns within its own deadline; each start loads its own caller's package; a channel
at any descriptor number is waited on; a property a host unit cannot carry is refused by name.

No systemd unit and no user manager is used. Every host these tests start is a child process or a thread of the test
itself, started by a stand-in for the user manager, in a temporary folder the test made; each test has a hard count on
the hosts it may start and a time each child may run, and ends every child it started.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

from recursion_core import plugin_host
from recursion_core.control_transport.framing import HEADER
from recursion_core.daemon import host_units, launcher
from recursion_core.daemon.tests import test_adv2_realgil as realgil
from recursion_core.plugin_security import host_channel as hc
from recursion_core.plugin_security.limits import ARTIFACT
from recursion_core.plugin_security.tests.support import manifest_bytes

LINUX = sys.platform.startswith('linux')
CORE = Path(__file__).resolve().parents[3]
# A free-threaded build runs the host with its lock check on; a build with the lock can only run it with the check off.
GIL_CHECK = 'on' if not getattr(sys, '_is_gil_enabled', lambda: True)() else 'off'
HOSTS_PER_TEST = 4      # the hard count on the hosts one test may start
CHILD_LIMIT_S = 60      # the longest a child of these tests may run before it is ended
JOIN_S = 5              # how long a test waits on a thread of its own that must have ended long before
HIGH_FD_HELD = 1100     # descriptors held so that the channel gets a number of 1024 or more
RESTARTED = '--restarted-once'
HELLO = {'type': 'hello', 'channel': hc.CHANNEL}
BYE = {'type': 'bye', 'reason': 'done'}
POLITE = b'def operator(command):\n    return {"echo": command}\n'
WHERE = b'''
import sys

def operator(command):
    return {"core": sys.modules["recursion_core"].__file__, "argv_tail": sys.orig_argv[-2:], "path0": sys.path[0]}
'''
LAST_WORD = b'import sys\n\ndef operator(command):\n    return sys.orig_argv[-1]\n'


def b64(data):
    return base64.b64encode(data).decode('ascii')


def package(source, plugin_id='stand.in'):
    return manifest_bytes('01' * 32, plugin_id=plugin_id, source=source), {ARTIFACT: source}


def load_message(source, plugin_id='stand.in'):
    manifest, files = package(source, plugin_id)
    return {'type': 'load', 'plugin_id': plugin_id, 'manifest_b64': b64(manifest),
            'files': {path: b64(data) for path, data in files.items()}}


def read_frames(data):
    found = []
    while data:
        (length,) = HEADER.unpack_from(data)
        end = HEADER.size + length
        found.append(hc.decode(data[HEADER.size:end], sender='host'))
        data = data[end:]
    return found


def copy_core(dest):
    """A stand-in core folder: a copy of this checkout's recursion_core (no tests) inside ``dest``."""
    shutil.copytree(CORE / 'recursion_core', dest / 'recursion_core',
                    ignore=shutil.ignore_patterns('__pycache__', 'tests', '*.pyc'))
    return dest


class StandInCore:
    """The core service as a host channel needs it: one client per plugin, answering every request with {}."""

    def client_for(self, plugin_id):
        return lambda request: b'{}'


class StandInProcess:
    def poll(self):
        return 0

    def kill(self):
        pass


class ProcessManager:
    """A stand-in for the user manager whose hosts are real child processes of this test.

    ``start_piped`` runs the argv the daemon built, with the channel as its standard input and output, in the
    working folder the daemon made and with ``env`` (what a user manager hands, not this test's environment). The
    cgroup kill ends that child. At most ``limit`` children; each is ended after CHILD_LIMIT_S and at the test's end.
    """

    def __init__(self, test, env, limit=HOSTS_PER_TEST):
        self.env, self.limit, self.children, self.argv = env, limit, [], []
        self.logs = Path(tempfile.mkdtemp(dir=test.tmp))
        test.addCleanup(self.end_all)

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.children) >= self.limit:
            raise AssertionError('this test started more hosts than its count allows')
        self.argv.append(list(argv))
        with open(self.logs / unit, 'wb') as errors:
            child = subprocess.Popen(argv, stdin=channel, stdout=channel, stderr=errors, cwd=working_directory,
                                     env=self.env, close_fds=True)
        guard = threading.Timer(CHILD_LIMIT_S, child.kill)
        guard.daemon = True
        guard.start()
        self.children.append((unit, child, guard))
        return child

    def unit_identity(self, unit, timeout_s=None):
        return '/stand-in.slice/' + unit, 0

    def kill_cgroup(self, group, unit):
        for name, child, _ in self.children:
            if name == unit and child.poll() is None:
                child.kill()
        return True

    def unit_state(self, unit, timeout_s):
        """What the system shows of the stand-in's unit: gone once its child has ended."""
        for name, child, _ in self.children:
            if name == unit and child.poll() is None:
                return {'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running', 'Job': ''}
        return {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'Job': ''}

    def errors(self):
        return {path.name: path.read_bytes()[-2000:] for path in self.logs.iterdir()}

    def end_all(self):
        for _, child, guard in self.children:
            guard.cancel()
            if child.poll() is None:
                child.kill()
            child.wait(CHILD_LIMIT_S)


class ThreadManager:
    """A stand-in for the user manager whose 'host' is a thread of this test on a copy of the host's end.

    Mode 'hello' says hello; mode 'silent' says nothing. Either records every byte it reads and answers no call.
    The cgroup kill hangs up that host's end."""

    def __init__(self, test, modes, limit=HOSTS_PER_TEST):
        self.modes, self.limit, self.started, self.read, self.ends = modes, limit, [], {}, []
        self.argv = []
        if test is not None:
            test.addCleanup(self.hang_up_all)

    def start_piped(self, unit, argv, channel, *, properties=(), working_directory=None):
        if len(self.started) >= self.limit:
            raise AssertionError('this test started more hosts than its count allows')
        mode = self.modes[min(len(self.started), len(self.modes) - 1)]
        self.started.append(unit)
        self.argv.append(list(argv))
        self.read[unit] = b''
        end = channel.dup()
        self.ends.append((unit, end))

        def host():
            try:
                if mode == 'hello':
                    end.sendall(hc.encode(HELLO))
                while chunk := end.recv(1 << 16):
                    self.read[unit] += chunk
            except OSError:
                pass

        threading.Thread(target=host, daemon=True).start()
        return StandInProcess()

    def unit_identity(self, unit, timeout_s=None):
        return '/stand-in.slice/' + unit, 0

    def kill_cgroup(self, group, unit):
        self.hang_up(unit)
        return True

    def unit_state(self, unit, timeout_s):
        """What the system shows of the stand-in's unit after its kill: gone (the stand-in has no process)."""
        return {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'Job': ''}

    def hang_up(self, unit):
        for name, end in self.ends:
            if name == unit:
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def hang_up_all(self):
        for name, end in self.ends:
            self.hang_up(name)
            end.close()


def in_thread(work):
    box = {'began': time.monotonic()}

    def run():
        try:
            box['value'] = work()
        except BaseException as exc:  # noqa: BLE001 - the test reads what the work ended with
            box['error'] = exc
        box['took'] = time.monotonic() - box['began']

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def high_descriptor_case():
    """Run in a child of the test: a channel at a descriptor of 1024 or more, a start and a call that times out."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = HIGH_FD_HELD + 64
    if hard != resource.RLIM_INFINITY and hard < need:
        print(json.dumps({'skip': f'the hard limit on descriptors is {hard}'}))
        return
    resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
    held = [os.open(os.devnull, os.O_RDONLY) for _ in range(HIGH_FD_HELD)]
    out = {}
    with tempfile.TemporaryDirectory() as scratch:
        manager = ThreadManager(None, ['hello'])
        units = host_units.PluginHostUnits(manager, Path(scratch), StandInCore(), python='/stand-in/python',
                                           call_deadline_s=0.3, start_deadline_s=5)
        try:
            host = units.start('stand.in', *package(POLITE))
            out['fd'], out['start'] = host._sock.fileno(), 'ok'
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out['start'] = type(exc).__name__
        try:
            units.call('stand.in', 'operator', {'command': 'x'})
            out['call'] = 'answered'
        except host_units.HostUnavailable as exc:
            out['call'] = 'HostUnavailable ' + exc.reason
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            out['call'] = type(exc).__name__
        out['in_units'] = units.unit_of('stand.in') is not None
        out['degraded'] = units.degraded.get('stand.in', 0)
        units.close()
        manager.hang_up_all()
    for fd in held:
        os.close(fd)
    print(json.dumps(out))


class _Folder(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.tmp = Path(scratch.name)
        self.state = self.tmp / 'state'
        self.state.mkdir(mode=0o700)
        os.chmod(self.tmp, 0o700)

    def child(self, argv, *, env, stdin=b''):
        """Run one host as a child of this test, in an empty folder of its own; its status and its frames."""
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        done = subprocess.run(argv, input=stdin, capture_output=True, cwd=work, env=env, timeout=CHILD_LIMIT_S)
        return done.returncode, read_frames(done.stdout), done.stderr


@unittest.skipUnless(LINUX, 'plugin host units are Linux only')
class TheHostStartedAsTheDaemonStartsIt(_Folder):
    """The argv the daemon builds, sys.executable, an empty working folder and a user manager's environment."""

    def setUp(self):
        super().setUp()
        self.core_folder = copy_core(self.tmp / 'lib')
        home = self.tmp / 'home'
        home.mkdir()
        # What a user manager hands a unit: nothing of Recursion, so the host's first image restarts once.
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8'}

    def units(self, manager, **options):
        options.setdefault('gil_check', GIL_CHECK)
        units = host_units.PluginHostUnits(manager, self.state, StandInCore(), call_deadline_s=30,
                                           start_deadline_s=30, **options)
        self.addCleanup(units.close)
        return units

    def test_a_host_greets_through_its_one_restart_and_runs_the_core_of_the_folder_it_was_handed(self):
        manager = ProcessManager(self, self.env)
        units = self.units(manager, core_folder=self.core_folder)
        units.start('stand.in', *package(WHERE))
        answer = units.call('stand.in', 'operator', {'command': 'where'})
        self.assertEqual(answer['type'], 'result', (answer, manager.errors()))
        value = answer['value']
        self.assertTrue(value['core'].startswith(str(self.core_folder) + os.sep), value)
        self.assertEqual(value['argv_tail'], [str(self.core_folder), RESTARTED])  # through its one restart
        self.assertEqual(value['path0'], str(self.core_folder))
        self.assertEqual(manager.argv, [[sys.executable, *host_units.HOST_ARGV, str(self.core_folder)]])
        self.assertNotIn('PYTHONPATH', manager.env)
        self.assertEqual(len(manager.children), 1)

    def test_the_folder_handed_by_default_is_the_one_the_daemons_own_code_came_from(self):
        self.assertEqual(host_units.CORE_FOLDER, str(Path(host_units.__file__).resolve().parents[2]))
        self.assertTrue(Path(host_units.CORE_FOLDER, 'recursion_core', 'daemon', 'host_units.py').is_file())
        manager = ThreadManager(self, ['hello'])
        units = self.units(manager, python='/stand-in/python')
        units.start('stand.in', *package(POLITE))
        self.assertEqual(manager.argv, [['/stand-in/python', *host_units.HOST_ARGV, host_units.CORE_FOLDER]])
        self.assertNotIn('$', plugin_host.START_CODE)
        self.assertNotIn('%', plugin_host.START_CODE)

    def test_a_folder_that_is_not_absolute_or_not_a_folder_is_refused_when_the_units_are_made(self):
        a_file = self.tmp / 'a-file'
        a_file.write_bytes(b'')
        for bad in ('relative/core', str(self.tmp / 'missing'), str(a_file), a_file, b'/bytes', 3, ''):
            with self.subTest(core_folder=bad):
                with self.assertRaises(ValueError) as caught:
                    host_units.PluginHostUnits(launcher.EngineUnitManager(), self.state, StandInCore(),
                                               core_folder=bad)
                self.assertIn('write the absolute path of the folder that holds recursion_core', str(caught.exception))

    def test_a_host_handed_a_folder_without_recursions_code_refuses_and_the_daemon_says_it_did_not_start(self):
        empty = self.tmp / 'empty'
        empty.mkdir()
        manager = ProcessManager(self, self.env)
        units = self.units(manager, core_folder=empty)
        with self.assertLogs(host_units.log, logging.ERROR) as logged:
            with self.assertRaises(host_units.HostUnavailable) as caught:
                units.start('stand.in', *package(POLITE))
        self.assertEqual(caught.exception.reason, 'not_started')
        detail = caught.exception.detail
        self.assertTrue(detail.startswith('core_folder_not_usable: the plugin host could not start'), detail)
        self.assertIn('no plugin code ran', detail)
        self.assertIn('Reinstall Recursion by its recipe', detail)
        self.assertIn(repr(str(empty)), detail)
        self.assertIn('refused to start, and no plugin code ran', '\n'.join(logged.output))
        self.assertEqual(len(manager.children), 1)  # no restart and no second try
        self.assertIsNone(units.unit_of('stand.in'))


@unittest.skipUnless(LINUX, 'the restart is POSIX only; these hosts run on Linux')
class TheHostsOwnRestart(_Folder):
    def setUp(self):
        super().setUp()
        self.core_folder = copy_core(self.tmp / 'lib')

    def test_a_host_whose_core_is_found_only_through_the_environment_refuses_before_its_restart(self):
        status, frames, errors = self.child([sys.executable, '-B', '-m', 'recursion_core.plugin_host'],
                                            env={'PYTHONPATH': str(self.core_folder)},
                                            stdin=hc.encode(HELLO) + hc.encode(BYE))
        self.assertEqual(status, plugin_host.EXIT_RESTART_FAILED, errors)
        self.assertEqual(len(frames), 2, (frames, errors))
        self.assertEqual(frames[0], HELLO)
        refusal = frames[1]
        self.assertEqual((refusal['type'], refusal['id'], refusal['code']), ('refusal', None, 'host_start_refused'))
        self.assertTrue(refusal['detail'].startswith('restart_failed: '), refusal)
        for words in ('could not start again', 'PYTHONPATH', 'no plugin code ran', 'reinstall Recursion by its recipe'):
            self.assertIn(words, refusal['detail'])
        self.assertEqual(errors, b'')

    def test_a_folder_that_is_not_absolute_is_refused_on_the_channel_by_the_hosts_own_start(self):
        for folder in ('relative', str(self.tmp / 'missing')):
            with self.subTest(folder=folder):
                status, frames, errors = self.child([sys.executable, '-I', '-c', plugin_host.START_CODE, folder],
                                                    env={}, stdin=hc.encode(HELLO) + hc.encode(BYE))
                self.assertEqual(status, plugin_host.EXIT_FOLDER_REFUSED, errors)
                self.assertEqual(frames[0], HELLO)
                self.assertEqual(frames[1]['code'], 'host_start_refused')
                self.assertTrue(frames[1]['detail'].startswith('core_folder_not_usable: '), frames)
                self.assertTrue(frames[1]['detail'].endswith(repr(folder)), frames)
                self.assertEqual(len(frames), 2)

    def core_tree(self, name, source):
        """A folder whose recursion_core holds only a plugin_host of ``source`` (None: no plugin_host at all)."""
        folder = self.tmp / name
        (folder / 'recursion_core').mkdir(parents=True)
        (folder / 'recursion_core' / '__init__.py').write_text('')
        if source is not None:
            (folder / 'recursion_core' / 'plugin_host').mkdir()
            (folder / 'recursion_core' / 'plugin_host' / '__init__.py').write_text(source)
        return folder

    def start_in(self, folder):
        return self.child([sys.executable, '-I', '-B', '-c', plugin_host.START_CODE, str(folder)], env={},
                          stdin=hc.encode(HELLO) + hc.encode(BYE))

    def test_recursions_code_that_fails_to_import_is_refused_on_the_channel_with_its_cause(self):
        cases = (('import_error', 'from os import no_such_name_here\n',
                  "ImportError: cannot import name 'no_such_name_here'"),
                 ('syntax_error', 'def broken(:\n', 'SyntaxError: '),
                 ('missing_part', 'import recursion_core.no_such_part\n',
                  "ModuleNotFoundError: No module named 'recursion_core.no_such_part'"),
                 ('runtime_error', 'raise RuntimeError("the core broke here")\n', 'RuntimeError: the core broke here'))
        for name, source, cause in cases:
            with self.subTest(case=name):
                folder = self.core_tree(name, source)
                status, frames, errors = self.start_in(folder)
                self.assertEqual(status, plugin_host.EXIT_FOLDER_REFUSED, errors)
                self.assertEqual(len(frames), 2, (frames, errors))
                self.assertEqual(frames[0], HELLO)
                self.assertEqual((frames[1]['code'], frames[1]['id']), ('host_start_refused', None))
                detail = frames[1]['detail']
                self.assertTrue(detail.startswith('core_code_failed: the plugin host could not start'), detail)
                self.assertIn(cause, detail)
                self.assertIn('no plugin code ran', detail)
                self.assertIn('Reinstall Recursion by its recipe', detail)
                self.assertTrue(detail.endswith(repr(str(folder))), detail)

    def test_a_folder_whose_recursion_core_has_no_plugin_host_is_still_not_a_folder_that_holds_the_code(self):
        folder = self.core_tree('no_host', None)
        status, frames, errors = self.start_in(folder)
        self.assertEqual(status, plugin_host.EXIT_FOLDER_REFUSED, errors)
        self.assertTrue(frames[1]['detail'].startswith('core_folder_not_usable: '), frames)
        self.assertTrue(frames[1]['detail'].endswith(repr(str(folder))), frames)

    def test_a_channel_left_close_on_exec_survives_the_restart(self):
        # The starter leaves descriptors 0 and 1 close-on-exec; the environment is not clean, so the host restarts.
        wrap = ('import os, sys\nsys.path.insert(0, sys.argv[1])\nos.set_inheritable(0, False)\n'
                'os.set_inheritable(1, False)\nfrom recursion_core.plugin_host import main\nmain()\n')
        stdin = b''.join(hc.encode(message) for message in (
            dict(HELLO, gil_check='off'), load_message(LAST_WORD),
            {'type': 'call', 'id': 1, 'name': 'operator', 'args': {'command': 'x'}}, BYE))
        status, frames, errors = self.child([sys.executable, '-B', '-c', wrap, str(self.core_folder)],
                                            env={'CSL_HOST_TEST_NOT_CLEAN': '1'}, stdin=stdin)
        self.assertEqual(status, 0, errors)
        self.assertEqual(frames[0], HELLO, frames)
        self.assertEqual(frames[1:], [{'type': 'result', 'id': 1, 'value': RESTARTED}], frames)

    def test_off_linux_a_descriptor_above_a_lowered_limit_is_closed(self):
        code = ('import json, os, resource, sys\n'
                f'sys.path.insert(0, {str(CORE)!r})\n'
                'from recursion_core import plugin_host\n'
                'held = os.open(os.devnull, os.O_RDONLY)\n'
                'os.dup2(held, 100)\n'
                'os.close(held)\n'
                'resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n'
                "sys.platform = 'darwin'\n"
                'plugin_host._close_inherited()\n'
                'try:\n    os.fstat(100)\n    alive = True\n'
                'except OSError:\n    alive = False\n'
                "print(json.dumps({'alive': alive}))\n")
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        done = subprocess.run([sys.executable, '-B', '-I', '-c', code], capture_output=True, text=True, cwd=work,
                              timeout=CHILD_LIMIT_S)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {'alive': False})


@unittest.skipUnless(LINUX, 'plugin host units are Linux only')
class TheDaemonsSideOfAHostWithNoAnswer(_Folder):
    def units(self, manager, **options):
        units = host_units.PluginHostUnits(manager, self.state, StandInCore(), python='/stand-in/python', **options)
        self.addCleanup(units.close)
        return units

    def test_a_timed_out_call_returns_within_its_own_deadline_when_the_start_has_none(self):
        manager = ThreadManager(self, ['hello', 'silent'])
        units = self.units(manager, call_deadline_s=0.2, start_deadline_s=None)
        units.start('stand.in', *package(POLITE))
        with self.assertLogs(host_units.log, logging.ERROR) as logged:
            thread, box = in_thread(lambda: units.call('stand.in', 'operator', {'command': 'x'}))
            thread.join(JOIN_S)
        self.assertFalse(thread.is_alive(), 'the call that timed out did not return')
        self.assertIsInstance(box.get('error'), host_units.HostUnavailable, box)
        self.assertEqual(box['error'].reason, 'timed_out')
        self.assertLess(box['took'], 2.0)
        line = '\n'.join(logged.output)
        for words in ('stand.in', 'did not answer the call by its deadline and was ended',
                      'The call was not answered, so it may be sent again',
                      'a new host is started for the plugin with its next call'):
            self.assertIn(words, line)
        self.assertEqual(units.degraded, {'stand.in': 1})
        # The replacement never says hello: the next calls still return within their own deadline, so no start that
        # has no deadline holds the plugin's turn.
        for _ in range(2):
            thread, box = in_thread(lambda: units.call('stand.in', 'operator', {'command': 'x'}))
            thread.join(JOIN_S)
            self.assertFalse(thread.is_alive(), 'a later call waited behind a start that has no deadline')
            self.assertIsInstance(box.get('error'), host_units.HostUnavailable, box)
            self.assertLess(box['took'], 2.0)
        self.assertEqual(len(manager.started), 3)

    def test_overlapping_starts_each_load_their_own_callers_package_and_the_last_is_loaded_again(self):
        manager = ThreadManager(self, ['hello'])
        units = self.units(manager, call_deadline_s=0.3, start_deadline_s=5)
        sources = {'A': b'def operator(command):\n    return "AAAA"\n', 'B': b'def operator(command):\n    return "BBBB"\n'}
        turn = units._turns.setdefault('stand.in', threading.Lock())
        turn.acquire()
        threads = {}
        try:
            for tag in ('A', 'B'):
                threads[tag] = in_thread(lambda tag=tag: units.start('stand.in', *package(sources[tag])))
                time.sleep(0.2)  # each start reaches the plugin's turn before the next begins
        finally:
            turn.release()
        for thread, _ in threads.values():
            thread.join(JOIN_S)
        hosts = {}
        for tag, (thread, box) in threads.items():
            self.assertFalse(thread.is_alive())
            self.assertIn('value', box, box)
            hosts[tag] = box['value']
        time.sleep(0.2)  # the stand-in hosts record what reached them
        for tag, other in (('A', 'B'), ('B', 'A')):
            read = manager.read[hosts[tag].unit]
            self.assertIn(b64(sources[tag]).encode(), read, tag)
            self.assertNotIn(b64(sources[other]).encode(), read, tag)
        last = max(hosts, key=lambda tag: manager.started.index(hosts[tag].unit))
        manager.hang_up(hosts[last].unit)
        with self.assertRaises(host_units.HostUnavailable):
            units.call('stand.in', 'operator', {'command': 'x'})
        with self.assertRaises(host_units.HostUnavailable):  # a new host, loaded with the last start's package
            units.call('stand.in', 'operator', {'command': 'x'})
        time.sleep(0.2)
        self.assertEqual(len(manager.started), 3)
        self.assertIn(b64(sources[last]).encode(), manager.read[manager.started[2]])

    def test_a_channel_at_a_descriptor_of_1024_or_more_is_waited_on_and_its_errors_are_host_unavailable(self):
        code = (f'import sys\nsys.path.insert(0, {str(CORE)!r})\n'
                'from recursion_core.daemon.tests import test_host_start\ntest_host_start.high_descriptor_case()\n')
        done = subprocess.run([sys.executable, '-B', '-I', '-c', code], capture_output=True, text=True,
                              cwd=self.tmp, timeout=CHILD_LIMIT_S)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        out = json.loads(done.stdout.strip().splitlines()[-1])
        if 'skip' in out:
            self.skipTest(out['skip'])
        self.assertEqual(out['start'], 'ok', out)
        self.assertGreaterEqual(out['fd'], 1024, out)
        self.assertEqual(out['call'], 'HostUnavailable timed_out', out)
        self.assertFalse(out['in_units'], out)
        self.assertEqual(out['degraded'], 1, out)

    def test_an_error_of_the_wait_itself_is_an_os_error_never_a_raw_value_error(self):
        one, other = socket.socketpair()
        other.close()
        one.close()  # a closed socket: the wait cannot take it
        with self.assertRaises(OSError) as caught:
            host_units._ready(one, False, time.monotonic() + 1)
        self.assertNotIsInstance(caught.exception, ValueError)
        self.assertIn('the wait on the host channel failed', str(caught.exception))

    def test_a_property_a_host_unit_cannot_carry_is_refused_by_name(self):
        manager = launcher.EngineUnitManager()
        manager._verbatim_argv = True
        one, other = socket.socketpair()
        self.addCleanup(one.close)
        self.addCleanup(other.close)
        unit = 'recursion-host-0123456789abcdef.service'
        for item in ('RemainAfterExit=yes', 'KillMode=process', 'Delegate=no', 'KillSignal=SIGKILL',
                     'TimeoutStopSec=0', 'SendSIGKILL=no', 'Type=simple', 'StandardOutput=journal'):
            name = item.partition('=')[0]
            with self.subTest(property=item):
                with unittest.mock.patch.object(launcher.subprocess, 'Popen') as popen:
                    with self.assertRaises(launcher.PipedUnitPropertyRefused) as caught:
                        manager.start_piped(unit, ['python'], one, properties=(item,))
                popen.assert_not_called()
                self.assertEqual(caught.exception.name, name)
                self.assertIn(f'a plugin host unit cannot carry the property {name}: ', str(caught.exception))
                self.assertIn(f'Take {name} out of the properties given to the plugin hosts', str(caught.exception))
                with self.assertRaises(launcher.PipedUnitPropertyRefused):
                    host_units.PluginHostUnits(manager, self.state, StandInCore(), properties=(item,))
        with self.assertRaises(launcher.PipedUnitPropertyRefused) as caught:
            manager.start_piped(unit, ['python'], one, properties=('RemainAfterExit=yes',))
        self.assertIn('stays active after the host has ended', str(caught.exception))
        with unittest.mock.patch.object(launcher.subprocess, 'Popen') as popen:
            manager.start_piped(unit, ['python'], one, properties=('TasksMax=8',))
        self.assertIn('--property=TasksMax=8', popen.call_args[0][0])


@unittest.skipUnless(LINUX and realgil.FREE_THREADED and realgil.CC and os.path.exists(
    os.path.join(realgil.INCLUDE, 'Python.h')) and realgil._crypto(),
    'needs a free-threaded Linux build with a C compiler, its headers and cryptography')
class ARealExtensionInAHostStartedAsTheDaemonStartsIt(_Folder):
    """The small extension of test_adv2_realgil, which does not mark itself safe without the lock, in a host."""

    def setUp(self):
        super().setUp()
        self.ext = self.tmp / 'ext'
        self.ext.mkdir()
        (self.ext / 'adv2gil.c').write_text(realgil.C_SOURCE)
        suffix = realgil.sysconfig.get_config_var('EXT_SUFFIX')
        done = subprocess.run([realgil.CC, '-shared', '-fPIC', '-I', realgil.INCLUDE, '-o',
                               str(self.ext / ('adv2gil' + suffix)), str(self.ext / 'adv2gil.c')],
                              capture_output=True, text=True, timeout=120, check=False)
        if done.returncode != 0:
            self.skipTest(f'could not build the test extension: {done.stderr[-300:]}')
        self.core_folder = copy_core(self.tmp / 'lib')

    def test_a_call_that_imports_it_is_refused_for_the_lock_and_no_further_work_runs(self):
        home = self.tmp / 'home'
        home.mkdir()
        manager = ProcessManager(self, {'PATH': '/usr/bin:/bin', 'HOME': str(home)})
        units = host_units.PluginHostUnits(manager, self.state, StandInCore(), gil_check='on', call_deadline_s=30,
                                           start_deadline_s=30, core_folder=self.core_folder)
        self.addCleanup(units.close)
        source = (f'import sys\nsys.path.insert(0, {str(self.ext)!r})\n\n'
                  'def operator(command):\n    import adv2gil\n    return "ran"\n').encode()
        units.start('stand.in', *package(source))
        for _ in range(2):
            answer = units.call('stand.in', 'operator', {'command': 'x'})
            self.assertEqual((answer['type'], answer['code']), ('refusal', 'host_gil_enabled'), answer)


if __name__ == '__main__':
    unittest.main()
