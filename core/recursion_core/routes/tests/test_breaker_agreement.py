"""RC-14, RC-09, RC-16, and the Claude half of RC-06.

The Codex half of RC-06 is not in this draft. Fake clock and fake processes only.
"""

from __future__ import annotations

import unittest

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes import limits as lim
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


_ROUTE = {"engine": "claude_code", "model": "m", "connection": {"kind": "subscription", "id": "A"}}
_LIMITS = {"on_limit": "ask", "fallback": []}
_CONN = "subscription:A"


def _open_then_probe(breaker, clock, *, gap_s):
    """Book a connection limit, lease one probe, then book a later connection limit while it runs."""

    breaker.record(_CONN, "m", "tA", {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "s7")})
    clock.advance(1)
    probe = breaker.admit(_CONN, "m", "tB", deadline_s=60)
    clock.advance(gap_s)
    reset_at = clock() + 300
    breaker.record(_CONN, "m", "tA", {"code": c.PLAN_LIMIT,
                                      "limit": c.limit_signal("connection", "s7", reset_at=reset_at)})
    return probe, reset_at


class Answer488Test(unittest.TestCase):
    def test_probe_success_keeps_a_limit_booked_while_it_ran(self):
        # The review's two witnesses: a limit at the same clock reading as the lease, and a limit
        # 30 s later. The probe's success is about the run that already started, so the fresh
        # request stays out until the vendor window.
        for name, gap_s, early_s in (("same_instant", 0, 299), ("thirty_seconds", 30, 299)):
            with self.subTest(name):
                clock = FakeClock(1_000_000.0)
                breaker = ConnectionBreaker(clock, cooldown_s=1)
                probe, reset_at = _open_then_probe(breaker, clock, gap_s=gap_s)
                self.assertTrue(probe["admitted"] and probe["probe"])
                self.assertEqual(breaker._keys[(_CONN,)]["probe"], probe["probe"])
                clock.advance(1)
                effect = breaker.record(_CONN, "m", "tB", {"code": None}, probe["probe"])
                fresh = breaker.admit(_CONN, "m", "tC", deadline_s=60)
                self.assertFalse(fresh["admitted"], "a fresh request launched before the vendor reset")
                self.assertEqual(fresh["code"], c.BREAKER_OPEN)
                self.assertEqual(fresh["retry_at"], reset_at)
                self.assertEqual(reset_at - clock(), early_s)
                self.assertEqual(breaker.state(_CONN), "open")
                self.assertEqual(effect, "probe_released")
                self.assertIsNone(breaker._keys[(_CONN,)]["probe"])

    def test_probe_success_with_no_later_limit_admits_the_next_request(self):
        clock = FakeClock(1_000_000.0)
        breaker = ConnectionBreaker(clock, cooldown_s=1)
        breaker.record(_CONN, "m", "tA", {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "s7")})
        clock.advance(1)
        probe = breaker.admit(_CONN, "m", "tB", deadline_s=60)
        self.assertEqual(breaker.record(_CONN, "m", "tB", {"code": None}, probe["probe"]), "closed")
        self.assertEqual(breaker.state(_CONN), "closed")
        nxt = breaker.admit(_CONN, "m", "tC", deadline_s=60)
        self.assertTrue(nxt["admitted"])
        self.assertIsNone(nxt["probe"])

    def test_notice_on_a_truthy_non_mapping_limit(self):
        for bad in ("limit", [1, 2], 1):
            with self.subTest(bad=bad):
                notice = lim.notice({"code": c.PLAN_LIMIT, "limit": bad, "retry_at": None, "detail": "typed"},
                                    _ROUTE, _LIMITS)
                self.assertEqual(notice["reset_at"], None)
                self.assertEqual(notice["choices"][0]["until"], None)

    def test_notice_wait_is_a_number_or_none(self):
        for bad in ("soon", True, [1], None):
            with self.subTest(bad=bad):
                notice = lim.notice(
                    {"code": c.PLAN_LIMIT,
                     "limit": {"scope": "connection", "model": None, "reset_at": bad, "source": "s"},
                     "retry_at": None, "detail": "typed"},
                    _ROUTE, _LIMITS)
                self.assertIsNone(notice["reset_at"])
                self.assertEqual(notice["choices"][0]["until"], None)
                self.assertEqual(notice["choices"][0]["detail"], "the breaker gave no time")
        booked = lim.notice(
            {"code": c.PLAN_LIMIT,
             "limit": {"scope": "connection", "model": None, "reset_at": 2_000_000.0, "source": "s"},
             "retry_at": 1_000_300.0, "detail": "typed"},
            _ROUTE, _LIMITS)
        self.assertEqual(booked["reset_at"], 1_000_300.0)
        self.assertEqual(booked["choices"][0], {"action": "wait", "until": 1_000_300.0})

    def test_connection_id_with_model_marker_reports_connection_scope(self):
        odd = "subscription:a#model:b"
        clock = FakeClock()
        breaker = ConnectionBreaker(clock)
        breaker.record(odd, "m", "t", {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "s7")})
        refused = breaker.admit(odd, "m", "t2")
        self.assertFalse(refused["admitted"])
        route = {"engine": "claude_code", "model": "m",
                 "connection": {"kind": "subscription", "id": "a#model:b"}}
        notice = lim.notice(c.result("r1", refused["code"], detail=refused["key"], retry_at=refused["retry_at"]),
                            route, _LIMITS)
        self.assertEqual(notice["scope"], "connection")
        breaker = ConnectionBreaker(clock)
        breaker.record("subscription:a", "b", "t",
                       {"code": c.PLAN_LIMIT, "limit": c.limit_signal("model", "s7", model="b")})
        refused = breaker.admit("subscription:a", "b", "t2")
        self.assertFalse(refused["admitted"])
        notice = lim.notice(c.result("r2", refused["code"], detail=refused["key"], retry_at=refused["retry_at"]),
                            route, _LIMITS)
        self.assertEqual(notice["scope"], "model")

    def test_past_reset_uses_backoff(self):
        clock = FakeClock(1_000_000.0)
        breaker = ConnectionBreaker(clock, cooldown_s=300)
        res = c.result("r", c.PLAN_LIMIT, limit=c.limit_signal("connection", "s7", reset_at=999_000.0))
        self.assertEqual(breaker.record("subscription:A", "m", "t", res), "opened")
        self.assertEqual(res["retry_at"], 1_000_300.0)
        self.assertEqual(breaker.state("subscription:A"), "open")
        self.assertFalse(breaker.admit("subscription:A", "m", "t2", deadline_s=60)["admitted"])


if __name__ == "__main__":
    unittest.main()
