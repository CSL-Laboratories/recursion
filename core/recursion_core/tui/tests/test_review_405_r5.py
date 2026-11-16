"""Review 5 of #405 (the session reads at 7239b049) and the Liaison's ruling on it: an operator's transcript never
loses a line in silence and never shows one twice in silence. One class per case of the README's "What a
transcript relies on"; a test that fails on 7239b049 is one where that head lost a line, or (said in the test)
showed none of the marker the ruling asks for. S2 to S4 of the read are at the end.
"""

from __future__ import annotations

import collections.abc
import unittest
from pathlib import Path

from recursion_core.tui import protocol as p
from recursion_core.tui import sessions as s
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fake_daemon import FakeDaemon, gap_line
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_sessions import SID, new_app, pane_rows, shown

FAR = 10 ** 9
MARK = "-- view rebuilt: "


def texts(lines) -> list[str]:
    return ["\n".join(s.rows(line)) for line in lines]


def marks(app: TuiApp) -> list[str]:
    return [text for text in shown(app) if text.startswith(MARK)]


def opened(daemon: FakeDaemon | None = None) -> tuple[TuiApp, FakeDaemon, list[str]]:
    app, daemon = new_app(daemon)
    app.command(f"transcript {SID}")
    return app, daemon, list(shown(app))


def queued(daemon: FakeDaemon) -> tuple[TuiApp, list]:
    """An app whose requests wait until the test answers them, in order (``drain``)."""

    pending: list = []
    app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=220, height=120,
                 dispatch=lambda method, params, done: pending.append((method, params, done)))
    app.open()
    drain(daemon, pending)
    return app, pending


def drain(daemon: FakeDaemon, pending: list) -> None:
    while pending:
        method, params, done = pending.pop(0)
        done(method, params, daemon.call(method, params))


class FarGapOnce(FakeDaemon):
    """Answers the first read with one gap line that names a position far past the log."""

    told = False

    def call(self, method, params=None):
        if method == s.READ and not self.told:
            self.told = True
            self.calls.append((method, params))
            return {"code": "ok", "projection": "transcript", "lines": [gap_line(1, FAR, 0)], "more": False,
                    "epoch_advanced": False, "cursor": {"epoch": 1, "session_id": self.session_id, "seq": FAR}}
        return super().call(method, params)


class DropsOneRead(FakeDaemon):
    """The control API is lost for the ``n``-th read: it is answered as ``call_api`` answers a lost connection."""

    def __init__(self, n: int) -> None:
        super().__init__()
        self.n, self.reads = n, 0

    def call(self, method, params=None):
        if method == s.READ:
            self.reads += 1
            if self.reads == self.n:
                return {"code": p.UNAVAILABLE, "detail": "connection_lost"}
        return super().call(method, params)


class TheCheckTest(unittest.TestCase):  # what every read after a line was shown asks
    def test_a_read_after_a_line_was_shown_asks_from_the_line_before_it_for_one_line_more(self) -> None:
        app, daemon, _ = opened()
        daemon.calls.clear()
        app.command(f"transcript {SID}")
        params = daemon.calls[0][1]
        self.assertEqual((params["cursor"]["seq"], params["limit"]), (daemon.high_water - 1, s.READ_LIMIT + 1))


class CursorAheadTest(unittest.TestCase):
    def test_after_only_gap_lines_the_read_goes_back_to_the_start_and_shows_each_line_once(self) -> None:
        app, daemon, _ = opened(FarGapOnce())
        app.command(f"transcript {SID}")  # cursor_ahead at the gap's last position: no line was shown
        # 7239b049 lost and doubled nothing here; it said "from seq 0, the last line shown" (S3): red for that.
        self.assertIn(f"no position seq {FAR}; no line has been shown", app.notice)
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [f"events 1 to {FAR} are not available (retention)", s.GAP_MARK,
                                      *texts(daemon.lines)])  # the marker row after each gap line (review 6, F1)
        self.assertEqual(marks(app), [])


