"""ADV1 red team of the composed daemon's surfaces: control socket, CLI and GUI door (CSL-738 candidate).

Each test is one attack, written against 0beb782f. The defects fixed since (ADV1-3, -4, -5, -12,
-13) now pass and say "(fixed)"; the others pass and record that the hardening held.
"""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control import cli
from recursion_core.control.api import LIST_LIMIT_MAX
from recursion_core.control.tests.fakes import make_api, started
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport import InsecureSocketPath
from recursion_core.control_transport.framing import encode_frame
from recursion_core.control_transport.unix import MAX_CONNECTIONS
from recursion_core.daemon import AlreadyRunning, Daemon, Registry, UserLock
from recursion_core.daemon import control_adapter as adapter
from recursion_core.daemon import launcher
from recursion_core.daemon.gui import LoopbackGUI
from recursion_core.daemon.tests.adv1_support import BOOT, ComposedBase, wait_for
from recursion_core.tui import protocol

LINUX = sys.platform.startswith('linux')
CORE = str(Path(launcher.__file__).parents[2])


def raw_call(path, payload: bytes, *, timeout=10.0):
    """Send raw bytes on a fresh connection and return the decoded reply frame, or None on close."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(payload)
        header = b''
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        (length,) = struct.unpack('>I', header)
        body = b''
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)


def frame(obj) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack('>I', len(body)) + body


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class SocketAttackTest(ComposedBase):
    def test_socket_and_directory_are_owner_only_and_a_planted_path_is_refused(self):
        _, composed = self.boot()
        path = Path(composed.server.path)
        self.assertEqual(stat.S_IMODE(os.lstat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(path.parent / 'gui-device-token').st_mode), 0o600)
        self.shutdown()
        # A symlink, a regular file or a directory planted at the socket path stops the start; nothing is unlinked.
        target = self.tmp / 'elsewhere'
        target.write_text('x')
        for plant in ('symlink', 'file', 'dir'):
            if plant == 'symlink':
                path.symlink_to(target)
            elif plant == 'file':
                path.write_text('planted')
            else:
                path.mkdir()
            with self.assertRaises(InsecureSocketPath):
                self.boot()
            self.assertTrue(os.path.lexists(path), plant)
            self.assertFalse((path.parent / 'gui-device-token').exists(), 'a refused start leaves no token file')
            if plant == 'dir':
                path.rmdir()
            else:
                path.unlink()
        self.assertEqual(target.read_text(), 'x')
        # A symlinked runtime directory is refused too, before any file is written through it.
        real = self.tmp / 'real-rt'
        real.mkdir(mode=0o700)
        (self.runtime / 'recursion').rename(self.tmp / 'old-recursion')
        (self.runtime / 'recursion').symlink_to(real)
        with self.assertRaises(PermissionError):
            self.boot()
        self.assertEqual(os.listdir(real), [])

    def test_stale_socket_after_a_sigkilled_daemon_is_cleared_and_the_token_rotates(self):
        script = (
            'import os, sys, time\n'
            'from pathlib import Path\n'
            'from recursion_core.daemon import Daemon, Registry\n'
            'from recursion_core.daemon import control_adapter as a, launcher\n'
            'd = Daemon(Registry(Path(sys.argv[1])), launcher.EngineUnitManager(), boot_id=lambda: "boot-1")\n'
            'c = a.compose_control(d, Path(sys.argv[1]), environ={"XDG_RUNTIME_DIR": sys.argv[2], '
            '"XDG_CONFIG_HOME": sys.argv[3], "PATH": "/usr/bin:/bin"}, routes={})\n'
            'print("ready", flush=True)\n'
            'time.sleep(120)\n')
        child = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.state), str(self.runtime),
                                  str(self.tmp / 'config')], stdout=subprocess.PIPE, text=True,
                                 env={'PYTHONPATH': CORE, 'PATH': '/usr/bin:/bin'})
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait()))
        self.assertEqual(child.stdout.readline().strip(), 'ready')
        sock_path = self.runtime / 'recursion' / 'control.sock'
        old_token = (self.runtime / 'recursion' / 'gui-device-token').read_text().strip()
        self.assertEqual(SocketAPI(str(sock_path), timeout_s=10).call('run.list')['code'], 'ok')
        os.kill(child.pid, 9)
        child.wait()
        self.assertTrue(sock_path.exists(), 'SIGKILL leaves the socket inode behind')
        down = SocketAPI(str(sock_path), timeout_s=5).call('run.stop', {'run_id': 'run-x'})
        self.assertEqual((down['code'], down['delivered']), ('control_api_unavailable', False))
        _, composed = self.boot()
        self.assertEqual(self.api(composed).call('run.list')['code'], 'ok')
        new_token = (self.runtime / 'recursion' / 'gui-device-token').read_text().strip()
        self.assertNotEqual(old_token, new_token)
        self.assertFalse(composed.door({'HTTP_AUTHORIZATION': 'Bearer ' + old_token}))

    def test_two_daemons_racing_for_the_user_lock_one_wins(self):
        results = []
        barrier = threading.Barrier(8)

        def contender():
            barrier.wait()
            try:
                lock = UserLock(self.runtime)
                lock.__enter__()
                results.append(lock)
            except AlreadyRunning:
                results.append('refused')

        threads = [threading.Thread(target=contender) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if r != 'refused']
        self.assertEqual(len(winners), 1)
        winners[0].__exit__(None, None, None)

    def test_a_second_composition_without_the_user_lock_leaves_the_live_daemons_token_file_alone(self):
        # ADV1-12 (fixed). main() takes UserLock first, so this only matters to an embedder or test that
        # composes twice. The second compose fails at the socket lock; it used to have rewritten and then
        # unlinked the token file the live daemon's GUI depends on. The file is now written only after
        # server.start() succeeds, and a door removes only the file it wrote.
        _, first = self.boot()
        token_file = self.runtime / 'recursion' / 'gui-device-token'
        live = token_file.read_text().strip()
        with self.assertRaises(InsecureSocketPath):
            self.boot()
        self.assertTrue(first.door({'HTTP_AUTHORIZATION': 'Bearer ' + live}))  # in-memory token unchanged
        self.assertEqual(token_file.read_text().strip(), live, 'the losing composition left the live token file')
        self.assertEqual(os.listdir(token_file.parent).count('gui-device-token.tmp'), 0)
        # A door that never published, or whose file was replaced by another door, unlinks nothing.
        stranger = adapter.DeviceDoor(token_file.parent, publish=False)
        stranger.close()
        self.assertEqual(token_file.read_text().strip(), live)
        other = adapter.DeviceDoor(token_file.parent)  # replaces the file (a successor's door)
        first.door.close()
        self.assertTrue(token_file.exists(), 'the older door does not remove its successor\'s file')
        other.close()
        self.assertFalse(token_file.exists())

    def test_garbage_and_oversized_frames_are_typed_and_the_daemon_keeps_serving(self):
        _, composed = self.boot()
        path = composed.server.path
        cases = {
            'too_large': struct.pack('>I', 0xFFFFFFFF),
            'not_json': struct.pack('>I', 5) + b'\xff\xfe{{{',
            'not_object': frame([1, 2, 3]),
            'too_deep': struct.pack('>I', 200000) + b'[' * 100000 + b']' * 100000,
        }
        for name, payload in cases.items():
            reply = raw_call(path, payload)
            self.assertEqual(reply['ok'], False, name)
            self.assertEqual(reply['error']['code'], 'bad_frame', name)
        for message in ({'op': 'control', 'method': 'run.stop', 'params': ['run-x']},
                        {'op': 'control', 'method': 7, 'params': {}},
                        {'op': 'control'}, {'op': 'shell', 'cmd': 'id'}, {}):
            reply = raw_call(path, frame(message))
            self.assertFalse(reply['ok'], message)
            self.assertIn(reply['error']['code'], ('request_invalid', 'unknown_op'), message)
        for params in ({'run_id': ''}, {'run_id': None}, {'run_id': 7}, {'run_id': {'a': 1}},
                       {'run_id': '../../etc/passwd'}, {'run_id': 'x' * 100000}, {'run_id': 'a\x00b'}, {}):
            reply = raw_call(path, frame({'op': 'control', 'method': 'run.stop', 'params': params}))
            self.assertTrue(reply['ok'], params)
            self.assertIn(reply['result']['code'], ('request_invalid', 'run_unknown'), params)
            self.assertNotIn('stopped', json.dumps(reply))
        # A truncated frame then a hang-up: the worker frees its slot.
        for _ in range(MAX_CONNECTIONS + 4):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(path)
                sock.sendall(struct.pack('>I', 100) + b'{"op":')
        self.assertTrue(wait_for(lambda: self.api(composed).call('run.list')['code'] == 'ok', 10))
        self.assertEqual(self.api(composed).call('run.list')['code'], 'ok')

    def test_many_concurrent_clients_get_typed_answers_and_one_stop_lands(self):
        _, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        answers, lock = [], threading.Lock()

        def worker(i):
            for _ in range(5):
                method, params = ('run.stop', {'run_id': run_id}) if i % 6 == 0 else ('run.status', {'run_id': run_id})
                answer = SocketAPI(composed.server.path, timeout_s=30).call(method, params)
                with lock:
                    answers.append((method, answer))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        codes = {a['code'] for _, a in answers}
        self.assertLessEqual(codes, {'ok', 'stopped', 'pending', 'control_api_unavailable'}, codes)
        for method, answer in answers:
            if answer['code'] == 'control_api_unavailable':
                self.assertEqual((answer['delivered'], answer['detail']), (False, 'busy'), answer)
        self.assertTrue(any(m == 'run.stop' and a['code'] == 'stopped' for m, a in answers))
        self.assertIsNotNone(self.units.procs[f'recursion-run-{run_id}.service'].poll())

    def test_client_that_hangs_up_right_after_sending_stop_still_gets_the_run_stopped(self):
        _, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(composed.server.path)
            sock.sendall(frame({'op': 'control', 'method': 'run.stop', 'params': {'run_id': run_id}}))
        self.assertTrue(wait_for(lambda: api.call('run.status', {'run_id': run_id})['run']['state'] == 'stopped', 20))
        self.assertIsNotNone(self.units.procs[f'recursion-run-{run_id}.service'].poll())

    def test_an_engine_peer_is_closed_at_accept_and_never_holds_a_slot(self):
        """ADV1-4 (fixed): the scope check runs at accept, before a slot is taken."""
        _, composed = self.boot()
        hog = subprocess.Popen([sys.executable, '-c',
                                'import socket, sys, time\n'
                                'sys.stdin.readline()\n'
                                'socks = []\n'
                                'for _ in range(%d):\n'
                                '    s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); socks.append(s)\n'
                                'time.sleep(0.5)\n'
                                's = socks[0]; s.settimeout(5)\n'
                                'print(s.recv(4096).decode("utf-8", "replace"), flush=True)\n'
                                'time.sleep(60)\n' % (MAX_CONNECTIONS * 2), composed.server.path],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (hog.kill(), hog.wait()))
        real = adapter.peer_is_engine
        with mock.patch.object(adapter, 'peer_is_engine', side_effect=lambda pid: pid == hog.pid or real(pid)):
            hog.stdin.write('go\n')
            hog.stdin.flush()
            self.assertIn('"scope_refused"', hog.stdout.readline())  # one typed frame, then closed
            self.assertEqual(composed.server.refused, ['not_admitted'] * (MAX_CONNECTIONS * 2))
            self.assertEqual(composed.server.accepted, 0)
            for _ in range(MAX_CONNECTIONS + 2):  # every ordinary slot is still free for the operator
                self.assertEqual(self.api(composed).call('run.list')['code'], 'ok')

    def test_an_engine_peer_cannot_hold_every_connection_slot_and_starve_stop(self):
        """ADV1-4 (fixed): a peer inside a run unit was refused only after a frame, so it could hold all 16
        slots. Stop now has reserved connections, served when every ordinary one is taken."""
        _, composed = self.boot()
        api = self.api(composed)
        run_id = self.start(api)
        accepted = composed.server.accepted
        hog = subprocess.Popen([sys.executable, '-c',
                                'import socket, sys, time\n'
                                'socks = []\n'
                                'for _ in range(%d):\n'
                                '    s = socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); socks.append(s)\n'
                                'print("held", flush=True)\n'
                                'time.sleep(60)\n' % MAX_CONNECTIONS, composed.server.path],
                               stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (hog.kill(), hog.wait()))
        self.assertEqual(hog.stdout.readline().strip(), 'held')
        # Every ordinary slot is really held (the hog got in before anything called it an engine).
        self.assertTrue(wait_for(lambda: composed.server.accepted == accepted + MAX_CONNECTIONS, 10))
        engine_pids = {hog.pid}
        real = adapter.peer_is_engine
        # The hog stands for an engine: the daemon would refuse it scope_refused if it ever sent a frame.
        with mock.patch.object(adapter, 'peer_is_engine', side_effect=lambda pid: pid in engine_pids or real(pid)):
            time.sleep(0.3)
            receipt = SocketAPI(composed.server.path, timeout_s=10).call('run.stop', {'run_id': run_id})
            # Anything but Stop on a reserved connection is "busy, not delivered", as over the cap before.
            listed = SocketAPI(composed.server.path, timeout_s=10).call('run.list')
            self.assertEqual((listed['code'], listed.get('delivered'), listed.get('detail')),
                             ('control_api_unavailable', False, 'busy'), listed)
            again = SocketAPI(composed.server.path, timeout_s=10).call('run.stop', {'run_id': run_id})
        self.assertNotEqual(receipt.get('detail'), 'busy', receipt)
        self.assertEqual(receipt['code'], 'stopped', receipt)
        self.assertEqual((again['code'], again.get('repeat')), ('stopped', True), again)

    def seed_settled_runs(self, count):
        """``count`` settled runs with systemd's real cgroup paths, as ADV1-5 measured them (~975 bytes each)."""
        store = Registry(self.state)
        long_group = ('/user.slice/user-1000.slice/user@1000.service/app.slice/'
                      'recursion-run-run-%s.service')
        ids = []
        for i in range(count):
            run_id = 'run-%032x' % i
            store.add(run_id, BOOT)
            store.state(run_id, 'terminated', '')
            group = long_group % ('%032x' % i)
            marker = 'cg-%032x' % i
            store.control_put(run_id, json.dumps({
                'run_id': run_id, 'tenant': 't1', 'engine': 'claude_code', 'state': 'stopped', 'cgroup': group,
                'started_at': 1.0e9, 'stop': {'requested_at': 1.0e9, 'checks': 1, 'reason': None, 'requested': True},
                'start_key': {'client_key': 'key-%032x' % i, 'engine': 'claude_code', 'prompt_sha256': 'f' * 64},
                'generation': 4, 'members': [], 'cgroup_marker': marker,
                'stop_attestation': {'proof': 'empty', 'cgroup': group, 'cgroup_marker': marker,
                                     'member_ids': [], 'generation': 3}}, sort_keys=True))
            ids.append(run_id)
        store.close()
        return ids

    @staticmethod
    def unpaged_list(composed):
        """run.list as the daemon answered it before paging: every record, each with its ``observed`` block."""
        answer = composed.control.api.call('run.list', {})
        for run in answer['runs']:
            composed.control._observe(run, full=False)
        return answer

    def test_run_list_still_answers_after_a_thousand_runs(self):
        """ADV1-5 (fixed): past ~1075 runs the full list outgrew the 1 MiB frame and the socket answered
        ``daemon:response_too_large``. run.list now answers ``ok`` with the first page and ``next_after``;
        following it reads every run exactly once, in order, with each entry as before. Nothing is pruned."""
        count = 1300  # ~975 bytes per listed run with systemd's real cgroup paths: 1 MiB / 975 ~= 1075
        ids = self.seed_settled_runs(count)
        _, composed = self.boot()
        api = self.api(composed)
        expected = self.unpaged_list(composed)['runs']
        self.assertEqual([run['run_id'] for run in expected], ids)

        listed = api.call('run.list')
        self.assertEqual(listed['code'], 'ok', {k: listed.get(k) for k in ('code', 'detail', 'delivered')})
        self.assertLess(len(listed['runs']), count)
        self.assertEqual(listed['next_after'], listed['runs'][-1]['run_id'])
        walked, pages, answer = list(listed['runs']), 1, listed
        while 'next_after' in answer:
            answer = api.call('run.list', {'after': answer['next_after']})
            self.assertEqual(answer['code'], 'ok', answer.get('detail'))
            self.assertTrue(answer['runs'])
            walked.extend(answer['runs'])
            pages += 1
            self.assertLess(pages, 10)
        self.assertEqual(walked, expected)  # every run once, same order, every field as the unpaged entry
        self.assertEqual(len({run['run_id'] for run in walked}), count)

        limited = api.call('run.list', {'limit': 100})
        self.assertEqual((limited['code'], len(limited['runs']), limited['next_after']),
                         ('ok', 100, ids[99]))
        self.assertEqual(limited['runs'], expected[:100])
        last = api.call('run.list', {'after': ids[-2], 'limit': 5})
        self.assertEqual(last, {'code': 'ok', 'runs': expected[-1:]})  # the last page has no next_after

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(['list'], api, out, err), cli.EXIT_OK, out.getvalue()[:300])
        printed = json.loads(out.getvalue())
        self.assertEqual([run['run_id'] for run in printed['runs']], ids)
        self.assertNotIn('next_after', printed)
        out = io.StringIO()
        self.assertEqual(cli.main(['conditions'], api, out, io.StringIO()), cli.EXIT_OK, out.getvalue()[:300])
        self.assertEqual(json.loads(out.getvalue()), {'code': 'ok', 'conditions': []})

    def test_run_list_without_parameters_below_the_frame_is_byte_for_byte_unchanged(self):
        """ADV1-5: the paging is additive. A list that fits one frame is the same reply frame as before."""
        self.seed_settled_runs(3)
        _, composed = self.boot()
        request = frame({'op': 'control', 'method': 'run.list', 'params': {}})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(composed.server.path)
            sock.sendall(request)
            data = b''
            while len(data) < 4 or len(data) < 4 + struct.unpack('>I', data[:4])[0]:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        before = encode_frame({'ok': True, 'result': self.unpaged_list(composed)})
        self.assertEqual(data, before)
        self.assertEqual(sorted(json.loads(data[4:])['result']), ['code', 'runs'])

    def test_run_list_refuses_a_stale_or_unknown_cursor_and_a_limit_out_of_bounds(self):
        """ADV1-5: a cursor that names no run is ``request_invalid``, never an empty page; ``limit`` is 1..cap."""
        ids = self.seed_settled_runs(4)
        _, composed = self.boot()
        api = self.api(composed)
        for after, detail in (('run-not-listed', 'after_unknown'), ('run-%032x' % 99, 'after_unknown'),
                              ('', 'after'), (5, 'after'), (['run-1'], 'after')):
            with self.subTest(after=after):
                self.assertEqual(api.call('run.list', {'after': after}), {'code': 'request_invalid', 'detail': detail})
        for limit in (0, -1, LIST_LIMIT_MAX + 1, True, '5', 1.5):
            with self.subTest(limit=limit):
                self.assertEqual(api.call('run.list', {'limit': limit}), {'code': 'request_invalid', 'detail': 'limit'})
        self.assertEqual([r['run_id'] for r in api.call('run.list', {'limit': LIST_LIMIT_MAX})['runs']], ids)
        one = api.call('run.list', {'limit': 1})
        self.assertEqual(([r['run_id'] for r in one['runs']], one['next_after']), ([ids[0]], ids[0]))
        self.assertNotIn('next_after', api.call('run.list', {'limit': 4}))
        self.assertEqual(api.call('run.list', {'after': ids[-1]}), {'code': 'ok', 'runs': []})  # past the end
        self.assertEqual(len(api.call('run.list', {'after': ids[0]})['runs']), 3)


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class PeerScopeTest(unittest.TestCase):
    def test_peer_check_reads_the_cgroup_path_by_component_substring(self):
        def check(text):
            with mock.patch.object(adapter.Path, 'read_text', return_value=text):
                return adapter.peer_is_engine(12345)

        self.assertTrue(check('0::/app.slice/recursion-run-run-1.service\n'))
        self.assertTrue(check('0::/app.slice/recursion-run-run-1.service/child\n'))  # Delegate= child cgroup
        # Documents: any component that merely contains the text is refused (false positive, fail closed).
        self.assertTrue(check('0::/app.slice/my-recursion-run-notes.service\n'))
        # A process that left the run unit's cgroup is no longer recognised: see ADV1-2 (real-manager test).
        self.assertFalse(check('0::/app.slice/escaped.scope\n'))

    def test_cgroup_namespace_does_not_hide_the_path_from_the_daemon(self):
        # /proc/<pid>/cgroup is rendered relative to the READER's cgroup namespace, so an engine that
        # unshares its own cgroup namespace still shows its full path to the daemon.
        try:
            done = subprocess.run(['unshare', '-U', '-C', '--', sys.executable, '-c',
                                   'import os, time; print(os.getpid(), flush=True); time.sleep(2)'],
                                  capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            self.skipTest('unshare not available')
        if done.returncode != 0:
            self.skipTest('unprivileged user+cgroup namespaces not available: %s' % done.stderr.strip()[:200])
        child = subprocess.Popen(['unshare', '-U', '-C', '--', sys.executable, '-c',
                                  'import time; print(open("/proc/self/cgroup").read().strip(), flush=True); '
                                  'time.sleep(5)'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait()))
        inside = child.stdout.readline().strip()
        python_pid = None
        for _ in range(100):
            kids = Path(f'/proc/{child.pid}/task/{child.pid}/children').read_text().split()
            if kids or 'python' in Path(f'/proc/{child.pid}/comm').read_text():
                python_pid = int(kids[0]) if kids else child.pid
                break
            time.sleep(0.02)
        seen = Path(f'/proc/{python_pid}/cgroup').read_text().strip()
        self.assertEqual(inside, '0::/')
        self.assertEqual(seen, Path('/proc/self/cgroup').read_text().strip())


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class CliTruthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-cli-'))
        os.chmod(self.tmp, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, True))
        self.path = str(self.tmp / 'control.sock')

    def fake_daemon(self, behaviour):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(4)
        self.addCleanup(server.close)
        held = []

        def serve():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            held.append(conn)
            try:
                header = conn.recv(4)
                length = struct.unpack('>I', header)[0]
                body = b''
                while len(body) < length:
                    body += conn.recv(length - len(body))
                if behaviour == 'hang':
                    time.sleep(5)
                elif behaviour == 'close':
                    conn.close()
                elif behaviour == 'garbage':
                    conn.sendall(struct.pack('>I', 6) + b'nojson')
                elif behaviour == 'half':
                    conn.sendall(struct.pack('>I', 50) + b'{"ok": true, "result": {"code": "stop')
                    conn.close()
                elif behaviour == 'list':
                    conn.sendall(frame(['stopped']))
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(lambda: [c.close() for c in held])

    def run_cli(self, argv, timeout_s):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.run(argv, lambda: SocketAPI(self.path, timeout_s=timeout_s))
        return code, json.loads(out.getvalue())

    def test_no_daemon_is_not_delivered(self):
        code, answer = self.run_cli(['stop', 'run-1'], 2)
        self.assertEqual((code, answer['code'], answer['delivered']), (1, 'control_api_unavailable', False))

    def test_hung_garbage_or_truncated_reply_is_delivered_unknown_never_stopped(self):
        for behaviour in ('hang', 'close', 'garbage', 'half', 'list'):
            with self.subTest(behaviour=behaviour):
                if os.path.lexists(self.path):
                    os.unlink(self.path)
                self.fake_daemon(behaviour)
                started = time.monotonic()
                code, answer = self.run_cli(['stop', 'run-1'], 1.0)
                self.assertLess(time.monotonic() - started, 4.0)
                self.assertEqual(code, 1, answer)
                self.assertEqual(answer['code'], 'control_api_unavailable', answer)
                self.assertEqual(answer['delivered'], 'unknown', answer)

    def test_cli_exit_codes_follow_the_answer_not_the_transport(self):
        class API:
            def __init__(self, answer):
                self.answer = answer

            def call(self, method, params=None):
                return dict(self.answer)

        for answer, expected in (({'code': 'stopped'}, 0), ({'code': 'pending', 'reason': 'x'}, 3),
                                 ({'code': 'run_unknown'}, 4), ({'code': 'registry_unavailable'}, 1),
                                 ({'code': 'control_api_unavailable', 'delivered': 'unknown'}, 1),
                                 ({'code': 'STOPPED'}, 1), ({}, 1)):
            out = io.StringIO()
            self.assertEqual(cli.main(['stop', 'r'], API(answer), out=out), expected, answer)


