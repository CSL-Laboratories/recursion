"""TUI-B: malformed answers, event-log reads, conditions under a writer, the log cursor, JSONL.

In-memory calls and bounded waits only. No terminal, daemon or provider.
"""
from __future__ import annotations

import inspect
import json
import sys
import threading
import types
import unittest

from recursion_core.tui import events, protocol as p
from recursion_core.tui.app import TuiApp, call_api
from recursion_core.tui.events import EventLog
from recursion_core.tui.term import LogReader


def add(log, text="notice"):
    return log.append(0, "notice", "recursion", "live", text, text, "runs")


class API:
    def __init__(self, answer=None):
        self.answer = {"code": "ok", "runs": []} if answer is None else answer
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, dict(params)))
        return self.answer


def fresh(**kwargs):
    return TuiApp(API(), clock=lambda: 0, environ={"NO_COLOR": "1"}, **kwargs)


class MalformedAnswerTest(unittest.TestCase):
    def test_F04_malformed_answers_are_typed_unavailable_answers(self):
        class WrongCode(API):
            def call(self, method, params):
                return {"code": [], "runs": []}

        with self.subTest("JSON answer with wrongly typed code"):
            app = TuiApp(WrongCode(), clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
            app.open()
            self.assertEqual(app.last["code"], "control_api_unavailable")
        with self.subTest("injected API answer cannot be copied"):
            cycle = {"code": "ok"}
            cycle["run"] = cycle

            class Circular(API):
                def call(self, method, params):
                    return cycle

            answer = call_api(Circular(), "run.status", {"run_id": "r"})
            self.assertEqual(answer["code"], "control_api_unavailable")

    def test_f02_unserializable_reply_becomes_typed_unavailable(self):
        answer = {"code": "ok"}
        answer["run"] = answer
        result = call_api(API(answer), "run.status", {"run_id": "run-1"})
        self.assertEqual(result["code"], "control_api_unavailable")

    def test_f02_wrong_code_type_does_not_abort_answer_handling(self):
        app = fresh()
        app._api = API({"code": []})
        app.request("run.status", {"run_id": "run-1"})
        self.assertEqual(app.last["code"], "control_api_unavailable")

    def test_f02_wrong_stop_shape_does_not_abort_a_stop_answer(self):
        app = fresh()
        app.on_answer("run.list", {}, {"code": "ok", "runs": [
            {"run_id": "run-1", "state": "running", "stop": "pending"}]})
        app.on_answer("run.stop", {"run_id": "run-1"}, {"code": "stopped", "run_id": "run-1"})
        self.assertIn("stopped", app.status_row())
        self.assertTrue(any(e.event_type == "answer" and "run.stop" in e.text for e in app.log))

    def test_f4_wrongly_typed_code_is_a_condition_not_typeerror(self):
        for code in ([], {}, ["ok"]):
            with self.subTest(code=code):
                conditions = p.keyed_conditions({"code": code})
                self.assertIn(p.UNAVAILABLE_LABEL, [label for label, _, _ in conditions])
                self.assertIn("code", " ".join(text for _, _, text in conditions).lower())


class EventLogReadTest(unittest.TestCase):
    def test_f1_iterator_survives_a_concurrent_append(self):
        log = EventLog(cap=4)
        add(log, "first")
        add(log, "second")
        it = iter(log)
        self.assertEqual(next(it).seq, 1)
        add(log, "third")
        self.assertEqual(next(it).seq, 2)

    def test_f1_since_and_latest_can_overlap_a_writer(self):
        for name, needle, arg in (("since", "if event.seq <= seq:", 0),
                                  ("latest", "if event.event_type == event_type:", "missing")):
            with self.subTest(reader=name):
                log = EventLog(cap=4)
                add(log, "first")
                add(log, "second")
                method = getattr(log, name)
                lines, start = inspect.getsourcelines(method)
                pause_line = start + next(i for i, line in enumerate(lines) if needle in line)
                entered, release = threading.Event(), threading.Event()
                values, errors = [], []

                def trace(frame, event, arg):
                    if (event == "line" and frame.f_code is method.__func__.__code__
                            and frame.f_lineno == pause_line and not entered.is_set()):
                        entered.set()
                        if not release.wait(2):
                            raise TimeoutError("reader schedule deadline")
                    return trace

                def reader():
                    sys.settrace(trace)
                    try:
                        values.append(method(arg))
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                    finally:
                        sys.settrace(None)

                worker = threading.Thread(target=reader, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2), "reader did not reach the schedule point")
                    if log._lock.acquire(blocking=False):
                        log._lock.release()
                        add(log, "third")
                    else:
                        release.set()
                        worker.join(2)
                        self.assertFalse(worker.is_alive())
                        add(log, "third")
                finally:
                    release.set()
                    worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                if name == "since":
                    events, skipped = values[0]
                    self.assertEqual([e.seq for e in events], [1, 2])
                    self.assertEqual(skipped, 0)
                else:
                    self.assertIsNone(values[0])

    def race_at(self, method, needle, argument):
        log = events.EventLog(4)
        add(log, "first")
        add(log, "second")
        reached, attempted, written = (threading.Event() for _ in range(3))
        result = {}
        function = getattr(events.EventLog, method)
        lines, start = inspect.getsourcelines(function)
        target = next((start + i for i, line in enumerate(lines) if needle in line), None)
        if target is None:
            target = next(start + i for i, line in enumerate(lines) if "return " in line)

        def trace(frame, event, arg):
            if frame.f_code is function.__code__ and event == "line" and frame.f_lineno == target and not reached.is_set():
                reached.set()
                if not attempted.wait(1):
                    raise AssertionError("writer did not attempt")
                written.wait(0.1)
            return trace

        def read():
            sys.settrace(trace)
            try:
                result["value"] = getattr(log, method)(argument)
            except Exception as exc:
                result["error"] = exc
            finally:
                sys.settrace(None)

        def write():
            attempted.set()
            add(log, "third")
            written.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        self.assertTrue(reached.wait(1), "reader did not reach synchronization point")
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        reader.join(2)
        writer.join(2)
        self.assertFalse(reader.is_alive() or writer.is_alive(), "bounded workers did not finish")
        self.assertNotIn("error", result, repr(result.get("error")))
        return result["value"]

    def test_since_during_append(self):
        found, skipped = self.race_at("since", "if event.seq <= seq", 0)
        self.assertIn([e.seq for e in found], ([1, 2], [1, 2, 3]))
        self.assertEqual(skipped, 0)

    def test_latest_during_append(self):
        self.assertIsNone(self.race_at("latest", "if event.event_type == event_type", "absent"))

    def test_since_does_not_report_retained_event_as_evicted(self):
        found, skipped = self.race_at("since", "first = newer[0].seq", 2)
        self.assertIn([e.seq for e in found], ([], [3]))
        self.assertEqual(skipped, 0)

    def test_iterator_snapshot_survives_concurrent_append(self):
        errors = []
        for round_no in range(20):
            log = events.EventLog(4)
            add(log, "first")
            add(log, "second")
            reader = iter(log)
            first = next(reader)
            writer = threading.Thread(target=lambda: add(log, "third"), daemon=True)
            writer.start()
            writer.join(1)
            self.assertFalse(writer.is_alive())
            try:
                snapshot = [first, *reader]
                self.assertEqual([e.seq for e in snapshot], [1, 2])
            except RuntimeError as exc:
                errors.append((round_no, str(exc)))
        self.assertEqual(errors, [])


