"""Six engines are routes at the release; grok, pi and gemini_cli are detected and shown, not routed.

A route, a fallback route or a run start naming one of the three refuses ``engine_not_routed`` before
anything is spawned.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes import engines
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, request
from recursion_core.subscription_routes import WrapperInstall, check_route, measure_package_pin

NOT_ROUTED = "engine_not_routed"
DETAIL = "detected, not routed in this release"
NOT_POSIX = os.name == "nt" or not hasattr(os, "geteuid")
PROFILE = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
README = pathlib.Path(__file__).resolve().parents[2] / "subscription_routes" / "README.md"


class ReleasePartitionTest(unittest.TestCase):
    def test_routed_and_not_routed_with_the_loop_are_exactly_the_catalogue(self) -> None:
        self.assertEqual(getattr(engines, "RELEASE_ROUTED", None),
                         ("claude_code", "codex", "opencode", "cursor", "hermes", "antigravity"))
        self.assertEqual(getattr(engines, "DETECTED_NOT_ROUTED", None), ("grok", "pi", "gemini_cli"))
        parts = engines.RELEASE_ROUTED + engines.DETECTED_NOT_ROUTED + ("recursion_loop",)
        self.assertEqual(len(parts), len(set(parts)))   # no name in two parts
        self.assertEqual(sorted(parts), sorted(engines.CATALOGUE))
        self.assertEqual(len(engines.CATALOGUE), 10)
        self.assertEqual(engines.SEEDED, ("claude_code", "codex"))


class RegisterTest(unittest.TestCase):
    def test_a_routed_engine_still_registers(self) -> None:
        table = engines.EngineTable()
        table.register("opencode", lambda *a, **k: None)
        self.assertEqual(table.names(), ("claude_code", "codex", "opencode"))


class RouteRefusesNotRoutedTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "route-home"
        self.home.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.launcher = FakeLauncher(lambda argv, env: [{"ok": True}], self.clock)
        self.parent = {"PATH": "/usr/bin", "HOME": "/home/operator"}
        drive = lambda process, req, deadline_at, events: c.result(req["request_id"], None, text="driven")
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: "sk-test", self.parent,
                                    argv=self.argv, driver=drive,
                                    operator_home="/home/operator")
        self.service = RouteService(adapters, ConnectionBreaker(self.clock), self.clock)

    @staticmethod
    def argv(req: dict) -> list[str]:
        wrapper = req["route"].get("wrapper")
        return [wrapper.launcher_path if wrapper is not None else req["route"]["engine"]]

    def pin(self) -> WrapperInstall:
        pkg = self.root / "pin"
        pkg.mkdir()
        pkg.joinpath("launcher.js").write_text("launcher\n", encoding="ascii")
        pkg.joinpath("lib.js").write_text("lib\n", encoding="ascii")
        launcher = str(pkg / "launcher.js")
        return WrapperInstall(launcher, str(pkg), measure_package_pin(launcher, str(pkg)))

    def req(self, engine: str, **kw) -> dict:
        return request(engine=engine, model="m", kind="api_key", conn_id="k1", credential_env="ANTHROPIC_API_KEY",
                       profile=dict(PROFILE), home=str(self.home), **kw)

    @unittest.skipIf(NOT_POSIX, "POSIX-only: the route home owner check needs os.geteuid")
    def test_pi_run_start_with_a_profile_check_route_accepts_is_refused_before_launch(self) -> None:
        wrapper = self.pin()
        child = {"PATH": "/usr/bin", "HOME": str(self.home), "ANTHROPIC_API_KEY": "sk-test"}
        check_route("pi", dict(PROFILE), child, route_home=str(self.home), parent_env=self.parent,
                    operator_home="/home/operator", wrapper=wrapper)   # accepted: no refusal raised
        res = self.service.handle(self.req("pi", wrapper=wrapper))
        self.assertEqual((res["status"], res["code"], res["detail"]), ("error", NOT_ROUTED, DETAIL))
        self.assertEqual(self.launcher.calls, [])

    def test_a_route_naming_each_not_routed_engine_is_refused_typed(self) -> None:
        for engine in ("grok", "pi", "gemini_cli"):
            with self.assertRaises(c.RouteError, msg=engine) as ctx:
                c.validate_request(self.req(engine))
            self.assertEqual((ctx.exception.code, ctx.exception.detail), (NOT_ROUTED, DETAIL), engine)
            res = self.service.handle(self.req(engine))
            self.assertEqual((res["code"], res["detail"]), (NOT_ROUTED, DETAIL), engine)
        self.assertEqual(self.launcher.calls, [])

    def test_a_fallback_route_naming_pi_is_refused_before_launch(self) -> None:
        fallback = self.req("pi")["route"]
        req = self.req("hermes", limits={"on_limit": "fallback", "fallback": [fallback]})
        res = self.service.handle(req)
        self.assertEqual((res["code"], res["detail"]), (NOT_ROUTED, DETAIL))
        self.assertEqual(self.launcher.calls, [])


class GeminiCliReadmeTest(unittest.TestCase):
    def test_the_dropped_route_section_quotes_the_owner_answer_and_keeps_the_earlier_call(self) -> None:
        text = README.read_text(encoding="utf-8")
        start = text.index("## Antigravity CLI is the Google route; the Gemini CLI route was dropped")
        section = text[start:text.index("\n## ", start + 1)]
        self.assertIn('*"Yes we dropped it, let\'s keep it dropped." (Lucas, Nov 18 2026; '
                      'lucas-decisions-1116-research3.md entry 79, D14)*', section)
        self.assertIn("Gemini CLI is detected and shown, not routed", section)
        self.assertIn("Owner call (Lucas, 2026-11-09)", section)


if __name__ == "__main__":
    unittest.main()
