"""The daemon process must not hand a shut standard descriptor to its own lock or socket.

Started with fd 0 or fd 2 not open, the next file the daemon opens (the runtime lock)
takes that number, and a later socket can too. Neither a file nor a socket of the daemon
may be fd 0, 1 or 2, and a control connection must not already hold the daemon's own bytes.
On the free-threaded build, Stop through the control CLI still arrives.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

from recursion_core.control_transport import UnixControlClient

LINUX = sys.platform.startswith('linux') and os.path.isdir('/proc/self/fd')
REAL_RUNTIME = (LINUX and sys.implementation.name == 'cpython' and sys.version_info[:2] == (3, 14)
                and sysconfig.get_config_var('Py_GIL_DISABLED'))
GUARD_S = 20


def _launcher(argv, shut_fd):
    return 'import os, sys\n' f'os.close({int(shut_fd)})\n' f'os.execv(sys.executable, {list(argv)!r})\n'


def _targets(pid):
    found = {}
    for fd in (0, 1, 2):
        try:
            found[fd] = os.readlink(f'/proc/{pid}/fd/{fd}')
        except OSError as exc:
            found[fd] = f'absent:{type(exc).__name__}'
    return found


def _inherited_pipes(proc):
    found = set()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            found.add(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
        except OSError:
            pass
    return found


def _unsolicited(path):
    """Bytes the daemon wrote before the client sent a frame."""

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(0.4)
        sock.connect(path)
        try:
            return sock.recv(65536)
        except (TimeoutError, socket.timeout):
            return b''
    finally:
        sock.close()


@unittest.skipUnless(REAL_RUNTIME, 'needs Linux free-threaded CPython 3.14; the daemon refuses any other runtime')
class DaemonStandardFdTest(unittest.TestCase):
    def _assert_standard(self, pid, shut_fd, inherited):
        targets = _targets(pid)
        for fd, target in targets.items():
            self.assertFalse(target.startswith('socket:'), targets)
            self.assertFalse(target.startswith('anon_inode:'), targets)
            if target.startswith('pipe:'):
                self.assertIn(target, inherited, targets)
            elif target.startswith('/'):
                self.assertEqual('/dev/null', target, targets)
            else:
                self.fail(repr(targets))
        self.assertEqual('/dev/null', targets[shut_fd], targets)
        return targets

    def _stubs(self, bins):
        bins.mkdir()
        (bins / 'loginctl').write_text('#!/bin/sh\n[ "$1" = show-user ] || exit 91\nprintf "yes\\n"\n')
        (bins / 'systemctl').write_text(
            '#!/bin/sh\n[ "$1" = --user ] && [ "$2" = list-units ] || exit 92\nexit 0\n')
        for program in bins.iterdir():
            program.chmod(0o700)

    def _start(self, shut_fd):
        tmp = Path(tempfile.mkdtemp(prefix='rc-daemon-fd-', dir=os.environ.get('TMPDIR')))
        os.chmod(tmp, 0o700)
        self._stubs(tmp / 'bin')
        runtime = tmp / 'runtime'
        runtime.mkdir(mode=0o700)
        env = {
            'PATH': str(tmp / 'bin'),
            'HOME': str(tmp),
            'XDG_RUNTIME_DIR': str(runtime),
            'XDG_STATE_HOME': str(tmp / 'state'),
            'XDG_CONFIG_HOME': str(tmp / 'config'),
            'PYTHONPATH': str(Path(__file__).resolve().parents[3]),
            'PYTHONDONTWRITEBYTECODE': '1',
            'TMPDIR': os.environ.get('TMPDIR', '/tmp'),
        }
        argv = [sys.executable, '-X', 'gil=0', '-B', '-m', 'recursion_core.daemon',
                '--gui-port', '0', '--sweep-seconds', '30']
        proc = subprocess.Popen(
            [sys.executable, '-B', '-c', _launcher(argv, shut_fd)],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        inherited = _inherited_pipes(proc)
        sock = runtime / 'recursion' / 'control.sock'

        def finish():
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            shutil.rmtree(tmp, True)

        self.addCleanup(finish)
        deadline = time.monotonic() + GUARD_S
        while time.monotonic() < deadline and proc.poll() is None and not sock.exists():
            time.sleep(0.05)
        self.assertIsNone(proc.poll(), 'the daemon ended before it served')
        self.assertTrue(sock.exists(), 'the control socket was not created')
        return proc, inherited, str(sock)

    def test_daemon_with_fd_2_not_open_keeps_our_files_off_stdio_and_stop_arrives(self):
        proc, inherited, sock = self._start(2)
        self._assert_standard(proc.pid, 2, inherited)
        self.assertEqual(b'', _unsolicited(sock))
        client = UnixControlClient(sock, timeout_s=5)
        try:
            self.assertEqual(client.request({'op': 'ping'})['op'], 'pong')
        finally:
            client.close()
        runtime = str(Path(sock).parents[1])  # the socket lives in <runtime>/recursion/
        env = {
            'PATH': '/usr/bin:/bin',
            'HOME': runtime,
            'XDG_RUNTIME_DIR': runtime,
            'PYTHONPATH': str(Path(__file__).resolve().parents[3]),
            'PYTHONDONTWRITEBYTECODE': '1',
            'TMPDIR': os.environ.get('TMPDIR', '/tmp'),
        }
        stop_argv = [sys.executable, '-B', '-m', 'recursion_core.control', 'stop', 'run-1']
        stop = subprocess.run(
            [sys.executable, '-B', '-c', _launcher(stop_argv, 2)],
            env=env, capture_output=True, timeout=GUARD_S, check=False,
        )
        self.assertEqual(4, stop.returncode, (stop.stdout, stop.stderr))
        self.assertEqual({'code': 'run_unknown', 'run_id': 'run-1'}, json.loads(stop.stdout))
        self.assertIsNone(proc.poll(), 'Stop took the daemon down')

    def test_daemon_with_fd_0_not_open_keeps_our_files_off_stdio(self):
        proc, inherited, sock = self._start(0)
        self._assert_standard(proc.pid, 0, inherited)
        self.assertEqual(b'', _unsolicited(sock))
        client = UnixControlClient(sock, timeout_s=5)
        try:
            self.assertEqual(client.request({'op': 'ping'})['op'], 'pong')
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