class ConditionsLockTest(unittest.TestCase):
    def test_F06_conditions_survive_a_concurrent_pending_stop_answer(self):
        app = TuiApp(API(), clock=lambda: 1000.0, environ={"NO_COLOR": "1"})
        app.open()
        app.on_answer("run.stop", {"run_id": "old"}, {"code": "pending", "run_id": "old"})
        entered, release, updated = (threading.Event() for _ in range(3))
        errors = []
        source, first_line = inspect.getsourcelines(TuiApp._current)
        target = first_line + next(i for i, line in enumerate(source)
                                   if "found[(p.UNCERTAIN, subject)]" in line)
        code = TuiApp._current.__code__

        def trace(frame, event, arg):
            if event == "line" and frame.f_code is code and frame.f_lineno == target and not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("reader release missing")
            return trace

        def read():
            sys.settrace(trace)
            try:
                app.conditions()
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                sys.settrace(None)

        def update():
            try:
                app.on_answer("run.stop", {"run_id": "new"}, {"code": "pending", "run_id": "new"})
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                updated.set()

        workers = [threading.Thread(target=read, daemon=True)]
        workers[0].start()
        try:
            self.assertTrue(entered.wait(2))
            workers.append(threading.Thread(target=update, daemon=True))
            workers[-1].start()
            updated.wait(0.05)
        finally:
            release.set()
            for worker in workers:
                worker.join(2)
        self.assertFalse(any(t.is_alive() for t in workers))
        self.assertEqual(errors, [])
        self.assertEqual(len(app._uncertain), 2)


