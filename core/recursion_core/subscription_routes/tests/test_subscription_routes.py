"""CSL-786: Pi, Hermes and OpenCode profiles refuse Claude-subscription routes before spawn (candidate tests).

Run from the repository root: ``python3 -B -m unittest discover -s core/recursion_core/subscription_routes/tests -t core -v``.
No engine, provider or network is used. Every credential value is a fake string, and no real
credential store is opened: store checks run against scratch homes the tests create.
"""

from __future__ import annotations

import builtins
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.subscription_routes import (
    CLAUDE_SUBSCRIPTION_ENV,
    ENGINE_SELECTOR_ENV,
    ENGINE_STORE_ENTRIES,
    ENGINES,
    HERMES_ADOPT_EXTERNAL_LOGINS,
    SAFE_ENV,
    SubscriptionRouteRefused,
    check_route,
    guard_then_spawn,
)

N = 5
FAKE_KEY = "fake-api-key-not-a-credential"
FAKE_OAUTH = "fake-oauth-token-not-a-credential"
ENGINES_UNDER_TEST = ("pi", "hermes", "opencode")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="csl786-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.operator = self.root / "operator"
        self.operator.mkdir(mode=0o700)
        self.home = self.root / "route-home"
        self.home.mkdir(mode=0o700)
        self.launcher = mock.MagicMock(name="launcher")

    def env(self, **extra: str) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "ANTHROPIC_API_KEY": FAKE_KEY, **extra}

    def parent(self, **extra: str) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.operator), **extra}

    def profile(self, **changes: object) -> dict:
        base = {"provider": "anthropic", "auth": "api_key", "credential_env": "ANTHROPIC_API_KEY", "model": "anthropic/m"}
        base.update(changes)
        return {key: value for key, value in base.items() if value is not None}

    def spawn(self, engine: str, profile: dict, env: dict, parent: dict | None = None, hermes_override: bool = True):
        # Every Hermes profile must carry Hermes's own auth.adopt_external_logins: false (PRODUCT.md 055cc7a5
        # section 2, E9). Added here for Hermes unless a test turns it off to show the refusal.
        if hermes_override and engine == "hermes" and HERMES_ADOPT_EXTERNAL_LOGINS not in profile:
            profile = {**profile, HERMES_ADOPT_EXTERNAL_LOGINS: False}
        return guard_then_spawn(self.launcher, [engine, "run"], engine, profile, env, route_home=str(self.home),
                                parent_env=parent if parent is not None else self.parent(),
                                operator_home=str(self.operator))

    def assertRefusedBeforeSpawn(self, engine: str, profile: dict, env: dict, cause: str, parent: dict | None = None,
                                 hermes_override: bool = True):
        for _ in range(N):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                self.spawn(engine, profile, env, parent, hermes_override)
            self.assertEqual((engine, cause), (caught.exception.engine, caught.exception.cause))
            self.assertNotIn(FAKE_OAUTH, str(caught.exception))
            self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.launcher.assert_not_called()


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class SubscriptionEnvTests(_Base):
    """The named OAuth variables refuse in every engine's child env, before the launcher runs."""

    def test_pi_oauth_and_auth_token(self) -> None:
        for name in ("ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"):
            with self.subTest(name=name):
                self.assertRefusedBeforeSpawn("pi", self.profile(), self.env(**{name: FAKE_OAUTH}), "claude_subscription_env")

    def test_hermes_token_and_claude_code_oauth_token(self) -> None:
        for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            with self.subTest(name=name):
                self.assertRefusedBeforeSpawn("hermes", self.profile(), self.env(**{name: FAKE_OAUTH}), "claude_subscription_env")

    def test_every_subscription_name_refuses_in_every_engine(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            for name in CLAUDE_SUBSCRIPTION_ENV:
                with self.subTest(engine=engine, name=name):
                    self.assertRefusedBeforeSpawn(engine, self.profile(), self.env(**{name: FAKE_OAUTH}), "claude_subscription_env")

    def test_an_oauth_token_in_the_api_key_variable_refuses(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            with self.subTest(engine=engine):
                env = self.env(ANTHROPIC_API_KEY="sk-ant-oat01-" + FAKE_OAUTH)
                self.assertRefusedBeforeSpawn(engine, self.profile(), env, "claude_oauth_token_in_api_key_env")

    def test_a_claude_config_dir_refuses(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            with self.subTest(engine=engine):
                env = self.env(CLAUDE_CONFIG_DIR=str(self.operator / ".claude"))
                self.assertRefusedBeforeSpawn(engine, self.profile(), env, "claude_store_env")


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class SubscriptionProviderTests(_Base):
    """Provider and auth selection: subscription providers refuse; the profile must be explicit."""

    def test_opencode_claude_agent_provider(self) -> None:
        for provider in ("claude-agent", "Claude-Agent", "claude_agent", "claude-code", "claude-agent-sdk", "claude"):
            with self.subTest(provider=provider):
                profile = self.profile(provider=provider, model=f"{provider}/sonnet")
                self.assertRefusedBeforeSpawn("opencode", profile, self.env(), "claude_subscription_provider")

    def test_opencode_model_naming_the_claude_agent_provider(self) -> None:
        profile = self.profile(model="claude-agent/sonnet")
        self.assertRefusedBeforeSpawn("opencode", profile, self.env(), "claude_subscription_provider")

    def test_pi_and_hermes_anthropic_oauth_selection(self) -> None:
        for engine in ("pi", "hermes"):
            for auth in ("oauth", "subscription", "claude_login", "setup_token", "stored", "OAuth"):
                with self.subTest(engine=engine, auth=auth):
                    self.assertRefusedBeforeSpawn(engine, self.profile(auth=auth), self.env(), "claude_subscription_auth")
            for provider in ("anthropic-oauth", "claude-max", "claude-pro"):
                with self.subTest(engine=engine, provider=provider):
                    if provider in ENGINES[engine].subscription_providers or provider.startswith("claude"):
                        self.assertRefusedBeforeSpawn(engine, self.profile(provider=provider, model=None), self.env(),
                                                      "claude_subscription_provider")

    def test_missing_or_unknown_fields_fail_closed(self) -> None:
        cases = (
            ({"provider": None}, "provider_missing"),
            ({"auth": None}, "auth_missing"),
            ({"credential_env": None}, "credential_env_missing"),
            ({"auth": "auto"}, "auth_not_api_key"),
            ({"auth": "default"}, "auth_not_api_key"),
            ({"provider": "openrouter", "model": "openrouter/m"}, "provider_not_allowed"),
            ({"model": "openai/gpt"}, "model_provider_mismatch"),
            ({"model": "no-provider"}, "model_malformed"),
            ({"credential_env": "OPENAI_API_KEY"}, "credential_env_not_allowed"),
        )
        for engine in ENGINES_UNDER_TEST:
            for changes, cause in cases:
                with self.subTest(engine=engine, cause=cause):
                    self.assertRefusedBeforeSpawn(engine, self.profile(**changes), self.env(), cause)
            with self.subTest(engine=engine, cause="profile_unknown_field"):
                profile = {**self.profile(), "use_stored_credentials": True}
                self.assertRefusedBeforeSpawn(engine, profile, self.env(), "profile_unknown_field")

    def test_unknown_engines_refuse(self) -> None:
        for engine in ("claude_code", "codex", "Pi", "opencode2", "hermes-agent", ""):
            with self.subTest(engine=engine), self.assertRaises(SubscriptionRouteRefused) as caught:
                self.spawn(engine, self.profile(), self.env())
            self.assertEqual("unknown_engine", caught.exception.cause)
        self.launcher.assert_not_called()

    def test_missing_or_second_credential_refuses(self) -> None:
        env = self.env()
        del env["ANTHROPIC_API_KEY"]
        self.assertRefusedBeforeSpawn("pi", self.profile(), env, "credential_missing")
        self.assertRefusedBeforeSpawn("hermes", self.profile(), self.env(OPENAI_API_KEY=FAKE_KEY), "extra_credential_env")


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class StoredCredentialTests(_Base):
    """Hermes (and the OpenCode Claude Agent provider) can adopt Claude Code's stored login: refused by home rules."""

    def test_operator_home_or_inside_it_refuses(self) -> None:
        inside = self.operator / "route"
        inside.mkdir(mode=0o700)
        for home, cause in ((self.operator, "route_home_is_operator_home"), (inside, "route_home_is_operator_home")):
            for engine in ENGINES_UNDER_TEST:
                with self.subTest(engine=engine, home=home.name), self.assertRaises(SubscriptionRouteRefused) as caught:
                    profile = self.profile(**({HERMES_ADOPT_EXTERNAL_LOGINS: False} if engine == "hermes" else {}))
                    guard_then_spawn(self.launcher, [engine], engine, profile, self.env(HOME=str(home)),
                                     route_home=str(home), parent_env=self.parent(), operator_home=str(self.operator))
                self.assertEqual(cause, caught.exception.cause)
        self.launcher.assert_not_called()

    def test_a_claude_store_in_the_route_home_refuses_without_being_read(self) -> None:
        real_open = builtins.open
        opened: list[str] = []

        def spy(path, *args, **kwargs):  # records any open of a store path
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        for entry in (".claude", ".claude.json", ".config/claude"):
            for engine in ("hermes", "opencode", "pi"):
                with self.subTest(entry=entry, engine=engine):
                    target = self.home / entry
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.endswith(".json"):
                        target.write_text("{}", encoding="ascii")
                    else:
                        target.mkdir()
                    with mock.patch("builtins.open", spy), mock.patch("io.open", spy):
                        self.assertRefusedBeforeSpawn(engine, self.profile(), self.env(), "claude_store_in_route_home")
                    shutil.rmtree(target) if target.is_dir() else target.unlink()
        self.assertEqual([], [path for path in opened if "claude" in path])

    def test_a_link_named_like_the_store_also_refuses(self) -> None:
        (self.home / ".claude").symlink_to(self.root / "nowhere")
        self.assertRefusedBeforeSpawn("hermes", self.profile(), self.env(), "claude_store_in_route_home")

    def test_child_home_must_be_the_route_home_and_private(self) -> None:
        self.assertRefusedBeforeSpawn("hermes", self.profile(), self.env(HOME=str(self.operator)), "child_home_not_route_home")
        os.chmod(self.home, 0o755)
        self.assertRefusedBeforeSpawn("hermes", self.profile(), self.env(), "route_home_not_private")


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class SelectorAndExtraEnvTests(_Base):
    """Engine config/home selectors and any name outside the small baseline refuse before the launcher runs."""

    def test_engine_config_and_home_selectors_refuse(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            for name in ("HERMES_HOME", "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "OPENCODE_CONFIG",
                         "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"):
                with self.subTest(engine=engine, name=name):
                    value = '{"provider":{"claude-agent":{}}}' if name == "OPENCODE_CONFIG_CONTENT" else str(self.operator)
                    self.assertRefusedBeforeSpawn(engine, self.profile(), self.env(**{name: value}), "engine_selector_env")
        self.assertLessEqual({"HERMES_HOME", "OPENCODE_CONFIG_CONTENT", "XDG_DATA_HOME"}, set(ENGINE_SELECTOR_ENV))

    def test_any_other_name_fails_closed(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            for name in ("PI_OAUTH_PROVIDER", "OPENCODE_PROVIDER", "HERMES_PROVIDER", "NODE_OPTIONS", "LD_PRELOAD",
                         "HTTPS_PROXY", "SSL_CERT_FILE", "ANTHROPIC_BASE_URL"):
                with self.subTest(engine=engine, name=name):
                    self.assertRefusedBeforeSpawn(engine, self.profile(), self.env(**{name: "x"}), "child_env_not_allowed")

    def test_the_safe_baseline_launches(self) -> None:
        env = {name: "x" for name in SAFE_ENV}
        env.update(PATH="/usr/bin:/bin", HOME=str(self.home), LANG="C.UTF-8", ANTHROPIC_API_KEY=FAKE_KEY)
        for engine in ENGINES_UNDER_TEST:
            with self.subTest(engine=engine):
                decision, _ = self.spawn(engine, self.profile(), env)
                self.assertEqual("ANTHROPIC_API_KEY", decision.credential_env)
        self.assertEqual(len(ENGINES_UNDER_TEST), self.launcher.call_count)

    def test_engine_store_entries_in_the_route_home_refuse_without_being_read(self) -> None:
        real_open = builtins.open
        opened: list[str] = []

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        for entry in (".pi/agent", ".hermes", ".local/share/opencode/auth.json", ".config/opencode", ".codex/auth.json"):
            for engine in ENGINES_UNDER_TEST:
                with self.subTest(entry=entry, engine=engine):
                    target = self.home / entry
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.endswith(".json"):
                        target.write_text("{}", encoding="ascii")
                    else:
                        target.mkdir()
                    with mock.patch("builtins.open", spy), mock.patch("io.open", spy):
                        self.assertRefusedBeforeSpawn(engine, self.profile(), self.env(), "engine_store_in_route_home")
                    shutil.rmtree(self.home / entry.split("/")[0])
        self.assertEqual([], [path for path in opened if str(self.home) in path])
        self.assertLessEqual({".pi", ".hermes", ".local/share/opencode"}, set(ENGINE_STORE_ENTRIES))


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class LaunchOptionTests(_Base):
    """guard_then_spawn cannot be used to replace the child env or widen the launch."""

    def test_env_shell_executable_and_preexec_refuse_before_any_check(self) -> None:
        for key, value in (("env", {"CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH}), ("shell", True), ("executable", "/bin/sh"),
                           ("preexec_fn", print), ("pass_fds", (0,)), ("close_fds", False), ("user", 0)):
            for engine in ENGINES_UNDER_TEST:
                with self.subTest(key=key, engine=engine), self.assertRaises(SubscriptionRouteRefused) as caught:
                    guard_then_spawn(self.launcher, [engine], engine, self.profile(), self.env(),
                                     route_home=str(self.home), operator_home=str(self.operator), **{key: value})
                self.assertEqual(("launch_option_denied", key), (caught.exception.cause, caught.exception.detail))
        self.launcher.assert_not_called()

    def test_malformed_argv_refuses(self) -> None:
        for argv in ("pi run", [], [None], b"pi"):
            with self.subTest(argv=argv), self.assertRaises(SubscriptionRouteRefused) as caught:
                guard_then_spawn(self.launcher, argv, "pi", self.profile(), self.env(), route_home=str(self.home),
                                 operator_home=str(self.operator))
            self.assertEqual("argv_malformed", caught.exception.cause)
        self.launcher.assert_not_called()

    def test_the_launcher_gets_exactly_the_checked_env_and_no_shell(self) -> None:
        self.spawn("opencode", self.profile(), self.env())
        args, kwargs = self.launcher.call_args
        self.assertEqual((["opencode", "run"],), args)
        self.assertEqual({"env": self.env(), "shell": False}, kwargs)


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class HealthyApiProviderTests(_Base):
    """Ordinary API-key routes launch, and a subscription name only in the parent is flagged, not refused."""

    def test_anthropic_and_openai_api_keys_launch_in_every_engine(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            for provider, credential in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")):
                with self.subTest(engine=engine, provider=provider):
                    env = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), credential: FAKE_KEY}
                    profile = self.profile(provider=provider, credential_env=credential, model=f"{provider}/m")
                    for _ in range(N):
                        decision, _ = self.spawn(engine, profile, env)
                        self.assertEqual((engine, provider, credential, ()), (decision.engine, decision.provider,
                                                                              decision.credential_env, decision.flags))
        self.assertEqual(len(ENGINES_UNDER_TEST) * 2 * N, self.launcher.call_count)
        args, kwargs = self.launcher.call_args
        self.assertEqual(env, kwargs["env"])

    def test_a_parent_subscription_token_is_flagged_when_kept_out_of_the_child(self) -> None:
        parent = self.parent(CLAUDE_CODE_OAUTH_TOKEN=FAKE_OAUTH, ANTHROPIC_OAUTH_TOKEN=FAKE_OAUTH)
        decision, _ = self.spawn("hermes", self.profile(), self.env(), parent)
        self.assertEqual(("parent_env_has:ANTHROPIC_OAUTH_TOKEN", "parent_env_has:CLAUDE_CODE_OAUTH_TOKEN"),
                         decision.flags)
        self.assertNotIn(FAKE_OAUTH, repr(decision))
        self.launcher.assert_called_once()

    def test_model_is_optional(self) -> None:
        decision, _ = self.spawn("pi", self.profile(model=None), self.env())
        self.assertEqual("anthropic", decision.provider)

    def test_check_route_alone_never_launches(self) -> None:
        check_route("opencode", self.profile(), self.env(), route_home=str(self.home), parent_env=self.parent(),
                    operator_home=str(self.operator))
        self.launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
