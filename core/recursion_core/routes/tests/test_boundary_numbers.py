"""RC-01 Claude half, RC-07, RC-08, RC-15 and RC-17.

The Codex half of RC-01 is not in this draft. RC-08 includes the Codex witness because that
number is copied through the shared scalar helper.
"""

from __future__ import annotations

import json
import subprocess
import unittest

from recursion_core.routes import ClaudeCodeAdapter, CodexAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import (
    FakeClock, FakeLauncher, FakeProcess, claude_ok, codex_script, request, runs_adapter,
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


@runs_adapter
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
                svc, _, _, _ = _claude([script])
                res = self._handle(svc, request())
                self.assertIn(res["code"], c.CODES)

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


if __name__ == "__main__":
    unittest.main()
