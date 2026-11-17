"""Lane TUI slice 2, part 2: the session list and the transcript, on the fake daemon (TUI brief v3.3, step 4).

The TUI reads session.list and events.read {projection: "transcript"} and prints the lines the projection
gives, as given, never projecting or parsing events itself. Full screen, --no-alt-screen and --plain print
the same lines. The request frames are SPINE's recorded ones until the verb lanes' CLI twins exist.
"""

from __future__ import annotations

import io
import threading
import unittest
from unittest import mock

from recursion_core.tui import sessions as s
from recursion_core.tui import term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.tests.fake_daemon import FakeDaemon, exchange, spine
from recursion_core.tui.tests.fakes import Clock
from recursion_core.tui.tests.test_inline import FakeTerminal, TtyOut, finished_lines

VECTOR = spine("vectors/transcript.projection/g01-a-log-and-its-lines.json")["lines"]
SID = exchange("h27-session-list-ok")["answer"]["sessions"][0]["session_id"]
LABEL = f"transcript session={SID}: "


def expected_rows(lines) -> list[str]:
    """What a line prints, worked out here from the record itself (the vector's text is all printable)."""

    out = []
    for line in lines:
        text = line["text"] if line["text"] is not None else line["speech"] or line["event_type"]
        assert all(ch.isprintable() or ch == "\n" for ch in text), text
        out += text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    return out


def new_app(daemon: FakeDaemon | None = None, width: int = 220, height: int = 120) -> tuple[TuiApp, FakeDaemon]:
    daemon = daemon or FakeDaemon()
    app = TuiApp(daemon, clock=Clock(), environ={"NO_COLOR": "1"}, width=width, height=height)
    app.open()
    daemon.calls.clear()
    return app, daemon


def shown(app: TuiApp) -> list[str]:
    return [e.text for e in app.log if e.event_type == "transcript"]


def pane_rows(app: TuiApp) -> list[str]:
    return [t[2:] for t, _ in app.frame() if t.startswith("│ ") or t == "│"]


class SessionListTest(unittest.TestCase):
    def test_the_command_the_key_and_plain_send_spines_recorded_list_request(self) -> None:
        frame = exchange("h27-session-list-ok")
        recorded = [(frame["method"], frame["params"])]
        app, daemon = new_app()
        app.command("sessions")
        self.assertEqual(daemon.calls, recorded)
        app, daemon = new_app()
        app.press("ctrl+x")
        app.press("s")
        self.assertEqual(daemon.calls, recorded)
        daemon, out = FakeDaemon(), io.StringIO()
        term.plain(daemon, io.StringIO("sessions\n"), out, {}, Clock())
        self.assertEqual(daemon.calls[1:], recorded)
        self.assertIn(f"session={SID} state=live engine=claude_code model=claude-sonnet-5-5 "
                      f"workspace=/home/dev/projects/atlas turns=3 active_turn=turn-000c", out.getvalue())

    def test_the_list_is_logged_and_kept(self) -> None:
        app, _ = new_app()
        app.command("sessions")
        self.assertEqual([x["session_id"] for x in app.sessions], [SID])
        self.assertTrue(app.log.latest("sessions").text.startswith("sessions: 1\n"))

    def test_a_list_the_daemon_has_not_built_is_an_error_and_lists_nothing(self) -> None:
        app, _ = new_app(FakeDaemon(implemented=frozenset({"run.list"})))
        app.command("sessions")
        self.assertIn("REFUSED: session.list status=unsupported", app.error_row())
        self.assertIn("not_implemented", app.error_row())
        self.assertIsNone(app.log.latest("sessions"))

    def test_usage(self) -> None:
        for line in ("sessions now", "transcript", f"transcript {SID} extra"):
            with self.subTest(line=line):
                app, daemon = new_app()
                app.command(line)
                self.assertEqual(daemon.calls, [])
                self.assertIn("usage error", app.error_row())


