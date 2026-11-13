"""Required test 7 and the O7 TUI packet transcripts: NO_COLOR and stale watch_only snapshots, refusal
shown as text, an error that stays on screen, and screen-reader / NO_COLOR transcripts in which
refusal, uncertainty, stale and unreconciled states stay readable as text.

Golden transcripts live in tests/transcripts/. To regenerate after a deliberate change:
RECURSION_TUI_REGEN=1 PYTHONPATH=core python3 -B -m unittest recursion_core.tui.tests.test_o7_text
"""

from __future__ import annotations

import io
import json
import os
import random
import time
import unittest

from recursion_core.control import cli
from recursion_core.control.api import ControlAPI
from recursion_core.control.registry import EMPTY, POPULATED, InMemoryRunRegistry
from recursion_core.control.tests.fakes import FakeKillTree
from recursion_core.tui import protocol as p
from recursion_core.tui import term
from recursion_core.tui.app import TuiApp, _listed_run
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

    def test_stopped_answer_clears_uncertainty_for_a_run_absent_from_the_list(self) -> None:
        # 4504133355: a Stop of a run another client started stayed UNCERTAIN after the API said stopped.
        api = ScriptedAPI({"run.list": {"code": "ok", "runs": []},
                           "run.stop": {"code": "pending", "run_id": "run-1", "reason": "cgroup_unknown"}})
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        app.command("stop run-1")
        self.assertIn("run=run-1", app._uncertain)
        api.answers["run.stop"] = {"code": "stopped", "run_id": "run-1", "reason": "stopped"}
        app.command("stop run-1")
        self.assertEqual(app.last["code"], "stopped")
        self.assertNotIn("run=run-1", app._uncertain)
        self.assertNotIn(p.UNCERTAIN, {label for label, _ in app.conditions()})

    def test_stale_list_stays_stale_after_a_fresh_status(self) -> None:
        # 4503920848: attaching replaced the stale list answer and watch_only turned off.
        run = {"run_id": "r", "state": "running", "engine": "codex", "tenant": "t1"}
        api = ScriptedAPI({"run.list": {"code": "ok", "runs": [run], "stale": True},
                           "run.status": {"code": "ok", "run": run}})
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        self.assertTrue(app.list_stale)
        self.assertTrue(app.watch_only)
        app.attach("r")
        self.assertEqual(app.last["code"], "ok")
        self.assertTrue(app.list_stale)
        self.assertTrue(app.watch_only)
        app.command("start --tenant t1 --engine codex --prompt p")
        self.assertNotIn("run.start", [method for method, _ in api.calls])
        api.answers["run.list"] = {"code": "ok", "runs": [run]}
        app.press("r")
        self.assertFalse(app.list_stale)
        self.assertFalse(app.watch_only)

    def test_failed_status_refresh_is_not_cleared_by_a_successful_list(self) -> None:
        # 4503920903: refresh's list succeeded and its status failed, and watch_only stayed off.
        run = {"run_id": "r", "state": "running", "engine": "codex", "tenant": "t1"}

        class API:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.status_code = "ok"

            def call(self, method: str, params: dict | None = None) -> dict:
                self.calls.append(method)
                if method == "run.list":
                    return {"code": "ok", "runs": [run]}
                if method == "run.status":
                    if self.status_code != "ok":
                        return {"code": self.status_code}
                    return {"code": "ok", "run": run}
                return {"code": "started", "run": {**run, "run_id": "new"}}

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        app.attach("r")
        self.assertFalse(app.watch_only)
        api.status_code = "control_api_unavailable"
        app.press("r")
        self.assertIsNone(app.refresh_failed)
        self.assertIsNotNone(app.status_refresh_failed)
        self.assertTrue(app.watch_only)
        app.request("run.list", {})
        self.assertTrue(app.watch_only, "a later list does not clear a failed status")
        app.command("start --tenant t1 --engine codex --prompt p")
        self.assertNotIn("run.start", api.calls)
        api.status_code = "ok"
        app.request("run.status", {"run_id": "r"})
        self.assertFalse(app.watch_only)

    def test_a_dropped_status_run_does_not_clear_watch_only(self) -> None:
        # call_api drops a cyclic run and leaves {code: ok}. That is not an applied read.
        run = {"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}
        leaked = "SHOULD-NOT-LEAK"

        class API:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.mode = "down"

            def call(self, method: str, params: dict | None = None) -> dict:
                self.calls.append(method)
                if method == "run.list":
                    return {"code": "ok", "runs": [dict(run)]}
                if method == "run.status":
                    if self.mode != "cyclic":
                        return {"code": "ok", "run": dict(run)} if self.mode == "ok" else {"code": "control_api_unavailable"}
                    cyclic = {"run_id": "r", "state": "stopped", "engine": "e", "tenant": "t", "note": leaked}
                    cyclic["self"] = cyclic
                    return {"code": "ok", "run": cyclic}
                return {"code": "started", "run": {**run, "run_id": "new"}}

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"}, width=80, height=24)
        app.open()
        app.attach("r")
        api.mode = "down"
        app.request("run.status", {"run_id": "r"})
        self.assertTrue(app.watch_only)
        self.assertIsNotNone(app.status_refresh_failed)
        api.mode = "cyclic"
        app.request("run.status", {"run_id": "r"})
        self.assertEqual(app.last, {"code": "ok"})
        self.assertIsNotNone(app.status_refresh_failed)
        self.assertTrue(app.watch_only)
        self.assertEqual(app.runs[0]["state"], "running")
        self.assertTrue(any("without a run" in err for err in app.errors))
        self.assertNotIn(leaked, "\n".join(app.errors))
        self.assertNotIn(leaked, "\n".join(app.transcript))
        app.command("start --tenant t1 --engine codex --prompt hi")
        self.assertNotIn("run.start", api.calls)
        self.assertIn("refused locally: start while watch_only", app.errors[-1])
        api.mode = "ok"
        app.request("run.status", {"run_id": "r"})
        self.assertIsNone(app.status_refresh_failed)
        self.assertFalse(app.watch_only)

    def test_a_malformed_list_row_does_not_replace_the_snapshot(self) -> None:
        # Sync path of the same fail-open: [null] (and any other non-dict row) is not an empty list.
        run = {"run_id": "r", "state": "running", "engine": "codex", "tenant": "t1"}

        class API:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.rows: list = [run]

            def call(self, method: str, params: dict | None = None) -> dict:
                self.calls.append(method)
                if method == "run.list":
                    return {"code": "ok", "runs": self.rows}
                if method == "run.start":
                    return {"code": "started", "run": {**run, "run_id": "new"}}
                return {"code": "ok", "run": dict(run)}

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        self.assertEqual([r["run_id"] for r in app.runs], ["r"])
        for rows in ([None], ["nope"], [{"run_id": "other", "state": "running"}, None]):
            with self.subTest(rows=rows):
                api.rows = rows
                api.calls.clear()
                app.refresh_failed = "earlier read failed"
                app.request("run.list", {})
                self.assertEqual([row["run_id"] for row in app.runs], ["r"])
                self.assertIsNotNone(app.refresh_failed)
                self.assertTrue(app.watch_only)
                app.command("start --tenant t1 --engine codex --prompt p")
                self.assertNotIn("run.start", api.calls)
                self.assertIn("watch_only", app.errors[-1])

    def test_a_dict_row_without_a_string_run_id_does_not_replace_the_snapshot(self) -> None:
        # All-dict rows still committed: {}, {"state": "running"}, {"run_id": None}, and a mix of a
        # real id with {}. The snapshot dropped run r, the refresh latch cleared, and Start ran.
        run = {"run_id": "r", "state": "running", "engine": "codex", "tenant": "t1"}
        shapes = (
            [{}],
            [{"state": "running"}],
            [{"run_id": None}],
            [{"run_id": "other"}, {}],
            [{"run_id": "other", "state": None}],
            [{"run_id": "", "state": "running"}],
        )

        class API:
            def __init__(self) -> None:
                self.calls: list[str] = []
                self.rows: list = [run]

            def call(self, method: str, params: dict | None = None) -> dict:
                self.calls.append(method)
                if method == "run.list":
                    return {"code": "ok", "runs": self.rows}
                if method == "run.start":
                    return {"code": "started", "run": {**run, "run_id": "new"}}
                return {"code": "ok", "run": dict(run)}

        api = API()
        app = TuiApp(api, clock=Clock(), environ={"NO_COLOR": "1"})
        app.open()
        self.assertEqual([row["run_id"] for row in app.runs], ["r"])
        for rows in shapes:
            with self.subTest(rows=rows):
                api.rows = rows
                api.calls.clear()
                app.runs = [dict(run)]
                app.selected = "r"
                app.refresh_failed = "earlier read failed"
                app.request("run.list", {})
                self.assertEqual([row.get("run_id") for row in app.runs], ["r"])
                self.assertEqual(app.selected, "r")
                self.assertEqual(app.refresh_failed, "run.list answered ok with a malformed run")
                self.assertTrue(app.watch_only)
                app.command("start --tenant t1 --engine codex --prompt p")
                self.assertNotIn("run.start", api.calls)
                self.assertIn("watch_only", app.errors[-1])


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


class StopBeforeLaunchRecordedTest(unittest.TestCase):
    """Red-team round 3 finding 1: a Stop sent while the launch is unrecorded stays visible after refresh."""

    def setUp(self) -> None:
        self.model_calls = 0

        def starter(params: dict) -> dict:  # launched, but the handle names no cgroup: start_unconfirmed
            self.model_calls += 1
            return {}

        registry = InMemoryRunRegistry()
        self.api = ControlAPI(registry, FakeKillTree([EMPTY]), starter, clock=lambda: 1000.0,
                              new_run_id=lambda: "run-" + "a" * 32)
        start = self.api.call("run.start", {"tenant": "t1", "engine": "codex", "prompt": "p"})
        self.assertEqual((start["code"], start["detail"]), ("start_unconfirmed", "starter_handle_invalid"))
        self.run_id = start["run"]["run_id"]

    def labels(self, app: TuiApp) -> set[str]:
        return {label for label, text in app.conditions() if self.run_id in text}

    def test_pending_stop_on_a_starting_run_survives_refresh_status_and_conditions(self) -> None:
        app = TuiApp(self.api, clock=Clock(), environ={"NO_COLOR": "1"}, width=100, height=30)
        app.open()
        app.attach(self.run_id)
        app.press("s")
        app.press("y")
        self.assertEqual((app.last["code"], app.last["reason"]), ("pending", "cgroup_unknown"))
        self.assertEqual(self.labels(app), {p.UNCERTAIN, p.UNRECONCILED})
        record = self.api.call("run.status", {"run_id": self.run_id})["run"]
        self.assertEqual((record["state"], record["stop"]["reason"]), ("starting", "cgroup_unknown"))
        for step in (lambda: app.press("r"), lambda: app.command(f"status {self.run_id}"),
                     lambda: app.command("conditions")):
            step()
            self.assertEqual(self.labels(app), {p.UNCERTAIN, p.UNRECONCILED})
            text = to_text(app.frame())
            self.assertNotIn("conditions: none", text)
            self.assertIn(f"run={self.run_id} state=starting reason=cgroup_unknown", text)
        self.assertEqual(self.model_calls, 1, "the Stop and the reads start no model call")

    def test_cli_conditions_reports_the_pending_stop_on_a_starting_run(self) -> None:
        self.api.call("run.stop", {"run_id": self.run_id})
        out = io.StringIO()
        cli.main(["conditions"], self.api, out, io.StringIO())
        shown = json.loads(out.getvalue())["conditions"]
        self.assertIn({"label": p.UNRECONCILED,
                       "text": f"run={self.run_id} state=starting reason=cgroup_unknown: "
                               "stop requested, cleanup not confirmed"}, shown)

    def test_only_a_confirmed_stop_clears_it(self) -> None:
        stop = {"requested_at": 1000.0, "checks": 1, "reason": None, "requested": True}
        cases = [({"state": "running", "stop": None}, False), ({"state": "starting", "stop": None}, False),
                 ({"state": "stopped", "stop": stop}, False), ({"state": "stop_pending", "stop": stop}, True),
                 ({"state": "starting", "stop": {**stop, "reason": "cgroup_unknown"}}, True),
                 ({"state": "start_failed", "stop": {**stop, "reason": "cgroup_unknown"}}, True)]
        for run, want in cases:
            with self.subTest(run=run):
                self.assertIs(p.stop_unconfirmed({"run_id": "r", **run}), want)


_TERMINAL = frozenset({"stopped"})


def _plant_stop_pending(app: TuiApp) -> dict:
    """A stop_pending row with its Stop mark, UNCERTAIN, and both refresh latches set."""

    row = {"run_id": "r", "state": "stop_pending", "engine": "e", "tenant": "t",
           "stop": {"reason": "cgroup_populated", "requested": True}}
    app.runs = [dict(row)]
    app.selected = "r"
    app._uncertain["run=r"] = "stop not confirmed for run=r"
    now = time.monotonic()
    app._read_at["r"] = now
    app._stop_mark["r"] = (now, "stop_pending", "cgroup_populated")
    app.status_refresh_failed = "run.status answered control_api_unavailable"
    app._status_failed_run = "r"
    app.refresh_failed = "earlier read failed"
    return row


class StopPendingAnswerTest(unittest.TestCase):
    """A status or start answer must not retire stop_pending, UNCERTAIN, or the refresh latch
    unless it is a real Stop or a valid list/status row whose state is terminal."""

    def setUp(self) -> None:
        self.calls: list[str] = []

        class API:
            def __init__(self, outer: "StopPendingAnswerTest") -> None:
                self.outer = outer

            def call(self, method: str, params: dict | None = None) -> dict:
                self.outer.calls.append(method)
                if method == "run.list":
                    return {"code": "ok", "runs": [
                        {"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}]}
                if method == "run.start":
                    return {"code": "started", "run": {"run_id": "new", "state": "running"}}
                if method == "run.stop":
                    return {"code": "pending", "run_id": "r", "reason": "cgroup_populated"}
                return {"code": "ok", "run": {"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}}

        self.api = API(self)
        self.app = TuiApp(self.api, clock=Clock(), environ={"NO_COLOR": "1"})

    def _stop(self) -> None:
        self.app.open()
        self.app.command("stop r")
        self.assertEqual(self.app.runs[0]["state"], "stop_pending")
        self.assertEqual(self.app.runs[0]["stop"]["reason"], "cgroup_populated")
        self.assertIn("run=r", self.app._uncertain)
        self.app.status_refresh_failed = "run.status answered control_api_unavailable"
        self.assertTrue(self.app.watch_only)

    def test_status_without_a_string_state_keeps_stop_pending_and_the_latch(self) -> None:
        # code ok merged any dict, cleared status_refresh_failed, and dropped UNCERTAIN,
        # so the next start was dispatched.
        self._stop()
        shapes = (
            {"run_id": "r"},
            {"run_id": "r", "state": None},
            {"run_id": "r", "state": 1},
            {"state": "running"},
        )
        for run in shapes:
            with self.subTest(run=run):
                self._stop()
                self.calls.clear()
                self.app.on_answer("run.status", {"run_id": "r"}, {"code": "ok", "run": dict(run)})
                self.assertEqual(self.app.runs[0]["state"], "stop_pending")
                self.assertEqual(self.app.runs[0]["stop"]["reason"], "cgroup_populated")
                self.assertEqual([row.get("run_id") for row in self.app.runs], ["r"])
                self.assertIsNotNone(self.app.status_refresh_failed)
                self.assertTrue(self.app.watch_only)
                self.assertIn("run=r", self.app._uncertain)
                self.app.command("start --tenant t1 --engine codex --prompt p")
                self.assertNotIn("run.start", self.calls)
                self.assertIn("watch_only", self.app.errors[-1])

    def test_started_without_a_string_state_does_not_replace_stop_pending(self) -> None:
        self._stop()
        for run in ({"run_id": "r"}, {"run_id": "r", "state": None}, {"run_id": "r", "state": 1}):
            with self.subTest(run=run):
                self._stop()
                self.app.on_answer("run.start", {"tenant": "t1", "engine": "e", "prompt": "p"},
                                   {"code": "started", "run": dict(run)})
                self.assertEqual(self.app.runs[0]["state"], "stop_pending")
                self.assertEqual(self.app.runs[0]["stop"]["reason"], "cgroup_populated")
                self.assertEqual([row.get("run_id") for row in self.app.runs], ["r"])
                self.assertIsNotNone(self.app.status_refresh_failed)
                self.assertIn("run=r", self.app._uncertain)
                self.assertEqual(self.app._stop_mark["r"][1], "stop_pending")

    def test_start_code_stopped_does_not_paint_the_row_stopped(self) -> None:
        # The stopped/pending branch applied to every method. A start answer of stopped
        # painted the row stopped and cleared the stop reason and UNCERTAIN.
        payloads = (
            {"code": "stopped", "run_id": "r"},
            {"code": "stopped", "run_id": "r",
             "run": {"run_id": "r", "state": "stop_pending",
                     "stop": {"reason": "cgroup_populated", "requested": True}}},
            {"code": "pending", "run_id": "r"},
        )
        for answer in payloads:
            with self.subTest(code=answer["code"], run="run" in answer):
                self._stop()
                self.app.on_answer("run.start", {"tenant": "t1", "engine": "e", "prompt": "p"}, answer)
                self.assertEqual(self.app.runs[0]["state"], "stop_pending")
                self.assertEqual(self.app.runs[0]["stop"]["reason"], "cgroup_populated")
                self.assertIn("run=r", self.app._uncertain)
                self.assertEqual(self.app._stop_mark["r"][1], "stop_pending")
                self.assertIsNotNone(self.app.status_refresh_failed)
                self.assertTrue(self.app.watch_only)
                self.assertTrue(any(err.startswith("unexpected run.start status=") for err in self.app.errors))


class AnswerTableTest(unittest.TestCase):
    """Each answer is one (method, code) transition. A pair that is not in the table, a blank
    state, and a cross-method code leave the row, the Stop mark and the status latch alone."""

    def setUp(self) -> None:
        self.calls: list[str] = []

        class API:
            def __init__(self, outer: "AnswerTableTest") -> None:
                self.outer = outer

            def call(self, method: str, params: dict | None = None) -> dict:
                self.outer.calls.append(method)
                return {"code": "started", "run": {"run_id": "new", "state": "running"}}

        self.api = API(self)
        self.app = TuiApp(self.api, clock=Clock(), environ={"NO_COLOR": "1"})

    def _ready(self) -> None:
        _plant_stop_pending(self.app)
        self.app.snapshot_at = self.app._clock()
        self.calls.clear()

    def _assert_held(self, method: str, code: object) -> None:
        row = self.app.runs[0]
        self.assertEqual(row.get("state"), "stop_pending", (method, code, row))
        self.assertEqual((row.get("stop") or {}).get("reason"), "cgroup_populated")
        self.assertEqual([item.get("run_id") for item in self.app.runs], ["r"])
        self.assertEqual(self.app._stop_mark["r"][1], "stop_pending")
        self.assertIsNotNone(self.app.status_refresh_failed)
        self.assertIn("run=r", self.app._uncertain)
        self.assertTrue(self.app.watch_only)
        self.assertTrue(any(err.startswith(f"unexpected {method} status=") for err in self.app.errors),
                        self.app.errors)

    def test_stop_ok_does_not_merge_a_stateless_row_or_paint_stopped(self) -> None:
        # run.stop code ok used to merge any dict: a state-less row replaced stop_pending,
        # and {state: stopped} painted stopped and cleared UNCERTAIN without a stop code.
        payloads = (
            {"run_id": "r"},
            {"run_id": "r", "state": "stopped"},
            {"run_id": "r", "state": ""},
            {"run_id": "r", "state": " "},
            {"run_id": "r", "state": "running"},
        )
        for run in payloads:
            with self.subTest(run=run):
                self._ready()
                before = len(self.app.errors)
                self.app.on_answer("run.stop", {"run_id": "r"}, {"code": "ok", "run": dict(run)})
                self._assert_held("run.stop", "ok")
                self.assertTrue(any(err.startswith("unexpected run.stop status=") for err in self.app.errors[before:]))

    def test_start_ok_stopped_does_not_paint_even_when_the_read_is_stale(self) -> None:
        self._ready()
        self.app._answer_started = 0.0
        try:
            self.app.on_answer("run.start", {"tenant": "t1", "engine": "e", "prompt": "p"},
                               {"code": "ok", "run": {"run_id": "r", "state": "stopped"}})
        finally:
            self.app._answer_started = None
        self._assert_held("run.start", "ok")

    def test_stop_started_does_not_drop_stop_pending_for_any_state_shape(self) -> None:
        # code started merged for every method. run.stop started with stopped, blank, or
        # running dropped stop_pending.
        for state in ("stopped", "", " ", "running"):
            with self.subTest(state=state):
                self._ready()
                self.app.on_answer("run.stop", {"run_id": "r"},
                                   {"code": "started", "run": {"run_id": "r", "state": state}})
                self._assert_held("run.stop", "started")

    def test_stale_status_clears_the_latch_but_stop_pending_still_refuses_start(self) -> None:
        # A stale listed status cleared the latch, then Start dispatched run.start.
        self._ready()
        self.app._answer_started = 0.0
        try:
            self.app.on_answer("run.status", {"run_id": "r"},
                               {"code": "ok", "run": {"run_id": "r", "state": "running",
                                                      "engine": "e", "tenant": "t"}})
        finally:
            self.app._answer_started = None
        self.assertEqual(self.app.runs[0]["state"], "stop_pending")
        self.assertEqual(self.app._stop_mark["r"][1], "stop_pending")
        self.assertIsNone(self.app.status_refresh_failed)
        self.app.refresh_failed = None
        self.assertFalse(self.app.watch_only)
        self.app.command("start --tenant t1 --engine codex --prompt p")
        self.assertNotIn("run.start", self.calls)
        self.assertIn("stop_pending", self.app.errors[-1])

    def test_status_blank_state_does_not_clear_the_latch_or_enable_start(self) -> None:
        # _listed_run accepted any string state, so '' and ' ' cleared the latch and UNCERTAIN.
        for state in ("", " "):
            with self.subTest(state=state):
                self._ready()
                self.calls.clear()
                self.app.on_answer("run.status", {"run_id": "r"},
                                   {"code": "ok", "run": {"run_id": "r", "state": state}})
                self.assertEqual(self.app.runs[0]["state"], "stop_pending")
                self.assertEqual(self.app.runs[0]["stop"]["reason"], "cgroup_populated")
                self.assertIsNotNone(self.app.status_refresh_failed)
                self.assertIn("run=r", self.app._uncertain)
                self.assertTrue(self.app.watch_only)
                self.app.command("start --tenant t1 --engine codex --prompt p")
                self.assertNotIn("run.start", self.calls)
                self.assertIn("watch_only", self.app.errors[-1])

    def test_list_blank_state_does_not_replace_stop_pending(self) -> None:
        for state in ("", " "):
            with self.subTest(state=state):
                self._ready()
                self.app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": "r", "state": state}]})
                self.assertEqual(self.app.runs[0]["state"], "stop_pending")
                self.assertEqual(self.app.refresh_failed, "run.list answered ok with a malformed run")
                self.assertIsNotNone(self.app.status_refresh_failed)
                self.assertIn("run=r", self.app._uncertain)

    def test_empty_list_after_malformed_status_unwedges_start(self) -> None:
        # A malformed ok set the latch. A later valid empty list plus run_unknown left it set,
        # so Start stayed refused with no run left.
        self.app.runs = [{"run_id": "r", "state": "running", "engine": "e", "tenant": "t"}]
        self.app.selected = "r"
        self.app.snapshot_at = self.app._clock()
        self.app.on_answer("run.status", {"run_id": "r"},
                           {"code": "ok", "run": {"run_id": "r", "state": None}})
        self.assertIsNotNone(self.app.status_refresh_failed)
        self.assertEqual(self.app.runs[0]["state"], "running")
        self.app.command("start --tenant t1 --engine codex --prompt p")
        self.assertNotIn("run.start", self.calls)
        self.app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        self.assertEqual(self.app.runs, [])
        self.assertIsNone(self.app.status_refresh_failed)
        self.app.on_answer("run.status", {"run_id": "r"}, {"code": "run_unknown", "run_id": "r"})
        self.assertIsNone(self.app.status_refresh_failed)
        self.assertFalse(self.app.watch_only)
        self.assertFalse(any(err.startswith("unexpected run.status status=") for err in self.app.errors))
        self.calls.clear()
        self.app.command("start --tenant t1 --engine codex --prompt p")
        self.assertIn("run.start", self.calls)

    def test_a_list_that_still_shows_stop_pending_keeps_start_refused(self) -> None:
        self._ready()
        self.app.on_answer("run.list", {}, {"code": "ok", "runs": [
            {"run_id": "r", "state": "stop_pending", "engine": "e", "tenant": "t",
             "stop": {"reason": "cgroup_populated", "requested": True}}]})
        self.assertEqual(self.app.runs[0]["state"], "stop_pending")
        self.assertIsNotNone(self.app.status_refresh_failed)
        self.app.refresh_failed = None
        self.assertTrue(self.app.watch_only)
        self.app.command("start --tenant t1 --engine codex --prompt p")
        self.assertNotIn("run.start", self.calls)

    def test_stop_code_stopped_still_paints_and_a_fresh_status_stopped_does_too(self) -> None:
        self._ready()
        self.app.on_answer("run.status", {"run_id": "r"},
                           {"code": "ok", "run": {"run_id": "r", "state": "stopped"}})
        self.assertEqual(self.app.runs[0]["state"], "stopped")
        self.assertNotIn("run=r", self.app._uncertain)
        self._ready()
        self.app.on_answer("run.stop", {"run_id": "r"},
                           {"code": "stopped", "run_id": "r", "reason": "stopped"})
        self.assertEqual(self.app.runs[0]["state"], "stopped")
        self.assertEqual(self.app._stop_mark["r"][1], "stopped")
        self.assertNotIn("run=r", self.app._uncertain)

    def test_start_started_does_not_paint_stopped_over_stop_pending(self) -> None:
        self._ready()
        self.app.on_answer("run.start", {"tenant": "t1", "engine": "e", "prompt": "p"},
                           {"code": "started", "run": {"run_id": "r", "state": "stopped"}})
        self.assertEqual(self.app.runs[0]["state"], "stop_pending")
        self.assertEqual(self.app._stop_mark["r"][1], "stop_pending")
        self.assertIsNotNone(self.app.status_refresh_failed)
        self.assertFalse(any(err.startswith("unexpected run.start status=") for err in self.app.errors))


class SeededStopModelTest(unittest.TestCase):
    """Seed 20261113.

    Owner notes: a whitespace run_id is a real id, and [] is a valid empty list.
    stop_pending never leaves without a run.stop answer or a valid fresh list/status
    row whose state is terminal (stopped).
    """

    def test_owner_notes_and_stop_pending_departure(self) -> None:
        app = TuiApp(ScriptedAPI({}), clock=Clock(), environ={"NO_COLOR": "1"})
        # Whitespace id: accepted, not a malformed row.
        app.refresh_failed = "earlier read failed"
        app.on_answer("run.list", {}, {"code": "ok", "runs": [{"run_id": " ", "state": "running"}]})
        self.assertEqual(app.runs[0]["run_id"], " ")
        self.assertIsNone(app.refresh_failed)
        app.status_refresh_failed = "run.status answered control_api_unavailable"
        app.on_answer("run.status", {"run_id": "\t"},
                      {"code": "ok", "run": {"run_id": "\t", "state": "running"}})
        self.assertEqual([row["run_id"] for row in app.runs], [" ", "\t"])
        self.assertIsNone(app.status_refresh_failed)
        # [] is a real empty snapshot: it commits and clears the list latch.
        app.runs = [{"run_id": "r", "state": "running"}]
        app.refresh_failed = "earlier read failed"
        app.on_answer("run.list", {}, {"code": "ok", "runs": []})
        self.assertEqual(app.runs, [])
        self.assertIsNone(app.refresh_failed)

        rng = random.Random(20261113)
        model = TuiApp(ScriptedAPI({}), clock=Clock(), environ={"NO_COLOR": "1"})
        for trial in range(60):
            for step in range(8):
                _plant_stop_pending(model)
                stop_at = model._read_at["r"]
                kind = rng.randrange(14)
                method, answer, started = _model_op(kind)
                model._answer_started = started
                try:
                    model.on_answer(method, {"run_id": "r"}, answer)
                finally:
                    model._answer_started = None
                state = next((row.get("state") for row in model.runs if row.get("run_id") == "r"), None)
                allowed = _departure_allowed(method, answer, started, stop_at)
                self.assertTrue(
                    state == "stop_pending" or allowed,
                    f"trial {trial} step {step} kind {kind}: stop_pending became {state!r} "
                    f"on {method} {answer.get('code')}",
                )
                if method == "run.status" and answer.get("code") == "ok" and not _listed_run(answer.get("run")):
                    self.assertEqual(state, "stop_pending")
                    self.assertIsNotNone(model.status_refresh_failed)
                    self.assertIn("run=r", model._uncertain)
                if method == "run.start" and answer.get("code") in ("stopped", "pending"):
                    self.assertEqual(state, "stop_pending")
                    self.assertEqual(model.runs[0]["stop"]["reason"], "cgroup_populated")
                    self.assertIn("run=r", model._uncertain)
                    self.assertEqual(model._stop_mark["r"][1], "stop_pending")
                    self.assertIsNotNone(model.status_refresh_failed)
                if method == "run.start" and answer.get("code") == "started" and not _listed_run(answer.get("run")):
                    self.assertEqual(state, "stop_pending")
                    self.assertEqual(model.runs[0]["stop"]["reason"], "cgroup_populated")
                    self.assertIsNotNone(model.status_refresh_failed)

    def test_seeded_method_code_and_payload_shape(self) -> None:
        """Draw (method, code, payload shape), including blank states and cross-method codes.

        Seed 20261113. A pair outside the answer table changes nothing. A blank state is not a
        row. Only run.stop code stopped, or a list/status row whose state is stopped, may paint
        stopped. Only a fresh valid list that drops the run, or a valid status, clears the latch.
        """

        rng = random.Random(20261113)
        model = TuiApp(ScriptedAPI({}), clock=Clock(), environ={"NO_COLOR": "1"})
        combos = _model_combos()
        for trial in range(48):
            for step in range(8):
                _plant_stop_pending(model)
                stop_at = model._read_at["r"]
                method, answer, started = combos[rng.randrange(len(combos))]
                answer = json.loads(json.dumps(answer))
                err_at = len(model.errors)
                model._answer_started = started
                try:
                    model.on_answer(method, {"run_id": "r"}, answer)
                finally:
                    model._answer_started = None
                state, latch_cleared, unexpected = _predict_answer(method, answer, started, stop_at)
                got = next((row.get("state") for row in model.runs if row.get("run_id") == "r"), None)
                self.assertEqual(
                    got, state,
                    f"trial {trial} step {step}: {method} {answer.get('code')!r} "
                    f"shape {answer!r} started={started!r} left state {got!r}, want {state!r}",
                )
                if state == "stop_pending":
                    row = next(item for item in model.runs if item.get("run_id") == "r")
                    self.assertEqual(row["stop"]["reason"], "cgroup_populated")
                    self.assertEqual(model._stop_mark["r"][1], "stop_pending")
                    self.assertIn("run=r", model._uncertain)
                if unexpected:
                    self.assertTrue(
                        any(err.startswith(f"unexpected {method} status=") for err in model.errors[err_at:]),
                        f"trial {trial} step {step}: {method} {answer.get('code')!r} was silent",
                    )
                    self.assertIsNotNone(model.status_refresh_failed)
                    self.assertEqual(model.refresh_failed, "earlier read failed")
                elif latch_cleared:
                    self.assertIsNone(model.status_refresh_failed, (method, answer, started))
                else:
                    self.assertIsNotNone(model.status_refresh_failed, (method, answer, started))


def _model_op(kind: int) -> tuple[str, dict, float | None]:
    """One drawn answer. ``started`` None is a synchronous (fresh) read; 0 is older than the Stop."""

    if kind == 0:
        return "run.status", {"code": "ok", "run": {"run_id": "r"}}, None
    if kind == 1:
        return "run.status", {"code": "ok", "run": {"run_id": "r", "state": None}}, None
    if kind == 2:
        return "run.status", {"code": "ok", "run": {"run_id": "r", "state": 1}}, None
    if kind == 3:
        return "run.status", {"code": "ok", "run": {"run_id": "r", "state": "running", "stop": 1}}, 0.0
    if kind == 4:
        return "run.status", {"code": "ok", "run": {"run_id": "r", "state": "stopped"}}, None
    if kind == 5:
        return "run.list", {"code": "ok", "runs": [{"run_id": "r", "state": "stopped", "engine": "e", "tenant": "t"}]}, None
    if kind == 6:
        return "run.list", {"code": "ok", "runs": [{"run_id": "r"}]}, None
    if kind == 7:
        return "run.list", {"code": "ok", "runs": [{"run_id": "r", "state": "running"}]}, 0.0
    if kind == 8:
        return "run.start", {"code": "started", "run": {"run_id": "r"}}, None
    if kind == 9:
        return "run.start", {"code": "started", "run": {"run_id": "r", "state": None}}, None
    if kind == 10:
        return "run.start", {"code": "stopped", "run_id": "r"}, None
    if kind == 11:
        return "run.start", {"code": "stopped", "run_id": "r",
                             "run": {"run_id": "r", "state": "stop_pending",
                                     "stop": {"reason": "cgroup_populated", "requested": True}}}, None
    if kind == 12:
        return "run.start", {"code": "pending", "run_id": "r"}, None
    return "run.stop", {"code": "stopped", "run_id": "r", "reason": "stopped"}, None


def _departure_allowed(method: str, answer: dict, started: float | None, stop_at: float) -> bool:
    """True when this answer may take the row out of stop_pending.

    A run.stop answer may. So may a valid fresh run.list or run.status row whose state is
    terminal. Anything else, including a start answer and a row with no string state, may not.
    """

    code = answer.get("code")
    fresh = started is None or started >= stop_at
    if method == "run.stop" and code == "stopped" and answer.get("run_id") == "r":
        return True
    if method not in ("run.list", "run.status") or code != "ok" or not fresh:
        return False
    if method == "run.list":
        rows = answer.get("runs")
        if not isinstance(rows, list) or not all(_listed_run(row) for row in rows):
            return False
        if fresh and not any(row.get("run_id") == "r" for row in rows):
            return True
        return any(row.get("run_id") == "r" and row.get("state") in _TERMINAL for row in rows)
    run = answer.get("run")
    return bool(_listed_run(run) and run.get("run_id") == "r" and run.get("state") in _TERMINAL)


# Pairs the answer table allows. Kept here so a draw can tell an unexpected code from a real one
# without reading the table out of the implementation under test.
_MODEL_ALLOWED = {
    ("run.list", "ok"),
    ("run.list", "registry_unavailable"),
    ("run.list", "control_api_unavailable"),
    ("run.status", "ok"),
    ("run.status", "run_unknown"),
    ("run.status", "request_invalid"),
    ("run.status", "registry_unavailable"),
    ("run.status", "control_api_unavailable"),
    ("run.stop", "stopped"),
    ("run.stop", "pending"),
    ("run.stop", "run_unknown"),
    ("run.stop", "request_invalid"),
    ("run.stop", "registry_unavailable"),
    ("run.stop", "control_api_unavailable"),
    ("run.start", "started"),
    ("run.start", "request_invalid"),
    ("run.start", "start_failed"),
    ("run.start", "start_unconfirmed"),
    ("run.start", "registry_unavailable"),
    ("run.start", "control_api_unavailable"),
}
_MODEL_CODES = ("ok", "started", "stopped", "pending", "run_unknown", "request_invalid", "")
_MODEL_SHAPES = (
    {"run_id": "r", "state": "stopped"},
    {"run_id": "r", "state": "running"},
    {"run_id": "r", "state": "stop_pending",
     "stop": {"reason": "cgroup_populated", "requested": True}},
    {"run_id": "r", "state": ""},
    {"run_id": "r", "state": " "},
    {"run_id": "r"},
    {"run_id": "r", "state": None},
    {"run_id": "", "state": "running"},
    {"run_id": " ", "state": "running"},
    {"state": "stopped"},
    None,  # empty list, or no run object
)


def _model_row_ok(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    run_id, state = row.get("run_id"), row.get("state")
    return isinstance(run_id, str) and bool(run_id) and state in {
        "starting", "start_failed", "running", "stop_pending", "stopped"}


def _model_combos() -> list[tuple[str, dict, float | None]]:
    found = []
    for method in ("run.list", "run.status", "run.stop", "run.start"):
        for code in _MODEL_CODES:
            for shape in _MODEL_SHAPES:
                for started in (None, 0.0):
                    found.append((method, _model_answer(method, code, shape), started))
    return found


def _model_answer(method: str, code: str, shape: dict | None) -> dict:
    if method == "run.list":
        return {"code": code, "runs": [] if shape is None else [dict(shape)]}
    if shape is None:
        if method == "run.stop":
            return {"code": code, "run_id": "r", "reason": "cgroup_populated"}
        return {"code": code}
    body = dict(shape)
    if method == "run.stop":
        rid = body.get("run_id")
        return {"code": code, "run_id": rid if isinstance(rid, str) else "r",
                "reason": "cgroup_populated", "run": body}
    return {"code": code, "run": body}


def _predict_answer(method: str, answer: dict, started: float | None,
                    stop_at: float) -> tuple[str | None, bool, bool]:
    """``(state of run r or None if the snapshot dropped it, latch cleared, unexpected)``."""

    code = answer.get("code")
    if (method, code) not in _MODEL_ALLOWED:
        return "stop_pending", False, True
    fresh = started is None or started >= stop_at
    if method == "run.stop" and code == "stopped" and answer.get("run_id") == "r":
        return "stopped", False, False
    if method == "run.status" and code == "ok":
        run = answer.get("run")
        if not _model_row_ok(run):
            return "stop_pending", False, False
        if run.get("run_id") != "r":
            return "stop_pending", True, False
        if run.get("state") == "stopped":
            return "stopped", True, False
        return "stop_pending", True, False
    if method == "run.list" and code == "ok":
        rows = answer.get("runs")
        if not isinstance(rows, list) or not all(_model_row_ok(row) for row in rows):
            return "stop_pending", False, False
        incoming = next((row for row in rows if row.get("run_id") == "r"), None)
        if not fresh:
            if incoming is not None and incoming.get("state") == "stopped":
                return "stopped", False, False
            return "stop_pending", False, False
        if incoming is None:
            pending = any(row.get("state") == "stop_pending" for row in rows)
            return None, not pending, False
        if incoming.get("state") == "stopped":
            return "stopped", False, False
        return "stop_pending", False, False
    return "stop_pending", False, False
