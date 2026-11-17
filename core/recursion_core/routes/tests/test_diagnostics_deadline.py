"""RC-02 and RC-11: Claude diagnostics carry codes, and no work starts after the deadline.

The marker and the two deadline witnesses are the routes-core triage tests for this draft only.
"""

from __future__ import annotations

import json
import unittest

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
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
            svc.handle(request(deadline_s=1))
            self.assertEqual(procs[0].sent, [], "the prompt was written after the deadline")
        with self.subTest("admission_wait"):
            svc, launcher, breaker, clock = _claude([claude_ok()])
            real = breaker.admit

            def slow_admit(*a, **k):
                clock.advance(61)
                return real(*a, **k)

            breaker.admit = slow_admit
            svc.handle(request(deadline_s=60))
            self.assertEqual(launcher.calls, [], "launched after the deadline")


if __name__ == "__main__":
    unittest.main()
