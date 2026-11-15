"""What a profile route does on Windows: the subscription_routes refusal, typed, and no launch.

The three launch-guard cases that reach the POSIX route home check skip on Windows; this file is
what the Windows gate asserts in their place. On a POSIX host the same class runs under the
simulation in child_env/tests/test_simulated_windows.py. Fake doubles only.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def test_an_api_key_profile_route_refuses_platform_unsupported_and_never_launches(self) -> None:
        home = tempfile.mkdtemp(prefix="route-home-")
        self.addCleanup(shutil.rmtree, home, True)
        clock = FakeClock()
        launcher = FakeLauncher(lambda argv, env: [{"ok": True}], clock)
        drive = lambda process, req, deadline_at, events: c.result(req["request_id"], None, text="driven")
        adapters = profile_adapters(launcher, clock, lambda cid, name: "sk-test",
                                    {"PATH": "/usr/bin", "HOME": "/home/operator"},
                                    argv=lambda req: [req["route"]["engine"]], driver=drive, operator_home="/home/operator")
        service = RouteService(adapters, ConnectionBreaker(clock), clock)
        profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
        res = service.handle(request(engine="opencode", model="m", kind="api_key", conn_id="k1",
                                     credential_env="ANTHROPIC_API_KEY", profile=profile, home=home))
        self.assertEqual((res["code"], res["detail"]), (c.SUBSCRIPTION_ROUTE_REFUSED, "platform_unsupported"))
        self.assertEqual(launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
