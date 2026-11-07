"""Required test 7 and the O7 TUI packet transcripts: NO_COLOR and stale watch_only snapshots, refusal
shown as text, an error that stays on screen, and screen-reader / NO_COLOR transcripts in which
refusal, uncertainty, stale and unreconciled states stay readable as text.

Golden transcripts live in tests/transcripts/. To regenerate after a deliberate change:
RECURSION_TUI_REGEN=1 PYTHONPATH=core python3 -B -m unittest recursion_core.tui.tests.test_o7_text
"""

from __future__ import annotations

import io
import os
import unittest

from recursion_core.control.registry import EMPTY, POPULATED
from recursion_core.tui import protocol as p
from recursion_core.tui import term
from recursion_core.tui.app import TuiApp
from recursion_core.tui.events import speech_lines
from recursion_core.tui.tests.fakes import TRANSCRIPTS, Clock, FakeDriver, ScriptedAPI, fixture
from recursion_core.tui.view import to_ansi, to_text

ANSWERS = fixture("answers.json")["cases"]


class FlakyAPI:
    """The real control API over fakes, whose transport can be cut (a refresh failure)."""

    def __init__(self, inner) -> None:
        self.inner, self.down = inner, False

    def call(self, method, params=None):
        if self.down:
            raise ConnectionError("daemon socket closed")
        return self.inner.call(method, params)


def scenario_driver() -> tuple[FakeDriver, FlakyAPI]:
    # run-1 confirms on its second check; run-2 stays populated (stop_pending, unreconciled).
    driver = FakeDriver(confirm_answers=[POPULATED, POPULATED, POPULATED, EMPTY])
    driver.start_run()
    driver.start_run()
    driver.api.call("run.stop", {"run_id": "run-2"})
    return driver, FlakyAPI(driver.api)


def frame_scenario() -> str:
    driver, api = scenario_driver()
    clock = Clock()
    app = TuiApp(api, clock=clock, environ={"NO_COLOR": "1"}, width=100, height=30, stale_after_s=10.0)
    shots = []

    def shot(title: str) -> None:
        shots.append(f"=== {title}\n{to_text(app.frame())}")

    app.open()
    app.attach("run-1")
    app.on_event({"run_id": "run-1", "text": "engine says \x1b]0;title\x07hi\n\x1b]52;c;ZXZpbA==\x07done\n"})
    shot("open and attach run-1; run-2 is stop_pending (UNRECONCILED)")
    app.press("s")
    app.press("y")
    shot("stop run-1 answered pending (UNCERTAIN)")
    app.command("stop nope")
    shot("stop of an unknown run is refused (REFUSED, error row)")
    clock.now += 30
    api.down = True
    app.press("r")
    shot("refresh failed and snapshot is 30s old (UNAVAILABLE, STALE, watch_only)")
    app.command("start --tenant t1 --engine codex --prompt hi")
    shot("start refused locally while watch_only; stop still allowed")
    for i in range(500):
        app.on_event({"run_id": "run-1", "text": f"flood line {i}\n"})
    shot("after 500 output lines the latest error is still on screen")
    return "\n".join(shots) + "\n=== transcript (append-only, the log's text)\n" + "\n".join(app.transcript) + "\n"


def screen_reader_scenario() -> tuple[str, TuiApp]:
    driver, api = scenario_driver()
    clock = Clock()
    app = TuiApp(api, clock=clock, environ={"NO_COLOR": "1"})

    def lines():
        yield "list\n"
        yield "status run-1\n"
        yield "stop run-1\n"
        yield "stop nope\n"
        yield "approve run-1\n"
        clock.now += 30
        api.down = True
        yield "list\n"
        yield "start --tenant t1 --engine codex --prompt hi\n"
        yield "conditions\n"
        api.down = False
        yield "list\n"
        yield "q\n"

    out = io.StringIO()
    term.plain(api, lines(), out, {"NO_COLOR": "1"}, app=app)
    return out.getvalue(), app


def golden(name: str, text: str) -> str:
    path = TRANSCRIPTS / name
    if os.environ.get("RECURSION_TUI_REGEN") == "1":
        path.write_text(text, encoding="utf-8")
    return path.read_text(encoding="utf-8")


