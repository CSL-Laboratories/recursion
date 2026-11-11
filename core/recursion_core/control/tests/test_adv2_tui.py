"""ADV2 red-team: the TUI entry (``python -m recursion_core.control.tui``) over a real Unix socket.

PRODUCT.md d84cdd26 (CSLR-D-012) 3.6: "Engine output (ESC, OSC 52, DCS, title changes, look-alike
prompt text) is untrusted: it cannot send input, change the target or draw outside its pane"
(*[proposed: research]*), and 3.4 S6: Stop is "pending" when unconfirmed, on every surface. The
daemon here is a stand-in served by the real ``control_transport.UnixControlServer`` (or a raw
socket for garbage frames); the client is the real ``SocketAPI`` the entry hands the TUI.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control import tui as tui_entry
from recursion_core.control.client import SocketAPI
from recursion_core.tui.app import TuiApp

POSIX = os.name == 'posix' and hasattr(os, 'getuid') and sys.platform.startswith('linux')
POSIX_ONLY = 'Linux-only: needs an owner-only AF_UNIX socket with peer credentials'
HOSTILE = ('\x1b]52;c;cm0gLXJmIH4=\x07'      # OSC 52 clipboard write
           '\x1b]0;owned\x07'                # title change
           '\x1bP+q\x1b\\'                   # DCS
           '\x1b[2J\x1b[H'                   # clear + home
           '\x9b31m'                         # C1 CSI
           '\rrecursion> stop run-other\n'   # look-alike prompt
           '\x07\x08\x7f')
RAW_CONTROL = [chr(c) for c in range(0x20) if chr(c) not in '\n'] + ['\x7f'] + [chr(c) for c in range(0x80, 0xa0)]


def hostile_run(run_id='run-1'):
    return {'run_id': run_id, 'state': 'running' + HOSTILE, 'tenant': 't' + HOSTILE, 'engine': 'codex' + HOSTILE,
            'observed': {'state': 'running', 'detail': HOSTILE}, 'cgroup': '/x',
            'output': {'bytes': 10, 'lines': ['engine says ' + HOSTILE], 'complete': False},
            'result': {'code': 'engine_error' + HOSTILE, 'detail': HOSTILE}}


class Daemon:
    """A stand-in daemon behind the real UnixControlServer; ``reply`` decides each answer."""

    def __init__(self, runtime, reply):
        from recursion_core.control_transport import UnixControlServer
        self.seen = []
        self._reply = reply
        self.server = UnixControlServer(str(Path(runtime) / 'recursion'), self._handle)
        self.server.start()

    def _handle(self, message, conn):
        self.seen.append(message.get('method'))
        return {'ok': True, 'result': self._reply(message)}  # the envelope DaemonControl.handle answers

    def close(self):
        self.server.close()


class RawDaemon:
    """A listener at the control socket path that answers every connection with fixed bytes."""

    def __init__(self, runtime, payload):
        directory = Path(runtime) / 'recursion'
        directory.mkdir(mode=0o700)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(directory / 'control.sock'))
        self.sock.listen(8)
        self.payload, self.stop = payload, False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                conn.recv(65536)
                conn.sendall(self.payload)
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self.stop = True
        self.sock.close()


@unittest.skipUnless(POSIX, POSIX_ONLY)
class Adv2TuiSocketTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix='rc-adv2-tui-')
        os.chmod(self.runtime, 0o700)
        self.addCleanup(shutil.rmtree, self.runtime, True)
        patcher = mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.runtime})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tui(self, lines, timeout_s=None):
        out, err = io.StringIO(), io.StringIO()
        defaults = dict(SocketAPI.__init__.__kwdefaults__)
        if timeout_s is not None:
            defaults['timeout_s'] = timeout_s
        with mock.patch.object(SocketAPI.__init__, '__kwdefaults__', defaults):
            code = tui_entry.main(['--plain'], stdin=io.StringIO(''.join(l + '\n' for l in lines) + 'q\n'),
                                  stdout=out, stderr=err, environ={})
        return code, out.getvalue(), err.getvalue()

    def assert_clean(self, text):
        for ch in RAW_CONTROL:
            self.assertNotIn(ch, text, f'raw control character {ch!r} reached the terminal stream')
        self.assertNotIn('Traceback', text)

    def test_hung_daemon_stop_reads_unavailable_never_stopped(self):
        release = threading.Event()
        self.addCleanup(release.set)
        daemon = Daemon(self.runtime, lambda m: (release.wait(20), {'code': 'stopped'})[1])
        self.addCleanup(daemon.close)
        started = time.monotonic()
        code, text, err = self.run_tui(['stop run-1'], timeout_s=1.0)
        elapsed = time.monotonic() - started
        release.set()
        self.assertEqual((code, err), (0, ''))
        self.assertLess(elapsed, 10)
        self.assertIn('answer run.stop status=control_api_unavailable', text)
        self.assertNotIn('status=stopped', text)
        self.assert_clean(text)

    def test_garbage_and_oversized_frames_read_unavailable(self):
        for payload in (b'\x00\x00\x00\x05nope!', struct.pack('>I', 1 << 30) + b'{', b'\xff' * 3,
                        struct.pack('>I', 2) + b'[]', struct.pack('>I', 15) + b'{"ok": "true"}\n'):
            with self.subTest(payload=payload[:12]):
                shutil.rmtree(Path(self.runtime) / 'recursion', True)
                raw = RawDaemon(self.runtime, payload)
                try:
                    code, text, err = self.run_tui(['list', 'stop run-1'], timeout_s=2.0)
                finally:
                    raw.close()
                self.assertEqual((code, err), (0, ''))
                self.assertIn('answer run.stop status=control_api_unavailable', text)
                self.assertNotIn('status=stopped', text)
                self.assert_clean(text)

    def test_wrong_shaped_answers_never_read_as_stopped(self):
        answers = iter([{'code': 'ok', 'runs': 'not a list'},  # the TUI's own list at open
                        {'code': 'ok', 'runs': [{'run_id': 'run-1', 'state': 'running'}]},
                        {'code': 'stopped'},  # stopped without a run id
                        {'code': None}, {'code': ['stopped']}, {'code': 'stopped', 'run_id': 5}])
        daemon = Daemon(self.runtime, lambda m: next(answers, {'code': 'pending', 'run_id': 'run-1'}))
        self.addCleanup(daemon.close)
        code, text, err = self.run_tui(['list', 'stop run-1', 'stop run-1', 'stop run-1', 'stop run-1'])
        self.assertEqual((code, err), (0, ''))
        self.assertNotIn('Traceback', text)
        self.assertEqual(daemon.seen, ['run.list'] * 2 + ['run.stop'] * 4)
        app_lines = [l for l in text.splitlines() if 'run=run-1' in l and 'stopped' in l.lower()]
        # Observed: an answer coded "stopped" with no run id is said as "status=stopped" for the request
        # (that is the daemon's word), but no run in the snapshot is marked stopped from it.
        self.assertNotIn('state=stopped', text, app_lines)

    def test_hostile_text_from_the_daemon_is_never_written_raw(self):
        def reply(message):
            method = message.get('method')
            if method == 'run.list':
                return {'code': 'ok', 'runs': [hostile_run(), hostile_run('run-2' + HOSTILE)]}
            if method == 'run.status':
                return {'code': 'ok', 'run': hostile_run(), 'detail': HOSTILE}
            return {'code': 'refused' + HOSTILE, 'run_id': 'run-1', 'reason': HOSTILE, 'detail': HOSTILE}
        daemon = Daemon(self.runtime, reply)
        self.addCleanup(daemon.close)
        code, text, err = self.run_tui(['list', 'status run-1', 'stop run-1'])
        self.assertEqual((code, err), (0, ''))
        self.assertIn('answer run.status status=ok', text)  # the hostile answers really arrived
        self.assertEqual(daemon.seen, ['run.list', 'run.list', 'run.status', 'run.stop'])
        self.assert_clean(text)
        self.assertNotIn('status=stopped', text)

    def test_hostile_text_never_reaches_the_full_screen_frame_raw(self):
        def reply(message):
            if message.get('method') == 'run.list':
                return {'code': 'ok', 'runs': [hostile_run()]}
            return {'code': 'ok', 'run': hostile_run()}
        daemon = Daemon(self.runtime, reply)
        self.addCleanup(daemon.close)
        app = TuiApp(SocketAPI(timeout_s=5), clock=time.monotonic, environ={})
        app.open()
        self.assertEqual(app.runs[0]['run_id'], 'run-1')
        app.command('list')
        app.command('status run-1')
        app.attach('run-1')
        app.resize(100, 40)
        frame = '\n'.join(str(cell) for row in app.frame() for cell in (row if isinstance(row, tuple) else (row,)))
        for ch in RAW_CONTROL:
            self.assertNotIn(ch, frame, f'raw {ch!r} in a frame row')
        # Observed on 08775416: the run.status ``output`` tail is not shown in the TUI's output pane; the
        # TUI has no feed from the daemon's engine output.
        self.assertNotIn('engine says', frame)


class Adv2TuiStopAnswerTests(unittest.TestCase):
    def test_a_stopped_answer_for_another_run_does_not_confirm_this_stop(self):
        class Api:
            def call(self, method, params=None):
                if method == 'run.list':
                    return {'code': 'ok', 'runs': [{'run_id': 'run-1', 'state': 'running'}]}
                return {'code': 'stopped', 'run_id': 'run-9'}
        app = TuiApp(Api(), clock=time.monotonic, environ={})
        app.open()
        app.stop('run-1')
        self.assertEqual(app.runs[0].get('state'), 'running')  # holds: the snapshot is not marked stopped
        said = '\n'.join(app.transcript)
        self.assertNotIn('stop run=run-1: control API answered stopped', said)
        self.assertIn('stop run=run-1: stop_answer_run_mismatch', said)
        self.assertIn('this Stop is not confirmed', said)


if __name__ == '__main__':
    unittest.main()