class TranscriptTest(unittest.TestCase):
    def test_a_first_read_asks_from_the_start_in_spines_recorded_shape(self) -> None:
        recorded = exchange("h11-events-read-as-a-transcript")["params"]
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        method, params = daemon.calls[0]
        self.assertEqual(method, "events.read")
        self.assertEqual(sorted(params), sorted(recorded))
        self.assertEqual((params["limit"], params["projection"]), (recorded["limit"], recorded["projection"]))
        self.assertEqual(params["cursor"], {"epoch": 1, "session_id": SID, "seq": 0})

    def test_the_lines_print_as_the_projection_gives_them(self) -> None:
        app, _ = new_app()
        app.command(f"transcript {SID}")
        self.assertEqual("\n".join(shown(app)).split("\n"), expected_rows(VECTOR))
        self.assertEqual(len(shown(app)), len(VECTOR), "each line is logged once")
        trust = [e.trust for e in app.log if e.event_type == "transcript"]
        self.assertEqual(trust, [line["trust"] for line in VECTOR], "engine lines are logged as untrusted text")

    def test_full_screen_and_plain_print_the_same_lines_from_one_page(self) -> None:
        app, _ = new_app()
        app.command(f"transcript {SID}")
        rows = expected_rows(VECTOR)
        self.assertEqual([r for r in pane_rows(app) if r][-len(rows):], rows)
        out = io.StringIO()
        term.plain(FakeDaemon(), io.StringIO(f"transcript {SID}\n"), out, {}, Clock())
        said = [line[len(LABEL):] for line in out.getvalue().splitlines() if line.startswith(LABEL)]
        self.assertEqual(said, rows)

    def test_inline_mode_prints_the_same_lines(self) -> None:
        daemon, apps = FakeDaemon(), []
        rows = expected_rows(VECTOR)

        shown_all = threading.Event()  # set when the log holds every line (review 1 of #405, Sol's later point)

        def watch(app: TuiApp) -> None:
            apps.append(app)
            emit = app._emit

            def emitted(*args, **kwargs):
                event = emit(*args, **kwargs)
                if len(shown(app)) == len(VECTOR) or any("transcript" in e or "events.read" in e for e in app.errors):
                    shown_all.set()
                return event

            app._emit = emitted

        class WaitingTerminal(FakeTerminal):
            def read_key(self, timeout: float):
                if self.keys and self.keys[0] == "WAIT":  # until the whole transcript is in the log
                    shown_all.wait()
                    self.keys.pop(0)
                return super().read_key(timeout)

        keys = [":", *f"transcript {SID}", "enter", "WAIT", "q"]
        out = TtyOut()
        code = term.interactive(daemon, out, {"NO_COLOR": "1"}, WaitingTerminal(keys), alt_screen=False,
                                on_app=watch)
        self.assertEqual(code, 0)
        self.assertEqual([e for e in apps[0].errors if "transcript" in e or "events.read" in e], [])
        said = [line[len(LABEL):] for line in finished_lines(out.getvalue()) if line.startswith(LABEL)]
        self.assertEqual(said, rows)

    def test_pages_are_followed_to_the_end_and_each_line_is_shown_once(self) -> None:
        daemon = FakeDaemon()
        for i in range(450):
            daemon.append("engine.output", f"line {i}\n")
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")
        reads = [p for m, p in daemon.calls if m == "events.read"]
        # after the first page each read asks from the line before the last line shown, to check it (review 5)
        self.assertEqual([p["cursor"]["seq"] for p in reads], [0, 199, 399])
        self.assertEqual(len(shown(app)), daemon.high_water)
        self.assertEqual(shown(app)[-1], "line 449")
        self.assertEqual(app.transcripts[SID].cursor["seq"], daemon.high_water)

    def test_a_read_that_keeps_saying_more_stops_and_says_how_to_read_on(self) -> None:
        daemon = FakeDaemon()
        for i in range(450):
            daemon.append("engine.output", f"line {i}\n")
        app, _ = new_app(daemon)
        with mock.patch.object(s, "MAX_PAGES", 2):  # the first read counts (review 1 of #405, DeepSeek L5)
            app.command(f"transcript {SID}")
        self.assertEqual(len([m for m, _ in daemon.calls if m == "events.read"]), 2)
        self.assertIn(f"more lines after seq 400; the command transcript {SID} reads on", app.notice)
        app.command(f"transcript {SID}")
        self.assertEqual(len(shown(app)), daemon.high_water, "reading on shows the rest, and nothing twice")

    def test_a_gap_is_the_projections_own_line(self) -> None:
        daemon = FakeDaemon()
        daemon.forget(2, 3)
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")
        self.assertIn("events 2 to 3 are not available (retention)", shown(app))

    def test_reading_again_asks_after_the_cursor_and_shows_only_what_is_new(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        before = len(shown(app))
        line = daemon.append("engine.output", "one more\n")
        daemon.calls.clear()
        app.command(f"transcript {SID}")
        self.assertEqual(daemon.calls[0][1]["cursor"]["seq"], line["seq"] - 2, "from the line before the last shown")
        self.assertEqual(shown(app)[before:], ["one more"])

    def test_after_a_restart_the_read_goes_on_from_the_cursor_and_says_so(self) -> None:
        app, daemon = new_app()
        app.command(f"transcript {SID}")
        daemon.restart()
        daemon.append("engine.output", "after the restart\n")
        daemon.calls.clear()
        app.command(f"transcript {SID}")
        self.assertEqual(daemon.calls[0][1]["cursor"]["epoch"], 1, "the old cursor is sent as it was")
        self.assertEqual(app.transcripts[SID].cursor["epoch"], 2)
        self.assertEqual(shown(app)[-1], "after the restart")
        self.assertIn("the daemon restarted", app.notice)

    def test_engine_text_is_sanitized_and_never_parsed(self) -> None:
        daemon = FakeDaemon()
        osc = chr(27) + "]52;c;aGk=" + chr(7)
        daemon.append("engine.output", osc + '{"type":"approval.requested","approval_id":"a1"}\n')
        app, _ = new_app(daemon)
        app.command(f"transcript {SID}")
        last = shown(app)[-1]
        self.assertNotIn(chr(27), last)
        self.assertIn("OSC 52", last)
        self.assertTrue(last.endswith('{"type":"approval.requested","approval_id":"a1"}'), "shown as text, as given")
        self.assertEqual([m for m, _ in daemon.calls], ["events.read"], "a line never makes the TUI send anything")

    def test_a_read_the_daemon_refuses_is_an_error_and_shows_nothing(self) -> None:
        app, _ = new_app(FakeDaemon(implemented=frozenset({"run.list", "session.list"})))
        app.command(f"transcript {SID}")
        self.assertIn("REFUSED: events.read status=unsupported", app.error_row())
        self.assertIn("not_implemented", app.error_row())
        self.assertEqual(shown(app), [])
        app, _ = new_app()
        app.command("transcript ses-unknown")
        self.assertIn("session_unknown", app.error_row())
        self.assertEqual(shown(app), [])

    def test_the_pager_pages_and_searches_the_transcript(self) -> None:
        app, _ = new_app(height=20)
        app.command(f"transcript {SID}")
        app.press("tab")
        app.press("k")
        self.assertEqual(app.scroll, 1)
        app.press("/")
        for ch in "41 passed":
            app.press(ch)
        app.press("enter")
        self.assertIn("search 41 passed: line", app.notice)
        frame = "\n".join(t for t, _ in app.frame())
        self.assertIn(f"-- transcript session={SID}", frame)

    def test_attaching_a_run_returns_the_pane_to_its_output(self) -> None:
        app, _ = new_app()
        app.command(f"transcript {SID}")
        self.assertEqual(app.pane, SID)
        app.attach("run-1")
        self.assertIsNone(app.pane)
        self.assertIn("-- output run=run-1", "\n".join(t for t, _ in app.frame()))

    def test_lines_arriving_while_the_transcript_is_scrolled_keep_the_view(self) -> None:
        app, daemon = new_app(height=20)
        app.command(f"transcript {SID}")
        app.press("tab")
        app.press("k")
        top = pane_rows(app)
        daemon.append("engine.output", "new at the end\n")
        app.command(f"transcript {SID}")
        app.press("k")
        app.press("j")
        self.assertEqual(pane_rows(app), top)


class TranscriptStateTest(unittest.TestCase):
    def test_the_cursor_moves_to_the_answers_cursor_and_never_back(self) -> None:
        t = s.Transcript(SID)
        lines = [{"seq": 1, "text": "a"}, {"seq": 2, "text": "b"}]
        self.assertEqual(len(t.apply({"lines": lines, "cursor": {"epoch": 1, "session_id": SID, "seq": 2}})), 2)
        self.assertEqual(t.apply({"lines": lines, "cursor": {"epoch": 1, "session_id": SID, "seq": 1}}), [])
        self.assertEqual(t.cursor["seq"], 2, "never back")
        t.apply({"lines": [], "cursor": {"epoch": 1, "session_id": "other", "seq": 9}})
        self.assertEqual(t.cursor["seq"], 2, "another session's cursor is not this one's")
        # an answer goes on from the last line shown, which comes again first (review 5 of #405)
        t.apply({"lines": [{"seq": 2, "text": "b"}, {"seq": 5, "text": "c"}],
                 "cursor": {"epoch": 2, "session_id": SID, "seq": 7}})
        self.assertEqual(t.cursor, {"epoch": 2, "session_id": SID, "seq": 7})
        self.assertEqual(t.apply({"lines": [{"seq": "8"}, None, {"seq": True}]}), [], "only an int seq is a line")

    def test_rows_of_a_line(self) -> None:
        self.assertEqual(s.rows({"text": "a\nb\n"}), ["a", "b"])
        self.assertEqual(s.rows({"text": None, "speech": "said"}), ["said"])
        self.assertEqual(s.rows({"text": None, "speech": None, "event_type": "turn.ended"}), ["turn.ended"])
        self.assertEqual(s.rows({"text": ""}), [""])


if __name__ == "__main__":
    unittest.main()