class LogCursorTest(unittest.TestCase):
    def test_f03_concurrent_append_between_snapshot_and_cursor_is_not_skipped(self):
        snapped, appended = threading.Event(), threading.Event()
        errors = []

        class PausingLog(EventLog):
            paused = False

            def since(self, seq):
                result = super().since(seq)
                if not self.paused:
                    self.paused = True
                    snapped.set()
                    if not appended.wait(2):
                        raise AssertionError("writer did not finish")
                return result

        log = PausingLog()
        log.append(0, "notice", "recursion", "live", "first", "first", "keys")
        reader = LogReader(types.SimpleNamespace(log=log))

        def writer():
            try:
                if not snapped.wait(2):
                    raise AssertionError("reader did not snapshot")
                log.append(0, "notice", "recursion", "live", "second", "second", "keys")
            except BaseException as exc:
                errors.append(type(exc).__name__)
            finally:
                appended.set()

        worker = threading.Thread(target=writer, daemon=True)
        worker.start()
        try:
            first = reader.new_lines()
            second = reader.new_lines()
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(first + second, ["first", "second"])


class JsonlValidationTest(unittest.TestCase):
    def valid(self):
        return events.Event(1, 0, "output", "engine", "untrusted", "live", "hello", "hello", "runs").as_dict()

    def assert_invalid(self, text, line_no):
        try:
            events.from_jsonl(text)
        except Exception as exc:
            self.assertIsInstance(exc, ValueError)
            self.assertEqual(type(exc).__module__, events.__name__)
            self.assertEqual(getattr(exc, "code", None), "event_invalid")
            self.assertEqual(getattr(exc, "line_no", None), line_no)
        else:
            self.fail("invalid event was accepted")

    def test_rejects_invalid_field_types_and_trust(self):
        for changes in ({"speech": ["hello"]}, {"seq": "one"}, {"seq": True}, {"trust": "trusted"},
                        {"event_type": "unknown"}):
            with self.subTest(changes=changes):
                self.assert_invalid(json.dumps({**self.valid(), **changes}) + "\n", 1)

    def test_truncated_or_wrong_shape_record_has_typed_line_error(self):
        good = json.dumps(self.valid()) + "\n"
        for bad in ('{"seq":', '{}\n', '[]\n'):
            with self.subTest(bad=bad):
                self.assert_invalid(good + bad, 2)

    def test_valid_roundtrip_preserves_unicode_and_noninitial_sequence(self):
        log = events.EventLog(1)
        add(log, "first")
        add(log, "a\u2028b\u2029c")
        text = events.to_jsonl(log)
        self.assertEqual(events.from_jsonl(text), list(log))
        self.assertEqual(len(text.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
