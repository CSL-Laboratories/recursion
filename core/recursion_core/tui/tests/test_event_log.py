"""The one event log as the CI form of O7's TUI transcript evidence (CSL-799 correction, item 5).

One JSON Lines golden per condition in tests/transcripts/: refusal, uncertainty, stale with watch_only,
unreconciled, unavailable, and an error that stays on screen. Each scenario runs on the fake clock, so
``time_rel_ms`` is stable. Every golden also checks that the full-screen frame and the linear renderer
say the same words, both read from this log. To regenerate after a deliberate change:
RECURSION_TUI_REGEN=1 PYTHONPATH=core python3 -B -m unittest recursion_core.tui.tests.test_event_log
"""

from __future__ import annotations

import io
import json
import unittest

from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui import term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.events import EVENT_TYPES, FIELDS, SOURCES, EventLog, from_jsonl, speech_lines, to_jsonl
from recursion_core.tui.tests.fakes import Clock, FakeDriver
from recursion_core.tui.tests.test_o7_text import FlakyAPI, golden
from recursion_core.tui.view import to_text


def new_app(confirm=(EMPTY,), runs: int = 1, pending_run: bool = False) -> tuple[TuiApp, FlakyAPI, Clock]:
    driver = FakeDriver(confirm_answers=list(confirm))
    for _ in range(runs):
        driver.start_run()
    if pending_run:
        driver.api.call("run.stop", {"run_id": f"run-{runs}"})
    api, clock = FlakyAPI(driver.api), Clock()
    return TuiApp(api, clock=clock, environ={"NO_COLOR": "1"}, width=100, height=30), api, clock


def refusal(app, api, clock):
    app.open()
    clock.now += 0.25
    app.command("stop nope")
    clock.now += 0.25
    app.command("list")  # a successful answer clears REFUSED; the error stays


def uncertainty(app, api, clock):
    app.open()
    app.attach("run-1")
    clock.now += 0.5
    app.press("s")
    app.press("y")  # pending: cgroup still populated
    clock.now += 0.5
    app.command("status run-1")  # still stop_pending: nothing new is said
    clock.now += 0.5
    app.command("stop run-1")  # now confirmed: UNCERTAIN and UNRECONCILED clear


def stale_watch_only(app, api, clock):
    app.open()
    clock.now += 30
    app.command("conditions")  # STALE appears with time alone, then the on-demand readout
    app.command("start --tenant t1 --engine codex --prompt hi")  # refused locally while watch_only
    app.command("stop run-1")  # stop still goes
    app.command("list")  # a fresh read clears STALE


def unreconciled(app, api, clock):
    app.open()  # run-2 is stop_pending: UNRECONCILED appears once
    for _ in range(3):
        clock.now += 1
        app.command("list")  # and is not said again
    app.command("status run-2")
    app.command("conditions")


def unavailable(app, api, clock):
    app.open()
    api.down = True
    clock.now += 1
    app.command("list")  # UNAVAILABLE and STALE (refresh failed) appear
    app.command("list")  # same conditions: nothing new is said
    api.down = False
    clock.now += 1
    app.command("list")  # both clear


def error_stays(app, api, clock):
    app.open()
    app.attach("run-1")
    app.command("stop nope")
    for i in range(3):
        clock.now += 0.1
        app.on_event({"run_id": "run-1", "text": f"engine line {i} \x1b]52;c;ZXZpbA==\x07\n"})
    app.on_event({"run_id": "run-1", "text": "partial "})
    app.on_event({"run_id": "run-1", "text": "line\n"})
    app.press("r")


SCENARIOS = {
    "refusal": (refusal, {}, p.REFUSED),
    "uncertainty": (uncertainty, {"confirm": [POPULATED, EMPTY]}, p.UNCERTAIN),
    "stale_watch_only": (stale_watch_only, {}, p.STALE),
    "unreconciled": (unreconciled, {"confirm": [POPULATED], "runs": 2, "pending_run": True}, p.UNRECONCILED),
    "unavailable": (unavailable, {}, p.UNAVAILABLE_LABEL),
    "error_stays": (error_stays, {}, None),
}


def run_scenario(name: str) -> TuiApp:
    script, kwargs, _ = SCENARIOS[name]
    app, api, clock = new_app(**kwargs)
    script(app, api, clock)
    app.frame()  # the full-screen view reads the same log
    return app


