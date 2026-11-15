"""Review 4 of #405 (the session reads at 6526108d): DeepSeek V4.1 Flash's confirming read. Each finding changed
here is a test that fails on that head: R1 a log read again from its start filled the pane twice, R2 a new log's
gap line was hidden by an old far gap, R3 a refusal of the start itself was said as a recovery, R4 an answer whose
iteration did not end held the app's lock, and the sentences.
"""

from __future__ import annotations

import collections.abc
import threading
import unittest

from recursion_core.tui import sessions as s
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.test_sessions import SID, new_app, shown


class SameEpochReadAgainTest(unittest.TestCase):  # R1
    def test_a_log_read_again_from_its_start_in_the_same_epoch_shows_nothing_twice_in_silence(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        first = list(shown(app))
        del daemon.lines[3:]  # the log kept three of the seven positions shown, in the same epoch
        app.command(f"transcript {SID}")  # cursor_ahead: the view is rebuilt below a marker (review 5 of #405)
        app.command(f"transcript {SID}")  # reads on: nothing new
        marker = shown(app)[len(first)]
        self.assertTrue(marker.startswith("-- view rebuilt: "), marker)
        self.assertEqual(shown(app), [*first, marker, *first[:3]], "shown again only below the marker")


class NewLogGapTest(unittest.TestCase):  # R2
    def test_a_new_logs_gap_is_shown_after_an_old_far_gap(self) -> None:
        far = 10 ** 9

        class OnceFar(FakeDaemon):
            told = False

            def call(self, method, params=None):
                if method == s.READ and not self.told:
                    self.told = True
                    self.calls.append((method, params))
                    return {"code": "ok", "projection": "transcript", "lines": [gap_line(1, far, 0)],
                            "cursor": {"epoch": 1, "session_id": self.session_id, "seq": far}, "more": False,
                            "epoch_advanced": False}
                return super().call(method, params)

        daemon = OnceFar()
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")  # the far gap
        daemon.lines.clear()
        daemon.restart()  # a new log
        for i in range(4):
            daemon.append("engine.output", f"new {i}\n")
        daemon.forget(2, 3)
        app.command(f"transcript {SID}")  # refused at the far position: back to the last line shown, seq 0
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app)[-3:], ["new 0", "events 2 to 3 are not available (retention)", "new 3"])


class StartRefusedTest(unittest.TestCase):  # R3
    def test_a_daemon_that_refuses_the_start_too_is_said_as_an_error(self) -> None:
        class RefusesAll(FakeDaemon):
            def call(self, method, params=None):
                if method == s.READ:
                    self.calls.append((method, params))
                    return {"code": "request_invalid", "detail": "cursor_ahead"}
                return super().call(method, params)

        app, _ = new_app(RefusesAll())
        for _ in range(2):
            app.command(f"transcript {SID}")
            self.assertIn("refuses even the start of its log (seq 0)", app.error_row())
            self.assertNotIn("goes on from the start", app.notice)
        self.assertEqual(app.transcripts[SID].cursor, s.first_cursor(SID))


class CopyOutsideTheLockTest(unittest.TestCase):  # R4
    def test_an_answer_whose_iteration_does_not_end_does_not_hold_the_lock(self) -> None:
        entered, release = threading.Event(), threading.Event()

        class Paused(collections.abc.Mapping):
            data = {"code": "ok", "sessions": []}

            def __getitem__(self, key):
                return self.data[key]

            def __len__(self):
                return len(self.data)

            def __iter__(self):
                entered.set()
                release.wait()
                return iter(self.data)

        app, _ = new_app()
        worker = threading.Thread(target=app.on_answer, args=(s.LIST, dict(s.LIST_PARAMS), Paused()))
        worker.start()
        try:
            entered.wait()  # the worker is copying the answer
            got = app._lock.acquire(blocking=False)  # time is not used: the lock is free or it is not
            if got:
                app._lock.release()
        finally:
            release.set()
            worker.join()
        self.assertTrue(got, "the copy is made before the lock is taken")
        self.assertIsNotNone(app.log.latest("sessions"), "and the answer is taken")


class SentencesTest(unittest.TestCase):
    def test_the_sentences_say_what_the_code_does(self) -> None:
        doc = " ".join(s.__doc__.split())
        self.assertIn("never shows one twice in silence", doc)
        self.assertIn("A refusal of the start itself is said as an error", doc)
        self.assertIn("in an epoch and across the epochs of one store", doc)
        self.assertIn("last_line", s.Transcript.__doc__)


if __name__ == "__main__":
    unittest.main()
