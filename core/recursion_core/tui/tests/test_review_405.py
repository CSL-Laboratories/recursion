"""Review 1 of #405 (the session list and the transcript at ff6193c0): both reads' findings, each as a test that
fails on that head (the coverage of rules that no test held passes there by design).

GPT-6.1 Sol's read: R1 the session list in full screen, R2 a page in another projection or of another log
changes nothing, R3 the fake daemon says implemented only of what it serves; and its later points (the first
read counts toward the 50, the two default caps the tests did not pin, the epoch that never goes back).
DeepSeek V4.1 Flash's break-it read: H1 an answer whose lines is not a list wedged the TUI, M1 a lone
surrogate reached the terminal, L1 a cursor ahead of the log, L2 a line whose text is not text, L3 four
rules no test held, L4 the fake's answers to a request outside the schema, L5 the 51st read and an ok list
that holds no list, L6 three sentences.
"""

from __future__ import annotations

import copy
import importlib.util
import io
import pathlib
import unittest
from unittest import mock

from recursion_core.tui import events
from recursion_core.tui import keys
from recursion_core.tui import protocol
from recursion_core.tui import sanitize
from recursion_core.tui import sessions as s
from recursion_core.tui import term
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.keys import KeyDecoder
from recursion_core.tui.tests.fake_daemon import FakeDaemon, exchange
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_sessions import SID, new_app, shown

BAR = chr(0x2502)
SURROGATE = chr(0xD800)
ROOT = pathlib.Path(__file__).resolve().parents[4]


def frame_text(app: TuiApp) -> str:
    return "\n".join(text for text, _ in app.frame())


def pane_rows(app: TuiApp) -> list[str]:
    return [text[2:] for text, _ in app.frame() if text.startswith(BAR + " ")]


def reads(daemon: FakeDaemon) -> list[dict]:
    return [params for method, params in daemon.calls if method == s.READ]


class Answering(FakeDaemon):
    """The fake, with each answer to ``events.read`` passed through ``change`` first (a daemon that errs)."""

    def __init__(self, change, **kwargs) -> None:
        super().__init__(**kwargs)
        self.change = change

    def call(self, method, params=None):
        answer = super().call(method, params)
        return self.change(copy.deepcopy(answer)) if method == s.READ and answer.get("code") == "ok" else answer


class SessionListShownTest(unittest.TestCase):  # Sol R1
    def test_the_command_and_the_key_show_the_list_in_full_screen(self) -> None:
        for how in ("command", "key"):
            with self.subTest(how=how):
                app, _ = new_app(width=240, height=40)
                if how == "command":
                    app.command("sessions")
                else:
                    app.press("ctrl+x")
                    app.press("s")
                rows = pane_rows(app)
                self.assertEqual(rows, [s.session_row(x) for x in exchange("h27-session-list-ok")["answer"]["sessions"]])
                self.assertIn(f"session={SID}", rows[0])
                self.assertIn("-- sessions (session.list", frame_text(app))

    def test_an_empty_list_says_so(self) -> None:
        daemon = FakeDaemon()
        daemon._answers["session.list"] = {"code": "ok", "sessions": []}
        app, _ = new_app(daemon)
        app.command("sessions")
        self.assertEqual(pane_rows(app), ["no sessions listed"])

    def test_a_refused_list_says_so_in_the_pane_and_the_error_row(self) -> None:
        app, _ = new_app(FakeDaemon(implemented=frozenset()))
        app.command("sessions")
        self.assertEqual(pane_rows(app), ["nothing listed: session.list answered unsupported (the error row says why)"])
        self.assertIn("REFUSED: session.list status=unsupported", app.error_row())

    def test_attaching_a_run_or_opening_a_transcript_leaves_the_list(self) -> None:
        app, _ = new_app()
        app.command("sessions")
        app.command(f"transcript {SID}")
        self.assertIn(f"-- transcript session={SID}", frame_text(app))
        app.command("sessions")
        app.attach("run-1")
        self.assertIn("-- output run=run-1", frame_text(app))
        self.assertIsNone(app.pane)

    def test_the_pager_scrolls_the_list(self) -> None:
        daemon = FakeDaemon()
        one = exchange("h27-session-list-ok")["answer"]["sessions"][0]
        daemon._answers["session.list"] = {"code": "ok", "sessions": [dict(one, session_id=f"ses-{i:03}") for i in range(40)]}
        app, _ = new_app(daemon, height=20)
        app.command("sessions")
        self.assertIn("session=ses-039", pane_rows(app)[-1])
        app.press("tab")
        app.press("home")
        self.assertIn("session=ses-000", pane_rows(app)[-1])


