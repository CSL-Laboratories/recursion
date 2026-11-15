"""R3 finding 8, the Codex half: OPENAI_API_KEY and CODEX_HOME refuse early (lane DRIVER-CX).

DRIVER-CC's ``test_r3_finding_8.py`` holds the Claude Code half and leaves this half to this lane's
``test_codex_*`` files. The brief's step 1, under Lucas's D0: "CODEX_HOME and OPENAI_API_KEY refuse early
with an existing code". Before, the adapter offered ``OPENAI_API_KEY`` and the operator's ``CODEX_HOME``,
and the production launcher then refused the first (``child_env:OPENAI_API_KEY:not_carried_by_policy``)
and silently dropped the second. Both are now ``launch_refused`` before custody or the launcher is asked;
``CODEX_API_KEY``, the one name the launcher's policy carries, is the selectable key. ``CODEX_HOME``
follows reading (a) of lane questions Q3 (refuse when the operator sets it), still open with the Liaison.

The R3 review's test for this half (Codex GPT-6 Astra, ``fix-8.patch``,
``test_documented_codex_key_name_composes_with_production_launcher``) asked that the documented key
compose with the production launcher; the brief chose refusal for ``OPENAI_API_KEY``, so that test is
carried here in the form the brief settles: the adapter and the launcher agree (``CODEX_API_KEY``
composes; ``OPENAI_API_KEY`` is refused before the launcher). Fakes only, made-up keys.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recursion_core.routes import CodexAdapter, ConnectionBreaker, RouteError, RouteService
from recursion_core.routes import codex as codex_module
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request

on_any_host = patch("recursion_core.routes.base.platform_supported", lambda: True, create=True)  # see finding_10
PARENT = {"PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8"}


class Custody:
    def __init__(self):
        self.asked = []

    def __call__(self, conn_id, name):
        self.asked.append((conn_id, name))
        return "FAKE-route-key"


@on_any_host
class EarlyRefusalTest(unittest.TestCase):
    def run_route(self, parent, **req):
        clock = FakeClock()
        self.custody = Custody()
        self.launcher = FakeLauncher([codex_script()], clock)
        service = RouteService({"codex": CodexAdapter(self.launcher, clock, self.custody, parent, wall_clock=clock)},
                               ConnectionBreaker(clock), clock)
        return service.handle(request(engine="codex", model="gpt-5", **req))

    def test_openai_api_key_is_refused_before_custody_and_launch(self):
        res = self.run_route(PARENT, kind="api_key", conn_id="key-1", credential_env="OPENAI_API_KEY")
        self.assertEqual((res["code"], res["detail"]),
                         (c.LAUNCH_REFUSED, "credential_env OPENAI_API_KEY not allowed for codex"))
        self.assertEqual((self.custody.asked, self.launcher.calls), ([], []))

    def test_codex_api_key_is_the_selectable_key(self):
        res = self.run_route(PARENT, kind="api_key", conn_id="key-1", credential_env="CODEX_API_KEY")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.custody.asked, [("key-1", "CODEX_API_KEY")])
        env = self.launcher.calls[0]["env"]
        self.assertEqual(env["CODEX_API_KEY"], "FAKE-route-key")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(codex_module.CODEX_API_KEY_ENV, ("CODEX_API_KEY",))

    def test_a_set_codex_home_is_refused_on_every_route_kind_before_custody_and_launch(self):
        key_route = {"kind": "api_key", "conn_id": "key-1", "credential_env": "CODEX_API_KEY"}
        for req in ({"kind": "subscription"}, key_route):
            for home in ("/home/op/.codex", ""):
                with self.subTest(kind=req["kind"], home=home):
                    res = self.run_route({**PARENT, "CODEX_HOME": home}, **req)
                    self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, "codex_home_not_carried"))
                    self.assertEqual((self.custody.asked, self.launcher.calls), ([], []))

    def test_without_codex_home_nothing_changes_and_none_is_offered(self):
        res = self.run_route(PARENT, kind="subscription")
        self.assertEqual(res["status"], "ok")
        env = self.launcher.calls[0]["env"]
        self.assertNotIn("CODEX_HOME", env)
        self.assertEqual(self.custody.asked, [])

    def test_the_refusal_names_no_path(self):
        res = self.run_route({**PARENT, "CODEX_HOME": "/home/op/FAKE-private-home"}, kind="subscription")
        self.assertNotIn("FAKE-private-home", str(res))


@unittest.skipUnless(sys.platform == "linux", "Linux ProductionLauncher")
class ProductionLauncherAgreesTest(unittest.TestCase):
    """The R3 review's composition test, in the form the brief settles (see the module docstring)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r3-8-codex-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)

    def test_the_selectable_key_composes_with_the_production_launcher(self):
        from recursion_core.daemon.launcher import ProductionLauncher
        adapter = CodexAdapter(FakeLauncher(), FakeClock(), lambda *_: "FAKE-codex-key", {"PATH": "/usr/bin"})
        req = request(engine="codex", kind="api_key", credential_env="CODEX_API_KEY")
        launcher = ProductionLauncher("codex", daemon=None, manager=None, state_dir=self.root)
        env = launcher.child_environment(adapter.env(req))
        self.assertIn("FAKE-codex-key", env.values())

    def test_openai_api_key_never_reaches_the_production_launcher(self):
        adapter = CodexAdapter(FakeLauncher(), FakeClock(), lambda *_: "FAKE-openai-key", {"PATH": "/usr/bin"})
        req = request(engine="codex", kind="api_key", credential_env="OPENAI_API_KEY")
        with self.assertRaises(RouteError) as cm:
            adapter.env(req)
        self.assertEqual(cm.exception.code, c.LAUNCH_REFUSED)


if __name__ == "__main__":
    unittest.main()
