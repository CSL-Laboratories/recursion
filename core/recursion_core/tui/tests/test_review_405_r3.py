"""Review 3 of #405 (the session reads at e4810bc3): DeepSeek V4.1 Flash's confirming read. Each finding changed
here is a test that fails on that head: F1 a daemon whose log is shorter than what was shown kept the transcript
refused for good, F2 an answer whose own methods raise still stopped the loop, F3 a gap line answered again after
the cursor was put back was shown twice, F4 the sentences.
"""

from __future__ import annotations

import collections.abc
import unittest

from recursion_core.tui import sessions as s
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_sessions import SID, new_app, shown


class ShorterLogTest(unittest.TestCase):  # F1
    def test_a_log_shorter_than_what_was_shown_is_read_again_from_its_start(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        self.assertEqual(app.transcripts[SID].printed, daemon.high_water)
        daemon.lines.clear()  # a restart whose log did not persist
        daemon.restart()
        app.command(f"transcript {SID}")  # cursor_ahead for the last line shown itself
        self.assertIn("the next read goes on from the start of its log", app.notice)
        self.assertEqual(app.transcripts[SID].cursor, s.first_cursor(SID))
        daemon.append("engine.output", "after the restart\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app)[-1], "after the restart")

    def test_a_gap_of_the_new_log_is_shown_though_the_old_log_showed_one_there(self) -> None:
        daemon = FakeDaemon()
        daemon.forget(2, 3)
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")  # line 1, the gap of 2 and 3, lines 4 to 7
        daemon.lines.clear()
        daemon.forgotten.clear()
        daemon.restart()
        app.command(f"transcript {SID}")  # refused, and the next read goes on from the start
        for i in range(4):
            daemon.append("engine.output", f"new {i}\n")
        daemon.forget(2, 3)
        app.command(f"transcript {SID}")
        gaps = [text for text in shown(app) if text == "events 2 to 3 are not available (retention)"]
        self.assertEqual(len(gaps), 2, "one for each log")
        self.assertEqual(shown(app)[-1], "new 3")


class AnyMappingTest(unittest.TestCase):  # F2
    def test_an_answer_whose_own_methods_raise_is_copied_or_refused_and_the_loop_goes_on(self) -> None:
        class GetRaises(dict):
            def get(self, *args):
                raise RuntimeError("a broken answer")

        class ContainsRaises(dict):
            def __contains__(self, key):
                raise RuntimeError("a broken answer")

        class KeysRaise(collections.abc.Mapping):
            def __getitem__(self, key):
                raise RuntimeError("a broken answer")

            def __iter__(self):
                raise RuntimeError("a broken answer")

            def __len__(self):
                return 1

        for answer in (GetRaises(code="ok", sessions=[]), ContainsRaises(code="ok", sessions=[]), KeysRaise()):
            with self.subTest(answer=type(answer).__name__):
                daemon = FakeDaemon()
                app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=120, height=30)
                loop = Loop(app, daemon)  # driven by hand: no thread is started
                app.open()
                loop._answers.put((app.on_answer, s.LIST, dict(s.LIST_PARAMS), answer))
                loop.tick()
                loop.tick()  # a second frame reads the kept answer again
                self.assertIs(type(app.last), dict)
                loop.press("q")
                loop.tick()
                self.assertTrue(app.closed)


class GapShownOnceTest(unittest.TestCase):  # F3
    def test_a_gap_answered_again_after_the_cursor_was_put_back_is_shown_once(self) -> None:
        far = 10 ** 9

        class FarGap(FakeDaemon):
            def call(self, method, params=None):
                cursor = (params or {}).get("cursor") or {}
                if method == s.READ and isinstance(cursor.get("seq"), int) and cursor["seq"] < far:
                    self.calls.append((method, params))
                    return {"code": "ok", "projection": "transcript", "lines": [gap_line(cursor["seq"] + 1, far, 0)],
                            "cursor": {"epoch": 1, "session_id": self.session_id, "seq": far}, "more": False,
                            "epoch_advanced": False}
                return super().call(method, params)

        app, _ = new_app(FarGap())
        for _ in range(3):  # the gap; cursor_ahead and the cursor put back; the same gap again
            app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [f"events 1 to {far} are not available (retention)"])


class SentencesTest(unittest.TestCase):  # F4
    def test_the_sentences_say_what_the_code_does(self) -> None:
        doc = " ".join(s.__doc__.split())
        self.assertIn("or to the start of the log when that line was the position refused", doc)
        self.assertIn("A replay.gap line already shown is not shown again", doc)


if __name__ == "__main__":
    unittest.main()
