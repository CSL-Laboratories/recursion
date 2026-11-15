"""Codex GPT-6.1 Sol's round-2 survivor witnesses (lane DRIVER-CX keeps them).

Copied unchanged from review-2-402-gpt-6.1-sol-evidence/test_survivors.py in the lane's folder, the read of draft #402 at 749773c8; they failed there and
pass at the answer commit. Fakes only: no real codex, no credential, no network.
"""

"""Coverage witnesses for new rules whose single removals the lane tests missed."""
import threading
import unittest
from recursion_core.routes.engines import codex as cx
from recursion_core.routes.tests.fakes import FakeClock
from recursion_core.routes.tests.test_codex_driver import AppServer, Launcher, done
from recursion_core.routes.tests.test_codex_attended import tool, LateTyping


class Coverage(unittest.TestCase):
    def test_session_close_tolerates_oserror(self):
        class Process:
            def close_stdin(self):
                raise BrokenPipeError(32, 'synthetic dead pipe')
        session = cx._Session(Process(), 'subscription:synthetic')
        session.close()
        self.assertTrue(session.closed)

    def test_session_loop_delivers_actual_drafts_to_capture_callback(self):
        module = tool()
        server = AppServer(script={1: [done()]})
        console = module.Console(lambda _: None)
        driver = cx.CodexDriver(Launcher(server), FakeClock(), approvals=console)
        captured = []
        module.session_loop(driver, LateTyping(server, ['hello', '/quit']), lambda _: None, console,
                            env={}, read_s=0, on_draft=captured.append)
        self.assertEqual([d['type'] for d in captured], ['input.accepted', 'usage', 'result'])

    def test_close_waits_for_active_session_write(self):
        writing, release, closed = threading.Event(), threading.Event(), threading.Event()
        class Process:
            def send(self, line):
                writing.set()
                release.wait(3)
            def close_stdin(self):
                closed.set()
        session = cx._Session(Process(), 'subscription:synthetic')
        writer = threading.Thread(target=session.write, args=({'synthetic': 1},))
        writer.start()
        self.assertTrue(writing.wait(3))
        closer = threading.Thread(target=session.close)
        closer.start()
        try:
            self.assertFalse(closed.wait(.1))
        finally:
            release.set()
            writer.join(3)
            closer.join(3)
        self.assertTrue(closed.is_set())


if __name__ == '__main__':
    unittest.main(verbosity=2)