class EventLogGoldenTest(unittest.TestCase):
    def test_each_condition_has_a_jsonl_golden(self) -> None:
        for name, (_, _, label) in SCENARIOS.items():
            with self.subTest(name):
                app = run_scenario(name)
                text = to_jsonl(app.log)
                self.assertEqual(text, golden(f"{name}.jsonl", text))
                events = from_jsonl(text)
                self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))
                for line in text.splitlines():
                    self.assertEqual(tuple(json.loads(line)), FIELDS)
                for e in events:
                    self.assertIn(e.event_type, EVENT_TYPES)
                    self.assertIn(e.source, SOURCES)
                    self.assertEqual(e.trust, "untrusted" if e.source == "engine" else "trusted")
                    self.assertIn(e.state, ("live", "watch_only", "closed"))
                    self.assertNotIn("\x1b", e.text + e.speech)
                if label:
                    said = [e.speech for e in events if e.event_type == "condition"]
                    self.assertTrue(any(s.startswith(f"{label}: ") for s in said), said)

    def test_conditions_are_logged_only_on_an_edge(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                current: dict[str, str] = {}
                for e in run_scenario(name).log:
                    if e.event_type != "condition":
                        continue
                    head, body = e.speech.split(": ", 1)
                    label = head.split(" ")[0]
                    subject = body.split(" ")[0] if label in (p.UNRECONCILED, p.UNCERTAIN) else ""
                    if label == p.UNCERTAIN:
                        subject = body.split("for ")[1].split(":")[0]
                    key = (label, subject)
                    if head.endswith("cleared, was"):
                        self.assertEqual(current.pop(key, None), body, f"cleared what was not said: {e.speech}")
                    else:
                        self.assertNotEqual(current.get(key), body, f"said twice without a change: {e.speech}")
                        current[key] = body

    def test_unreconciled_is_said_once_while_it_lasts(self) -> None:
        app = run_scenario("unreconciled")
        said = [e for e in app.log if e.event_type == "condition"]
        self.assertEqual(len(said), 1)
        self.assertTrue(said[0].speech.startswith("UNRECONCILED: run=run-2 state=stop_pending"))
        readout = [e for e in app.log if e.event_type == "conditions"]
        self.assertEqual(readout[-1].speech.splitlines()[0], "conditions now: 1")

    def test_frame_and_linear_renderer_use_the_same_words(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                app = run_scenario(name)
                frame = to_text(app.frame())
                linear = speech_lines(app.log)
                for label, text in app.shown.values():
                    row = f"{label}: {text}"
                    self.assertTrue(any(line == row for line in linear), row)
                    self.assertTrue(any(r and row.startswith(r.rstrip("…")) for r in frame.splitlines()), row)
                if app.last_error:
                    self.assertIn(app.last_error.text, linear)
                    self.assertTrue(any(app.last_error.text.startswith(r.rstrip("…"))
                                        for r in frame.splitlines() if r.startswith("error ")))

    def test_error_stays_until_replaced(self) -> None:
        app = run_scenario("error_stays")
        rows = [t for t, _ in app.frame()]
        self.assertTrue(any(r.startswith("error 1: REFUSED: run.stop status=run_unknown") for r in rows), rows)
        self.assertEqual(app.last["code"], "ok", "later successful answers do not clear it")
        output = [e for e in app.log if e.event_type == "output"]
        self.assertEqual({(e.source, e.trust) for e in output}, {("engine", "untrusted")})
        self.assertIn("⟦OSC 52⟧", output[0].text)
        self.assertEqual(output[-2].speech, "", "a partial line is not said until it finishes")
        self.assertEqual(output[-1].speech, "output run=run-1: partial line")

    def test_time_comes_from_the_injected_clock(self) -> None:
        app = run_scenario("stale_watch_only")
        self.assertEqual(app.log.latest("open").time_rel_ms, 0)
        self.assertEqual(app.log.latest("conditions").time_rel_ms, 30_000)


class LinearRendererTest(unittest.TestCase):
    def test_usage_error_says_only_the_error(self) -> None:
        app, api, _ = new_app(confirm=[POPULATED], runs=2, pending_run=True)
        out = io.StringIO()
        term.plain(api, iter(["approve run-1\n", "stop\n", "q\n"]), out, {}, app=app)
        lines = out.getvalue().splitlines()
        i = next(n for n, x in enumerate(lines) if x.startswith("error 1: usage error"))
        self.assertTrue(lines[i + 1].startswith("error 2: usage error"))
        self.assertTrue(lines[i + 2].startswith("closed: "))
        self.assertEqual(sum(x.startswith("UNRECONCILED: ") for x in lines), 1)

    def test_screen_reader_is_another_name_for_plain(self) -> None:
        outs = []
        for flag in ("--plain", "--screen-reader"):
            driver = FakeDriver()
            driver.start_run()
            out = io.StringIO()

            class Tty(io.StringIO):
                def isatty(self) -> bool:
                    return True

            self.assertEqual(term.main([flag], lambda: driver.api, stdin=Tty("list\nconditions\nq\n"), stdout=out,
                                       environ={}, clock=Clock()), 0)
            outs.append(out.getvalue())
        self.assertEqual(outs[0], outs[1])
        self.assertIn("conditions now: none", outs[0])
        self.assertNotIn("\x1b", outs[0])

    def test_unknown_flag_is_a_usage_error(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(term.main(["--approve"], lambda: None, stdin=io.StringIO(""), stdout=out, environ={},
                                   stderr=err), 2)
        self.assertIn("usage error: unrecognized arguments: --approve", err.getvalue())
        self.assertEqual(out.getvalue(), "", "a diagnostic is not part of the transcript stream")

    def test_a_reader_that_falls_behind_a_bounded_log_is_told(self) -> None:
        log = EventLog(cap=3)
        for i in range(5):
            log.append(i, "notice", "recursion", "live", f"n{i}", f"n{i}", "runs")
        events, skipped = log.since(0)
        self.assertEqual(([e.text for e in events], skipped), (["n2", "n3", "n4"], 2))
        self.assertEqual(log.since(4), ([log.latest("notice")], 0))


if __name__ == "__main__":
    unittest.main()
