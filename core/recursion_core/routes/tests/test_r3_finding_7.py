"""R3-7, the Claude Code half: Windows refuses at the shared adapter boundary before any launch.

Astra's red test (``D:\\GitHub\\gemini-wave\\R3\\fix-7.patch``, written against 1abd2f77) asked that the
Claude Code and Codex adapters never call an injected launcher on Windows. Lucas, Nov 12 ("Yes, as
recommended", decision sheet D9): "Windows refuses uniformly at the shared adapter boundary, before any
path or connector is looked at." The refusal lives in ``RouteAdapter.run``; the Codex half of the test
goes in lane DRIVER-CX's ``test_codex_*`` files, and the own-loop checkers' half belongs to
``subscription_routes``, which no lane keeps before the second wave. Fake doubles only.
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.routes import ClaudeCodeAdapter, ConnectionBreaker, RouteService
from recursion_core.routes import contract as c
from recursion_core.routes.host import PLATFORM_UNSUPPORTED, platform_supported
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, claude_ok, request

PKG = Path(__file__).resolve().parents[1]


def _service(scripts, credentials=lambda *_: "", parent_env=None):
    clock = FakeClock()
    launcher = FakeLauncher(scripts, clock)
    adapter = ClaudeCodeAdapter(launcher, clock, credentials, parent_env or {})
    return RouteService({"claude_code": adapter}, ConnectionBreaker(clock), clock), launcher


class WindowsBoundaryTest(unittest.TestCase):
    def test_engine_adapters_do_not_invoke_a_launcher_on_windows(self):
        # Astra's test, its claude_code case.
        service, launcher = _service([claude_ok()])
        with mock.patch.object(os, "name", "nt"), mock.patch.object(sys, "platform", "win32"):
            result = service.handle(request(engine="claude_code"))
        self.assertEqual(launcher.calls, [])
        self.assertEqual(result["status"], "error")
        self.assertEqual((result["code"], result["detail"]), (c.LAUNCH_REFUSED, PLATFORM_UNSUPPORTED))

    def test_no_connector_is_asked_before_the_refusal(self):
        # "before any path or connector is looked at": an API-key route's credential is never looked up.
        asked = []

        def credentials(conn_id, name):
            asked.append((conn_id, name))
            return "FAKE-key"

        service, launcher = _service([claude_ok()], credentials, {"PATH": "/usr/bin"})
        with mock.patch.object(os, "name", "nt"):
            result = service.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY"))
        self.assertEqual((result["code"], result["detail"]), (c.LAUNCH_REFUSED, PLATFORM_UNSUPPORTED))
        self.assertEqual((asked, launcher.calls), ([], []))

    def test_a_build_without_geteuid_is_refused_the_same_way(self):
        # The same test child_env and subscription_routes apply: no os.geteuid is not a POSIX host.
        service, launcher = _service([claude_ok()])
        with mock.patch.dict(os.__dict__):
            os.__dict__.pop("geteuid", None)
            self.assertFalse(platform_supported())
            result = service.handle(request())
        self.assertEqual((result["code"], result["detail"]), (c.LAUNCH_REFUSED, PLATFORM_UNSUPPORTED))
        self.assertEqual(launcher.calls, [])

    @unittest.skipUnless(platform_supported(), "the launch half needs a platform where adapters launch")
    def test_the_same_request_launches_where_the_platform_is_supported(self):
        # The refusal is the platform's alone: the identical request launches once outside the patch.
        service, launcher = _service([claude_ok()])
        result = service.handle(request())
        self.assertEqual((result["status"], len(launcher.calls)), ("ok", 1))


class HostModuleTest(unittest.TestCase):
    def test_the_host_module_reads_os_name_and_geteuid_and_nothing_else(self):
        # The adapter modules may not import os (test_launch_guards); host.py may, for these two reads only.
        tree = ast.parse((PKG / "host.py").read_text())
        uses = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                uses.add(node.attr)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "hasattr":
                if isinstance(node.args[0], ast.Name) and node.args[0].id == "os":
                    uses.add(("hasattr", node.args[1].value))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                self.assertTrue(names in (["os"], ["annotations"]), names)
        self.assertEqual(uses, {"name", ("hasattr", "geteuid")})


if __name__ == "__main__":
    unittest.main()
