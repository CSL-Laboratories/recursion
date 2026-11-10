"""2026-11-10 owner call (Lucas, CSL-840): Hermes is full access only.

A Hermes route refuses ``hermes_full_access_only`` unless ``full_access_acknowledged`` is the bool
``True``, before any other check and before anything is written to the route home. Full access
relaxes nothing else for Hermes, and the other engines are unchanged.

Fakes only: a MagicMock launcher and temp trees. No engine binary is executed and no network is used.
"""

from __future__ import annotations

import os
import shutil
import unittest
from unittest import mock

from recursion_core.subscription_routes import (
    HERMES_ADOPT_EXTERNAL_LOGINS,
    RouteRequirements,
    check_route,
    guard_then_spawn,
    wrapper_pin,
)
from recursion_core.subscription_routes.tests.test_owner_calls import ADOPT_YAML, _Calls
from recursion_core.subscription_routes.tests.test_subscription_routes import FAKE_OAUTH

FULL = "full_access_profile_acknowledged"
ONLY = "hermes_full_access_only"


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class HermesFullAccessOnlyTests(_Calls):
    def api(self, **changes: object) -> dict:
        return {**self.profile(), HERMES_ADOPT_EXTERNAL_LOGINS: False, **changes}

    def sub(self) -> dict:
        return {"provider": "grok", "auth": "subscription", "method": "grok_sign_in", "model": "grok/test-model",
                HERMES_ADOPT_EXTERNAL_LOGINS: False}

    def valid(self):
        """The two otherwise valid Hermes routes: (kind, profile, child env, argv)."""

        return (("api_key", self.api(), self.env(), [self.launcher_path, "run", "anthropic/m"]),
                ("subscription", self.sub(), self.bare(), [self.launcher_path, "run", "grok/test-model"]))

    def check(self, profile: object, env: dict, **inputs: object):
        """check_route for Hermes exactly as given: no full access unless the test passes it."""

        inputs.setdefault("wrapper", self.wrapper)
        inputs.setdefault("parent_env", self.parent())
        return check_route("hermes", profile, env, route_home=str(self.home), operator_home=str(self.operator),
                           **inputs)

    def guard(self, profile: object, env: dict, argv: list[str], **inputs: object):
        """guard_then_spawn for Hermes exactly as given: no full access unless the test passes it."""

        inputs.setdefault("wrapper", self.wrapper)
        inputs.setdefault("parent_env", self.parent())
        return guard_then_spawn(self.launcher, argv, "hermes", profile, env, route_home=str(self.home),
                                operator_home=str(self.operator), **inputs)

    def assert_only(self, call, *args: object, **inputs: object) -> None:
        exc = self.refused(ONLY, call, *args, **inputs)
        self.assertEqual(("hermes", ""), (exc.engine, exc.detail))
        self.assertNotIn(FAKE_OAUTH, str(exc))

    def config(self):
        return self.home / ".hermes" / "config.yaml"

    # (a) check_route refuses a valid Hermes route without the acknowledgment.
    def test_check_route_refuses_a_valid_hermes_route_without_full_access(self) -> None:
        for kind, profile, env, _ in self.valid():
            with self.subTest(kind=kind):
                # Valid in every other respect: the same inputs are accepted under full access.
                self.assertEqual("hermes", self.check(profile, env, full_access_acknowledged=True).engine)
                self.assert_only(self.check, profile, env)
                self.assert_only(self.check, profile, env, full_access_acknowledged=False)
        self.assertEqual([], os.listdir(self.home))

    # (b) the guard refuses, never calls the launcher and leaves the route home as it was.
    def test_guard_refuses_before_the_launcher_and_writes_nothing_to_the_route_home(self) -> None:
        (self.home / "kept.txt").write_text("kept\n", encoding="ascii")
        real_rename, real_link = os.rename, os.link
        for kind, profile, env, argv in self.valid():
            for inputs in ({}, {"full_access_acknowledged": False}):
                with self.subTest(kind=kind, inputs=inputs), \
                        mock.patch.object(os, "rename", side_effect=real_rename) as rename, \
                        mock.patch.object(os, "link", side_effect=real_link) as link:
                    self.assert_only(self.guard, profile, env, argv, **inputs)
                    rename.assert_not_called()
                    link.assert_not_called()
                    self.assertEqual(["kept.txt"], os.listdir(self.home))
                    self.assertFalse(os.path.lexists(self.home / ".hermes"))
                    self.assertFalse(os.path.lexists(self.config()))
                    self.assertFalse([name for name in os.listdir(self.home) if name.startswith(".hermes.recursion-")])
        self.launcher.assert_not_called()

    def test_an_existing_hermes_dir_is_left_untouched_without_full_access(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        _, profile, env, argv = self.valid()[1]
        self.assert_only(self.guard, profile, env, argv)
        self.assertEqual(["state.db"], os.listdir(self.home / ".hermes"))
        self.assertEqual([".hermes"], os.listdir(self.home))
        self.launcher.assert_not_called()

    # (c) the refusal comes before every other cause.
    def test_the_refusal_fires_before_other_causes(self) -> None:
        argv = [self.launcher_path, "run", "anthropic/m"]
        no_adopt = self.profile()
        cases = (
            ("hermes_adopt_external_logins_not_off", no_adopt, self.env(), {}),
            ("hermes_adopt_external_logins_not_off", self.api(**{HERMES_ADOPT_EXTERNAL_LOGINS: True}), self.env(), {}),
            ("wrapper_pin_missing", self.api(), self.env(), {"wrapper": None}),
            ("claude_subscription_env", self.api(), self.env(CLAUDE_CODE_OAUTH_TOKEN=FAKE_OAUTH), {}),
            ("claude_subscription_provider", self.api(provider="claude-max", model=None), self.env(), {}),
            ("profile_unknown_field", self.api(use_stored_credentials=True), self.env(), {}),
            ("profile_malformed", ["not", "a", "mapping"], self.env(), {}),
            ("child_home_not_route_home", self.api(), self.env(HOME=str(self.operator)), {}),
            ("hermes_home_not_owned", self.api(), self.env(),
             {"requirements": RouteRequirements(owned_hermes_home=str(self.outside))}),
        )
        for cause, profile, env, inputs in cases:
            with self.subTest(cause=cause):
                # The case really is bad: under full access it refuses for its own cause.
                self.refused(cause, self.check, profile, env, full_access_acknowledged=True, **inputs)
                self.assert_only(self.check, profile, env, **inputs)
                self.assert_only(self.check, profile, env, full_access_acknowledged=False, **inputs)
                self.assert_only(self.guard, profile, env, argv, **inputs)
        self.assertEqual([], os.listdir(self.home))
        self.launcher.assert_not_called()

    def test_the_refusal_fires_before_the_pin_and_route_home_checks(self) -> None:
        argv = [self.launcher_path, "run", "anthropic/m"]
        (self.install / "lib.js").write_text("lib-v2\n", encoding="ascii")
        self.refused("wrapper_pin_mismatch", self.check, self.api(), self.env(), full_access_acknowledged=True)
        self.assert_only(self.check, self.api(), self.env())
        (self.install / "lib.js").write_text("lib-v1\n", encoding="ascii")
        (self.home / ".hermes").mkdir(mode=0o700)
        self.config().write_bytes(b"model: theirs\n")
        self.refused("hermes_config_blocks_adopt_override", self.check, self.api(), self.env(),
                     full_access_acknowledged=True)
        self.assert_only(self.check, self.api(), self.env())
        self.assert_only(self.guard, self.api(), self.env(), argv)
        self.assertEqual(b"model: theirs\n", self.config().read_bytes())
        self.config().unlink()
        (self.home / ".hermes" / "auth.json").write_text(FAKE_OAUTH, encoding="ascii")
        self.refused("engine_store_in_route_home", self.check, self.api(), self.env(), full_access_acknowledged=True)
        self.assert_only(self.check, self.api(), self.env())
        self.assert_only(self.guard, self.api(), self.env(), ["hermes", "run", "anthropic/m"])  # argv[0] too
        self.launcher.assert_not_called()

    # (d) a malformed acknowledgment or requirements value keeps its own code.
    def test_a_non_bool_acknowledgment_still_refuses_as_malformed(self) -> None:
        for kind, profile, env, argv in self.valid():
            for value in ("true", "True", 1, 0, None, [True], 1.0):
                with self.subTest(kind=kind, value=value):
                    self.refused("route_full_access_malformed", self.check, profile, env,
                                 full_access_acknowledged=value)
                    self.refused("route_full_access_malformed", self.guard, profile, env, argv,
                                 full_access_acknowledged=value)
        self.assertEqual([], os.listdir(self.home))
        self.launcher.assert_not_called()

    def test_malformed_requirements_still_refuse_first(self) -> None:
        for bad in ({"extra_launch_options": ("umask",)}, "umask", True,
                    RouteRequirements(extra_launch_options=["cwd"]), RouteRequirements(owned_hermes_home="relative")):
            for inputs in ({}, {"full_access_acknowledged": False}, {"full_access_acknowledged": True}):
                with self.subTest(bad=bad, inputs=inputs):
                    self.refused("requirements_malformed", self.check, self.api(), self.env(), requirements=bad,
                                 **inputs)
        self.refused("requirements_forbidden_launch_option", self.check, self.api(), self.env(),
                     requirements=RouteRequirements(extra_launch_options=("env",)))

    # (e) with the acknowledgment a valid route runs, and nothing else is relaxed.
    def test_full_access_accepts_a_valid_hermes_route_and_writes_the_exact_switch(self) -> None:
        for kind, profile, env, argv in self.valid():
            with self.subTest(kind=kind):
                self.launcher.reset_mock()
                shutil.rmtree(self.home / ".hermes", ignore_errors=True)  # each kind starts in a clean home
                checked = self.check(profile, env, full_access_acknowledged=True)
                self.assertIn(FULL, checked.flags)
                self.assertTrue(checked.beta)
                self.assertEqual(((HERMES_ADOPT_EXTERNAL_LOGINS, False),), checked.engine_config)
                self.assertFalse(os.path.lexists(self.home / ".hermes"))  # check_route alone writes nothing
                decision, _ = self.guard(profile, env, argv, full_access_acknowledged=True)
                self.assertEqual(checked, decision)
                self.assertIn(FULL, decision.flags)
                self.assertTrue(decision.beta)
                self.assertEqual(37, len(ADOPT_YAML))
                self.assertEqual(ADOPT_YAML, self.config().read_bytes())
                self.assertEqual(["config.yaml"], os.listdir(self.home / ".hermes"))
                self.assertEqual([".hermes"], os.listdir(self.home))
                self.assertEqual(1, self.launcher.call_count)
                kwargs = self.launcher.call_args.kwargs
                self.assertEqual(env, kwargs["env"])
                self.assertIs(kwargs["shell"], False)
                self.assertEqual(((HERMES_ADOPT_EXTERNAL_LOGINS, False),), kwargs["engine_config"])

    def test_full_access_still_requires_and_verifies_the_wrapper_pin(self) -> None:
        full = {"full_access_acknowledged": True}
        argv = [self.launcher_path, "run", "anthropic/m"]
        self.refused("wrapper_pin_missing", self.check, self.api(), self.env(), wrapper=None, **full)
        self.refused("wrapper_pin_missing", self.guard, self.api(), self.env(), argv, wrapper=None, **full)
        for name, text in (("lib.js", "lib-v2\n"), ("extra.js", "extra\n"), ("launcher.js", "launcher-v2\n")):
            with self.subTest(name=name):
                path = self.install / name
                before = path.read_bytes() if path.exists() else None
                path.write_text(text, encoding="ascii")
                self.refused("wrapper_pin_mismatch", self.check, self.api(), self.env(), **full)
                self.refused("wrapper_pin_mismatch", self.guard, self.api(), self.env(), argv, **full)
                path.unlink() if before is None else path.write_bytes(before)
        self.assertEqual([], os.listdir(self.home))
        self.launcher.assert_not_called()

    def test_full_access_still_requires_adopt_external_logins_false(self) -> None:
        full = {"full_access_acknowledged": True}
        for kind, profile, env, argv in self.valid():
            missing = {key: value for key, value in profile.items() if key != HERMES_ADOPT_EXTERNAL_LOGINS}
            with self.subTest(kind=kind, value="missing"):
                self.assertEqual("missing", self.refused("hermes_adopt_external_logins_not_off", self.guard, missing,
                                                         env, argv, **full).detail)
            for value in (True, "false", 0, None):
                with self.subTest(kind=kind, value=value):
                    self.refused("hermes_adopt_external_logins_not_off", self.guard,
                                 {**profile, HERMES_ADOPT_EXTERNAL_LOGINS: value}, env, argv, **full)
        self.assertEqual([], os.listdir(self.home))
        self.launcher.assert_not_called()

    def test_full_access_still_refuses_a_config_with_other_bytes(self) -> None:
        full = {"full_access_acknowledged": True}
        same_size = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(same_size))
        (self.home / ".hermes").mkdir(mode=0o700)
        for content in (same_size, ADOPT_YAML + b"model: x\n", b"", ADOPT_YAML[:-1]):
            for kind, profile, env, argv in self.valid():
                with self.subTest(content=content, kind=kind):
                    self.config().write_bytes(content)
                    self.refused("hermes_config_blocks_adopt_override", self.check, profile, env, **full)
                    self.refused("hermes_config_blocks_adopt_override", self.guard, profile, env, argv, **full)
                    self.assertEqual(content, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_full_access_still_binds_argv0_to_the_pinned_launcher(self) -> None:
        full = {"full_access_acknowledged": True}
        other = self.outside / "hermes"
        other.write_text("launcher-v1\n", encoding="ascii")  # same bytes as the pinned launcher
        for argv0 in ("hermes", "./launcher.js", "launcher.js", str(other), str(self.install / "lib.js"), ""):
            with self.subTest(argv0=argv0):
                self.refused("argv0_not_pinned", self.guard, self.api(), self.env(), [argv0, "run", "anthropic/m"],
                             **full)
        self.assertEqual([], os.listdir(self.home))  # refused before the switch is written
        self.launcher.assert_not_called()

    def test_full_access_still_refuses_another_entry_in_the_hermes_dir_on_an_api_key_route(self) -> None:
        full = {"full_access_acknowledged": True}
        argv = [self.launcher_path, "run", "anthropic/m"]
        self.guard(self.api(), self.env(), argv, **full)
        self.launcher.reset_mock()
        # What a first Hermes launch leaves behind: the second launch in this home still refuses.
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        exc = self.refused("engine_store_in_route_home", self.guard, self.api(), self.env(), argv, **full)
        self.assertEqual(".hermes", exc.detail)
        self.refused("engine_store_in_route_home", self.check, self.api(), self.env(), **full)
        self.config().unlink()  # other state and no switch
        self.refused("engine_store_in_route_home", self.guard, self.api(), self.env(), argv, **full)
        self.assertEqual(["state.db"], os.listdir(self.home / ".hermes"))
        self.launcher.assert_not_called()

    def test_full_access_still_applies_the_env_credential_and_home_rules(self) -> None:
        full = {"full_access_acknowledged": True}
        argv = [self.launcher_path, "run", "anthropic/m"]
        for cause, profile, env, inputs in (
            ("claude_subscription_env", self.api(), self.env(ANTHROPIC_TOKEN=FAKE_OAUTH), {}),
            ("claude_store_env", self.api(), self.env(CLAUDE_CONFIG_DIR=str(self.operator / ".claude")), {}),
            ("engine_selector_env", self.api(), self.env(HERMES_HOME=str(self.operator)), {}),
            ("child_env_not_allowed", self.api(), self.env(HTTPS_PROXY="http://127.0.0.1:1"), {}),
            ("extra_credential_env", self.api(), self.env(OPENAI_API_KEY="x"), {}),
            ("claude_subscription_auth", self.api(auth="oauth"), self.env(), {}),
            ("subscription_unconfirmed", {**self.sub(), "provider": "chatgpt", "method": "sign_in_with_chatgpt",
                                          "model": "chatgpt/m"}, self.bare(), {}),
            ("google_subscription_refused", {**self.sub(), "provider": "gemini", "model": "gemini/m"}, self.bare(), {}),
            ("child_home_not_route_home", self.api(), self.env(HOME=str(self.operator)), {}),
            ("hermes_home_not_owned", self.api(), self.env(),
             {"requirements": RouteRequirements(owned_hermes_home=str(self.outside))}),
            ("launch_option_unknown", self.api(), self.env(), {"start_new_session": True}),
            ("launch_option_denied", self.api(), self.env(), {"shell": True}),
        ):
            with self.subTest(cause=cause):
                self.refused(cause, self.guard, profile, env, argv, **inputs, **full)
        (self.home / ".claude").mkdir()
        self.refused("claude_store_in_route_home", self.guard, self.api(), self.env(), argv, **full)
        (self.home / ".claude").rmdir()
        (self.home / ".pi").mkdir()
        self.refused("engine_store_in_route_home", self.guard, self.api(), self.env(), argv, **full)
        self.assertEqual([".pi"], os.listdir(self.home))
        self.launcher.assert_not_called()

    # (f) the other engines are unchanged.
    def test_pi_opencode_cursor_and_antigravity_still_launch_without_full_access(self) -> None:
        for engine in ("pi", "cursor", "antigravity"):
            for inputs in ({}, {"full_access_acknowledged": False}):
                with self.subTest(engine=engine, inputs=inputs):
                    self.launcher.reset_mock()
                    decision, _ = self.launch(engine, **inputs)
                    self.assertEqual(engine, decision.engine)
                    self.assertTrue(decision.beta)
                    self.assertNotIn(FULL, decision.flags)
                    self.assertEqual(1, self.launcher.call_count)
        for inputs in ({}, {"full_access_acknowledged": False}):
            with self.subTest(engine="opencode", inputs=inputs):
                self.launcher.reset_mock()
                decision, _ = guard_then_spawn(self.launcher, ["opencode", "run"], "opencode", self.profile(),
                                               self.env(), route_home=str(self.home), parent_env=self.parent(),
                                               operator_home=str(self.operator), **inputs)
                self.assertEqual(("opencode", False, ()), (decision.engine, decision.beta, decision.flags))
                self.assertEqual(1, self.launcher.call_count)
        self.assertEqual([], os.listdir(self.home))  # and none of them gets a .hermes

    # (g) the pin limits are the ones on master.
    def test_the_tree_cap_is_unchanged_and_full_access_does_not_lift_it(self) -> None:
        self.assertEqual(100_000, wrapper_pin._MAX_FILES)
        self.assertEqual(32 * 1024 * 1024, wrapper_pin._MAX_FILE_BYTES)
        self.assertEqual(200, wrapper_pin._MAX_DEPTH)
        # One entry past the real cap, without building 100,001 files: the install root's listing
        # repeats a real file's name. The walk counts entries, so it refuses on entry 100,001.
        real_listdir = os.listdir
        install = os.path.realpath(self.install)

        def crowded(path: object = ".") -> list[str]:
            if os.path.realpath(os.fspath(path)) == install:
                return ["lib.js"] * (wrapper_pin._MAX_FILES + 1)
            return real_listdir(path)

        with mock.patch.object(wrapper_pin.os, "listdir", side_effect=crowded):
            exc = self.refused("wrapper_tree_too_large", self.check, self.api(), self.env(),
                               full_access_acknowledged=True)
            self.assertEqual("100001", exc.detail)
            self.assert_only(self.check, self.api(), self.env())  # and without it, the tree is not walked
        self.assertEqual("hermes", self.check(self.api(), self.env(), full_access_acknowledged=True).engine)

    # (h) only the caller's bool turns Hermes on.
    def test_no_environment_variable_or_profile_field_turns_hermes_on(self) -> None:
        names = ("FULL_ACCESS_ACKNOWLEDGED", "full_access_acknowledged", "RECURSION_FULL_ACCESS",
                 "RECURSION_FULL_ACCESS_ACKNOWLEDGED", "HERMES_FULL_ACCESS", "RECURSION_HERMES_FULL_ACCESS")
        for value in ("1", "true", "True"):
            loud = dict.fromkeys(names, value)
            with self.subTest(value=value), mock.patch.dict(os.environ, loud):
                for kind, profile, env, argv in self.valid():
                    parent = self.parent(**loud)
                    self.assert_only(self.check, profile, env, parent_env=parent)
                    self.assert_only(self.guard, profile, env, argv, parent_env=parent)
                    self.assert_only(self.guard, profile, env, argv, parent_env=parent, full_access_acknowledged=False)
                    for name in names:  # in the child env as well
                        self.assert_only(self.check, profile, {**env, name: value}, parent_env=parent)
                        self.assert_only(self.guard, profile, {**env, name: value}, argv, parent_env=parent)
                    self.assert_only(self.check, {**profile, "full_access_acknowledged": True}, env, parent_env=parent)
                # With the bool, the same child env name is still not an allowed name.
                self.refused("child_env_not_allowed", self.check, self.api(),
                             self.env(FULL_ACCESS_ACKNOWLEDGED=value), full_access_acknowledged=True)
                self.refused("profile_unknown_field", self.check, self.api(full_access_acknowledged=True), self.env(),
                             full_access_acknowledged=True)
        self.assertEqual([], os.listdir(self.home))
        self.launcher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
