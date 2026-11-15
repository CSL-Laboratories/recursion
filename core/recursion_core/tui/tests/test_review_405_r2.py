"""Review 2 of #405 (the session reads at 644f99fd): DeepSeek V4.1 Flash's break-it read, each finding as a test
that fails on that head.

M1 a gap line that names a position past the daemon's log left the transcript unreadable, L1 an answer that is
not a mapping still stopped the loop, L2 page_problem raised on a bad call, L3 an exception from the control
API ended a request lane and left its read in flight, L4 four sentences. And one rule the merge of #404's
f6a747ce brought: a transcript's rows are counted under #404's "not counted" mark, as engine output is.
"""

from __future__ import annotations

import copy
import queue
import unittest
from unittest import mock

from recursion_core.tui import app as app_module
from recursion_core.tui import sessions as s
from recursion_core.tui.app import Loop, TuiApp, _Output
from recursion_core.tui.sanitize import wrap
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_sessions import SID, new_app, shown


class Answering(FakeDaemon):
    def __init__(self, change) -> None:
        super().__init__()
        self.change = change

    def call(self, method, params=None):
        answer = super().call(method, params)
        return self.change(copy.deepcopy(answer)) if method == s.READ and answer.get("code") == "ok" else answer


class GapPastTheLogTest(unittest.TestCase):  # M1
    def test_a_gap_line_past_the_log_does_not_end_the_transcript(self) -> None:
        far = 10 ** 9

        def far_gap(answer):
            return dict(answer, lines=[answer["lines"][0], gap_line(2, far, 0)],
                        cursor=dict(answer["cursor"], seq=far), more=False)

        daemon = Answering(far_gap)
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")  # rule (7) allows the gap's last position; the page is taken
        self.assertEqual(app.transcripts[SID].cursor["seq"], far)
        daemon.change = lambda answer: answer
        daemon.append("engine.output", "after\n")
        app.command(f"transcript {SID}")  # asks from before the last line shown, never from the gap's end
        self.assertEqual(shown(app)[-1], "after")
        self.assertEqual(len(shown(app)), len(set(shown(app))), "nothing twice")
        self.assertNotIn("has no position", app.notice)


class AnyAnswerTest(unittest.TestCase):  # L1
    def test_an_answer_that_is_not_a_mapping_is_said_and_the_loop_goes_on(self) -> None:
        for answer in ("a string", None, [1, 2], 5):
            with self.subTest(answer=answer):
                daemon = FakeDaemon()
                app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=120, height=30)
                loop = Loop(app, daemon)  # driven by hand: no thread is started
                app.open()
                loop._answers.put((app.on_answer, s.LIST, dict(s.LIST_PARAMS), answer))
                loop.tick()
                self.assertIn("answer_not_object", app.error_row())
                loop.press("q")
                loop.tick()
                self.assertTrue(app.closed)


class PageProblemCallTest(unittest.TestCase):  # L2
    def test_a_bad_call_is_refused_not_raised(self) -> None:
        good = s.read_params(s.first_cursor(SID))
        for asked in ({}, None, {"cursor": {}}, {"cursor": dict(good["cursor"], seq="0")}, "x"):
            with self.subTest(asked=asked):
                self.assertEqual(s.page_problem(asked, {"projection": "transcript"}), "asked")
        for answer in ("x", None, [], 5):
            with self.subTest(answer=answer):
                self.assertEqual(s.page_problem(good, answer), "answer")


class LaneTest(unittest.TestCase):  # L3
    def test_an_exception_from_the_control_api_is_an_answer_and_the_lane_goes_on(self) -> None:
        for raised in (KeyboardInterrupt, SystemExit):
            with self.subTest(raised=raised.__name__):

                class Raising(FakeDaemon):
                    exc = raised  # bound now, for this subtest

                    def call(self, method, params=None):
                        if method == s.READ:
                            raise self.exc("from the api")
                        return super().call(method, params)

                daemon = Raising()
                app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=120, height=30)
                loop = Loop(app, daemon)
                app.transcripts[SID] = s.Transcript(SID)
                lane: queue.SimpleQueue = queue.SimpleQueue()
                lane.put((s.READ, s.read_params(s.first_cursor(SID)), app.on_answer))
                lane.put((s.LIST, dict(s.LIST_PARAMS), app.on_answer))
                lane.put(None)
                app.in_flight[s.READ] += 1
                app.in_flight[s.LIST] += 1
                try:
                    loop._lane(lane)  # on this thread: it must come back, having answered both
                except BaseException as err:  # noqa: BLE001 - a KeyboardInterrupt would end the whole test run
                    self.fail(f"the lane ended on {type(err).__name__}")
                loop.tick()
                self.assertEqual((app.in_flight[s.READ], app.in_flight[s.LIST]), (0, 0))
                self.assertIn("events.read status=control_api_unavailable", "\n".join(app.errors))
                self.assertIn(raised.__name__, "\n".join(app.errors))
                self.assertIsNotNone(app.log.latest("sessions"), "the next request in the lane was answered")


class TranscriptRowsCountTest(unittest.TestCase):  # the merge of f6a747ce
    def test_a_failure_while_a_transcripts_rows_are_added_leaves_nothing_wrong(self) -> None:
        real, calls = app_module.wrap, {"n": 0}

        def failing(text, width):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("a wrap failed part way")
            return real(text, width)

        out = _Output(3, SID)  # full: both deques drop their oldest as they grow
        out.extend(["one", "x" * 30, "three"])
        out.counts(12)
        with mock.patch.object(app_module, "wrap", failing), self.assertRaises(RuntimeError):
            out.extend(["four", "y" * 40, "six"])
        self.assertEqual(out.tail(5, 12), [row for line in out.source() for row in wrap(line, 12)][-5:])


class SentencesTest(unittest.TestCase):  # L4
    def test_the_sentences_say_what_the_code_does(self) -> None:
        doc = " ".join(s.__doc__.split())
        self.assertIn("a page it refuses changes nothing", doc)
        self.assertIn("cursor_epoch_ahead", doc)
        self.assertIn("cursor_ahead", doc)
        self.assertIn("an empty page included", " ".join(s.page_problem.__doc__.split()))
        self.assertIn("is not asked for again", " ".join(TuiApp._transcript_page.__doc__.split()))


if __name__ == "__main__":
    unittest.main()
