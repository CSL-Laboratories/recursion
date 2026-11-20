"""RC-01 Claude half, RC-07, RC-08, RC-15 and RC-17.

The Codex half of RC-01 is not in this draft. RC-08 includes the Codex witness because that
number is copied through the shared scalar helper.
"""

from __future__ import annotations

import enum
import json
import subprocess
import unittest

from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.base import scalar
from recursion_core.routes.breaker import _is_signal
from recursion_core.routes.tests.fakes import (
    FakeClock, FakeLauncher, FakeProcess, claude_ok, codex_script, request,
)

HUGE = "1" + "0" * 400


def _claude(scripts, clock=None, **kw):
    clock = clock or FakeClock()
    launcher = FakeLauncher(scripts, clock)
    breaker = ConnectionBreaker(clock)
    svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})}, breaker, clock, **kw)
    return svc, launcher, breaker, clock


def _codex(script, clock=None):
    clock = clock or FakeClock()
    launcher = FakeLauncher([script], clock)
    breaker = ConnectionBreaker(clock)
    svc = RouteService({"codex": CodexAdapter(launcher, clock, lambda *_: "", {})}, breaker, clock)
    return svc, launcher, breaker, clock


class BoundaryNumberTest(unittest.TestCase):
    def _handle(self, svc, req):
        try:
            return svc.handle(req)
        except Exception as exc:  # noqa: BLE001 - the defect is that something escapes
            self.fail(f"escaped RouteService.handle: {type(exc).__name__}: {exc}")

    def test_rc01_claude_nested_shapes_are_typed(self):
        cases = {
            "rateLimitType_list": [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": []}},
                                   {"type": "result", "subtype": "error_during_execution", "is_error": True}],
            "errors_int": [{"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": 7}],
        }
        for name, script in cases.items():
            with self.subTest(name):
                svc, _, breaker, _ = _claude([script])
                res = self._handle(svc, request())
                self.assertIn(res["code"], c.CODES)
                if name == "rateLimitType_list":
                    # An unlisted type, including one that cannot be a dict key, stays connection scope.
                    self.assertEqual(res["code"], c.PLAN_LIMIT)
                    self.assertEqual(res["detail"], "typed limit")
                    self.assertEqual(res["limit"]["scope"], "connection")
                    self.assertIsNone(res["limit"]["model"])
                    self.assertEqual(breaker.state("subscription:login-A"), "open")
                if name == "errors_int":
                    self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "error_during_execution"))

    def test_rc07_huge_integers_are_typed(self):
        with self.subTest("deadline_s"):
            svc, launcher, _, _ = _claude([claude_ok()])
            res = self._handle(svc, request(deadline_s=json.loads(HUGE)))
            self.assertEqual(res["code"], c.REQUEST_INVALID)
            self.assertEqual(launcher.calls, [])
        with self.subTest("vendor_reset"):
            clock = FakeClock()
            try:
                c.vendor_reset(json.loads(HUGE), clock, clock)
            except OverflowError as exc:
                self.fail(f"vendor_reset raised {exc}")
        with self.subTest("claude_resetsAt"):
            line = '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":' + HUGE + '}}'
            svc, _, _, _ = _claude([[line, {"type": "result", "subtype": "error_during_execution", "is_error": True}]])
            res = self._handle(svc, request())
            self.assertEqual(res["code"], c.PLAN_LIMIT)

    def test_rc08_results_stay_strict_json(self):
        claude_line = '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1e309}}'
        s = codex_script()
        codex_line = '{"id":2,"result":{"rateLimits":{"limitId":"codex","primary":{"usedPercent":1e309}}}}'
        for name, (svc, req) in {
            "claude": (_claude([[claude_line, *claude_ok()]])[0], request()),
            "codex": (_codex([s[0], codex_line, *s[2:]])[0], request(engine="codex", model="gpt-5")),
        }.items():
            with self.subTest(name):
                res = self._handle(svc, req)
                try:
                    json.dumps(res, allow_nan=False)
                except ValueError as exc:
                    self.fail(f"result is not strict JSON: {exc}")

    def test_rc15_malformed_reset_is_refused_before_state_changes(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        try:
            breaker.record("subscription:A", "m", "t", {"code": c.PLAN_LIMIT, "limit": {
                "scope": "connection", "source": "s7", "reset_at": "later"}})
        except TypeError as exc:
            self.fail(f"record raised {exc}")
        self.assertEqual(breaker.state("subscription:A"), "closed")

    def test_rc17_unlaunchable_model_is_typed(self):
        clock = FakeClock()

        def launcher(argv, *, env, shell=False):
            return subprocess.Popen(["/bin/true", *argv[1:]], env={}, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})},
                           ConnectionBreaker(clock), clock)
        res = self._handle(svc, request(model="m\x00"))
        self.assertIn(res["code"], (c.REQUEST_INVALID, c.LAUNCH_REFUSED))

    def test_limit_signal_drops_a_bool_and_a_huge_int(self):
        huge = json.loads(HUGE)
        with self.subTest("huge"):
            try:
                sig = c.limit_signal("connection", "s", reset_at=huge)
            except OverflowError as exc:
                self.fail(f"limit_signal raised {type(exc).__name__}: {exc}")
            self.assertIsNone(sig["reset_at"])
        with self.subTest("bool"):
            sig = c.limit_signal("connection", "s", reset_at=True)
            self.assertIsNone(sig["reset_at"])
            breaker = ConnectionBreaker(FakeClock())
            effect = breaker.record("subscription:A", "m", "t", {"code": c.PLAN_LIMIT, "limit": sig})
            self.assertEqual(effect, "opened")
            self.assertEqual(breaker.state("subscription:A"), "open")
        with self.subTest("kept"):
            self.assertEqual(c.limit_signal("connection", "s", reset_at=12.5)["reset_at"], 12.5)

    def test_rc17_lone_surrogate_model_is_request_invalid(self):
        clock = FakeClock()
        calls = []

        def launcher(argv, *, env, shell=False):
            calls.append(argv)
            return subprocess.Popen(["/bin/true", *argv[1:]], env={}, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})},
                           ConnectionBreaker(clock), clock)
        res = self._handle(svc, request(model="m\ud800"))
        self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "route.model"))
        self.assertEqual(calls, [])

    def test_record_of_a_non_dict_returns_none(self):
        breaker = ConnectionBreaker(FakeClock())
        try:
            effect = breaker.record("subscription:A", "m", "t", ["nope"])
        except AttributeError as exc:
            self.fail(f"record raised {type(exc).__name__}: {exc}")
        self.assertEqual(effect, "none")
        self.assertEqual(breaker.state("subscription:A"), "closed")

    def test_deadline_bool_and_zero_are_request_invalid(self):
        for value in (True, 0, 0.0):
            with self.subTest(value=value):
                svc, launcher, _, _ = _claude([claude_ok()])
                res = self._handle(svc, request(deadline_s=value))
                self.assertEqual((res["code"], res["detail"]), (c.REQUEST_INVALID, "deadline_s"))
                self.assertEqual(launcher.calls, [])

    def test_scalar_refuses_an_int_enum(self):
        class _Sample(enum.IntEnum):
            ONE = 1

        self.assertIsNone(scalar(_Sample.ONE))
        self.assertEqual(scalar(1), 1)

    def test_is_signal_refuses_a_bool_reset(self):
        self.assertFalse(_is_signal({"scope": "connection", "source": "s", "reset_at": True}))
        self.assertTrue(_is_signal({"scope": "connection", "source": "s", "reset_at": None}))
        self.assertTrue(_is_signal({"scope": "connection", "source": "s", "reset_at": 1.5}))

    def test_engine_error_keeps_the_usage_event(self):
        clock = FakeClock()
        proc = _RaisesAfterScript([
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1,
                                                             "rateLimitType": "five_hour"}},
        ], clock)

        def launcher(argv, **kwargs):
            return proc

        svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})},
                           ConnectionBreaker(clock), clock)
        res = self._handle(svc, request())
        self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, "TypeError"))
        self.assertEqual(res["events"][0]["kind"], "usage")
        self.assertEqual(res["events"][0]["source"], "claude.rate_limit_event")
        self.assertEqual(res["events"][0]["data"]["status"], "allowed")


class _RaisesAfterScript(FakeProcess):
    def read_line(self, timeout_s):
        if not self.script:
            raise TypeError("boom")
        return super().read_line(timeout_s)


if __name__ == "__main__":
    unittest.main()
