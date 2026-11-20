"""R3-13, parts 1 and 2: the offered billing acknowledgment works, and a fallback route may carry its wrapper.

Astra's red tests (``D:\\GitHub\\gemini-wave\\R3\\fix-13.patch``, written against 1abd2f77), parts 1 and 2. Part 1:
the question asked when the user's settings were not reported offers ``billing_ack=api_key_helper``, and
the answer was then refused. Part 2: a Pi or Hermes fallback route needs its ``wrapper`` pin and the limits
validator refused the key. Lucas, Nov 12, decision sheet D0 ("Yes, as recommended"): "take the stricter,
fail-closed option that the reviewer's test already encodes". Part 3 (the profile adapter calling a strict
launcher with undeclared keyword arguments) is in ``profile_route`` and ``subscription_routes``, which this
lane does not keep. Dummy data only; no provider, network or operator credential is used.
"""

from __future__ import annotations

import unittest

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.limits import validate_limits
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, claude_ok, request, runs_adapter

PI_FALLBACK = {"engine": "pi", "model": "anthropic/m",
               "connection": {"kind": "api_key", "id": "k1", "credential_env": "ANTHROPIC_API_KEY"},
               "profile": {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"},
               "wrapper": "dummy-pin"}


def _service(scripts):
    clock = FakeClock()
    launcher = FakeLauncher(scripts, clock)
    adapter = ClaudeCodeAdapter(launcher, clock, lambda *_: "", {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "FAKE"})
    return RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock), launcher


class RecoveryContractTest(unittest.TestCase):
    @runs_adapter
    def test_offered_billing_acknowledgment_is_effective(self):
        # Astra's test.
        clock = FakeClock()
        adapter = ClaudeCodeAdapter(FakeLauncher([claude_ok()], clock), clock, lambda *_: "", {})
        service = RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock)
        self.assertEqual(service.handle(request(api_key_helper=None, billing_ack="api_key_helper"))["status"], "ok")

    def test_fallback_can_carry_the_wrapper_required_by_its_route(self):
        # Astra's test.
        value = {"on_limit": "fallback", "fallback": [dict(PI_FALLBACK)]}
        validate_limits(value)  # Shape validation; a later wrapper verifier still owns pin validity.


class AcknowledgmentTest(unittest.TestCase):
    @runs_adapter
    def test_the_question_names_the_answer_that_now_works(self):
        service, launcher = _service([claude_ok()])
        asked = service.handle(request(rid="q", api_key_helper=None))
        self.assertEqual(asked["code"], c.ROUTE_CHOICE_REQUIRED)
        self.assertIn("billing_ack=api_key_helper", asked["detail"])
        self.assertEqual(launcher.calls, [])
        answered = service.handle(request(rid="a", api_key_helper=None, billing_ack="api_key_helper"))
        self.assertEqual(answered["status"], "ok")
        # It runs as a subscription run: the user's own sign-in, no key added.
        self.assertEqual(launcher.calls[0]["env"], {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "FAKE"})

    @runs_adapter
    def test_only_that_answer_counts(self):
        for ack in ("yes", True, "API_KEY_HELPER", ""):
            service, launcher = _service([claude_ok()])
            res = service.handle(request(api_key_helper=None, billing_ack=ack))
            self.assertEqual(res["code"], c.ROUTE_CHOICE_REQUIRED, ack)
            self.assertEqual(launcher.calls, [], ack)

    def test_a_fallback_key_nobody_reads_is_still_refused(self):
        for extra in ({"wrapperx": "dummy-pin"}, {"limits": {"on_limit": "ask"}}):
            entry = dict(PI_FALLBACK, **extra)
            with self.subTest(extra=extra), self.assertRaises(c.RouteError) as caught:
                validate_limits({"on_limit": "fallback", "fallback": [entry]})
            self.assertEqual((caught.exception.code, caught.exception.detail),
                             (c.REQUEST_INVALID, "route.limits.fallback entry"))


if __name__ == "__main__":
    unittest.main()