class SessionListAnswersTest(unittest.TestCase):  # DeepSeek L3 (ok only) and L5 (no list)
    def test_an_ok_answer_without_a_list_is_an_error_and_lists_nothing(self) -> None:
        for sessions in ("ses-1", {"a": 1}, None, 5):
            with self.subTest(sessions=sessions):
                daemon = FakeDaemon()
                daemon._answers["session.list"] = {"code": "ok", "sessions": sessions}
                app, _ = new_app(daemon)
                app.command("sessions")
                self.assertIn("session.list answered ok without a list", app.error_row())
                self.assertIsNone(app.log.latest("sessions"))
                self.assertEqual(app.sessions, [])

    def test_a_refusal_that_carries_a_list_is_not_taken(self) -> None:
        daemon = FakeDaemon()
        daemon._answers["session.list"] = {"code": "request_invalid", "detail": "state",
                                           "sessions": exchange("h27-session-list-ok")["answer"]["sessions"]}
        app, _ = new_app(daemon)
        app.command("sessions")
        self.assertEqual(app.sessions, [])
        self.assertIsNone(app.log.latest("sessions"))


class PageCheckTest(unittest.TestCase):  # Sol R2; DeepSeek H1, L1, L2
    def refused(self, change, problem: str, *, opened_with_cursor: dict | None = None,
                daemon: FakeDaemon | None = None) -> TuiApp:
        daemon = daemon or Answering(change)
        app, _ = new_app(daemon)
        if opened_with_cursor is not None:
            app.transcripts[SID] = s.Transcript(SID)
            app.transcripts[SID].cursor = dict(opened_with_cursor)
        before = copy.deepcopy(app.transcripts[SID].cursor) if SID in app.transcripts else s.first_cursor(SID)
        app.command(f"transcript {SID}")
        self.assertIn(f"the answer was not taken ({problem})", app.error_row())
        self.assertEqual(shown(app), [], "nothing of it is shown")
        self.assertEqual(app.transcripts[SID].cursor, before, "the cursor stays")
        self.assertEqual(len(reads(daemon)), 1, "it is not asked again")
        return app

    def test_a_page_in_another_projection_changes_nothing(self) -> None:
        self.refused(lambda a: dict(a, projection="raw", more=True), "projection")
        recorded = exchange("h32-events-read-ok")  # Sol's witness: SPINE's raw page, answered to a transcript read

        class Raw(FakeDaemon):
            def call(self, method, params=None):
                if method == s.READ:
                    self.calls.append((method, params))
                    return dict(copy.deepcopy(recorded["answer"]), more=True)
                return super().call(method, params)

        self.refused(None, "projection", opened_with_cursor=recorded["params"]["cursor"], daemon=Raw())

    def test_a_page_of_another_log_changes_nothing(self) -> None:
        def other(answer):
            answer["cursor"]["session_id"] = "ses-other"
            answer["more"] = True
            return answer
        self.refused(other, "cursor")

    def test_lines_that_are_not_a_list_change_nothing_and_the_tui_goes_on(self) -> None:
        for lines in (5, True, 2.5, {"a": 1}, "abc", None):
            with self.subTest(lines=lines):
                app = self.refused(lambda a, lines=lines: dict(a, lines=lines), "lines")
                app.press("q")
                self.assertTrue(app.closed)

    def test_a_line_of_the_wrong_shape_changes_nothing(self) -> None:
        for bad in ({"text": 5}, {"speech": ["x"]}, {"event_type": None}, {"seq": True}, {"seq": "1"}):
            with self.subTest(bad=bad):
                self.refused(lambda a, bad=bad: dict(a, lines=[dict(a["lines"][0], **bad), *a["lines"][1:]]), "line")
        self.refused(lambda a: dict(a, lines=[None, *a["lines"][1:]]), "line")

    def test_a_line_out_of_order_changes_nothing(self) -> None:
        self.refused(lambda a: dict(a, lines=a["lines"][1:]), "order")  # the first position is missing
        self.refused(lambda a: dict(a, lines=[a["lines"][1], a["lines"][0], *a["lines"][2:]]), "order")

    def test_a_cursor_ahead_of_its_lines_changes_nothing(self) -> None:  # DeepSeek L1
        def ahead(answer):
            answer["cursor"]["seq"] = 10 ** 9
            return answer
        self.refused(ahead, "cursor")

    def test_an_epoch_that_goes_back_or_is_misstated_changes_nothing(self) -> None:
        def advanced_without_a_new_epoch(answer):
            answer["epoch_advanced"] = True
            return answer
        self.refused(advanced_without_a_new_epoch, "epoch")

        def back(answer):
            answer["cursor"]["epoch"] = 1
            answer["epoch_advanced"] = False
            return answer
        restarted = Answering(back)
        restarted.restart()  # the daemon is at epoch 2, and so is the read
        self.refused(back, "epoch", opened_with_cursor={"epoch": 2, "session_id": SID, "seq": 0}, daemon=restarted)

    def test_more_must_be_true_or_false(self) -> None:
        self.refused(lambda a: dict(a, more="yes"), "more")

    def test_a_right_page_is_taken_whole(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        self.assertEqual(len(shown(app)), daemon.high_water)
        self.assertNotIn("not taken", app.error_row())

    def test_an_epoch_ahead_of_the_daemon_reads_on_from_epoch_1(self) -> None:  # DeepSeek L1
        far = {"epoch": 10 ** 9}

        def future(answer):
            answer["cursor"]["epoch"] = far["epoch"]
            answer["epoch_advanced"] = True
            return answer

        daemon = Answering(future)
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")  # taken: an epoch can only be checked against the one asked
        self.assertEqual(app.transcripts[SID].cursor["epoch"], 10 ** 9)
        daemon.change = lambda answer: answer
        daemon.append("engine.output", "after\n")
        app.command(f"transcript {SID}")  # refused cursor_epoch_ahead by the daemon
        self.assertIn("goes on from seq 7 with epoch 1", app.notice)
        app.command(f"transcript {SID}")
        self.assertEqual(shown(app)[-1], "after")
        self.assertEqual(reads(daemon)[-1]["cursor"], {"epoch": 1, "session_id": SID, "seq": 6}, "checks seq 7")


class SpineAgreesTest(unittest.TestCase):  # Sol R2: the reader's check is rule (7) as SPINE states it
    def test_page_problem_agrees_with_spines_page_problems(self) -> None:
        path = ROOT / "conformance" / "rules.py"
        if not path.exists():
            self.skipTest("conformance/rules.py is not beside this core (an installed core)")
        spec = importlib.util.spec_from_file_location("spine_rules_for_tui", path)
        rules = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rules)
        daemon = FakeDaemon()
        daemon.forget(3, 4)
        params = s.read_params(s.first_cursor(SID))
        good = daemon.call(s.READ, params)
        gap = next(line for line in good["lines"] if line["event_type"] == "replay.gap")

        def page(**changes):
            out = copy.deepcopy(good)
            for key, value in changes.items():
                out[key] = value
            return out

        cases = {
            "good": page(),
            "raw": page(projection="raw"),
            "other log": page(cursor=dict(good["cursor"], session_id="ses-other")),
            "cursor short": page(cursor=dict(good["cursor"], seq=good["cursor"]["seq"] - 1)),
            "cursor long": page(cursor=dict(good["cursor"], seq=good["cursor"]["seq"] + 1)),
            "epoch back": page(cursor=dict(good["cursor"], epoch=0)),
            "advanced, same epoch": page(epoch_advanced=True),
            "later epoch said": page(cursor=dict(good["cursor"], epoch=2), epoch_advanced=True),
            "later epoch unsaid": page(cursor=dict(good["cursor"], epoch=2), epoch_advanced=False),
            "first missing": page(lines=good["lines"][1:]),
            "swapped": page(lines=[good["lines"][1], good["lines"][0], *good["lines"][2:]]),
            "doubled": page(lines=[good["lines"][0], *good["lines"]]),
            "gap seq short": page(lines=[dict(line, seq=line["seq"] - 1) if line is gap else line for line in good["lines"]]),
            "empty, cursor asked": page(lines=[], cursor=dict(good["cursor"], seq=0), more=False),
            "empty, cursor moved": page(lines=[], more=False),
        }
        for name, answer in cases.items():
            with self.subTest(case=name):
                self.assertEqual(s.page_problem(params, answer) is None, rules.page_problems(params, answer) == [],
                                 (s.page_problem(params, answer), rules.page_problems(params, answer)))
        self.assertIsNone(s.page_problem(params, good))