class CursorBehindTest(unittest.TestCase):
    def test_two_reads_in_flight_show_each_line_once(self) -> None:
        daemon = FakeDaemon()
        app, pending = queued(daemon)
        app.command(f"transcript {SID}")
        app.command(f"transcript {SID}")  # sent before the first is answered: both ask from the start
        drain(daemon, pending)
        self.assertEqual(shown(app), texts(daemon.lines))

    def test_a_consumer_that_puts_the_cursor_back_sees_nothing_twice(self) -> None:
        app, daemon, first = opened()
        app.transcripts[SID].cursor = s.first_cursor(SID)
        daemon.append("engine.output", "one more\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, "one more"])

    def test_a_read_sent_before_the_view_was_rebuilt_shows_nothing(self) -> None:  # red on 7239b049: a line lost
        daemon = FakeDaemon()
        app, pending = queued(daemon)
        app.command(f"transcript {SID}")
        drain(daemon, pending)
        first = list(shown(app))
        daemon.lines[-1] = dict(daemon.lines[-1], text="written again\n")
        app.command(f"transcript {SID}")
        app.command(f"transcript {SID}")  # two reads in flight; the first answer rebuilds the view
        drain(daemon, pending)
        self.assertEqual(len(marks(app)), 1)
        self.assertEqual(shown(app), [*first, marks(app)[0], *texts(daemon.lines)])


class EpochChangedTest(unittest.TestCase):
    def test_a_restart_over_the_same_log_reads_on_and_shows_nothing_twice(self) -> None:
        app, daemon, first = opened()
        daemon.restart()
        daemon.append("engine.output", "after the restart\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, "after the restart"])
        self.assertIn("the daemon restarted", app.notice)

    def test_a_restart_whose_log_did_not_persist_is_shown_again_below_a_marker(self) -> None:  # red: lines lost
        app, daemon, first = opened()
        daemon.lines.clear()
        daemon.restart()
        for i in range(9):
            daemon.append("engine.output", f"new {i}\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *(f"new {i}" for i in range(9))])
        self.assertEqual(len(marks(app)), 1)
        self.assertIn("seq 7 no longer holds the line shown there (epoch 2)", marks(app)[0])

    def test_a_daemon_whose_epoch_went_back_and_whose_log_differs_is_shown_again(self) -> None:  # red: lines lost
        app, daemon = new_app()
        daemon.restart()
        app.command(f"transcript {SID}")  # read in epoch 2
        first = list(shown(app))
        daemon.epoch = 1  # another store: the epoch rises from 1 again
        daemon.lines.clear()
        for i in range(9):
            daemon.append("engine.output", f"other {i}\n")
        app.command(f"transcript {SID}")  # cursor_epoch_ahead
        self.assertIn("with epoch 1, and checks the last line shown first", app.notice)
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *(f"other {i}" for i in range(9))])
        self.assertEqual(len(marks(app)), 1)


class SameEpochTruncationTest(unittest.TestCase):
    def test_a_log_that_lost_its_last_positions_is_shown_again_below_a_marker(self) -> None:
        # 7239b049 lost and doubled nothing here, and said nothing in the transcript: red for the marker only.
        app, daemon, first = opened()
        del daemon.lines[3:]
        app.command(f"transcript {SID}")  # cursor_ahead
        self.assertEqual(shown(app), [*first, *marks(app), *first[:3]])
        self.assertIn("the daemon has no position seq 6, so it no longer holds seq 7", marks(app)[0])
        self.assertIn(marks(app)[0], pane_rows(app), "full screen shows the marker in the pane too")

    def test_a_line_the_daemon_adds_after_it_lost_positions_is_shown(self) -> None:  # S1; red: the line lost
        app, daemon, first = opened()
        del daemon.lines[3:]
        app.command(f"transcript {SID}")
        daemon.append("engine.output", "added after the log was lost\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *first[:3], "added after the log was lost"])
        self.assertEqual(len(marks(app)), 1)

    def test_a_log_that_lost_positions_and_grew_past_them_is_shown_again(self) -> None:  # S1; red: lines lost
        app, daemon, first = opened()
        del daemon.lines[3:]
        for i in range(6):
            daemon.append("engine.output", f"late {i}\n")  # seq 4 to 9: no read is refused
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *first[:3], *(f"late {i}" for i in range(6))])
        self.assertEqual(len(marks(app)), 1)

    def test_a_log_that_lost_only_the_last_line_shown_ends_before_it(self) -> None:
        # 7239b049 lost and doubled nothing here, and said nothing in the transcript: red for the marker only.
        app, daemon, first = opened()
        del daemon.lines[6:]
        app.command(f"transcript {SID}")  # a valid empty page at the high-water mark
        self.assertEqual(shown(app), [*first, *marks(app), *first[:6]])
        self.assertIn("the log ends before seq 7, the last line shown (epoch 1)", marks(app)[0])