class ListBudgetTest(unittest.TestCase):
    """ADV1-5: DaemonControl cuts run.list to the bytes one reply frame can carry, and says where it cut."""

    def control(self, count, budget):
        api = make_api()[0]
        for _ in range(count):
            started(api)
        daemon = mock.Mock()
        daemon.registry.get.side_effect = KeyError
        starter = mock.Mock(results={}, failures={})
        tmp = tempfile.mkdtemp(prefix='rc-adv15-')
        self.addCleanup(__import__('shutil').rmtree, tmp, True)
        return adapter.DaemonControl(api, api._registry, daemon, starter, tmp, list_reply_bytes=budget)

    def test_a_list_that_fits_is_unchanged_and_one_that_does_not_is_paged_to_fit(self):
        whole = self.control(12, adapter.LIST_REPLY_BYTES).call('run.list')
        self.assertEqual(sorted(whole), ['code', 'runs'])
        budget = adapter._wire_size({'code': 'ok', 'runs': whole['runs'][:3]})
        control = self.control(12, budget)
        first = control.call('run.list')
        self.assertEqual((len(first['runs']), first['next_after']), (3, whole['runs'][2]['run_id']))
        self.assertLessEqual(adapter._wire_size({k: v for k, v in first.items() if k != 'next_after'}), budget)
        self.assertEqual(protocol.list_all_runs(control.call), whole)
        # A limit the frame cannot carry is cut further; the cursor still names the last run sent.
        limited = control.call('run.list', {'limit': 10})
        self.assertEqual((len(limited['runs']), limited['next_after']), (3, whole['runs'][2]['run_id']))

    def test_the_request_id_echoed_in_the_reply_is_counted(self):
        whole = self.control(6, adapter.LIST_REPLY_BYTES).call('run.list')
        budget = adapter._wire_size({'code': 'ok', 'runs': whole['runs'][:3]})
        control = self.control(6, budget)
        conn = mock.Mock()
        conn.peer.pid = os.getpid()
        with mock.patch.object(adapter, 'peer_is_engine', return_value=False):
            plain = control.handle({'op': 'control', 'method': 'run.list', 'params': {}}, conn)['result']
            big_id = control.handle({'op': 'control', 'method': 'run.list', 'params': {}, 'id': 'x' * 400}, conn)
        self.assertEqual(len(plain['runs']), 3)
        self.assertLess(len(big_id['result']['runs']), 3)

    def test_a_run_too_large_for_a_frame_alone_is_still_sent_never_skipped(self):
        control = self.control(3, 10)
        first = control.call('run.list')
        self.assertEqual(len(first['runs']), 1)
        self.assertEqual(first['next_after'], first['runs'][0]['run_id'])
        self.assertEqual(len(protocol.list_all_runs(control.call)['runs']), 3)


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class GuiDoorAttackTest(ComposedBase):
    def request(self, port, method, path, headers, body=None, timeout=30):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                conn.putheader(name, value)
            if body is not None:
                conn.putheader('Content-Length', str(len(body)))
            conn.endheaders(body.encode() if body is not None else None)
            response = conn.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            conn.close()

    def test_door_refuses_dns_rebinding_cross_origin_query_tokens_and_old_tokens(self):
        _, composed = self.boot()
        token = (self.runtime / 'recursion' / 'gui-device-token').read_text().strip()
        run_id = self.start(self.api(composed))
        with LoopbackGUI(composed.client, composed.door, port=0) as gui:
            port = gui.address[1]
            host = '127.0.0.1:%d' % port
            origin = 'http://' + host
            good = {'Host': host, 'Authorization': 'Bearer ' + token}
            status, page, headers = self.request(port, 'GET', '/', good)
            self.assertEqual(status, 200)
            self.assertNotIn(token, page)
            self.assertNotIn('Set-Cookie', headers)  # no cookie is ever minted, so no cookie flags to get wrong
            self.assertEqual(headers.get('X-Frame-Options'), 'DENY')
            for bad_host in ('evil.example:%d' % port, 'localhost:%d' % port, '127.0.0.1', '[::1]:%d' % port,
                             host + '.evil.example', ''):
                self.assertEqual(self.request(port, 'GET', '/', dict(good, Host=bad_host))[0], 403, bad_host)
            self.assertEqual(self.request(port, 'GET', '/?token=' + token, {'Host': host})[0], 401)
            self.assertEqual(self.request(port, 'GET', '/', {'Host': host, 'Authorization': 'bearer ' + token})[0], 401)
            self.assertEqual(self.request(port, 'GET', '/', {'Host': host, 'Cookie': 'recursion_device="%s"' % token})[0], 401)
            form = {'Host': host, 'Content-Type': 'application/x-www-form-urlencoded',
                    'Cookie': 'recursion_device=' + token}
            body = 'run_id=%s&confirm=explicit' % run_id
            # A browser on another site carries the cookie but cannot forge Origin.
            for headers in (dict(form, Origin='http://evil.example'), dict(form), dict(form, Origin='null'),
                            dict(form, Origin=origin + ', http://evil.example'),
                            dict(form, Origin=origin, **{'Sec-Fetch-Site': 'cross-site'}),
                            dict(form, Origin=origin, **{'Sec-Fetch-Site': 'same-site'})):
                self.assertEqual(self.request(port, 'POST', '/stop', headers, body)[0], 403, headers)
            self.assertIsNone(self.units.procs[f'recursion-run-{run_id}.service'].poll(), 'no refused POST stopped it')
            # A JSON body (no form) or a GET to /stop does nothing.
            self.assertEqual(self.request(port, 'POST', '/stop', dict(form, Origin=origin, **{
                'Content-Type': 'application/json'}), json.dumps({'run_id': run_id}))[0], 400)
            self.assertEqual(self.request(port, 'GET', '/stop?run_id=' + run_id, good)[0], 405)
            self.assertIsNone(self.units.procs[f'recursion-run-{run_id}.service'].poll())
            status, page, _ = self.request(port, 'POST', '/stop', dict(form, Origin=origin), body)
            self.assertEqual(status, 200)
            self.assertIn('<strong>stopped</strong>', page)
        self.shutdown()
        _, composed = self.boot()
        rotated = (self.runtime / 'recursion' / 'gui-device-token').read_text().strip()
        self.assertNotEqual(rotated, token)
        self.assertFalse(composed.door({'HTTP_AUTHORIZATION': 'Bearer ' + token}))
        self.assertFalse(composed.door({'HTTP_COOKIE': 'recursion_device=' + token}))
        self.assertTrue(composed.door({'HTTP_COOKIE': 'x=1; recursion_device=%s; y=2' % rotated}))
        # Non-ASCII and very long offers are refused without raising.
        for offer in ('é' * 64, rotated + '\x00', 'a' * 100000, rotated[:-1] + '\udcff'):
            self.assertFalse(composed.door({'HTTP_AUTHORIZATION': 'Bearer ' + offer}))

    def test_slow_and_idle_clients_are_cut_off_and_cannot_take_every_gui_thread(self):
        """ADV1-3 (fixed): the headers have one deadline for the whole of them, and a full server closes the
        connection that has been sending headers longest, so no number of slow clients keeps the operator out.

        Three phases on one server: a dripped header is closed by the 1 s header deadline; with both timers
        at 30 s, idle connections past the thread bound are closed oldest first and the operator is answered
        in a fraction of that (only eviction can have made the room); idle connections left alone are closed
        by the 1 s deadline."""
        from recursion_core.daemon import gui as gui_module
        from recursion_core.gui.client import ControlClient
        door = adapter.DeviceDoor(self.runtime / 'recursion')
        self.addCleanup(door.close)
        token = door.path.read_text().strip()
        client = ControlClient(lambda *a: (501, b'{"error": "route_inventory_not_implemented"}'))
        threads, extra, idle_s = gui_module.MAX_REQUEST_THREADS, 8, 30.0
        with mock.patch.object(gui_module, 'HEADER_DEADLINE_S', 1.0), LoopbackGUI(client, door, port=0) as gui:
            port = gui.address[1]
            good = {'Host': '127.0.0.1:%d' % port, 'Authorization': 'Bearer ' + token}
            taken, process_request = [], gui.server.process_request

            def counted(request, address):
                try:
                    process_request(request, address)
                finally:
                    taken.append(address)

            def connect_idle():
                # One at a time, each after the server took the one before. The listen backlog is 5: a burst
                # overflows it, the kernel drops a SYN, and that connect (the operator's too) resends it 1 s
                # later. That delay is the kernel's, not the server's, and it is kept out of this test.
                before = len(taken)
                sock = socket.create_connection(('127.0.0.1', port))
                self.addCleanup(sock.close)
                self.assertTrue(wait_for(lambda: len(taken) > before, 10), 'the server did not take a connection')
                return sock

            def closed_by_server(sock, seconds):
                sock.settimeout(seconds)
                try:
                    return sock.recv(1) == b''
                except (BlockingIOError, TimeoutError):
                    return False
                except OSError:
                    return True

            # One byte every 0.3 s never trips the per-read timeout; the header deadline still closes it.
            with socket.create_connection(('127.0.0.1', port)) as drip:
                started = time.monotonic()
                closed = None
                try:
                    for byte in b'GET / HTTP/1.1\r\nX-Pad: ' + b'x' * 40:
                        drip.send(bytes([byte]))
                        time.sleep(0.3)
                except OSError:
                    closed = time.monotonic() - started
                self.assertIsNotNone(closed, 'the server never closed a client that drips its headers')
                self.assertLess(closed, 4.0)
            self.assertTrue(wait_for(lambda: not gui.server._reading and not gui.server._working, 5))
            with mock.patch.object(gui.server, 'process_request', counted):
                # More idle connections than there are threads: each new one evicts the oldest idle one. No
                # timer closes anything here for idle_s, so the room can only come from eviction.
                with mock.patch.object(gui_module, 'HEADER_DEADLINE_S', idle_s), \
                        mock.patch.object(gui_module, 'RECV_TIMEOUT_S', idle_s):
                    idle = [connect_idle() for _ in range(threads + extra)]
                    self.assertEqual(len(gui.server._reading), threads)
                    for index, sock in enumerate(idle):
                        self.assertEqual(closed_by_server(sock, 5 if index < extra else 0), index < extra,
                                         'idle connection %d of %d: the oldest %d are evicted, the rest stay'
                                         % (index, len(idle), extra))
                    started = time.monotonic()
                    try:
                        status = self.request(port, 'GET', '/', good, timeout=idle_s)[0]
                    except (OSError, http.client.HTTPException) as error:
                        self.fail('a server full of idle connections did not answer the operator: %r' % (error,))
                    waited = time.monotonic() - started
                    self.assertEqual(status, 200)
                    # Relative, not a wall bound: a quarter of the time any idle connection may still stay.
                    self.assertLess(waited, idle_s / 4, 'the operator waited on the idle connections')
                    self.assertTrue(closed_by_server(idle[extra], 5), 'the operator evicts the oldest idle one')
                    self.assertLessEqual(len(gui.server._reading) + len(gui.server._working), threads)
                # The rest would hold their threads for idle_s; hanging up frees them at once.
                for sock in idle:
                    sock.close()
                self.assertTrue(wait_for(lambda: not gui.server._reading and not gui.server._working, 5),
                                'threads of idle connections that hung up are free')
                # Back on the 1 s header deadline: idle connections nobody evicts or closes are cut off by it.
                late = [connect_idle() for _ in range(4)]
                self.assertTrue(wait_for(lambda: not gui.server._reading and not gui.server._working, 5),
                                'idle connections are gone after the header deadline')
                for sock in late:
                    self.assertTrue(closed_by_server(sock, 5), 'the header deadline closes an idle connection')
            self.assertEqual(self.request(port, 'GET', '/', good, timeout=30)[0], 200)

    def test_one_slow_client_cannot_hold_the_gui_and_its_stop(self):
        """ADV1-3 (fixed): the GUI served one connection at a time with a per-recv 2 s timeout, so a client
        that sends one byte a second (any local uid can reach 127.0.0.1) held it indefinitely."""
        door = adapter.DeviceDoor(self.runtime / 'recursion')
        self.addCleanup(door.close)
        token = door.path.read_text().strip()
        from recursion_core.gui.client import ControlClient
        client = ControlClient(lambda *a: (501, b'{"error": "route_inventory_not_implemented"}'))
        with LoopbackGUI(client, door, port=0) as gui:
            port = gui.address[1]
            stop = threading.Event()

            def drip():  # one byte every 0.5 s for 8 s, then hang up; it could go on for as long as it likes
                with socket.create_connection(('127.0.0.1', port)) as sock:
                    for byte in b'GET / HTTP/1.1\r\nX-Pad: '[:16]:
                        if stop.is_set():
                            return
                        sock.send(bytes([byte]))
                        time.sleep(0.5)

            thread = threading.Thread(target=drip, daemon=True)
            thread.start()
            self.addCleanup(lambda: (stop.set(), thread.join(5)))
            time.sleep(0.3)
            started = time.monotonic()
            status = self.request(port, 'GET', '/', {'Host': '127.0.0.1:%d' % port,
                                                     'Authorization': 'Bearer ' + token}, timeout=60)[0]
            waited = time.monotonic() - started
            stop.set()
        self.assertEqual(status, 200)
        self.assertLess(waited, 3.0, 'the operator waited %.1f s behind one slow client' % waited)


