"""Output order, the last unterminated line, the drain budget, and the partial-line cap."""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from recursion_core.tui import app as app_module
from recursion_core.tui.app import PARTIAL_CAP, Loop, TuiApp, _Output
from recursion_core.tui.events import speech_lines
from recursion_core.tui.view import to_text


class API:
    def __init__(self, answer=None):
        self.answer = answer or {"code": "ok", "runs": []}
        self.calls = []
        self.state = "running"

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "run.list":
            return {"code": "ok", "runs": [{"run_id": "r", "state": self.state}]}
        if method == "run.status":
            return {"code": "ok", "run": {"run_id": "r", "state": self.state}}
        if method == "run.stop":
            self.state = "stopped"
            return {"code": "stopped", "run_id": params["run_id"]}
        if self.answer is not None and method not in ("run.list", "run.status", "run.stop"):
            return self.answer
        return {"code": "started", "run": {"run_id": "new", "state": "running"}}


def new_app(api=None):
    return TuiApp(api or API(), clock=lambda: 1000.0, environ={"NO_COLOR": "1"})


def fresh(**kwargs):
    return TuiApp(API(), clock=time.monotonic, environ={"NO_COLOR": "1"}, **kwargs)


class OutputOrderTest(unittest.TestCase):
    def test_F05_output_pane_and_log_keep_the_same_order(self):
        api = API()
        app = new_app(api)
        app.open()
        app.attach("r")
        entered, release, second_done = (threading.Event() for _ in range(3))
        original = app._emit
        errors, snapshots = [], []

        def pause(kind, source, text, *args, **kwargs):
            if kind == "output" and text == "first\n":
                entered.set()
                if not release.wait(2):
                    raise AssertionError("output release missing")
            return original(kind, source, text, *args, **kwargs)

        app._emit = pause

        def write(text, done=None):
            try:
                app.on_event({"run_id": "r", "text": text})
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                if done is not None:
                    done.set()

        def read():
            try:
                frame = to_text(app.frame())
                recorded = "first\n" in [e.text for e in app.log if e.event_type == "output"]
                snapshots.append(("first" in frame, recorded))
            except Exception as exc:
                errors.append(repr(exc))

        workers = [threading.Thread(target=write, args=("first\n",), daemon=True)]
        workers[0].start()
        try:
            self.assertTrue(entered.wait(2))
            workers += [threading.Thread(target=write, args=("second\n", second_done), daemon=True),
                        threading.Thread(target=read, daemon=True)]
            for worker in workers[1:]:
                worker.start()
            second_done.wait(0.05)  # a lock may keep both observers waiting
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
        self.assertFalse(any(t.is_alive() for t in workers))
        self.assertEqual(errors, [])
        self.assertNotIn((True, False), snapshots, "the pane exposed output before the event log")
        self.assertEqual(list(app.outputs["r"].lines),
                         [e.text.rstrip("\n") for e in app.log if e.event_type == "output"])

    def test_f05_output_log_preserves_the_order_used_by_the_frame(self):
        app = fresh()
        inside, release = threading.Event(), threading.Event()
        errors = []
        real_append = app.log.append

        def gated_append(ms, event_type, *args):
            if threading.current_thread().name == "s2b-first" and event_type == "output":
                inside.set()
                if not release.wait(2):
                    raise AssertionError("append gate timed out")
            return real_append(ms, event_type, *args)

        def first():
            try:
                app.on_event({"run_id": "run-1", "text": "first\n"})
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=first, name="s2b-first", daemon=True)
        written = threading.Event()

        def second():
            try:
                app.on_event({"run_id": "run-1", "text": "second\n"})
            except BaseException as exc:
                errors.append(exc)
            finally:
                written.set()

        writer = threading.Thread(target=second, daemon=True)
        with mock.patch.object(app.log, "append", gated_append):
            worker.start()
            try:
                self.assertTrue(inside.wait(2))
                writer.start()
                written.wait(0.5)
            finally:
                release.set()
                worker.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(list(app.outputs["run-1"].lines), ["first", "second"])
        self.assertEqual([e.text for e in app.log if e.event_type == "output"], ["first\n", "second\n"])


