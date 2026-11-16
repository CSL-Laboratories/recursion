"""The containment setting reaches the route-home rule (lane INSTALL's request to lane ENGINE-E9, Nov 15).

``allow_inside_operator_home`` is ``setup/containment.py``'s setting, read once by the daemon. True
(``inside_operator_home``, the setting's default) refuses only the operator's home itself as a route
home, as ``setup.route_home.check`` does by default; anything else (``outside_operator_home``, and this
policy's own default, so a caller that passes nothing keeps today's stricter rule) also refuses a route
home inside it. No engine, provider or network is used; every credential value is a fake string.
"""

from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

from recursion_core.routes.profile_route import profile_adapters
from recursion_core.subscription_routes import (
    HERMES_ADOPT_EXTERNAL_LOGINS,
    SubscriptionRouteRefused,
    check_antigravity_route,
    check_cursor_route,
    check_route,
    guard_antigravity_then_spawn,
    guard_cursor_then_spawn,
    guard_then_spawn,
)
from recursion_core.subscription_routes.tests.test_subscription_routes import ENGINES_UNDER_TEST, _Base


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class ContainmentSettingTests(_Base):
    def route_home(self, *names: str):
        path = self.operator
        for name in names:
            path = path / name
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def launch(self, engine: str, home, **setting):
        profile = self.profile(**({HERMES_ADOPT_EXTERNAL_LOGINS: False} if engine == "hermes" else {}))
        argv = [self.wrapper.launcher_path if engine in ("pi", "hermes") else engine, "run", "anthropic/m"]
        return guard_then_spawn(self.launcher, argv, engine, profile, self.env(HOME=str(home)), route_home=str(home),
                                parent_env=self.parent(), operator_home=str(self.operator),
                                wrapper=self.wrapper if engine in ("pi", "hermes") else None, **setting)

    def refused(self, call, cause: str = "route_home_is_operator_home") -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            call()
        self.assertEqual(caught.exception.cause, cause)

    def test_inside_the_operators_home_is_accepted_only_when_the_setting_allows_it(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            home = self.route_home(".local", "state", "recursion", "homes", engine)  # the launcher's place
            with self.subTest(engine=engine):
                self.launch(engine, home, allow_inside_operator_home=True)
                self.refused(lambda: self.launch(engine, home, allow_inside_operator_home=False))
                self.refused(lambda: self.launch(engine, home))  # nothing passed: today's rule
        self.assertEqual(self.launcher.call_count, len(ENGINES_UNDER_TEST))

    def test_a_subscription_sign_in_takes_the_setting_too(self) -> None:
        home = self.route_home("homes", "opencode")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        profile = {"provider": "chatgpt", "auth": "subscription", "method": "sign_in_with_chatgpt",
                   "model": "chatgpt/test-model"}
        argv = ["opencode", "run", "chatgpt/test-model"]
        common = {"route_home": str(home), "parent_env": self.parent(), "operator_home": str(self.operator)}
        guard_then_spawn(self.launcher, argv, "opencode", profile, env, allow_inside_operator_home=True, **common)
        self.assertEqual(self.launcher.call_count, 1)
        for setting in ({}, {"allow_inside_operator_home": False}):
            with self.subTest(setting=setting):
                self.refused(lambda: guard_then_spawn(self.launcher, argv, "opencode", profile, env,
                                                      **common, **setting))
        self.assertEqual(self.launcher.call_count, 1)

    def test_the_operators_home_itself_is_refused_whatever_the_setting(self) -> None:
        alias = self.root / "operator-alias"
        alias.symlink_to(self.operator)
        for engine in ENGINES_UNDER_TEST:
            for home in (self.operator, alias):
                for allow in (True, False):
                    with self.subTest(engine=engine, home=home.name, allow=allow):
                        self.refused(lambda: self.launch(engine, home, allow_inside_operator_home=allow))
        self.launcher.assert_not_called()

    def test_anything_but_true_is_the_stricter_rule(self) -> None:
        home = self.route_home("homes", "opencode")
        for value in (1, "true", "yes", None, [True]):
            with self.subTest(value=value):
                self.refused(lambda: self.launch("opencode", home, allow_inside_operator_home=value))
        self.launcher.assert_not_called()

    def test_a_route_home_outside_the_operators_home_is_accepted_whatever_the_setting(self) -> None:
        for allow in (True, False):
            with self.subTest(allow=allow):
                self.launch("opencode", self.home, allow_inside_operator_home=allow)
        self.assertEqual(self.launcher.call_count, 2)

    def test_cursor_and_antigravity_take_the_setting_too(self) -> None:
        home = self.route_home("homes", "cursor")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        common = {"route_home": str(home), "parent_env": self.parent(), "operator_home": str(self.operator)}
        check_cursor_route(env, wrapper=self.wrapper, allow_inside_operator_home=True, **common)
        guard_cursor_then_spawn(self.launcher, [self.wrapper.launcher_path], env, wrapper=self.wrapper,
                                allow_inside_operator_home=True, **common)
        self.assertEqual(self.launcher.call_count, 1)
        with mock.patch("recursion_core.subscription_routes.policy.verify_antigravity_pin",
                        side_effect=AssertionError("the pin is reached")):  # True: past the home, to the pin
            with self.assertRaisesRegex(AssertionError, "the pin is reached"):
                check_antigravity_route(env, version="1", binary_path="/x", launcher_path="/y", pin=mock.Mock(),
                                        allow_inside_operator_home=True, **common)
            with self.assertRaisesRegex(AssertionError, "the pin is reached"):
                guard_antigravity_then_spawn(self.launcher, ["/x"], env, version="1", binary_path="/x",
                                             launcher_path="/y", pin=mock.Mock(), allow_inside_operator_home=True,
                                             **common)
        for setting in ({}, {"allow_inside_operator_home": False}):
            with self.subTest(setting=setting):
                self.refused(lambda: check_cursor_route(env, wrapper=self.wrapper, **common, **setting))
                # The home is checked before Antigravity's pin, which is not reached here.
                pin = mock.Mock(name="pin")
                self.refused(lambda: check_antigravity_route(env, version="1", binary_path="/x", launcher_path="/y",
                                                             pin=pin, **common, **setting))
                self.refused(lambda: guard_antigravity_then_spawn(self.launcher, ["/x"], env, version="1",
                                                                  binary_path="/x", launcher_path="/y", pin=pin,
                                                                  **common, **setting))
        self.assertEqual(self.launcher.call_count, 1)

    def test_the_keyword_is_the_stricter_rule_by_default_on_every_entry(self) -> None:
        for function in (check_route, guard_then_spawn, check_cursor_route, guard_cursor_then_spawn,
                         check_antigravity_route, guard_antigravity_then_spawn):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters["allow_inside_operator_home"]
                self.assertEqual((parameter.kind, parameter.default), (inspect.Parameter.KEYWORD_ONLY, False))

    def test_the_profile_route_passes_the_setting_on(self) -> None:
        request = {"request_id": "r1", "route": {"home": str(self.home), "connection": {"kind": "subscription"},
                                                 "profile": self.profile()}}
        for setting, expected in (({}, False), ({"allow_inside_operator_home": True}, True),
                                  ({"allow_inside_operator_home": False}, False)):
            with self.subTest(setting=setting), \
                    mock.patch("recursion_core.routes.profile_route.guard_then_spawn",
                               side_effect=SubscriptionRouteRefused("pi", "stopped_here")) as guard:
                adapters = profile_adapters(mock.Mock(name="launcher"), mock.Mock(name="clock"),
                                            lambda cid, name: "fake", self.parent(), argv=lambda req: ["pi"],
                                            driver=mock.Mock(name="driver"), operator_home=str(self.operator),
                                            **setting)
                for engine, adapter in adapters.items():
                    guard.reset_mock()
                    adapter.run(request, 0.0)
                    self.assertEqual(guard.call_args.kwargs["allow_inside_operator_home"], expected, engine)


if __name__ == "__main__":
    unittest.main()
