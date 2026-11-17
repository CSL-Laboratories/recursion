"""RC-14, RC-09, RC-16, and the Claude half of RC-06.

The Codex half of RC-06 is not in this draft. Fake clock and fake processes only.
"""

from __future__ import annotations

import unittest

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeProcess, request


class _Launcher:
    def __init__(self, script, clock):
        self.script, self.clock = script, clock

    def __call__(self, argv, **kw):
        return FakeProcess(self.script, self.clock)


class BreakerAgreementTest(unittest.TestCase):
    def test_rc06_connection_limit_survives_later_model_limit(self):
        cases = {
            "later_event": [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}},
                            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day_opus"}},
                            {"type": "result", "subtype": "error_during_execution", "is_error": True}],
            "later_result": [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}},
                             {"type": "result", "subtype": "error_during_execution", "is_error": True,
                              "api_error_params": {"rate_limit_info": {"rateLimitType": "seven_day_opus"}}}],
        }
        for name, script in cases.items():
            with self.subTest(name):
                clock = FakeClock()
                launcher = _Launcher(script, clock)
                breaker = ConnectionBreaker(clock)
                svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})}, breaker, clock)
                svc.handle(request())
                self.assertEqual(breaker.state("subscription:login-A"), "open")

    def test_rc09_backoff_stays_capped(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        for i in range(1025):
            try:
                breaker.record("subscription:A", "m", "t", {"code": c.PLAN_LIMIT,
                                                           "limit": c.limit_signal("connection", "s7.typed")})
            except OverflowError as exc:
                self.fail(f"record {i + 1} raised {exc}")

    def test_rc14_late_limit_keeps_live_probe_lease(self):
        clock = FakeClock()
        breaker = ConnectionBreaker(clock, cooldown_s=1)
        conn = "subscription:A"
        breaker.record(conn, "m", "tA", {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "s7")})
        clock.advance(1)
        b = breaker.admit(conn, "m", "tB", deadline_s=60)
        self.assertTrue(b["admitted"] and b["probe"])
        breaker.record(conn, "m", "tA", {"code": c.PLAN_LIMIT,
                                        "limit": c.limit_signal("connection", "s7", reset_at=clock() + 1)})
        clock.advance(1)
        c_ = breaker.admit(conn, "m", "tC", deadline_s=60)
        self.assertFalse(c_["admitted"], "a second probe was admitted while probe B still runs")

    def test_rc16_wait_notice_matches_breaker(self):
        clock = FakeClock()

        class Late(FakeProcess):
            def read_line(self, timeout_s):
                line = super().read_line(timeout_s)
                if line is not None and '"result"' in line:
                    clock.advance(11)
                return line

        def launcher(argv, **kw):
            return Late([{"type": "rate_limit_event", "rate_limit_info": {
                "status": "rejected", "rateLimitType": "five_hour", "resetsAt": clock() + 10}},
                {"type": "result", "subtype": "error_during_execution", "is_error": True}], clock)

        breaker = ConnectionBreaker(clock)
        svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {}, wall_clock=clock)},
                           breaker, clock, limits={"on_limit": "wait", "fallback": []})
        res = svc.handle(request())
        until = res["limit_notice"]["reset_at"]
        clock.now = until
        again = breaker.admit("subscription:login-A", "claude-sonnet", "t1", deadline_s=60)
        self.assertTrue(again["admitted"], f"the notice said wait until {until}; the breaker still refuses")


if __name__ == "__main__":
    unittest.main()