@unittest.skipUnless(LINUX, 'the daemon is Linux only')
class RuntimeDirTest(unittest.TestCase):
    def test_an_existing_group_readable_runtime_directory_is_refused_not_tightened(self):
        # ADV1-13 (fixed): DeviceDoor's directory check runs before the transport's ensure_private_dir. It
        # used to chmod a 0755 directory to 0700, so the transport's "refuse, do not repair" rule never fired.
        tmp = Path(tempfile.mkdtemp(prefix='rc-adv1-rt-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(tmp, True))
        os.chmod(tmp, 0o700)
        directory = tmp / 'recursion'
        directory.mkdir()
        for mode in (0o755, 0o750, 0o701, 0o770):
            os.chmod(directory, mode)
            for publish in (True, False):
                with self.assertRaises(PermissionError):
                    adapter.DeviceDoor(directory, publish=publish)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), mode)
            self.assertEqual(os.listdir(directory), [])
        # The composition refuses the same directory and leaves it as it was: no socket, lock or token file.
        os.chmod(directory, 0o755)
        daemon = Daemon(Registry(tmp / 'state'), launcher.EngineUnitManager(), boot_id=lambda: BOOT)
        self.addCleanup(daemon.close)
        with self.assertRaises(PermissionError):
            adapter.compose_control(daemon, tmp / 'state', routes={}, environ={
                'XDG_RUNTIME_DIR': str(tmp), 'XDG_CONFIG_HOME': str(tmp / 'config'), 'PATH': '/usr/bin:/bin'})
        self.assertEqual((stat.S_IMODE(os.stat(directory).st_mode), os.listdir(directory)), (0o755, []))
        # A missing directory is still made at 0700, and a 0700 one is used as it is.
        os.rmdir(directory)
        door = adapter.DeviceDoor(directory)
        self.addCleanup(door.close)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(door.path).st_mode), 0o600)
        adapter.DeviceDoor(directory, publish=False)


if __name__ == '__main__':
    unittest.main()
