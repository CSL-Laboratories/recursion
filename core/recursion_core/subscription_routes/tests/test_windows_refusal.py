"""What subscription_routes does on Windows: a typed refusal, asserted on a real Windows host.

The route home rules are POSIX owner and mode checks, so the suites that reach them skip on Windows
and this file is what the Windows gate asserts about them. On a POSIX host the same class runs under
the simulation in child_env/tests/test_simulated_windows.py. Every value is fake; nothing launches.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest import mock

from recursion_core.subscription_routes import SubscriptionRouteRefused, check_route, guard_then_spawn

WINDOWS_ONLY = "Windows-only assertions; a POSIX host runs them simulated (child_env/tests/test_simulated_windows.py)"
FAKE_KEY = "fake-api-key-not-a-credential"


@unittest.skipUnless(os.name == "nt", WINDOWS_ONLY)
class WindowsRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="csl786-win-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "route-home")
        self.operator = os.path.join(self.root, "operator")
        os.mkdir(self.home)
        os.mkdir(self.operator)
        self.profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY",
                        "model": "anthropic/m"}
        self.env = {"PATH": "/usr/bin:/bin", "HOME": self.home, "ANTHROPIC_API_KEY": FAKE_KEY}
        self.parent = {"PATH": "/usr/bin:/bin", "HOME": self.operator}

    def test_an_otherwise_healthy_api_key_route_refuses_platform_unsupported(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("opencode", self.profile, self.env, route_home=self.home, parent_env=self.parent,
                        operator_home=self.operator)
        self.assertEqual(("opencode", "platform_unsupported"), (caught.exception.engine, caught.exception.cause))
        self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_guard_then_spawn_never_calls_the_launcher(self) -> None:
        launcher = mock.MagicMock(name="launcher")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_then_spawn(launcher, ["opencode", "run", "anthropic/m"], "opencode", self.profile, self.env,
                             route_home=self.home, parent_env=self.parent, operator_home=self.operator)
        self.assertEqual("platform_unsupported", caught.exception.cause)
        launcher.assert_not_called()
        self.assertEqual([], os.listdir(self.home))


if __name__ == "__main__":
    unittest.main()