class SameEpochRewriteTest(unittest.TestCase):
    def test_the_last_line_shown_written_again_is_shown_again_below_a_marker(self) -> None:  # red: the line lost
        app, daemon, first = opened()
        daemon.lines[-1] = dict(daemon.lines[-1], text="written again\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *first[:6], "written again"])
        self.assertEqual(len(marks(app)), 1)

    def test_an_earlier_line_written_again_is_not_seen(self) -> None:
        # What the TUI cannot see (README): the store refuses this write, and noticing it would take a value per
        # line that covers the log up to it. Green on both heads; it pins the limit.
        app, daemon, first = opened()
        daemon.lines[2] = dict(daemon.lines[2], text="written again\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), first)


class EmptyStreamTest(unittest.TestCase):
    def test_an_empty_log_shows_nothing_and_then_each_line_once(self) -> None:
        daemon = FakeDaemon()
        daemon.lines.clear()
        app, _, first = opened(daemon)
        self.assertEqual(first, [])
        daemon.append("engine.output", "a\n")
        daemon.append("engine.output", "b\n")
        app.command(f"transcript {SID}")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), ["a", "b"])


class BurstTest(unittest.TestCase):
    def test_a_reconnect_in_the_middle_of_a_burst_shows_each_line_once(self) -> None:
        daemon = DropsOneRead(2)
        for i in range(450):
            daemon.append("engine.output", f"line {i}\n")
        app, _, first = opened(daemon)  # the first page, then the connection is lost
        self.assertEqual(len(first), s.READ_LIMIT)
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), texts(daemon.lines))

    def test_a_reconnect_to_a_daemon_that_lost_its_log_mid_burst_is_shown_again(self) -> None:  # red: lines lost
        daemon = DropsOneRead(2)
        for i in range(450):
            daemon.append("engine.output", f"line {i}\n")
        app, _, first = opened(daemon)
        daemon.lines.clear()
        daemon.restart()
        for i in range(300):
            daemon.append("engine.output", f"new {i}\n")
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, *marks(app), *(f"new {i}" for i in range(300))])
        self.assertEqual(len(marks(app)), 1)


class RetentionTest(unittest.TestCase):  # rule (7): retention skips positions, and a gap line says so
    def test_a_gap_line_over_the_last_line_shown_and_past_it_is_shown_and_rebuilds_nothing(self) -> None:
        app, daemon, first = opened()
        daemon.append("engine.output", "never shown\n")
        daemon.append("engine.output", "after\n")
        daemon.forget(7, 8)  # retention took the last line shown and the one after it
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app)[len(first):][-1], "after")
        self.assertEqual(len(shown(app)), len(first) + 3, "one gap line, its marker row, then the line after it")
        self.assertIn("not available", shown(app)[len(first)])
        self.assertEqual(marks(app), [])

    def test_a_gap_line_over_only_the_last_line_shown_says_nothing_new(self) -> None:
        app, daemon, first = opened()
        daemon.append("engine.output", "after\n")
        daemon.forget(7, 7)
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app), [*first, "after"])


class InFlightAcrossARebuildTest(unittest.TestCase):
    def test_a_refusal_sent_before_the_view_was_rebuilt_is_not_taken(self) -> None:
        daemon = FakeDaemon()
        app, pending = queued(daemon)
        app.command(f"transcript {SID}")
        drain(daemon, pending)
        first = list(shown(app))
        del daemon.lines[3:]
        app.command(f"transcript {SID}")
        app.command(f"transcript {SID}")  # both refused cursor_ahead; the first rebuilds the view
        drain(daemon, pending)
        self.assertEqual(shown(app), [*first, *marks(app), *first[:3]])
        self.assertEqual(len(marks(app)), 1)
        self.assertNotIn("no line has been shown", app.notice)

    def test_the_read_that_rebuilds_the_view_keeps_the_epoch_held(self) -> None:
        app, daemon, _ = opened()
        daemon.lines[-1] = dict(daemon.lines[-1], text="written again\n")
        daemon.restart()
        daemon.calls.clear()
        app.command(f"transcript {SID}")
        cursors = [params["cursor"] for method, params in daemon.calls if method == s.READ]
        self.assertEqual(cursors, [{"epoch": 1, "session_id": SID, "seq": 6}, {"epoch": 2, "session_id": SID, "seq": 0}])