class AnswerGuardTest(unittest.TestCase):  # DeepSeek H1: no answer ends the loop
    def test_an_answer_that_cannot_be_read_is_an_error_and_the_loop_goes_on(self) -> None:
        daemon = FakeDaemon()
        app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=120, height=30)
        loop = Loop(app, daemon)  # driven by hand: no thread is started
        app.open()
        with mock.patch.object(TuiApp, "_session_list", side_effect=RuntimeError("boom")):
            loop._answers.put((app.on_answer, s.LIST, dict(s.LIST_PARAMS), {"code": "ok", "sessions": []}))
            loop.tick()
        self.assertIn("the answer to session.list could not be read (RuntimeError); it was not taken", app.error_row())
        loop.press("q")
        loop.tick()
        self.assertTrue(app.closed, "a key still reaches the app")

    def test_a_page_whose_lines_are_not_a_list_through_the_loop(self) -> None:
        daemon = Answering(lambda a: dict(a, lines=5))
        app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=120, height=30)
        loop = Loop(app, daemon)
        app.open()
        app.transcripts[SID] = s.Transcript(SID)
        loop._answers.put((app.on_answer, s.READ, s.read_params(s.first_cursor(SID)), daemon.call(s.READ, s.read_params(s.first_cursor(SID)))))
        loop.tick()
        self.assertIn("(lines)", app.error_row())
        loop.press("q")
        loop.tick()
        self.assertTrue(app.closed)


