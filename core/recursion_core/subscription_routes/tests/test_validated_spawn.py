"""Route children receive a child_env ValidatedEnv, not the parent process environment.

Fakes only: a stand-in binary and a patched Popen. No engine, provider or network is used.
Every secret-shaped value is a fake string.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.child_env import EnvRefused, build_child_env
from recursion_core.subscription_routes import (
    SubscriptionRouteRefused,
    guard_then_spawn,
    launch_env,
    spawn_copilot_own_loop,
    spawn_nous_own_loop,
    spawn_route_process,
)
from recursion_core.subscription_routes.tests.test_owner_calls import _Calls
from recursion_core.subscription_routes.tests.test_subscription_routes import FAKE_KEY

# A poisoned parent. The route key used below is FAKE_KEY, never one of these values.
POISON = {
    "GITHUB_TOKEN": "poison-github-token",
    "GH_TOKEN": "poison-gh-token",
    "GH_PAT": "poison-gh-pat",
    "AWS_ACCESS_KEY_ID": "poison-aws-access-key-id",
    "AWS_SECRET_ACCESS_KEY": "poison-aws-secret-access-key",
    "AWS_SESSION_TOKEN": "poison-aws-session-token",
    "OPENAI_API_KEY": "poison-openai-api-key",
    "ANTHROPIC_API_KEY": "poison-anthropic-api-key",
    "HTTPS_PROXY": "http://poison-user:poison-pass@proxy.example:3128",
    "http_proxy": "http://poison-user:poison-pass@proxy.example:3128",
    "GIT_CONFIG_COUNT": "poison-git-config-count",
    "GIT_CONFIG_KEY_0": "url.https://poison-git.example.insteadOf",
    "GIT_CONFIG_VALUE_0": "https://poison-user:poison-git-pass@github.com/",
}
_SECRET_NAMES = (
    "GITHUB_TOKEN", "GH_TOKEN", "GH_PAT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    "HTTPS_PROXY", "http_proxy",
)


def _no_poison(test: unittest.TestCase, env: dict, *, route_key: str | None = None) -> None:
    test.assertIsNot(env, os.environ)
    blob = "\n".join(env.values()) + "\n" + repr(env)
    for value in POISON.values():
        test.assertNotIn(value, blob)
    for name in _SECRET_NAMES:
        test.assertNotIn(name, env)
    if route_key == "ANTHROPIC_API_KEY":
        test.assertEqual(FAKE_KEY, env.get("ANTHROPIC_API_KEY"))
        test.assertNotIn("OPENAI_API_KEY", env)
    elif route_key == "OPENAI_API_KEY":
        test.assertEqual(FAKE_KEY, env.get("OPENAI_API_KEY"))
        test.assertNotIn("ANTHROPIC_API_KEY", env)
    else:
        test.assertNotIn("ANTHROPIC_API_KEY", env)
        test.assertNotIn("OPENAI_API_KEY", env)


@unittest.skipIf(os.name == "nt", "POSIX home and spawn checks")
class HarnessSpawnTests(_Calls):
    """Pi, Hermes, OpenCode, Cursor and Antigravity launch with the rebuilt env."""

    def setUp(self) -> None:
        super().setUp()
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        self.fake = self.bin / "fake-engine"
        self.fake.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        self.fake.chmod(0o755)

    def parent(self, **extra: str) -> dict:
        return super().parent(**POISON, **extra)

    def _launched(self, decision) -> dict:
        _validated, launched = launch_env(
            decision.child_env, route_home=str(self.home), credential_env=decision.credential_env,
        )
        return launched

    def _popen_env(self, decision) -> dict:
        with mock.patch("subprocess.Popen") as popen, mock.patch("subprocess.run") as run:
            spawn_route_process(
                [str(self.fake), "run"], decision.child_env, route_home=str(self.home),
                credential_env=decision.credential_env, cwd=str(self.home),
            )
        run.assert_not_called()
        popen.assert_called_once()
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["close_fds"], True)
        self.assertIs(kwargs["start_new_session"], True)
        return kwargs["env"]

    def test_each_guard_drops_a_poisoned_parent_and_uses_the_validated_env(self) -> None:
        with mock.patch.dict(os.environ, POISON):
            for engine in ("pi", "hermes", "cursor", "antigravity"):
                with self.subTest(engine=engine):
                    shutil.rmtree(self.home / ".hermes", ignore_errors=True)
                    self.launcher.reset_mock()
                    # Pi and Hermes take the caller's env, so a temp name shows the rebuild.
                    # Cursor and Antigravity launch from the bare env; they share _launch_checked.
                    if engine in ("pi", "hermes"):
                        decision, _ = self.launch(engine, use_env={**self.env(), "TMPDIR": "/tmp"})
                        route_key = "ANTHROPIC_API_KEY"
                    else:
                        decision, _ = self.launch(engine)
                        route_key = None
                    got = self.launcher.call_args.kwargs["env"]
                    _no_poison(self, got, route_key=route_key)
                    if engine in ("pi", "hermes"):
                        self.assertNotEqual("/tmp", got["TMPDIR"])
                    self.assertEqual(self._launched(decision), got)
                    spawned = self._popen_env(decision)
                    _no_poison(self, spawned, route_key=route_key)
                    self.assertEqual(got, spawned)

    def test_opencode_api_keys_do_not_carry_the_parent_secret(self) -> None:
        with mock.patch.dict(os.environ, POISON):
            for provider, credential in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")):
                with self.subTest(provider=provider):
                    self.launcher.reset_mock()
                    env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": "/tmp", credential: FAKE_KEY}
                    profile = self.profile(provider=provider, credential_env=credential, model=f"{provider}/m")
                    decision, _ = guard_then_spawn(
                        self.launcher, ["opencode", "run", f"{provider}/m"], "opencode", profile, env,
                        route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
                    )
                    got = self.launcher.call_args.kwargs["env"]
                    _no_poison(self, got, route_key=credential)
                    self.assertEqual(FAKE_KEY, got[credential])
                    self.assertNotEqual("/tmp", got["TMPDIR"])
                    self.assertEqual(self._launched(decision), got)
                    self.assertEqual(got, self._popen_env(decision))

    def test_a_sign_in_child_has_no_credential(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}
        profile = {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
                   "model": "chatgpt/test-model"}
        with mock.patch.dict(os.environ, POISON):
            decision, _ = guard_then_spawn(
                self.launcher, ["opencode", "run", "chatgpt/test-model"], "opencode", profile, env,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        got = self.launcher.call_args.kwargs["env"]
        _no_poison(self, got)
        self.assertEqual({"PATH", "HOME"}, set(got))
        self.assertEqual(got, self._popen_env(decision))

    def test_subprocess_run_is_not_the_launch(self) -> None:
        decision, _ = self.launch("cursor")
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            spawn_route_process(
                [str(self.fake)], decision.child_env, route_home=str(self.home),
                credential_env=decision.credential_env,
            )
        run.assert_not_called()
        popen.assert_called_once()


@unittest.skipIf(os.name == "nt", "POSIX home and spawn checks")
class OwnLoopSpawnTests(unittest.TestCase):
    """Copilot and Nous children are built by child_env and started through spawn."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="csl-own-loop-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.root.chmod(0o700)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        engine = self.bin / "fake-engine"
        engine.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        engine.chmod(0o755)
        self.argv = [str(engine)]
        self.base = {"PATH": str(self.bin), "HOME": "/home/not-the-route-home", "LANG": "C.UTF-8"}

    def _spawn(self, call, route) -> dict:
        parent = {**self.base, **POISON, "HTTP_PROXY": "http://127.0.0.1:9"}
        with mock.patch.dict(os.environ, POISON), mock.patch("subprocess.Popen") as popen, \
                mock.patch("subprocess.run") as run:
            decision, _proc = call(route, parent, self.argv, home=str(self.home))
        run.assert_not_called()
        popen.assert_called_once()
        env = popen.call_args.kwargs["env"]
        _no_poison(self, env)
        _no_poison(self, dict(decision.child_env))
        self.assertIsNot(env, os.environ)
        self.assertEqual(str(self.home), env["HOME"])
        self.assertNotEqual("/home/not-the-route-home", env["HOME"])
        expected = build_child_env(
            "codex", home=str(self.home), auth_mode="subscription",
            parent={k: v for k, v in decision.child_env.items() if k != "HOME"},
        )
        self.assertEqual(expected.to_dict(), env)
        self.assertIs(popen.call_args.kwargs["shell"], False)
        return env

    def test_copilot_spawn_scrubs_the_poisoned_parent(self) -> None:
        env = self._spawn(
            lambda route, parent, argv, home: spawn_copilot_own_loop(
                route, parent, argv, home=home, has_copilot_login=lambda: True,
            ),
            {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
        )
        # A loopback proxy with no userinfo is on the allowlist. The poisoned proxy is not.
        self.assertEqual("http://127.0.0.1:9", env["HTTP_PROXY"])
        self.assertNotIn("poison-pass", env["HTTP_PROXY"])

    def test_nous_spawn_scrubs_proxies_and_the_poisoned_parent(self) -> None:
        env = self._spawn(
            lambda route, parent, argv, home: spawn_nous_own_loop(
                route, parent, argv, home=home, current_user="u", interactive_users=["u"],
            ),
            {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
             "base_url": "http://127.0.0.1:8645/v1", "adapters": ["nous"]},
        )
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("HTTPS_PROXY", env)

    def test_userinfo_proxy_never_reaches_popen(self) -> None:
        parent = {**self.base, "HTTPS_PROXY": POISON["HTTPS_PROXY"]}
        with mock.patch("subprocess.Popen") as popen:
            decision, proc = spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                parent, self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertIsNotNone(proc)
        popen.assert_called_once()
        self.assertNotIn("HTTPS_PROXY", decision.child_env)
        self.assertNotIn("poison-pass", popen.call_args.kwargs["env"].values())
        self.assertNotIn("HTTPS_PROXY", popen.call_args.kwargs["env"])

    def test_a_non_anthropic_credential_with_a_nul_is_refused(self) -> None:
        # ANTHROPIC_API_KEY is checked inside build_child_env. Any other credential is grafted
        # afterwards and must get the same shape check, or a NUL reaches Popen and raises ValueError.
        with self.assertRaises(EnvRefused) as caught:
            launch_env(
                {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "OPENAI_API_KEY": "sk-test\x00injected"},
                route_home=str(self.home), credential_env="OPENAI_API_KEY",
            )
        self.assertEqual(caught.exception.cause, "route_key_malformed")
        self.assertNotIn("\x00", str(caught.exception))
        self.assertNotIn("sk-test", str(caught.exception))
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused):
            spawn_route_process(
                [self.argv[0], "run"],
                {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "OPENAI_API_KEY": "sk-test\x00injected"},
                route_home=str(self.home), credential_env="OPENAI_API_KEY", cwd=str(self.home),
            )
        popen.assert_not_called()

    def test_a_missing_non_anthropic_credential_is_refused(self) -> None:
        with self.assertRaises(EnvRefused) as caught:
            launch_env(
                {"PATH": "/usr/bin:/bin", "HOME": str(self.home)},
                route_home=str(self.home), credential_env="OPENAI_API_KEY",
            )
        self.assertEqual(caught.exception.cause, "route_key_malformed")

    def test_an_unsafe_path_never_reaches_popen(self) -> None:
        parent = {**self.base, "PATH": "relative-bin"}
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused):
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                parent, self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        popen.assert_not_called()


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class RefusedBeforePopenTests(_Calls):
    def test_a_route_refusal_does_not_spawn(self) -> None:
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(SubscriptionRouteRefused):
            guard_then_spawn(
                self.launcher, ["opencode", "run"], "opencode", self.profile(),
                {**self.env(), "GITHUB_TOKEN": POISON["GITHUB_TOKEN"]},
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.launcher.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
