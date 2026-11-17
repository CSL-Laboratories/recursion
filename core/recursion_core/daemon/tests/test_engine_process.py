"""The engine process wrapper closes once, bounds and decodes a line strictly, keeps its deadlines, and does not wait for a dead daemon.

Red tests of the sweep triage of daemon (STOP, CSL-238), entries E06, E07, E42, E40 and E33: each fails on
master without the change in ``daemon/launcher.py``, ``daemon/gui.py`` or ``daemon/engine_exec.py`` and passes with it.
The tests that are named ``keeps`` or ``still`` pin behaviour that must not change. Every fixture is synthetic and lies
below a temporary folder; no unit, manager or engine is started.
"""
import builtins
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from recursion_core.daemon import gui, launcher

LINUX = sys.platform.startswith('linux')
CHILD_LIMIT_S = 10


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


@unittest.skipUnless(LINUX, 'the launcher is Linux only')
class EngineProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='engine-process-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def reader(self):
        """A process with only what ``read_line`` reads: a buffer, a number to read from and a poll interval."""
        process = object.__new__(launcher.UnitEngineProcess)
        process._buffer = b''
        process._out = -1
        process._poll_s = 0.05
        process._ended = lambda: False
        return process

    # -- E06: two closers close one descriptor once --------------------------------------------------
    def test_two_closers_do_not_close_a_reused_descriptor(self):
        damaged = 0
        for _ in range(10):
            out = self.root / 'stdout.jsonl'
            out.write_bytes(b'')
            sentinel = self.root / 'sentinel'
            sentinel.write_bytes(b'ordinary content')
            process = launcher.UnitEngineProcess(Mock(), 'run-one', None, out, None)
            old_fd = process._out
            gate = threading.Barrier(2)
            lock = threading.Lock()
            replacement = []
            failures = []
            real_close, real_open = os.close, os.open

            def capture(obj, name, *default):
                value = builtins.getattr(obj, name, *default)
                if obj is process and name == '_out' and value is not None:
                    # Make the gap between the read of the number and its clearing as wide as a closer can have it.
                    try:
                        gate.wait(timeout=0.25)
                    except threading.BrokenBarrierError:
                        pass
                return value

            def close_and_reuse(fd):
                if fd != old_fd:
                    return real_close(fd)
                with lock:
                    real_close(fd)
                    if not replacement:
                        replacement.append(real_open(sentinel, os.O_RDONLY))

            def closer():
                try:
                    process.close()
                except BaseException as exc:
                    failures.append(type(exc).__name__)

            with patch.object(launcher, 'getattr', capture, create=True), patch.object(os, 'close', close_and_reuse):
                threads = [threading.Thread(target=closer) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertFalse(failures)
            self.assertEqual(replacement, [old_fd], 'the process reused the released descriptor')
            try:
                os.fstat(replacement[0])
            except OSError:
                damaged += 1
            else:
                real_close(replacement[0])
        self.assertEqual(damaged, 0, f'{damaged} of 10 rounds closed the descriptor that replaced the first one')

    def test_a_second_close_is_still_quiet(self):
        out = self.root / 'stdout.jsonl'
        out.write_bytes(b'')
        process = launcher.UnitEngineProcess(Mock(), 'run-one', None, out, None)
        process.close()
        process.close()
        self.assertIsNone(process._out)

    # -- E07: a newline does not excuse a line over the bound -----------------------------------------
    def test_a_newline_does_not_bypass_the_stdout_line_limit(self):
        out = self.root / 'stdout.jsonl'
        out.write_bytes(b'x' * 9 + b'\n')
        process = launcher.UnitEngineProcess(Mock(), 'run-one', None, out, None)
        self.addCleanup(process.close)
        with patch.object(launcher, 'MAX_LINE_BYTES', 8):
            with self.assertRaises(OSError) as raised:
                process.read_line(0.1)
        self.assertEqual(raised.exception.errno, errno.EMSGSIZE)

    def test_a_line_at_the_limit_is_still_returned(self):
        out = self.root / 'stdout.jsonl'
        out.write_bytes(b'x' * 8 + b'\n')
        process = launcher.UnitEngineProcess(Mock(), 'run-one', None, out, None)
        self.addCleanup(process.close)
        with patch.object(launcher, 'MAX_LINE_BYTES', 8):
            self.assertEqual(process.read_line(0.1), 'x' * 8)

    # -- E42: text that is not UTF-8 is refused, not repaired into valid JSON -------------------------
    def test_invalid_utf8_from_the_engine_is_refused(self):
        process = self.reader()
        process._buffer = b'{"text":"a\xffb"}\n'
        with self.assertRaises(OSError) as raised:
            process.read_line(0.1)
        self.assertEqual(raised.exception.errno, errno.EILSEQ)

    def test_invalid_utf8_in_the_last_unterminated_line_is_refused(self):
        process = self.reader()
        process._buffer = b'{"text":"a\xffb"}'
        process._ended = lambda: True
        with patch.object(launcher.os, 'read', return_value=b''):
            with self.assertRaises(OSError) as raised:
                process.read_line(0.1)
        self.assertEqual(raised.exception.errno, errno.EILSEQ)

    def test_a_line_of_valid_utf8_is_still_decoded(self):
        process = self.reader()
        process._buffer = '{"text":"é€😀"}\n'.encode('utf-8')
        self.assertEqual(process.read_line(0.1), '{"text":"é€😀"}')

    # -- E40: a result that arrives after its deadline is late ----------------------------------------
    def test_an_engine_line_completing_after_the_deadline_times_out(self):
        now = Clock()
        chunks = iter((b'x', b'\n'))

        def read(fd, size):
            now.value += 0.6
            return next(chunks)
        process = self.reader()
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(launcher.os, 'read', side_effect=read):
            with self.assertRaises(launcher.ReadTimeout):
                process.read_line(1.0)

    def test_a_late_liveness_read_does_not_deliver_the_output_as_timely(self):
        now = Clock()
        process = self.reader()

        def ended():
            now.value = 2.0
            return False
        process._ended = ended
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(launcher.os, 'read', return_value=b'x\n'):
            with self.assertRaises(launcher.ReadTimeout):
                process.read_line(1.0)

    def test_a_late_line_stays_buffered_for_the_next_read(self):
        now = Clock()
        process = self.reader()
        process._ended = lambda: False
        calls = []

        def read(fd, size):
            calls.append(1)
            now.value = 2.0
            return b'late\n'
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)), patch.object(launcher.os, 'read', read):
            with self.assertRaises(launcher.ReadTimeout):
                process.read_line(1.0)
            self.assertEqual(process.read_line(1.0), 'late')
        self.assertEqual(len(calls), 1, 'the second read took the line from the buffer')

    def test_a_line_already_buffered_keeps_being_returned_whatever_the_clock(self):
        now = Clock(100.0)
        process = self.reader()
        process._buffer = b'one\ntwo\n'
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)):
            self.assertEqual(process.read_line(0.0), 'one')
            now.value = 500.0
            self.assertEqual(process.read_line(0.0), 'two')

    def test_a_command_is_not_written_after_the_select_used_up_its_budget(self):
        now = Clock()
        process = self.reader()
        process._lock = threading.Lock()
        process._stdin = 99

        def writable(*args):
            now.value = launcher.SEND_TIMEOUT_S + 1
            return ([], [99], [])
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(launcher.select, 'select', side_effect=writable), \
                patch.object(launcher.os, 'write', return_value=2) as write:
            with self.assertRaises(TimeoutError):
                process.send('x')
        self.assertEqual(write.call_count, 0, 'an expired command was written to the engine')

    def test_a_command_in_time_is_still_written(self):
        now = Clock()
        process = self.reader()
        process._lock = threading.Lock()
        process._stdin = 99
        with patch.object(launcher, 'time', SimpleNamespace(monotonic=now)), \
                patch.object(launcher.select, 'select', return_value=([], [99], [])), \
                patch.object(launcher.os, 'write', side_effect=lambda fd, data: len(data)) as write:
            process.send('x')
        write.assert_called_once_with(99, b'x\n')

    def test_late_gui_bytes_are_refused(self):
        now = Clock()

        class Sock:
            def settimeout(self, timeout):
                pass

            def recv_into(self, buffer):
                now.value = 2.0
                buffer[:1] = b'x'
                return 1
        with patch.object(gui, 'time', SimpleNamespace(monotonic=now)):
            with self.assertRaises(TimeoutError):
                gui._DeadlineReader(Sock(), 1.0).readinto(bytearray(1))

    def test_gui_bytes_in_time_are_still_read(self):
        now = Clock()

        class Sock:
            def settimeout(self, timeout):
                pass

            def recv_into(self, buffer):
                buffer[:1] = b'x'
                return 1
        buffer = bytearray(1)
        with patch.object(gui, 'time', SimpleNamespace(monotonic=now)):
            self.assertEqual(gui._DeadlineReader(Sock(), 1.0).readinto(buffer), 1)
        self.assertEqual(bytes(buffer), b'x')

    # -- E33: the exec wrapper does not wait for a daemon that is gone ---------------------------------
    def wrapper(self, fifo, code):
        return subprocess.Popen([sys.executable, '-I', '-S', str(launcher.ENGINE_EXEC), str(fifo), '--',
                                 sys.executable, '-I', '-c', code],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_the_exec_wrapper_refuses_a_fifo_with_no_writer(self):
        fifo = self.root / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        os.close(os.open(fifo, os.O_RDWR | os.O_NONBLOCK))  # the daemon opened its end and is gone
        child = self.wrapper(fifo, 'raise SystemExit(0)')
        try:
            try:
                out, err = child.communicate(timeout=CHILD_LIMIT_S)
            except subprocess.TimeoutExpired:
                self.fail('engine_exec waits for a writer that is gone')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        self.assertEqual(child.returncode, 127, err)
        self.assertIn(b'launch refused', err)

    def test_the_exec_wrapper_still_takes_the_environment_the_daemon_writes_later(self):
        fifo = self.root / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        daemon_end = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # as the launcher holds it from before the unit starts
        self.addCleanup(os.close, daemon_end)
        child = self.wrapper(fifo, 'import os, sys; sys.stdout.write(os.environ["ENV_MARK"])')
        try:
            time.sleep(0.3)  # the wrapper is waiting; a write before it opens would be kept in the pipe as well
            body = json.dumps({'ENV_MARK': 'seen'}).encode()
            os.write(daemon_end, b'%d\n' % len(body) + body)
            try:
                out, err = child.communicate(timeout=CHILD_LIMIT_S)
            except subprocess.TimeoutExpired:
                self.fail('engine_exec did not take the environment')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        self.assertEqual((child.returncode, out), (0, b'seen'), err)

    def test_the_exec_wrapper_waits_for_the_rest_of_an_environment_written_in_two_parts(self):
        fifo = self.root / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        daemon_end = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, daemon_end)
        child = self.wrapper(fifo, 'import os, sys; sys.stdout.write(os.environ["ENV_MARK"])')
        try:
            body = json.dumps({'ENV_MARK': 'both-parts'}).encode()
            os.write(daemon_end, b'%d\n' % len(body) + body[:5])
            time.sleep(0.3)  # the wrapper has the header and waits for the rest of the body
            os.write(daemon_end, body[5:])
            try:
                out, err = child.communicate(timeout=CHILD_LIMIT_S)
            except subprocess.TimeoutExpired:
                self.fail('engine_exec did not take the environment')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        self.assertEqual((child.returncode, out), (0, b'both-parts'), err)

    def test_the_exec_wrapper_gives_up_on_a_daemon_that_never_writes(self):
        fifo = self.root / 'env.fifo'
        os.mkfifo(fifo, 0o600)
        daemon_end = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, daemon_end)
        # The wait is the module's ENV_WAIT_S, 30 s: the child runs the module with it set to a fraction of a second.
        script = self.root / 'short_wait.py'
        script.write_text('import importlib.util, sys\n'
                          'spec = importlib.util.spec_from_file_location("engine_exec", sys.argv[1])\n'
                          'module = importlib.util.module_from_spec(spec)\n'
                          'spec.loader.exec_module(module)\n'
                          'module.ENV_WAIT_S = 0.3\n'
                          'module.main(["x", sys.argv[2], "--", sys.executable, "-c", "pass"])\n')
        child = subprocess.Popen([sys.executable, '-I', str(script), str(launcher.ENGINE_EXEC), str(fifo)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = child.communicate(timeout=CHILD_LIMIT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            self.fail('engine_exec has no limit on its wait for the environment')
        self.assertEqual(child.returncode, 127, err)
        self.assertIn(b'TimeoutError', err)


if __name__ == '__main__':
    unittest.main()
