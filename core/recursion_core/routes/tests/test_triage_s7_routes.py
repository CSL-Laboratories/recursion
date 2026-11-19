"""S7 triage (routes-core): RC-12 only. Fake clock, no engine, no network, no credential.

The launcher's typed route error must come back as a result. Other route findings are not this lane.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from recursion_core.routes import ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.tests.fakes import FakeClock, request


class TypedBoundary(unittest.TestCase):
    def _handle(self, svc, req):
        try:
            return svc.handle(req)
        except Exception as exc:  # noqa: BLE001 - the defect is that something escapes
            self.fail(f"escaped RouteService.handle: {type(exc).__name__}: {exc}")

    # RC-12: the profile-route launcher's typed RouteError escapes handle().
    def test_rc12_profile_launcher_refusal_is_a_result(self):
        root = Path(tempfile.mkdtemp(prefix="s7-rc12-"))
        self.addCleanup(shutil.rmtree, root, True)
        operator, home = root / "operator", root / "home"
        operator.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
        clock = FakeClock()

        launched = []

        def launcher(argv, **kw):
            launched.append(argv)
            raise c.RouteError(c.LAUNCH_REFUSED, "synthetic policy refusal")

        adapters = profile_adapters(launcher, clock, lambda cid, name: "fake-key-not-a-credential",
                                    {"PATH": "/usr/bin:/bin", "HOME": str(operator)},
                                    argv=lambda r: ["opencode", "run"], driver=lambda *a: None,
                                    operator_home=str(operator))
        svc = RouteService(adapters, ConnectionBreaker(clock), clock)
        req = request(engine="opencode", model="openai/m", kind="api_key", conn_id="k1",
                      credential_env="OPENAI_API_KEY", home=str(home),
                      profile={"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY",
                               "model": "openai/m"})
        res = self._handle(svc, req)
        if os.name == "nt":
            # The home check refuses before any launch. WindowsRefusalTests pins the same result.
            self.assertEqual((res["code"], res["detail"]),
                             (c.SUBSCRIPTION_ROUTE_REFUSED, "platform_unsupported"))
            self.assertEqual(launched, [])
        else:
            self.assertEqual(res["code"], c.LAUNCH_REFUSED)


if __name__ == "__main__":
    unittest.main()