class FinalOutputTest(unittest.TestCase):
    def test_F07_close_flushes_partial_output_and_truncated_utf8(self):
        api = API()
        app = new_app(api)
        app.open()
        app.attach("r")
        app.on_event({"run_id": "r", "text": b"final result\xe2\x82"})
        app.close()
        lines = speech_lines(app.log)
        self.assertIn("output run=r: final result\ufffd", lines)
        self.assertEqual(list(app.log)[-1].event_type, "close")
        app.close()
        self.assertEqual(speech_lines(app.log).count("output run=r: final result\ufffd"), 1)

    def test_f03_close_flushes_final_output_and_partial_utf8_before_sealing(self):
        app = fresh()
        app.on_event({"run_id": "run-1", "text": b"result \xc3"})
        app.close()
        lines = speech_lines(app.log)
        self.assertIn("output run=run-1: result \ufffd", lines)
        self.assertEqual(list(app.log)[-1].event_type, "close")


class DrainBudgetTest(unittest.TestCase):
    def test_F08_output_budget_is_checked_between_events_in_a_batch(self):
        api = API()
        app = new_app(api)
        loop = Loop(app, api, drain_budget_s=0.008)
        for _ in range(2):
            loop.post_event({"run_id": "r", "text": "line\n"})
        now, handled = [0.0], []
        original = app.on_event

        def handle(event):
            original(event)
            handled.append(event)
            now[0] += 0.009

        app.on_event = handle
        with mock.patch("recursion_core.tui.app.time.monotonic", side_effect=lambda: now[0]):
            loop.tick()
        self.assertEqual(len(handled), 1, "the remainder of the prefetched batch ignored the deadline")

    def test_f09_drain_stops_between_events_when_its_budget_expires(self):
        app = TuiApp(API(), clock=lambda: 0.0, environ={"NO_COLOR": "1"})
        loop = Loop(app, API(), drain_budget_s=0.008)
        now, processed = [0.0], []
        real_event = app.on_event

        def event_received(event):
            real_event(event)
            processed.append(event["text"])
            now[0] = 1.0

        app.on_event = event_received
        for text in ("one\n", "two\n", "three\n"):
            loop.post_event({"run_id": "run-1", "text": text})
        with mock.patch.object(app_module.time, "monotonic", side_effect=lambda: now[0]):
            loop.tick()
        self.assertEqual(processed, ["one\n"])
        self.assertEqual(len(loop._events), 2, "undrained events must remain queued")


class PartialCapTest(unittest.TestCase):
    def test_f08_empty_chunk_does_not_erase_the_truncation_marker(self):
        app = fresh()
        app.on_event({"run_id": "run-1", "text": "x" * (PARTIAL_CAP + 1)})
        before = app.outputs["run-1"].partial
        self.assertTrue(before.startswith("⟦truncated⟧"))
        app.on_event({"run_id": "run-1", "text": ""})
        self.assertEqual(app.outputs["run-1"].partial, before)
        app.on_event({"run_id": "run-1", "text": "\n"})
        self.assertTrue(app.outputs["run-1"].lines[-1].startswith("⟦truncated⟧"))

    def test_D31_completed_line_obeys_partial_cap(self):
        text = "a" * (PARTIAL_CAP + 1)
        whole, split = _Output(2, "r"), _Output(2, "r")
        whole.feed(text + "\n")
        split.feed(text)
        split.feed("\n")
        self.assertEqual(list(whole.lines), list(split.lines), "retention changed at the LF chunk boundary")
        self.assertLessEqual(len(whole.lines[0]), PARTIAL_CAP)


if __name__ == "__main__":
    unittest.main()