class TranscriptTest(unittest.TestCase):
    def test_no_color_frames_match_golden(self) -> None:
        text = frame_scenario()
        self.assertEqual(text, golden("no_color_frames.txt", text))
        for label in (p.REFUSED, p.UNCERTAIN, p.STALE, p.UNRECONCILED, p.UNAVAILABLE_LABEL):
            self.assertIn(f"{label}: ", text)
        self.assertNotIn("\x1b", text)

    def test_screen_reader_transcript_matches_golden(self) -> None:
        text, app = screen_reader_scenario()
        self.assertEqual(text, golden("screen_reader.txt", text))
        self.assertEqual(text, "".join(line + "\n" for line in speech_lines(app.log)),
                         "the linear renderer says exactly the log's speech, in order")
        for needle in ("REFUSED: run_unknown nope", "UNCERTAIN: stop not confirmed for run=run-1",
                       "UNRECONCILED: run=run-2 state=stop_pending", "STALE: watch_only (start disabled; stop and reads work): refresh failed",
                       "UNAVAILABLE: control API unavailable", "watch_only", "usage error",
                       "closed: no request sent; runs keep going"):
            self.assertIn(needle, text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)

    def test_screen_reader_announces_a_condition_only_on_an_edge(self) -> None:
        text, _ = screen_reader_scenario()
        lines = text.splitlines()
        readout = lines.index(next(x for x in lines if x.startswith("conditions now: ")))
        announced = lines[:readout] + lines[readout + 6:]  # outside the on-demand readout
        appeared = [x for x in announced if x.startswith("UNRECONCILED: run=run-2 ")]
        self.assertEqual(len(appeared), 1, "run-2 stays stop_pending; it is announced once, when it appears")
        usage = lines.index(next(x for x in lines if "usage error" in x))
        # After the usage error the clock moved 30 s, so the next line is STALE appearing, not a repeat.
        self.assertTrue(lines[usage + 1].startswith("STALE: "), lines[usage + 1])
        self.assertNotIn(lines[usage + 1], lines[:usage], "a usage error sends nothing and repeats nothing")
        self.assertIn("STALE cleared, was: watch_only", text)
        self.assertIn("UNAVAILABLE cleared, was: control API unavailable", text)
        self.assertEqual(lines[readout], "conditions now: 5")
        self.assertEqual(sum(x.startswith("UNRECONCILED: run=run-2 ") for x in lines[readout + 1:readout + 6]), 1,
                         "the conditions command says run-2 again on demand")
        self.assertEqual([x.split(":")[0] for x in lines[readout + 1:readout + 6]],
                         ["UNAVAILABLE", "UNRECONCILED", "UNRECONCILED", "STALE", "UNCERTAIN"])
        self.assertNotIn("UNCERTAIN cleared", text, "run-1's stop is still unconfirmed; other answers do not clear it")


class AnswerTextTest(unittest.TestCase):
    def test_every_answer_shows_its_code_and_conditions_as_text(self) -> None:
        for case in ANSWERS:
            with self.subTest(case["name"]):
                api = ScriptedAPI({"run.list": {"code": "ok", "runs": [{"run_id": "run-1", "state": "running"}]},
                                   case["method"]: case["answer"]})
                app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=120)
                app.open()
                app.request(case["method"], {"run_id": "run-1"} if case["method"] != "run.list" else {})
                frame = to_text(app.frame())
                status_row = app.frame()[1][0]
                self.assertTrue(status_row.startswith(case["status_text"]), status_row)
                self.assertEqual(sorted({lab for lab, _ in app.conditions()}), sorted(case["labels"]))
                for label in case["labels"]:
                    self.assertIn(f"{label}: ", frame)
                for needle in case.get("must_show", []):
                    self.assertIn(needle, frame)
                self.assertIn(case["status_text"], "\n".join(app.transcript))


