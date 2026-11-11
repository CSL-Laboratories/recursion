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
    OwnLoopRouteRefused,
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
                self.launcher, ["opencode", "run", "--model", "chatgpt/test-model"], "opencode", profile, env,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        got = self.launcher.call_args.kwargs["env"]
        _no_poison(self, got)
        self.assertEqual({"PATH", "HOME"}, set(got))
        self.assertEqual(got, self._popen_env(decision))

    def test_an_openai_key_still_goes_through_the_executable_check(self) -> None:
        unsafe = self.bin / "unsafe-engine"
        unsafe.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        unsafe.chmod(0o777)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "OPENAI_API_KEY": FAKE_KEY}
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused) as caught:
            spawn_route_process(
                [str(unsafe), "run"], env, route_home=str(self.home), credential_env="OPENAI_API_KEY",
            )
        self.assertIn("argv0", caught.exception.cause)
        popen.assert_not_called()

    def test_credential_env_cannot_restore_home_or_a_loader(self) -> None:
        for name in ("HOME", "LD_PRELOAD", "PATH"):
            with self.subTest(name=name):
                with self.assertRaises(EnvRefused) as caught:
                    launch_env(
                        {"PATH": "/usr/bin:/bin", "HOME": str(self.home), name: "/tmp/not-the-route"},
                        route_home=str(self.home), credential_env=name,
                    )
                self.assertEqual("credential_env_denied", caught.exception.cause)

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
        argv = list(self.argv)
        if route.get("base_url"):
            argv.extend(["--base-url", str(route["base_url"])])
        for name in route.get("adapters") or ():
            argv.extend(["--adapter", name])
        if route.get("model"):
            argv.extend(["--model", str(route["model"])])
        parent = {**self.base, **POISON, "HTTP_PROXY": "http://127.0.0.1:9"}
        with mock.patch.dict(os.environ, POISON), mock.patch("subprocess.Popen") as popen, \
                mock.patch("subprocess.run") as run:
            decision, _proc = call(route, parent, argv, home=str(self.home))
        run.assert_not_called()
        popen.assert_called_once()
        env = popen.call_args.kwargs["env"]
        _no_poison(self, env)
        _no_poison(self, dict(decision.child_env))
        self.assertIsNot(env, os.environ)
        self.assertEqual(str(self.home), env["HOME"])
        self.assertNotEqual("/home/not-the-route-home", env["HOME"])
        engine = "copilot" if route.get("engine") == "copilot" else "codex"
        overrides = {"COPILOT_HOME": str(self.home / ".copilot")} if engine == "copilot" else None
        expected = build_child_env(
            engine, home=str(self.home), auth_mode="subscription",
            parent={k: v for k, v in decision.child_env.items() if k != "HOME"},
            overrides=overrides,
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

    def test_probe_sees_the_route_copilot_home_not_the_parent_account(self) -> None:
        # Copilot CLI >= 1.0.1 reads COPILOT_HOME, else ~/.copilot, and ignores XDG_CONFIG_HOME.
        operator = self.root / "operator"
        operator.mkdir(mode=0o700)
        operator_login = operator / ".copilot"
        operator_login.mkdir(mode=0o700)
        (operator_login / "account").write_text("A", encoding="ascii")
        (operator_login / "login").write_text("yes", encoding="ascii")
        route_login = self.home / ".copilot"
        route_login.mkdir(mode=0o700)
        (route_login / "account").write_text("B", encoding="ascii")
        (route_login / "login").write_text("yes", encoding="ascii")
        xdg = self.root / "xdg"
        xdg.mkdir(mode=0o700)
        (xdg / "account").write_text("X", encoding="ascii")
        marker = self.root / "seen"

        def probe() -> bool:
            home = os.environ.get("COPILOT_HOME") or os.path.join(os.environ.get("HOME", ""), ".copilot")
            account = os.path.join(home, "account")
            text = "missing"
            if os.path.isfile(account):
                with open(account, encoding="ascii") as handle:
                    text = handle.read()
            with open(marker, "w", encoding="ascii") as handle:
                handle.write(text + "\n" + home + "\n")
                if "XDG_CONFIG_HOME" in os.environ:
                    handle.write("xdg\n")
            return os.path.isfile(os.path.join(home, "login"))

        parent = {
            **self.base, "HOME": str(operator), "XDG_CONFIG_HOME": str(xdg),
            "COPILOT_HOME": str(operator_login),
        }
        with mock.patch("subprocess.Popen") as popen:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                parent, self.argv, home=str(self.home), has_copilot_login=probe,
            )
        self.assertEqual("B", marker.read_text(encoding="ascii").splitlines()[0])
        self.assertNotIn("xdg", marker.read_text(encoding="ascii"))
        env = popen.call_args.kwargs["env"]
        self.assertEqual(str(self.home), env["HOME"])
        self.assertEqual(str(route_login), env["COPILOT_HOME"])
        self.assertNotIn("XDG_CONFIG_HOME", env)

    def test_spawn_refuses_argv_that_contradicts_the_checked_route(self) -> None:
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(OwnLoopRouteRefused) as caught:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in",
                 "model": "gpt-good"},
                self.base, [self.argv[0], "--model", "gpt-bad"], home=str(self.home),
                has_copilot_login=lambda: True,
            )
        self.assertEqual(caught.exception.cause, "model_not_bound")
        popen.assert_not_called()
        nous = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                "base_url": "http://127.0.0.1:8645/v1", "adapters": ["nous"], "model": "nous/good"}
        argv = [self.argv[0], "--base-url", "https://evil.example/v1", "--adapter", "xai", "--model", "xai/bad"]
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(OwnLoopRouteRefused) as caught:
            spawn_nous_own_loop(
                nous, self.base, argv, home=str(self.home), current_user="u", interactive_users=["u"],
            )
        self.assertIn(caught.exception.cause, {"base_url_not_bound", "adapter_not_bound", "model_not_bound"})
        popen.assert_not_called()

    def test_copilot_keeps_a_no_proxy_entry_for_an_openai_host(self) -> None:
        parent = {**self.base, "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "api.openai.com"}
        with mock.patch("subprocess.Popen") as popen:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                parent, self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        launched = popen.call_args.kwargs["env"]
        self.assertEqual("api.openai.com", launched["NO_PROXY"])
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused) as caught:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                {**self.base, "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "api.github.com"},
                self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertEqual("no_proxy_engine_host", caught.exception.cause)
        for bypass in ("api.githubcopilot.com", "proxy.enterprise.githubcopilot.com", "*.githubcopilot.com",
                       "githubcopilot.com", "proxy.enterprise.githubcopilot.*", "foo.githubcopilot.*"):
            with self.subTest(bypass=bypass):
                with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused) as caught:
                    spawn_copilot_own_loop(
                        {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                        {**self.base, "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": bypass},
                        self.argv, home=str(self.home), has_copilot_login=lambda: True,
                    )
                self.assertEqual("no_proxy_engine_host", caught.exception.cause)
                popen.assert_not_called()
        with mock.patch("subprocess.Popen") as popen:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                {**self.base, "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "notgithubcopilot.com"},
                self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertEqual("notgithubcopilot.com", popen.call_args.kwargs["env"]["NO_PROXY"])
        with mock.patch("subprocess.Popen") as popen:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                {**self.base, "HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "*.example.com"},
                self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertEqual("*.example.com", popen.call_args.kwargs["env"]["NO_PROXY"])

    def test_copilot_home_must_be_a_private_directory(self) -> None:
        shared = self.root / "shared"
        shared.mkdir(mode=0o777)
        shared.chmod(0o777)
        link = self.home / ".copilot"
        link.symlink_to(shared)
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused) as caught:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                self.base, self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertEqual("COPILOT_HOME", caught.exception.name)
        popen.assert_not_called()
        link.unlink()
        writable = self.home / ".copilot"
        writable.mkdir(mode=0o777)
        writable.chmod(0o777)
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(EnvRefused) as caught:
            spawn_copilot_own_loop(
                {"engine": "copilot", "auth": "subscription", "method": "copilot_cli_sign_in"},
                self.base, self.argv, home=str(self.home), has_copilot_login=lambda: True,
            )
        self.assertEqual("COPILOT_HOME", caught.exception.name)
        popen.assert_not_called()

    def test_a_model_value_is_not_also_an_adapter_flag(self) -> None:
        # "--adapter" is the model value. A second scan must not read it as the adapter flag.
        url = "http://127.0.0.1:8645/v1"
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": url, "adapters": ["nous"], "model": "--adapter"}
        argv = [self.argv[0], "--base-url", url, "--model", "--adapter", "nous"]
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(OwnLoopRouteRefused) as caught:
            spawn_nous_own_loop(
                route, self.base, argv, home=str(self.home), current_user="u", interactive_users=["u"],
            )
        self.assertEqual("adapter_not_bound", caught.exception.cause)
        popen.assert_not_called()
        # --session consumes the following token on Copilot and Nous too.
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(OwnLoopRouteRefused) as caught:
            spawn_nous_own_loop(
                {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": url, "adapters": ["nous"], "model": "nous/good"},
                self.base, [self.argv[0], "--session", "--model", "nous/good", "--base-url", url, "--adapter", "nous"],
                home=str(self.home), current_user="u", interactive_users=["u"],
            )
        self.assertEqual("model_not_bound", caught.exception.cause)
        popen.assert_not_called()

    def test_base_url_refusal_does_not_echo_the_path(self) -> None:
        secret = "private-CANARYSECRET"
        url = f"http://127.0.0.1:9/{secret}"
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": url, "adapters": ["nous"], "model": "nous/good"}
        argv = [self.argv[0], "--base-url", "http://127.0.0.1:1/v1", "--adapter", "nous", "--model", "nous/good"]
        with mock.patch("subprocess.Popen") as popen, self.assertRaises(OwnLoopRouteRefused) as caught:
            spawn_nous_own_loop(
                route, self.base, argv, home=str(self.home), current_user="u", interactive_users=["u"],
            )
        self.assertEqual("base_url_not_bound", caught.exception.cause)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("/" + secret, caught.exception.detail)
        popen.assert_not_called()

    def test_spawn_uses_the_argv_snapshot_from_entry(self) -> None:
        url = "http://127.0.0.1:8645/v1"
        good = [self.argv[0], "--base-url", url, "--adapter", "nous", "--model", "nous/good"]
        bad = [self.argv[0], "--base-url", "https://remote.invalid/v1", "--adapter", "xai", "--model", "xai/bad"]

        class SecondRead(list):
            def __iter__(self):
                self._reads = getattr(self, "_reads", 0) + 1
                if self._reads > 1:
                    return iter(bad)
                return list.__iter__(self)

        argv = SecondRead(good)
        route = {"engine": "nous", "auth": "subscription", "method": "hermes_local_proxy",
                 "base_url": url, "adapters": ["nous"], "model": "nous/good"}
        with mock.patch("subprocess.Popen") as popen:
            spawn_nous_own_loop(
                route, self.base, argv, home=str(self.home), current_user="u", interactive_users=["u"],
            )
        launched = list(popen.call_args.args[0])
        self.assertEqual(good, launched)
        self.assertNotIn("https://remote.invalid/v1", launched)
        popen.assert_called_once()

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
