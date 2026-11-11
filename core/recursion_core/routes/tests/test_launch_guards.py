"""B11 (no Codex updater), B3 (subscription_routes reuse), the GIL guard and the explicit child env."""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import tempfile
import unittest

from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.codex import CodexAdapter, check_daemon_config, daemon_config
from recursion_core.routes.base import Deadline
from recursion_core.routes.launch import GilBuildRefused, child_env, require_free_threaded
from recursion_core.routes.profile_route import profile_adapters
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import FakeClock, FakeLauncher, codex_script, request
from recursion_core.subscription_routes import WrapperInstall, measure_package_pin

PKG = pathlib.Path(__file__).resolve().parents[1]
NOT_POSIX = os.name == "nt" or not hasattr(os, "geteuid")
POSIX_ONLY = ("POSIX-only: the route home owner check needs os.geteuid; subscription_routes refuses "
              "platform_unsupported on Windows; covered by WindowsRefusalTests")


class CodexUpdaterTest(unittest.TestCase):
    def test_stdio_worker_never_spawns_an_updater_over_a_simulated_hour(self) -> None:
        clock = FakeClock()
        launcher = FakeLauncher(lambda argv, env: codex_script("ok"), clock)
        service = RouteService({"codex": CodexAdapter(launcher, clock, lambda *_: "", {"PATH": "/usr/bin"})},
                               ConnectionBreaker(clock), clock)
        for i in range(48):  # 48 turns, one every 90 s: 72 minutes of fake clock
            res = service.handle(request(rid=f"r{i}", engine="codex", model="gpt-5"))
            self.assertEqual(res["status"], "ok")
            clock.advance(90)
        self.assertEqual(len(launcher.calls), 48)  # exactly one process per turn, nothing else
        for call in launcher.calls:
            argv = call["argv"]
            self.assertEqual(argv, ["codex", "-c", "check_for_update_on_startup=false", "app-server"])
            self.assertNotIn("update", argv)
        self.assertFalse(any("update" in a and "=false" not in a for call in launcher.calls for a in call["argv"]))

    def test_adapter_source_has_no_other_spawn_path(self) -> None:
        for name in ("codex.py", "base.py", "claude_code.py", "profile_route.py", "service.py", "launch.py"):
            text = (PKG / name).read_text()
            for banned in ("import os", "from os", "subprocess", "Popen", "prctl", "preexec_fn="):
                self.assertNotIn(banned, text, f"{name}: {banned}")

    def test_daemon_config_holds_updater_off(self) -> None:
        self.assertEqual(daemon_config(), {"updater": {"autoUpdateEnabled": False}})
        check_daemon_config(daemon_config())
        CodexAdapter(FakeLauncher(), FakeClock(), lambda *_: "", {}, daemon=daemon_config())

    def test_daemon_config_enabling_updater_refuses_to_start(self) -> None:
        for bad in ({"updater": {"autoUpdateEnabled": True}}, {"updater": {}}, {}, {"updater": {"autoUpdateEnabled": "false"}}):
            with self.assertRaises(c.RouteError) as ctx:
                CodexAdapter(FakeLauncher(), FakeClock(), lambda *_: "", {}, daemon=bad)
            self.assertEqual(ctx.exception.code, c.LAUNCH_REFUSED)


class SubscriptionRouteReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="route-home-")
        os.chmod(self.home, 0o700)
        self.addCleanup(shutil.rmtree, self.home)
        self.clock = FakeClock()
        self.launcher = FakeLauncher(lambda argv, env: [{"ok": True}], self.clock)
        drive = lambda process, req, deadline_at, events: c.result(req["request_id"], None, text="driven")
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: {"k1": "sk-test"}[cid],
                                    {"PATH": "/usr/bin", "HOME": "/home/operator", "CLAUDE_CODE_OAUTH_TOKEN": "x"},
                                    argv=lambda req: [req["route"]["engine"]], driver=drive, operator_home="/home/operator")
        self.service = RouteService(adapters, ConnectionBreaker(self.clock), self.clock)

    def pin(self) -> WrapperInstall:
        root = pathlib.Path(tempfile.mkdtemp(prefix="route-pin-"))
        self.addCleanup(shutil.rmtree, root)
        root.joinpath("launcher.js").write_text("launcher\n", encoding="ascii")
        root.joinpath("lib.js").write_text("lib\n", encoding="ascii")
        launcher = str(root / "launcher.js")
        return WrapperInstall(launcher, str(root), measure_package_pin(launcher, str(root)))

    def req(self, engine: str, profile: dict, **kw) -> dict:
        return request(engine=engine, model="m", kind="api_key", conn_id="k1", credential_env="ANTHROPIC_API_KEY",
                       profile=profile, home=self.home, **kw)

    @unittest.skipIf(NOT_POSIX, POSIX_ONLY)
    def test_driver_deadline_is_fenced_and_typed(self) -> None:
        def drive(process, req, deadline_at, events):
            raise Deadline()

        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: "sk-test",
                                    {"PATH": "/usr/bin", "HOME": "/home/operator"},
                                    argv=lambda req: ["pi"], driver=drive, operator_home="/home/operator")
        service = RouteService(adapters, ConnectionBreaker(self.clock), self.clock)
        res = service.handle(self.req("pi", {"provider": "anthropic", "auth": "api_key",
                                              "credential_env": "ANTHROPIC_API_KEY"}, wrapper=self.pin()))
        self.assertEqual(res["code"], c.DEADLINE_EXCEEDED)
        self.assertEqual(self.launcher.processes[-1].fenced, ["deadline"])

    def test_claude_subscription_provider_is_refused_and_never_launched(self) -> None:
        cases = [("pi", {"provider": "claude-pro", "auth": "oauth", "credential_env": "ANTHROPIC_API_KEY"}),
                 ("hermes", {"provider": "claude-code", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY",
                             "auth.adopt_external_logins": False}),  # required on every Hermes profile (#321)
                 ("opencode", {"provider": "claude-agent", "auth": "subscription", "credential_env": "ANTHROPIC_API_KEY"})]
        for engine, profile in cases:
            res = self.service.handle(self.req(engine, profile))
            self.assertEqual(res["code"], c.SUBSCRIPTION_ROUTE_REFUSED, engine)
            self.assertEqual(res["detail"], "claude_subscription_provider")
        self.assertEqual(self.launcher.calls, [])

    @unittest.skipIf(NOT_POSIX, POSIX_ONLY)
    def test_pi_without_a_wrapper_pin_refuses_before_launch(self) -> None:
        profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
        res = self.service.handle(self.req("pi", profile))
        self.assertEqual((res["code"], res["detail"]), (c.SUBSCRIPTION_ROUTE_REFUSED, "wrapper_pin_missing"))
        self.assertEqual(self.launcher.calls, [])

    @unittest.skipIf(NOT_POSIX, POSIX_ONLY)
    def test_api_key_profile_passes_the_guard_then_launches(self) -> None:
        profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
        res = self.service.handle(self.req("pi", profile, wrapper=self.pin()))
        self.assertEqual((res["status"], res["text"]), ("ok", "driven"))
        env = self.launcher.calls[0]["env"]
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": self.home, "ANTHROPIC_API_KEY": "sk-test"})

    def test_missing_custody_key_refuses_instead_of_launching_keyless(self) -> None:
        profile = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
        for engine in ("pi", "hermes", "opencode"):
            prof = dict(profile, **({"auth.adopt_external_logins": False} if engine == "hermes" else {}))
            req = request(engine=engine, model="m", kind="api_key", conn_id="no-such-key",
                          credential_env="OPENAI_API_KEY", profile=prof, home=self.home)
            res = self.service.handle(req)
            self.assertEqual((res["code"], res["detail"]), (c.LAUNCH_REFUSED, "no credential for no-such-key"), engine)
        self.assertEqual(self.launcher.calls, [])

    def test_unlisted_or_mismatched_credential_env_refuses_before_custody(self) -> None:
        asked = []
        adapters = profile_adapters(self.launcher, self.clock, lambda cid, name: asked.append(name) or "sk-test",
                                    {"PATH": "/usr/bin"}, argv=lambda req: ["pi"], driver=lambda *a: {},
                                    operator_home="/home/operator")
        service = RouteService(adapters, ConnectionBreaker(self.clock), self.clock)
        profile = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY"}
        for name in ("SECRET_THING", "OPENAI_API_KEY"):  # not listed for pi; listed but not the profile's
            req = request(engine="pi", model="m", kind="api_key", conn_id="k1", credential_env=name,
                          profile=profile, home=self.home)
            self.assertEqual(service.handle(req)["code"], c.LAUNCH_REFUSED, name)
        self.assertEqual((asked, self.launcher.calls), ([], []))

    def test_reuses_subscription_routes_not_a_copy(self) -> None:
        text = (PKG / "profile_route.py").read_text()
        self.assertIn("from recursion_core.subscription_routes import", text)
        self.assertNotIn("def check_route", text)
        self.assertIsNone(re.search(r"CLAUDE_SUBSCRIPTION_ENV\s*=", text))


class GilGuardTest(unittest.TestCase):
    def test_refuses_gil_build(self) -> None:
        with self.assertRaises(GilBuildRefused):
            require_free_threaded(lambda: True)

    def test_accepts_free_threaded(self) -> None:
        require_free_threaded(lambda: False)

    def test_refuses_interpreter_without_the_check(self) -> None:
        import recursion_core.routes.launch as launch

        saved = launch.sys
        try:
            launch.sys = type("NoCheck", (), {})()
            with self.assertRaises(GilBuildRefused):
                require_free_threaded()
        finally:
            launch.sys = saved


class ChildEnvTest(unittest.TestCase):
    def test_only_allowlisted_names_and_the_selected_credential(self) -> None:
        parent = {"PATH": "/bin", "HOME": "/h", "AWS_SECRET": "s", "OPENAI_API_KEY": "p"}
        self.assertEqual(child_env(parent), {"PATH": "/bin", "HOME": "/h"})
        self.assertEqual(child_env(parent, never=("HOME",), credential=("OPENAI_API_KEY", "route")),
                         {"PATH": "/bin", "OPENAI_API_KEY": "route"})


if __name__ == "__main__":
    unittest.main()