class SurrogateTest(unittest.TestCase):  # DeepSeek M1
    def test_a_lone_surrogate_is_marked(self) -> None:
        self.assertFalse(sanitize.visible(SURROGATE))
        row = s.rows({"text": "x" + SURROGATE + "y"})
        self.assertEqual(row, ["x" + chr(0x27E6) + "U+D800" + chr(0x27E7) + "y"])
        "\n".join(row).encode("utf-8")  # a UTF-8 terminal can be written it

    def test_a_transcript_and_engine_output_with_one_reach_a_strict_terminal(self) -> None:
        daemon = FakeDaemon()
        daemon.append("engine.output", "before " + SURROGATE + " after\n")
        out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict", newline="", write_through=True)
        self.assertEqual(term.plain(daemon, io.StringIO(f"transcript {SID}\n"), out, {"NO_COLOR": "1"}, Clock()), 0)
        app, _ = new_app()
        app.attach("run-1")
        app.on_event({"run_id": "run-1", "text": "x" + SURROGATE + "\n"})
        frame_text(app).encode("utf-8")


class FakeDaemonTest(unittest.TestCase):  # Sol R3, DeepSeek L4
    def test_every_method_listed_as_implemented_is_served(self) -> None:
        daemon = FakeDaemon()
        listed = daemon.call("methods.list")["methods"]
        built = sorted(m["name"] for m in listed if m["implemented"])
        self.assertIn("events.read", built)
        for method in built:
            with self.subTest(method=method):
                self.assertNotEqual(daemon.call(method, {}), {"code": "unsupported", "detail": "not_implemented"})
        for method in ("run.list", "run.status", "run.start", "run.stop"):
            self.assertNotIn(method, built, "the run verbs are not served here")

    def test_an_override_narrows_and_never_widens(self) -> None:
        self.assertEqual(FakeDaemon(implemented=frozenset({"run.list", "session.list"})).implemented, {"session.list"})

    def test_a_read_outside_the_schema_is_refused_not_raised(self) -> None:
        daemon = FakeDaemon()
        cursor = s.first_cursor(daemon.session_id)
        for change, detail in (({"limit": 0}, "limit"), ({"limit": -5}, "limit"), ({"limit": "abc"}, "limit"),
                               ({"limit": True}, "limit"), ({"limit": 1001}, "limit"),
                               ({"cursor": dict(cursor, seq="1")}, "cursor"), ({"cursor": dict(cursor, seq=True)}, "cursor"),
                               ({"cursor": dict(cursor, seq=-1)}, "cursor"), ({"cursor": dict(cursor, epoch=0)}, "cursor"),
                               ({"cursor": dict(cursor, session_id=5)}, "cursor"), ({"projection": "xyz"}, "projection")):
            with self.subTest(change=change):
                params = dict(s.read_params(cursor), **change)
                self.assertEqual(daemon.call(s.READ, params), {"code": "request_invalid", "detail": detail})
        self.assertEqual(daemon.call(s.READ, dict(s.read_params(cursor), projection="raw")),
                         {"code": "unsupported", "detail": "not_implemented"}, "the schema's only detail for unsupported")
        empty = daemon.call(s.READ, dict(s.read_params(dict(cursor, seq=daemon.high_water))))
        self.assertEqual((empty["lines"], empty["more"]), ([], False))


