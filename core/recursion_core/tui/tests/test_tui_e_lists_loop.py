"""Lists, loop lifetime, delivery text, and usage privacy. Evidence tests for this draft."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from recursion_core.tui import events, protocol
from recursion_core.tui.app import Loop, TuiApp
from recursion_core.tui.protocol import CommandError


def run(rid):
    return {"run_id": rid, "state": "running"}


def walk(pages):
    answers = iter(pages)
    return protocol.list_all_runs(lambda method, params: next(answers))


class FakeAPI:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def call(self, method, params):
        self.calls.append(method)
        return self.answer


class SolAPI:
    def __init__(self):
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
        return {"code": "started", "run": {"run_id": "new", "state": "running"}}


def new_app(api):
    return TuiApp(api, clock=lambda: 1000.0, environ={"NO_COLOR": "1"})


def fresh():
    return TuiApp(SolAPI(), clock=time.monotonic, environ={"NO_COLOR": "1"})


class DeliveryConditionTest(unittest.TestCase):
    def test_f3_unknown_delivery_survives_condition_rendering(self):
        def conditions(delivered):
            return protocol.keyed_conditions({"code": protocol.UNAVAILABLE, "detail": "request:TransportRefused",
                                              "delivered": delivered})
        unknown, unsent = conditions("unknown"), conditions(False)
        self.assertNotEqual(unknown, unsent)
        text = " ".join(text for _, _, text in unknown).lower()
        self.assertIn("unknown", text)
        self.assertIn("may", text)


class LaterPageReconciliationTest(unittest.TestCase):
    def test_f5_a_later_page_keeps_its_unreconciled_condition(self):
        for flagged in (1, 2):
            with self.subTest(flagged_page=flagged + 1):
                pages = [{"code": "ok", "runs": [run("a")], "next_after": "a"},
                         {"code": "ok", "runs": [run("b")], "next_after": "b"},
                         {"code": "ok", "runs": [run("c")]}]
                pages[flagged]["reconciliation"] = "unreconciled"
                joined = walk(pages)
                self.assertEqual(joined.get("reconciliation"), "unreconciled")
                self.assertIn(protocol.UNRECONCILED, [label for label, _, _ in protocol.keyed_conditions(joined)])


class SinglePageValidationTest(unittest.TestCase):
    def test_single_page_rejects_invalid_or_duplicate_runs(self):
        for runs in ("bad", [7], [{}], [{"run_id": "a"}, {"run_id": "a"}]):
            with self.subTest(runs=runs):
                result = protocol.list_all_runs(FakeAPI({"code": "ok", "runs": runs}).call)
                self.assertEqual(result["code"], protocol.LIST_INCOMPLETE)
                self.assertNotIn("runs", result)

    def test_bad_snapshot_keeps_start_disabled(self):
        api = FakeAPI({"code": "ok", "runs": [7]})
        app = TuiApp(api, clock=lambda: 0, environ={})
        app.open()
        self.assertTrue(app.watch_only)
        app.command("start --tenant t --engine e --prompt hi")
        self.assertNotIn("run.start", api.calls)

    def test_valid_one_page_preserves_fields(self):
        answer = {"code": "ok", "runs": [{"run_id": "a"}], "stale": True}
        self.assertEqual(protocol.list_all_runs(FakeAPI(answer).call), answer)

    def test_f6_one_page_with_duplicate_ids_is_not_a_complete_list(self):
        result = walk([{"code": "ok", "runs": [run("a"), run("a")]}])
        self.assertEqual(result.get("code"), protocol.LIST_INCOMPLETE)
        self.assertNotIn("runs", result)


class LoopStartTest(unittest.TestCase):
    def test_f06_second_start_must_not_leave_lane_threads_alive(self):
        app = fresh()
        loop = Loop(app, SolAPI(), tick_s=0.01)
        loop.join_timeout_s = 0.05
        loop.start()
        loop.start()
        app.close()
        try:
            stopped = loop.shutdown()
            self.assertTrue(stopped, "a repeated start leaves extra lane workers behind")
        finally:
            for lane in loop._lanes.values():
                lane.put(None)
                lane.put(None)
            for worker in loop._threads:
                worker.join(2)


class PublicMutatorTest(unittest.TestCase):
    def test_F09_public_mutators_change_nothing_after_close(self):
        api = SolAPI()
        app = new_app(api)
        app.open()
        app.close()
        before = (app.attached, app.selected, app.notice, list(app.errors), app.last_error,
                  list(app.log), list(api.calls))
        app.attach("different")
        app.stop("r")
        app.command("invalid")
        after = (app.attached, app.selected, app.notice, list(app.errors), app.last_error,
                 list(app.log), list(api.calls))
        self.assertEqual(after, before)

    def test_f07_public_commands_after_close_preserve_the_final_state(self):
        app = fresh()
        app.command("not-a-command")
        app.close()
        before = (app.selected, app.attached, app.notice, list(app.errors), app.last_error, list(app.log))
        app.attach("late-run")
        app.command("status another-run")
        app.stop("late-run")
        app.command("another-invalid-command")
        after = (app.selected, app.attached, app.notice, list(app.errors), app.last_error, list(app.log))
        self.assertEqual(after, before)


class CommandTypeTest(unittest.TestCase):
    def test_F12_wrong_command_type_is_command_error(self):
        with self.assertRaises(CommandError):
            protocol.parse_command(1)


class DiagnosticPrivacyTest(unittest.TestCase):
    def test_rejected_sensitive_argument_value_is_not_in_diagnostic(self):
        for extra in ("--api-key DEMO_PRIVATE_VALUE", "--token=DEMO_PRIVATE_VALUE"):
            with self.subTest(extra=extra):
                with self.assertRaises(protocol.CommandError) as caught:
                    protocol.parse_command("start --tenant t --engine e --prompt hi " + extra)
                self.assertNotIn("DEMO_PRIVATE_VALUE", str(caught.exception))

    def test_refusal_does_not_persist_rejected_private_value(self):
        api = FakeAPI({"code": "ok", "runs": []})
        app = TuiApp(api, clock=lambda: 0, environ={})
        app.open()
        app.command("start --tenant t --engine e --prompt hi --api-key DEMO_PRIVATE_VALUE")
        self.assertEqual(api.calls, ["run.list"])
        self.assertNotIn("DEMO_PRIVATE_VALUE", events.to_jsonl(app.log))


class InlinePacingWitness(unittest.TestCase):
    def test_D33_scripted_quit_can_overtake_stop_answer(self):
        from recursion_core.tui.tests import test_inline as original
        from recursion_core.tui.tests.fakes import FakeDriver
        ready, stopping, release, quit_read = (threading.Event() for _ in range(4))
        driver = FakeDriver()
        driver.start_run()
        actual_call = driver.api.call
        actual_read = original.FakeTerminal.read_key
        actual_answer = TuiApp.on_answer
        errors, results = [], []

        def call(method, params):
            if method == "run.stop":
                stopping.set()
                if not release.wait(3):
                    raise AssertionError("bounded API release missing")
            return actual_call(method, params)

        def answer(app, method, params, payload, asked=None):
            actual_answer(app, method, params, payload, asked=asked)
            if method == "run.list":
                ready.set()

        def read(terminal, timeout):
            if not ready.wait(2):
                raise AssertionError("initial list did not settle")
            key = actual_read(terminal, timeout)
            if key == "q":
                quit_read.set()
            return key

        def run_body():
            try:
                results.append(original.run_tui(["--no-alt-screen"], ["s", "y"], driver))
            except BaseException as exc:
                errors.append(repr(exc))

        worker = threading.Thread(target=run_body, daemon=True)
        with mock.patch.object(driver.api, "call", side_effect=call), \
                mock.patch.object(TuiApp, "on_answer", answer), \
                mock.patch.object(original.FakeTerminal, "read_key", read):
            worker.start()
            try:
                self.assertTrue(stopping.wait(2))
                quit_before_answer = quit_read.wait(0.5)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(results)
        self.assertFalse(quit_before_answer, "the actual inline fixture returned q while the Stop reply was held")


class ReadinessWaitWitness(unittest.TestCase):
    def test_D34_original_readiness_wait_is_bounded(self):
        from recursion_core.tui.tests import test_review_372 as original
        real_event = threading.Event
        made = []
        unbounded = []

        class StopWitness(Exception):
            pass

        class Event:
            def __init__(self):
                self.event = real_event()
                made.append(self)

            def __getattr__(self, name):
                return getattr(self.event, name)

            def wait(self, timeout=None):
                if self is made[0] and timeout is None:
                    unbounded.append(True)
                    raise StopWitness("prevented an indefinite wait in original test")
                return self.event.wait(min(timeout, 0.1) if timeout is not None else 0.1)

        case = original.ClosingTest("test_an_append_already_begun_is_refused_after_the_final_event")
        api = SolAPI()
        app = new_app(api)
        with mock.patch.object(original, "fresh", return_value=(app, api)), \
                mock.patch.object(threading, "Event", Event), \
                mock.patch.object(Loop, "tick", side_effect=ValueError("before append")):
            try:
                case.test_an_append_already_begun_is_refused_after_the_final_event()
            except (StopWitness, AssertionError):
                pass
        self.assertEqual(unbounded, [], "readiness wait has no timeout after worker exits with error")


if __name__ == "__main__":
    unittest.main()