class ColourTest(unittest.TestCase):
    def frame_after_refusal(self, environ):
        app = TuiApp(ScriptedAPI({"run.list": {"code": "ok", "runs": []}, "run.stop": {"code": "run_unknown",
                                                                                        "run_id": "x"}}),
                     clock=Clock(), environ=environ)
        app.open()
        app.command("stop x")
        return app

    def test_no_color_emits_no_sgr_and_keeps_every_label(self) -> None:
        plain = self.frame_after_refusal({"NO_COLOR": "1"})
        coloured = self.frame_after_refusal({})
        self.assertFalse(plain.color)
        self.assertTrue(coloured.color)
        ansi_plain = to_ansi(plain.frame(), plain.color)
        self.assertNotRegex(ansi_plain, r"\x1b\[\d+(;\d+)*m")
        self.assertRegex(to_ansi(coloured.frame(), coloured.color), r"\x1b\[1;31m")
        self.assertEqual(to_text(plain.frame()), to_text(coloured.frame()), "colour adds no information")
        self.assertIn("REFUSED: run_unknown x", to_text(plain.frame()))

    def test_empty_no_color_does_not_disable_colour(self) -> None:
        self.assertTrue(TuiApp(ScriptedAPI({}), clock=Clock(), environ={"NO_COLOR": ""}).color)
        self.assertFalse(TuiApp(ScriptedAPI({}), clock=Clock(), environ={"TERM": "dumb"}).color)


class StaleWatchOnlyTest(unittest.TestCase):
    def test_stale_snapshot_forces_watch_only_but_stop_still_goes(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        clock = Clock()
        app = TuiApp(driver.api, clock=clock, environ={"NO_COLOR": "1"}, stale_after_s=10.0)
        app.open()
        self.assertFalse(app.watch_only)
        clock.now += 10.5
        self.assertTrue(app.watch_only)
        self.assertIn("view=watch_only", app.frame()[0][0])
        self.assertIn("STALE: watch_only (start disabled; stop and reads work): snapshot older than 10.0s", to_text(app.frame()))
        app.command("start --tenant t1 --engine codex --prompt hi")
        self.assertEqual(driver.model_calls, 1, "no start while watch_only")
        self.assertIn("refused locally: start while watch_only", app.errors[-1])
        app.press("s")
        app.press("y")
        self.assertEqual(app.last["code"], "stopped")
        self.assertEqual(driver.registry.get(run_id)["state"], "stopped")
        app.press("r")
        self.assertFalse(app.watch_only, "a fresh read clears watch_only")

    def test_refresh_failure_keeps_old_snapshot_visible_and_stale(self) -> None:
        driver = FakeDriver()
        driver.start_run()
        api = FlakyAPI(driver.api)
        app = TuiApp(api, clock=Clock(), environ={})
        app.open()
        api.down = True
        app.press("r")
        self.assertEqual([r["run_id"] for r in app.runs], ["run-1"])
        self.assertTrue(app.watch_only)
        labels = {lab for lab, _ in app.conditions()}
        self.assertEqual(labels, {p.UNAVAILABLE_LABEL, p.STALE})
        self.assertNotEqual(app.last["code"], "ok")


class ErrorStaysTest(unittest.TestCase):
    def test_error_is_never_redrawn_away(self) -> None:
        driver = FakeDriver()
        run_id = driver.start_run()
        app = TuiApp(driver.api, clock=Clock(), environ={})
        app.open()
        app.attach(run_id)
        app.command("stop nope")
        err = app.errors[-1]
        for i in range(10_000):
            app.on_event({"run_id": run_id, "text": f"line {i}\n"})
        for _ in range(5):
            app.press("r")  # later successful answers do not clear it
        rows = [t for t, _ in app.frame()]
        self.assertTrue(any(r.startswith("error 1: REFUSED: run.stop status=run_unknown") for r in rows), rows)
        self.assertIn(f"error 1: {err}", app.transcript)
        self.assertEqual(app.last["code"], "ok")

    def test_usage_error_is_text_and_sends_nothing(self) -> None:
        api = ScriptedAPI({"run.list": {"code": "ok", "runs": []}})
        app = TuiApp(api, clock=Clock(), environ={})
        app.open()
        app.command("approve run-1")
        self.assertEqual(api.calls, [("run.list", {})])
        self.assertIn("usage error", app.errors[-1])


if __name__ == "__main__":
    unittest.main()
