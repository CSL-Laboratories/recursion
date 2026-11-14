"""R3-8, the Claude Code half: a passthrough route is refused early, by the adapter itself.

Astra's red tests (``D:\\GitHub\\gemini-wave\\R3\\fix-8.patch``, written against 1abd2f77): a cloud route whose
variables the daemon's child-environment policy drops was launched anyway, on the sign-in, and a gateway
route could hand the user's sign-in token to a credential-bearing URL. Lucas, Nov 12, decision sheet A2
("Yes, as recommended"): "The daemon refuses routes with key_source: api_key_helper and cloud passthrough
routes until credential custody is built"; A8 ("Yes, all four"): the gateway method stays refused with the
cloud ones. ``ClaudeCodeAdapter.env`` now refuses every passthrough route before it reads a variable, with
the daemon's own words. The Codex half (``OPENAI_API_KEY`` against the production launcher) goes in lane
DRIVER-CX's ``test_codex_*`` files. Dummy data only; no provider, network or operator credential is used.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteError, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.claude_code import PASSTHROUGH_METHOD_ENV, PASSTHROUGH_REFUSED
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, claude_ok, request, runs_adapter


class PrivateHome(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-regression-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)


@unittest.skipUnless(sys.platform == "linux", "Linux ProductionLauncher")
class CompositionTest(PrivateHome):
    def test_cloud_route_is_not_silently_launched_without_its_configuration(self):
        # Astra's test. An explicit refusal passes; a launch with the cloud settings dropped does not.
        from recursion_core.daemon.launcher import ProductionLauncher
        parent = {"PATH": "/usr/bin", "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "r3-project",
                  "CLOUD_ML_REGION": "r3-region"}
        adapter = ClaudeCodeAdapter(FakeLauncher(), FakeClock(), lambda *_: "", parent)
        req = request(kind="passthrough")
        req["route"]["connection"]["method"] = "vertex"
        launcher = ProductionLauncher("claude_code", daemon=None, manager=None, state_dir=self.root)
        try:
            env = launcher.child_environment(adapter.env(req))
        except RouteError:
            return  # Explicit refusal is safe; owner chooses supported passthrough inputs.
        self.assertEqual(env.get("ANTHROPIC_VERTEX_PROJECT_ID"), "r3-project")
        self.assertEqual(env.get("CLOUD_ML_REGION"), "r3-region")


class GatewayTest(unittest.TestCase):
    def test_gateway_cannot_receive_oauth_at_a_credential_bearing_url(self):
        # Astra's test.
        parent = {"CLAUDE_CODE_OAUTH_TOKEN": "FAKE-oauth", "ANTHROPIC_BASE_URL": "http://u:FAKE-pw@example.invalid/v1",
                  "ANTHROPIC_AUTH_TOKEN": "FAKE-bearer"}
        adapter = ClaudeCodeAdapter(FakeLauncher(), FakeClock(), lambda *_: "", parent)
        req = request(kind="passthrough")
        req["route"]["connection"]["method"] = "gateway"
        with self.assertRaises(RouteError):
            adapter.env(req)


# Every listed method's variables, all set and switched on: nothing the route needs is missing.
FULL_PARENT = {"PATH": "/usr/bin", "HOME": "/home/op", "CLAUDE_CODE_OAUTH_TOKEN": "FAKE-oauth",
               **{name: "1" for names in PASSTHROUGH_METHOD_ENV.values() for name in names},
               "ANTHROPIC_BASE_URL": "https://gateway.example.invalid", "ANTHROPIC_AUTH_TOKEN": "FAKE-bearer"}


class EarlyRefusalTest(unittest.TestCase):
    def test_every_method_is_refused_by_env_with_the_daemons_words(self):
        # A fully configured route of each method, and a method nobody listed, are refused alike.
        adapter = ClaudeCodeAdapter(FakeLauncher(), FakeClock(), lambda *_: "FAKE-key", FULL_PARENT)
        for method in (*PASSTHROUGH_METHOD_ENV, "some-other-builtin"):
            req = request(kind="passthrough", conn_id=f"user-{method}")
            req["route"]["connection"]["method"] = method
            with self.subTest(method=method), self.assertRaises(RouteError) as caught:
                adapter.env(req)
            self.assertEqual((caught.exception.code, caught.exception.detail), (c.LAUNCH_REFUSED, PASSTHROUGH_REFUSED))

    @runs_adapter
    def test_the_service_refuses_before_launch_and_asks_no_connector(self):
        asked = []

        def credentials(conn_id, name):
            asked.append((conn_id, name))
            return "FAKE-key"

        for method in PASSTHROUGH_METHOD_ENV:
            clock = FakeClock()
            launcher = FakeLauncher([claude_ok()], clock)
            breaker = ConnectionBreaker(clock)
            service = RouteService({"claude_code": ClaudeCodeAdapter(launcher, clock, credentials, FULL_PARENT)},
                                   breaker, clock)
            req = request(kind="passthrough", conn_id=f"user-{method}", bare=True)
            req["route"]["connection"]["method"] = method
            res = service.handle(req)
            self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, PASSTHROUGH_REFUSED), method)
            self.assertEqual((launcher.calls, asked), ([], []), method)
            # A refusal before launch is not an engine fault: the connection stays closed.
            self.assertEqual(breaker.state(f"passthrough:user-{method}"), "closed", method)


if __name__ == "__main__":
    unittest.main()
