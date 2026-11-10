"""Review of 1a3829b1: second model flag, userinfo echo, Copilot/Nous child env, probe deadline.

Each test fails on 1a3829b1. Fakes only: no network, no real credential home, no live CLI.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.subscription_routes import (
    SubscriptionRouteRefused,
    check_copilot_own_loop,
    check_nous_own_loop,
    guard_then_spawn,
    nous_proxy_doctor,
)
from recursion_core.subscription_routes import own_loop
from recursion_core.subscription_routes.own_loop import OwnLoopRouteRefused

SECRET = "fake-userinfo-secret"
COPILOT = {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"}


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class ModelFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="csl786-review-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = self.root / "route-home"
        self.home.mkdir(mode=0o700)

    def spawn(self, argv: list[str]) -> list[list[str]]:
        launched: list[list[str]] = []
        guard_then_spawn(
            lambda a, **_: launched.append(list(a)), argv, "opencode",
            {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
             "model": "chatgpt/gpt-5"},
            {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}, route_home=str(self.home),
            parent_env={"PATH": "/usr/bin:/bin", "HOME": str(self.operator)}, operator_home=str(self.operator),
        )
        return launched

    def test_a_second_model_flag_refuses(self) -> None:
        for argv in (["opencode", "chatgpt/gpt-5", "--model", "anthropic/claude-opus-4"],
                     ["opencode", "--model=chatgpt/gpt-5", "--model", "anthropic/claude-opus-4"],
                     ["opencode", "--model", "chatgpt/gpt-5", "-m=anthropic/claude-opus-4"],
                     ["opencode", "chatgpt/gpt-5", "-m"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SubscriptionRouteRefused) as caught:
                    self.spawn(argv)
                self.assertEqual(caught.exception.cause, "model_not_bound")

    def test_the_subscription_model_alone_still_launches(self) -> None:
        for argv in (["opencode", "chatgpt/gpt-5"], ["opencode", "--model", "chatgpt/gpt-5"],
                     ["opencode", "--model=chatgpt/gpt-5", "-m", "chatgpt/gpt-5"]):
            with self.subTest(argv=argv):
                self.assertEqual([argv], self.spawn(argv))


class UserinfoEchoTests(unittest.TestCase):
    def test_doctor_and_malformed_url_do_not_echo_userinfo(self) -> None:
        report = nous_proxy_doctor(f"http://noususer:{SECRET}@evil.example/v1", current_user="u",
                                   interactive_users=["u"])
        self.assertNotIn(SECRET, repr(report))
        self.assertEqual(("recursion_holds_no_token", "http://evil.example/v1"), (report["cause"], report["address"]))
        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_nous_own_loop({"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                                 "base_url": f"ftp://noususer:{SECRET}@127.0.0.1/v1"}, {"PATH": "/bin"})
        self.assertEqual("proxy_address_malformed", caught.exception.cause)
        self.assertNotIn(SECRET, str(caught.exception) + repr(caught.exception.detail))

    def test_a_url_without_userinfo_is_reported_unchanged(self) -> None:
        report = nous_proxy_doctor("http://evil.example:8080/v1", current_user="u", interactive_users=["u"])
        self.assertEqual("http://evil.example:8080/v1", report["address"])


class OwnLoopChildEnvTests(unittest.TestCase):
    def test_copilot_child_drops_secrets_bases_and_git_config_but_keeps_its_proxy(self) -> None:
        parent = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "PWD": "/work",
                  "HTTPS_PROXY": "http://proxy.example:3128", "copilot_api_url": "https://paid.example/v1",
                  "GITHUB_PASSWORD": SECRET, "GITHUB_PAT": SECRET, "AWS_SECRET_ACCESS_KEY": SECRET,
                  "OPENAI_API_BASE": "https://api.openai.com/v1",
                  "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                  "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {SECRET}"}
        decision = check_copilot_own_loop(COPILOT, parent, has_copilot_login=lambda: True)
        self.assertNotIn(SECRET, repr(dict(decision.child_env)))
        self.assertEqual({"PATH", "HOME", "LANG", "PWD", "HTTPS_PROXY"}, set(decision.child_env))

    def test_nous_child_drops_openai_api_base_and_grpc_proxy(self) -> None:
        nous = check_nous_own_loop(
            {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
             "base_url": "http://127.0.0.1:8645/v1"},
            {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "OPENAI_API_BASE": "https://api.openai.com/v1",
             "GRPC_PROXY": "http://proxy.example:443", "GITHUB_PAT": SECRET},
            current_user="u", interactive_users=["u"])
        self.assertEqual("http://127.0.0.1:8645/v1", nous.base_url)
        self.assertEqual({"PATH", "HOME"}, set(nous.child_env))


class CopilotProbeTests(unittest.TestCase):
    def test_systemexit_from_a_missing_cli_probe_is_a_refusal(self) -> None:
        def missing_cli() -> bool:
            raise SystemExit(127)

        with self.assertRaises(OwnLoopRouteRefused) as caught:
            check_copilot_own_loop(COPILOT, {"PATH": "/bin"}, has_copilot_login=missing_cli)
        self.assertEqual(("copilot_login_unknown", "SystemExit"), (caught.exception.cause, caught.exception.detail))

    def test_keyboard_interrupt_still_propagates(self) -> None:
        def interrupted() -> bool:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            check_copilot_own_loop(COPILOT, {"PATH": "/bin"}, has_copilot_login=interrupted)

    def test_hanging_probe_is_refused_at_the_deadline(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(own_loop, "COPILOT_PROBE_TIMEOUT_S", 0.2):
            with self.assertRaises(OwnLoopRouteRefused) as caught:
                check_copilot_own_loop(COPILOT, {"PATH": "/bin"}, has_copilot_login=lambda: release.wait(30))
        self.assertEqual(("copilot_login_unknown", "probe_timeout"), (caught.exception.cause, caught.exception.detail))


if __name__ == "__main__":
    unittest.main()
