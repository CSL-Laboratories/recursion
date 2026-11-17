"""RC-02 and RC-11: Claude diagnostics carry codes, and no work starts after the deadline.

The marker and the two deadline witnesses are the routes-core triage tests for this draft only.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.profile_route import ProfileRouteAdapter
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, FakeProcess, claude_ok, request

MARK = "S7-TRIAGE-MARKER\nFORGED-LINE"


def _claude(scripts, clock=None, **kw):
    clock = clock or FakeClock()
    launcher = FakeLauncher(scripts, clock)
    breaker = ConnectionBreaker(clock)
    svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})}, breaker, clock, **kw)
    return svc, launcher, breaker, clock


class ClaudeDiagnosticsTest(unittest.TestCase):
    def test_rc02_claude_diagnostics_do_not_echo_engine_text(self):
        cases = {
            "auth_detail": [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                             "api_error_status": 401, "error": MARK}],
            "subtype_detail": [{"type": "result", "subtype": MARK, "is_error": True}],
            "retry_event": [{"type": "system", "subtype": "api_retry", "attempt": 1, "error_status": 529,
                             "error": MARK}, *claude_ok()],
        }
        for name, script in cases.items():
            with self.subTest(name):
                svc, _, _, _ = _claude([script])
                res = svc.handle(request())
                self.assertNotIn("S7-TRIAGE-MARKER", json.dumps(res))

    def test_rc02_retry_whitelist_auth_detail_and_subtype_bounds(self):
        """The retry event, the auth detail and the subtype token are exact codes, not engine text."""
        kept = {"attempt": 1, "max_retries": 2, "retry_delay_ms": 500, "error_status": 529}
        retry_cases = {
            "four_ints": ([{"type": "system", "subtype": "api_retry", **kept, "error": MARK, "message": MARK},
                           *claude_ok()], kept),
            "negatives": ([{"type": "system", "subtype": "api_retry", "attempt": -1, "max_retries": -2,
                            "retry_delay_ms": -3, "error_status": -4, "error": MARK}, *claude_ok()], {}),
            "floats": ([{"type": "system", "subtype": "api_retry", "attempt": 1.0, "max_retries": 2.5,
                         "retry_delay_ms": 1e9, "error_status": 529.0}, *claude_ok()], {}),
            "strings": ([{"type": "system", "subtype": "api_retry", "attempt": "1", "max_retries": MARK,
                          "retry_delay_ms": "3", "error_status": "529"}, *claude_ok()], {}),
            "bool_attempt": ([{"type": "system", "subtype": "api_retry", "attempt": True, "error_status": 1},
                              *claude_ok()], {"error_status": 1}),
        }
        for name, (script, data) in retry_cases.items():
            with self.subTest(name):
                svc, _, _, _ = _claude([script])
                res = svc.handle(request())
                self.assertIsNone(res["code"])
                self.assertEqual(res["events"][0]["data"], data)
                self.assertNotIn("S7-TRIAGE-MARKER", json.dumps(res["events"][0]))
        auth_cases = {
            "status_401": ([{"type": "result", "subtype": "error_during_execution", "is_error": True,
                             "api_error_status": 401, "error": MARK}], c.AUTH_FAILED, "401"),
            "name": ([{"type": "result", "is_error": True, "error": "authentication_failed"}],
                     c.AUTH_FAILED, "authentication_failed"),
            "oauth_name": ([{"type": "result", "is_error": True, "error": "oauth_token_expired"}],
                           c.AUTH_FAILED, "oauth_token_expired"),
            "status_float": ([{"type": "result", "is_error": True, "api_error_status": 401.0}],
                             c.AUTH_FAILED, "auth_failed"),
            "text_without_status": ([{"type": "result", "is_error": True, "error": MARK, "subtype": "error"}],
                                    c.ENGINE_ERROR, "error"),
        }
        for name, (script, code, detail) in auth_cases.items():
            with self.subTest(name):
                svc, _, _, _ = _claude([script])
                res = svc.handle(request())
                self.assertEqual((res["code"], res["detail"]), (code, detail))
                self.assertNotIn("S7-TRIAGE-MARKER", json.dumps(res))
        token = "a" * 64
        subtype_cases = {
            "len_64": (token, token),
            "len_65": ("a" * 65, "error"),
            "digit_first": ("1abc", "error"),
            "trailing_underscore": ("abc_", "abc_"),
            "inner_underscores": ("a__b", "a__b"),
            "marker": (MARK, "error"),
        }
        for name, (subtype, detail) in subtype_cases.items():
            with self.subTest(name):
                svc, _, _, _ = _claude([[{"type": "result", "subtype": subtype, "is_error": True, "result": MARK}]])
                res = svc.handle(request())
                self.assertEqual((res["code"], res["detail"]), (c.ENGINE_ERROR, detail))
                self.assertNotIn("S7-TRIAGE-MARKER", json.dumps(res))


class DeadlineWorkTest(unittest.TestCase):
    def test_rc11_no_work_after_deadline(self):
        with self.subTest("launch_spends_deadline"):
            clock = FakeClock()
            procs = []

            def launcher(argv, **kw):
                clock.advance(2)
                procs.append(FakeProcess(claude_ok(), clock))
                return procs[-1]

            svc = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, lambda *_: "", {})},
                               ConnectionBreaker(clock), clock)
            res = svc.handle(request(deadline_s=1))
            self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
            self.assertEqual(res["detail"], "recursion deadline")
            self.assertEqual(res["breaker_effect"], "none")
            self.assertEqual(procs[0].sent, [], "the prompt was written after the deadline")
            self.assertEqual(procs[0].fenced, ["deadline"])
        with self.subTest("admission_wait"):
            svc, launcher, breaker, clock = _claude([claude_ok()])
            real = breaker.admit

            def slow_admit(*a, **k):
                clock.advance(61)
                return real(*a, **k)

            breaker.admit = slow_admit
            res = svc.handle(request(deadline_s=60))
            self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
            self.assertEqual(res["detail"], "recursion deadline")
            self.assertEqual(res["breaker_effect"], "none")
            self.assertEqual(launcher.calls, [], "launched after the deadline")
        with self.subTest("admission_releases_a_probe"):
            svc, launcher, breaker, clock = _claude([claude_ok()])
            breaker.record("subscription:login-A", "claude-sonnet", "t1",
                           {"code": c.PLAN_LIMIT, "limit": c.limit_signal("connection", "test.typed")})
            clock.advance(300)
            real = breaker.admit

            def lease_then_spend(*a, **k):
                decision = real(*a, **k)
                clock.advance(61)
                return decision

            breaker.admit = lease_then_spend
            res = svc.handle(request(deadline_s=60))
            breaker.admit = real
            self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
            self.assertEqual(res["detail"], "recursion deadline")
            self.assertEqual(launcher.calls, [])
            self.assertEqual(breaker.state("subscription:login-A"), "half_open")
            nxt = svc.handle(request(rid="r2"))
            self.assertIsNone(nxt["code"])
            self.assertEqual(len(launcher.calls), 1)
            self.assertEqual(res["breaker_effect"], "probe_released")


class ProfileDeadlineTest(unittest.TestCase):
    def test_a_profile_launch_that_spends_the_deadline_does_not_start_work(self):
        """ProfileRouteAdapter.run does not call super, so it fences a spent launch itself."""
        for engine, spent in (("opencode", 1.0), ("pi", 5.0), ("hermes", 1.0)):
            with self.subTest(engine=engine, spent=spent):
                clock = FakeClock()
                proc = FakeProcess([], clock)
                called: list[bool] = []

                def driver(process, request, deadline_at, events, called=called):
                    called.append(True)
                    process.send(json.dumps({"type": "user"}))
                    return c.result(request["request_id"], None, text="ran")

                adapter = ProfileRouteAdapter(
                    engine, FakeLauncher(), clock, lambda *_: "", {},
                    argv=lambda request, engine=engine: [engine], driver=driver)

                def launch(*_a, **_k):
                    clock.advance(spent)
                    return None, proc

                deadline_at = clock() + 1.0
                req = request(engine=engine, model="m", kind="subscription", profile={})
                with mock.patch("recursion_core.routes.profile_route.guard_then_spawn", launch):
                    res = adapter.run(req, deadline_at)
                self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
                self.assertEqual(res["detail"], "recursion deadline")
                self.assertEqual(res["events"], [])
                self.assertEqual(proc.fenced, ["deadline"])
                self.assertEqual(proc.sent, [])
                self.assertEqual(called, [])

    def test_a_profile_launch_still_inside_the_deadline_calls_the_driver(self):
        clock = FakeClock()
        proc = FakeProcess([], clock)
        called: list[bool] = []

        def driver(process, request, deadline_at, events, called=called):
            called.append(True)
            return c.result(request["request_id"], None, text="ran")

        adapter = ProfileRouteAdapter(
            "opencode", FakeLauncher(), clock, lambda *_: "", {},
            argv=lambda request: ["opencode"], driver=driver)

        def launch(*_a, **_k):
            clock.advance(0.999)
            return None, proc

        deadline_at = clock() + 1.0
        req = request(engine="opencode", model="m", kind="subscription", profile={})
        with mock.patch("recursion_core.routes.profile_route.guard_then_spawn", launch):
            res = adapter.run(req, deadline_at)
        self.assertIsNone(res["code"])
        self.assertEqual(res["text"], "ran")
        self.assertEqual(proc.fenced, [])
        self.assertEqual(called, [True])


if __name__ == "__main__":
    unittest.main()