class ConsumerTest(unittest.TestCase):  # Transcript.apply called by a consumer, with pages page_problem would refuse
    LINES = [{"seq": n, "event_type": "engine.output", "text": f"line {n}\n", "speech": None} for n in (1, 2, 3)]

    def page(self, lines, seq, more=False, session_id=SID):
        return {"lines": lines, "more": more, "cursor": {"epoch": 1, "session_id": session_id, "seq": seq}}

    def test_a_page_of_another_log_shows_nothing_and_changes_nothing(self) -> None:
        transcript = s.Transcript(SID)
        transcript.apply(self.page(self.LINES, 3))
        self.assertEqual(transcript.apply(self.page([], 9, session_id="ses-another")), [])
        self.assertEqual((transcript.cursor["seq"], transcript.last_seq, transcript.broken), (3, 3, None))

    def test_a_page_that_goes_past_the_last_line_shown_with_nothing_there_is_a_broken_log(self) -> None:
        transcript = s.Transcript(SID)
        transcript.apply(self.page(self.LINES[:2], 2))
        self.assertIsNone(transcript.apply(self.page(self.LINES[2:], 3)))
        self.assertIn("goes past seq 2, the last line shown", transcript.broken)

    def test_a_page_that_ends_before_the_last_line_shown_concludes_only_when_it_is_the_last(self) -> None:
        transcript = s.Transcript(SID)
        transcript.apply(self.page(self.LINES, 3))
        self.assertEqual(transcript.apply(self.page(self.LINES[:1], 1, more=True), asked=0), [])
        self.assertIsNone(transcript.broken)
        self.assertIsNone(transcript.apply(self.page(self.LINES[:1], 1), asked=0))
        self.assertIn("the log ends before seq 3", transcript.broken)


class ReadS2toS4Test(unittest.TestCase):
    def test_a_taken_answer_is_copied_once(self) -> None:  # S4: M7
        class Counted(collections.abc.Mapping):
            def __init__(self, data: dict) -> None:
                self.data, self.copies = data, 0

            def __getitem__(self, key):
                return self.data[key]

            def __len__(self):
                return len(self.data)

            def __iter__(self):
                return iter(self.data)

            def keys(self):  # dict() copies a mapping by its keys
                self.copies += 1
                return super().keys()

        answer = Counted({"code": "ok", "sessions": []})
        app, _ = new_app()
        app.on_answer(s.LIST, dict(s.LIST_PARAMS), answer)
        self.assertEqual(answer.copies, 1, "the plain copy made before the lock is the one kept")
        self.assertIs(type(app.last), dict)

    def test_a_consumers_answer_with_an_epoch_that_is_not_an_integer_raises_nothing(self) -> None:  # S4: M11
        transcript = s.Transcript(SID)
        line = {"seq": 1, "event_type": "engine.output", "text": "one\n", "speech": None}
        page = {"lines": [line], "cursor": {"epoch": "2", "session_id": SID, "seq": 1}}
        self.assertEqual(len(transcript.apply(page)), 1)
        self.assertEqual(transcript.cursor["epoch"], 1)

    def test_the_sentences_say_what_the_code_does(self) -> None:  # S1, S2
        doc = " ".join(s.__doc__.split())
        self.assertIn("never loses a line in silence and never shows one twice in silence", doc)
        self.assertIn("the README's \"What a transcript relies on\"", doc)
        readme = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8").split())
        self.assertIn("## What a transcript relies on", readme)
        self.assertIn("lines 115-124 and 349-352", readme)
        self.assertIn("`schemas/control/v1/common.v1.json` line 245", readme)
        self.assertIn("the answer itself is still read under the lock", readme)  # S2
        self.assertNotIn("so such an answer never holds it", readme)
        self.assertNotIn("a later epoch is a log of its own", readme)
        self.assertIn("What was shown stays above the marker", " ".join(TuiApp._rebuild.__doc__.split()))


if __name__ == "__main__":
    unittest.main()