class ReadsPerOpenTest(unittest.TestCase):  # DeepSeek L5, Sol's later point
    def test_one_open_reads_at_most_max_pages_the_first_included(self) -> None:
        daemon = FakeDaemon()
        for i in range(80):
            daemon.append("engine.output", f"line {i}\n")
        app, _ = new_app(daemon)
        with mock.patch.object(s, "READ_LIMIT", 1):
            app.command(f"transcript {SID}")
        self.assertEqual(len(reads(daemon)), s.MAX_PAGES)
        self.assertEqual(s.MAX_PAGES, 50)
        self.assertIn("more lines after seq 50", app.notice)


class UnheldRulesTest(unittest.TestCase):  # DeepSeek L3, Sol's survivors
    def test_a_bool_seq_is_no_line_on_a_fresh_transcript(self) -> None:
        t = s.Transcript(SID)
        self.assertEqual(t.apply({"lines": [{"seq": True, "text": "x"}]}), [])
        self.assertEqual(t.cursor["seq"], 0)

    def test_the_epoch_never_goes_back(self) -> None:
        t = s.Transcript(SID)
        t.cursor = {"epoch": 3, "session_id": SID, "seq": 2}
        t.apply({"lines": [], "cursor": {"epoch": 2, "session_id": SID, "seq": 2}})
        self.assertEqual(t.cursor["epoch"], 3)

    def test_a_page_for_a_transcript_never_opened_is_dropped(self) -> None:
        app, daemon = new_app()
        params = s.read_params(s.first_cursor(SID))
        errors = list(app.errors)
        app.on_answer(s.READ, params, daemon.call(s.READ, params))
        self.assertEqual(shown(app), [])
        self.assertEqual(app.errors, errors, "dropped, not an error")
        self.assertEqual(len(reads(daemon)), 1, "only the one this test sent")

    def test_the_decoder_keeps_its_default_caps(self) -> None:
        self.assertEqual(KeyDecoder().feed(b"\x1b[1;" + b"0" * 61 + b"5C"), ["ctrl+right"], "64 characters")
        self.assertEqual(KeyDecoder().feed(b"\x1b[1;" + b"0" * 62 + b"5C"), [], "65 are dropped whole")
        paste = KeyDecoder().feed(b"\x1b[200~" + b"a" * 65_537 + b"\x1b[201~")[0]
        self.assertEqual((len(paste.text), paste.dropped), (65_536, 1))
        self.assertEqual((keys.MAX_SEQ, keys.PASTE_CAP), (64, 65_536))

    def test_every_event_the_session_reads_log_has_a_known_type(self) -> None:
        app, _ = new_app()
        app.command("sessions")
        app.command(f"transcript {SID}")
        logged = {e.event_type for e in app.log}
        self.assertTrue({"sessions", "transcript"} <= logged)
        self.assertLessEqual(logged, set(events.EVENT_TYPES))


class SentencesTest(unittest.TestCase):  # DeepSeek L6
    def test_the_sentences_say_what_the_code_does(self) -> None:
        self.assertNotIn("never gets a command the CLI lacks", protocol.__doc__)
        self.assertIn("session reads", protocol.__doc__)
        self.assertIn("a first read sends epoch 1 and seq 0", " ".join(s.__doc__.split()))
        for name in ("session_id", "route.engine", "route.model", "workspace.path", "active_turn_id", "turns"):
            self.assertIn(name, s.session_row.__doc__)
        self.assertNotIn("no field is interpreted", s.session_row.__doc__)

    def test_a_line_with_no_text_says_so(self) -> None:
        self.assertEqual(s.rows({"text": None, "speech": None, "event_type": None}), ["(a line with no text)"])
        self.assertEqual(s.rows({"text": 5}), ["(a line with no text)"])


if __name__ == "__main__":
    unittest.main()
