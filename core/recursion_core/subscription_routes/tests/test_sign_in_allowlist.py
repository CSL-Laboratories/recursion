"""2026-11-08 briefing: harness subscription sign-ins (E3 OpenCode, E8 Pi, E9 Hermes).

Fakes only: a MagicMock launcher and temp homes. No engine, store, provider or network is touched.
"""

from __future__ import annotations

import builtins
import os
import shutil
import sys
import unittest
from unittest import mock

from recursion_core.subscription_routes import (
    HERMES_ADOPT_EXTERNAL_LOGINS, SUBSCRIPTION_SIGN_INS, SubscriptionRouteRefused, check_route, guard_then_spawn,
)
from recursion_core.subscription_routes.tests.test_subscription_routes import FAKE_KEY, FAKE_OAUTH, _Base

ALLOWED = [("opencode", "chatgpt", "sign_in_with_chatgpt"), ("opencode", "grok", "grok_sign_in"),
           ("pi", "chatgpt", "sign_in_with_chatgpt"), ("hermes", "grok", "grok_sign_in")]


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class AllowlistTests(_Base):
    def sub(self, provider: str, method: object = "", **extra: object) -> dict:
        # A subscription route must name its model; default to one under the signed-in provider.
        extra.setdefault("model", "_".join(provider.strip().lower().split()) + "/test-model")
        profile = {"provider": provider, "auth": "subscription", **{k: v for k, v in extra.items() if v is not None}}
        if method is not None:
            profile["method"] = method
        return profile

    def bare_env(self, **extra: str) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home), **extra}

    def test_the_four_allowed_sign_ins_launch_with_no_credential(self) -> None:
        for engine, provider, method in ALLOWED:
            with self.subTest(engine=engine, provider=provider):
                decision, _ = self.spawn(engine, self.sub(provider, method), self.bare_env())
                self.assertEqual((decision.provider, decision.auth, decision.method, decision.credential_env),
                                 (provider, "subscription", method, ""))
                env = self.launcher.call_args.kwargs["env"]
                self.assertEqual(set(env), {"PATH", "HOME"})

    def test_product_spellings_match(self) -> None:
        decision, _ = self.spawn("pi", self.sub("ChatGPT", "Sign in with ChatGPT"), self.bare_env())
        self.assertEqual(decision.method, "sign_in_with_chatgpt")
        decision, _ = self.spawn("opencode", self.sub(" Grok ", "grok-sign-in", auth="Subscription"), self.bare_env())
        self.assertEqual(decision.provider, "grok")

    def test_provider_not_named_for_that_harness_is_unconfirmed(self) -> None:
        for engine, provider in (("pi", "grok"), ("hermes", "chatgpt")):
            self.assertRefusedBeforeSpawn(engine, self.sub(provider, SUBSCRIPTION_SIGN_INS["opencode"][provider]),
                                          self.bare_env(), "subscription_unconfirmed")

    def test_unknown_providers_and_aliases_are_unconfirmed(self) -> None:
        for provider in ("openai", "chatgpt-plus", "xai", "supergrok", "copilot", "mistral", "codex"):
            self.assertRefusedBeforeSpawn("opencode", self.sub(provider, "sign_in_with_chatgpt"), self.bare_env(),
                                          "subscription_unconfirmed")

    def test_method_is_enforced_not_just_the_provider(self) -> None:
        for method in ("api_key_paste", "device_code", "sign_in_with_chatgpt_v2", "oauth", "grok_sign_in"):
            self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", method), self.bare_env(),
                                          "subscription_method_not_registered")
        self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "sign_in_with_chatgpt"), self.bare_env(),
                                      "subscription_method_not_registered")
        for method in (None, "", 7):
            self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", method), self.bare_env(), "method_missing")

    def test_claude_refused_in_all_three(self) -> None:
        for engine in ("pi", "hermes", "opencode"):
            for provider, cause in (("claude", "claude_subscription_provider"),
                                    ("Claude Max", "claude_subscription_provider"),
                                    ("anthropic", "claude_subscription_auth"),
                                    ("anthropic-oauth", "claude_subscription_provider")):
                self.assertRefusedBeforeSpawn(engine, self.sub(provider, "sign_in_with_chatgpt"), self.bare_env(),
                                              cause)

    def test_google_refused_in_all_three(self) -> None:
        for engine in ("pi", "hermes", "opencode"):
            for provider in ("google", "Gemini", "google-one", "antigravity", "google_ai_pro"):
                self.assertRefusedBeforeSpawn(engine, self.sub(provider, "sign_in_with_google"), self.bare_env(),
                                              "google_subscription_refused")

    def test_hermes_without_adopt_external_logins_false_is_refused(self) -> None:
        # PRODUCT.md 055cc7a5 section 2 (E9): "the E9 profile sets Hermes's auth.adopt_external_logins: false".
        self.assertEqual(HERMES_ADOPT_EXTERNAL_LOGINS, "auth.adopt_external_logins")
        for profile in (self.sub("grok", "grok_sign_in"), self.profile()):  # sign-in and API-key routes alike
            self.assertRefusedBeforeSpawn("hermes", profile, self.bare_env() if profile["auth"] != "api_key"
                                          else self.env(), "hermes_adopt_external_logins_not_off",
                                          hermes_override=False)
        for value in (True, "false", 0, "no"):  # only the boolean False turns adoption off
            self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in",
                                                             **{HERMES_ADOPT_EXTERNAL_LOGINS: value}),
                                          self.bare_env(), "hermes_adopt_external_logins_not_off",
                                          hermes_override=False)
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("hermes", {**self.sub("grok", "grok_sign_in"), HERMES_ADOPT_EXTERNAL_LOGINS: None},
                        self.bare_env(), route_home=str(self.home), parent_env=self.parent(),
                        operator_home=str(self.operator), wrapper=self.wrapper)
        self.assertEqual(caught.exception.cause, "hermes_adopt_external_logins_not_off")
        # The Recursion-only key the first draft checked is gone: it is not Hermes's setting.
        self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in", borrow_claude_code_login=False),
                                      self.bare_env(), "profile_unknown_field")

    def test_an_existing_hermes_config_is_not_opened(self) -> None:
        # guard_then_spawn must not read a config that may hold a credential. It refuses instead.
        (self.home / ".hermes").mkdir()
        (self.home / ".hermes" / "config.yaml").write_text("not-read\n", encoding="ascii")
        real_open = builtins.open

        def guard(path, *args, **kwargs):
            if str(path).endswith("config.yaml"):
                raise AssertionError(f"opened {path}")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", guard):
            self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in"), self.bare_env(),
                                          "hermes_config_blocks_adopt_override")

    def test_hermes_override_is_recorded_for_the_driver(self) -> None:
        for profile, env in ((self.sub("grok", "grok_sign_in"), self.bare_env()), (self.profile(), self.env())):
            decision, _ = self.spawn("hermes", {**profile, HERMES_ADOPT_EXTERNAL_LOGINS: False}, env,
                                     hermes_override=False)
            self.assertEqual(decision.engine_config, (("auth.adopt_external_logins", False),))
            self.launcher.reset_mock()
        # Hermes's key is Hermes's alone; the other engines record no engine config and refuse the key.
        for engine in ("pi", "opencode"):
            decision, _ = self.spawn(engine, self.profile(), self.env())
            self.assertEqual(decision.engine_config, ())
            self.launcher.reset_mock()
            self.assertRefusedBeforeSpawn(engine, self.profile(**{HERMES_ADOPT_EXTERNAL_LOGINS: False}), self.env(),
                                          "profile_unknown_field")

    def test_macos_keychain_is_not_hidden_by_home_so_hermes_needs_the_override_there(self) -> None:
        # Claude Code keeps its macOS login in the login Keychain, which a route home does not hide.
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in"), self.bare_env(),
                                          "hermes_adopt_external_logins_not_off", hermes_override=False)
            decision, _ = self.spawn("hermes", self.sub("grok", "grok_sign_in"), self.bare_env())
        self.assertEqual(decision.engine_config, ((HERMES_ADOPT_EXTERNAL_LOGINS, False),))

    def test_subscription_route_requires_a_model(self) -> None:
        # red-team #321 F1: with no model, the engine's own default (e.g. OpenCode's anthropic/...) picks.
        for engine, provider, method in ALLOWED:
            self.assertRefusedBeforeSpawn(engine, self.sub(provider, method, model=None), self.bare_env(),
                                          "model_missing")
        self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt", model="chatgpt/"),
                                      self.bare_env(), "model_name_empty")

    def test_subscription_model_must_be_on_the_argv(self) -> None:
        # Devin r4510802867: a passing model was not bound to the launch.
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_then_spawn(
                self.launcher, ["opencode", "run"], "opencode", self.sub("chatgpt", "sign_in_with_chatgpt"),
                self.bare_env(), route_home=str(self.home), parent_env=self.parent(),
                operator_home=str(self.operator),
            )
        self.assertEqual(caught.exception.cause, "model_not_bound")
        self.launcher.assert_not_called()
        decision, _ = self.spawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual("chatgpt/test-model", decision.model)
        self.assertIn("chatgpt/test-model", self.launcher.call_args.args[0])

    def test_no_token_rides_along_with_a_sign_in(self) -> None:
        cases = [({"OPENAI_API_KEY": FAKE_KEY}, "credential_env_on_subscription"),
                 ({"ANTHROPIC_API_KEY": FAKE_KEY}, "credential_env_on_subscription"),
                 ({"CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH}, "claude_subscription_env"),
                 ({"CODEX_ACCESS_TOKEN": FAKE_OAUTH}, "child_env_not_allowed"),
                 ({"XAI_API_KEY": FAKE_KEY}, "child_env_not_allowed"),
                 ({"OPENCODE_CONFIG": "/x"}, "engine_selector_env")]
        for extra, cause in cases:
            self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt"),
                                          self.bare_env(**extra), cause)
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt",
                                                     credential_env="OPENAI_API_KEY"), self.bare_env(),
                                      "credential_env_on_subscription")

    def test_own_store_allowed_other_stores_refused(self) -> None:
        (self.home / ".pi").mkdir()
        decision, _ = self.spawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual(decision.engine, "pi")
        self.launcher.reset_mock()
        for engine, entry, cause in (("hermes", ".pi", "engine_store_in_route_home"),
                                     ("opencode", ".pi", "engine_store_in_route_home")):
            self.assertRefusedBeforeSpawn(engine, self.sub("grok", "grok_sign_in"), self.bare_env(), cause)
        (self.home / ".pi").rmdir()
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "auth.json").touch()  # Hermes can import Codex's login: refused
        self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in"), self.bare_env(),
                                      "engine_store_in_route_home")

    def test_own_store_may_not_link_outside_the_route_home(self) -> None:
        # Devin r4505333922: a linked .pi/.hermes/opencode store could reach the operator's login.
        op_store = self.operator / ".pi"
        op_store.mkdir(exist_ok=True)
        (self.home / ".pi").symlink_to(op_store)
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_is_link")
        (self.home / ".pi").unlink()
        (self.home / ".pi").mkdir()
        (self.home / ".pi" / "agent").mkdir()
        (self.home / ".pi" / "agent" / "auth.json").symlink_to(op_store / "auth.json")
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_links_outside_route_home")
        (self.home / ".pi" / "agent" / "auth.json").unlink()
        (self.home / ".pi" / "agent" / "auth.json").write_text("{}")
        (self.home / ".pi" / "agent" / "alias.json").symlink_to(self.home / ".pi" / "agent" / "auth.json")
        decision, _ = self.spawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual(decision.engine, "pi")  # a file link that stays inside the route home is fine
        self.launcher.reset_mock()
        # Devin r4505333922, still open: a directory link inside the route home is not walked by
        # os.walk, so a second link in its target can leave the home.
        shutil.rmtree(self.home / ".pi")
        (self.home / ".pi").mkdir()
        side = self.home / "side"
        side.mkdir()
        (side / "escape").symlink_to(op_store)
        (self.home / ".pi" / "jump").symlink_to(side)
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_links_outside_route_home")
        (self.home / ".pi" / "jump").unlink()
        (side / "escape").unlink()
        (self.home / ".pi" / "jump").symlink_to(side)
        decision, _ = self.spawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual(decision.engine, "pi")  # the same directory link with nothing outside is kept
        self.launcher.reset_mock()
        shutil.rmtree(self.home / ".pi")
        (self.home / ".local" / "share").mkdir(parents=True)
        (self.home / ".local" / "share" / "opencode").symlink_to(op_store)
        self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_is_link")

    def test_unlistable_directory_inside_the_own_store_refuses(self) -> None:
        # Mode 0111 can be traversed by name but not listed. os.walk then skips it.
        op_store = self.operator / ".pi"
        op_store.mkdir(exist_ok=True)
        hidden = self.home / ".pi" / "hidden"
        hidden.mkdir(parents=True, mode=0o700)
        (hidden / "escape").symlink_to(op_store)
        hidden.chmod(0o111)
        self.assertEqual(os.path.realpath(hidden / "escape"), str(op_store))
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_unreadable")
        hidden.chmod(0o700)
        shutil.rmtree(self.home / ".pi")
        side = self.home / "side"
        side.mkdir(mode=0o700)
        (side / "escape").symlink_to(op_store)
        side.chmod(0o111)
        (self.home / ".pi").mkdir(mode=0o700)
        (self.home / ".pi" / "jump").symlink_to(side)
        self.assertEqual(os.path.realpath(side / "escape"), str(op_store))
        self.assertRefusedBeforeSpawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env(),
                                      "own_store_unreadable")
        side.chmod(0o700)

    def test_own_store_directory_count_is_bounded(self) -> None:
        store = self.home / ".pi"
        store.mkdir(mode=0o700)
        for index in range(10_001):
            (store / f"d{index}").mkdir()
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            self.spawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual(caught.exception.cause, "own_store_too_large")
        self.launcher.assert_not_called()

    def test_claude_store_still_refused_on_a_sign_in(self) -> None:
        (self.home / ".claude").mkdir()
        self.assertRefusedBeforeSpawn("hermes", self.sub("grok", "grok_sign_in"), self.bare_env(),
                                      "claude_store_in_route_home")

    def test_own_store_is_never_read_and_the_decision_says_it_was_not_inspected(self) -> None:
        # red-team #321 F1: an OAuth-labelled .hermes/auth.json (it could hold a login `hermes auth` adopted) is
        # not read, so the route may not pass silently: the decision flags the uninspected store. Whether to
        # read or refuse such stores is open for Lucas.
        (self.home / ".hermes").mkdir()
        (self.home / ".hermes" / "auth.json").write_text(FAKE_OAUTH)
        real_open = builtins.open

        def guard(path, *args, **kwargs):
            if str(self.home) in str(path):
                raise AssertionError(f"opened {path}")
            return real_open(path, *args, **kwargs)
        with mock.patch.object(builtins, "open", guard), mock.patch.object(os, "open", guard):
            decision = check_route("hermes", {**self.sub("grok", "grok_sign_in"), HERMES_ADOPT_EXTERNAL_LOGINS: False},
                                   self.bare_env(), route_home=str(self.home), parent_env=self.parent(),
                                   operator_home=str(self.operator), wrapper=self.wrapper)
        self.assertIs(builtins.open, real_open)
        self.assertIn("own_login_store_not_inspected:.hermes", decision.flags)
        self.assertNotIn(FAKE_OAUTH, repr(decision))
        (self.home / ".config" / "opencode").mkdir(parents=True)  # could hold a Claude provider plugin
        shutil.rmtree(self.home / ".hermes")
        decision, _ = self.spawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertEqual([f for f in decision.flags if f.startswith("own_login_store")],
                         ["own_login_store_not_inspected:.config/opencode"])

    def test_no_own_store_no_flag(self) -> None:
        decision, _ = self.spawn("pi", self.sub("chatgpt", "sign_in_with_chatgpt"), self.bare_env())
        self.assertFalse([f for f in decision.flags if f.startswith("own_login_store")])

    def test_model_must_match_the_signed_in_provider(self) -> None:
        decision, _ = self.spawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt", model="chatgpt/gpt-x"),
                                 self.bare_env())
        self.assertEqual(decision.provider, "chatgpt")
        self.launcher.reset_mock()
        self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt", model="claude-agent/m"),
                                      self.bare_env(), "claude_subscription_provider")
        self.assertRefusedBeforeSpawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt", model="grok/m"),
                                      self.bare_env(), "model_provider_mismatch")

    def test_method_on_an_api_key_route_refuses(self) -> None:
        self.assertRefusedBeforeSpawn("pi", self.profile(method="sign_in_with_chatgpt"), self.env(),
                                      "method_without_subscription")

    def test_api_key_still_works_everywhere(self) -> None:
        for engine in ("pi", "hermes", "opencode"):
            decision, _ = self.spawn(engine, self.profile(), self.env())
            self.assertEqual((decision.auth, decision.credential_env), ("api_key", "ANTHROPIC_API_KEY"))

    def test_other_auth_kinds_still_refuse(self) -> None:
        for auth in ("oauth", "stored", "claude_login"):
            with self.assertRaises(SubscriptionRouteRefused):
                self.spawn("opencode", self.sub("chatgpt", "sign_in_with_chatgpt", auth=auth), self.bare_env())
        self.launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
