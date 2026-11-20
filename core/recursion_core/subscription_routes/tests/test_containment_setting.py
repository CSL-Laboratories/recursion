"""The containment setting reaches the route-home rule (lane INSTALL's request to lane ENGINE-E9, Nov 15).

``allow_inside_operator_home`` carries lane INSTALL's containment setting as a bool: its value
``inside_operator_home`` (the default of INSTALL's design) is True, ``outside_operator_home`` is False.
Nothing in this tree reads that setting yet, so the policy's own default stays False: a caller that passes
nothing keeps the stricter rule, and a route home inside the operator's home is refused. True accepts there
only the engine's own home where the launcher keeps it, ``<XDG_STATE_HOME or ~/.local/state>/recursion/homes/
<engine>`` (DeepSeek's round-3 read of #425, F1: unbounded, True accepted ``~/.ssh`` or the operator's own
login store as the child's HOME). The operator's home itself is refused whatever the setting. No engine,
provider or network is used; every credential value is a fake string.
"""

from __future__ import annotations

import inspect
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from recursion_core.routes.profile_route import ProfileRouteAdapter, profile_adapters
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
# _Base and ENGINES_UNDER_TEST are test_subscription_routes.py's fixture, shared on purpose (DeepSeek r3 F9):
# a change to that fixture shows up here as well.
from recursion_core.subscription_routes.tests.test_subscription_routes import ENGINES_UNDER_TEST, _Base


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class ContainmentSettingTests(_Base):
    def route_home(self, *names: str, base=None):
        path = self.operator if base is None else base
        for name in names:
            path = path / name
            path.mkdir(mode=0o700, exist_ok=True)
        return path

    def engine_home(self, engine: str, base=None):
        """The launcher's place for ``engine``: ``<state>/recursion/homes/<engine>``, state ~/.local/state."""

        state = self.route_home(".local", "state") if base is None else base
        return self.route_home("recursion", "homes", engine, base=state)

    def launch(self, engine: str, home, parent=None, **setting):
        profile = self.profile(**({HERMES_ADOPT_EXTERNAL_LOGINS: False} if engine == "hermes" else {}))
        argv = [self.wrapper.launcher_path if engine in ("pi", "hermes") else engine, "run", "anthropic/m"]
        return guard_then_spawn(self.launcher, argv, engine, profile, self.env(HOME=str(home)), route_home=str(home),
                                parent_env=self.parent() if parent is None else parent,
                                operator_home=str(self.operator),
                                wrapper=self.wrapper if engine in ("pi", "hermes") else None, **setting)

    def refused(self, call, cause: str = "route_home_is_operator_home", detail: str | None = None) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            call()
        self.assertEqual(caught.exception.cause, cause)
        if detail is not None:
            self.assertEqual(caught.exception.detail, detail)

    def test_inside_the_operators_home_is_accepted_only_when_the_setting_allows_it(self) -> None:
        for engine in ENGINES_UNDER_TEST:
            home = self.engine_home(engine)  # the launcher's place
            with self.subTest(engine=engine):
                self.launch(engine, home, allow_inside_operator_home=True)
                self.refused(lambda: self.launch(engine, home, allow_inside_operator_home=False),
                             detail="inside_operator_home")
                self.refused(lambda: self.launch(engine, home), detail="inside_operator_home")  # nothing passed
        self.assertEqual(self.launcher.call_count, len(ENGINES_UNDER_TEST))

    def test_true_accepts_no_other_folder_inside_the_operators_home(self) -> None:
        # DeepSeek r3 F1: with True, any private folder inside the operator's home was accepted and handed on
        # as the child's HOME, the operator's .ssh and own OpenCode login store among them.
        ssh = self.route_home(".ssh")
        (ssh / "id_ed25519").write_text("synthetic-not-a-key\n", encoding="ascii")
        for engine in ENGINES_UNDER_TEST:
            other = "hermes" if engine != "hermes" else "pi"
            places = {
                ".ssh": ssh,
                ".claude": self.route_home(".claude"),
                ".config/gh": self.route_home(".config", "gh"),
                "own opencode store": self.route_home(".local", "share", "opencode"),
                "homes/<engine>": self.route_home("homes", engine),
                "the homes folder itself": self.engine_home(engine).parent,
                "another engine's home": self.engine_home(other),
                "a folder below the engine's home": self.route_home("sub", base=self.engine_home(engine)),
            }
            for name, home in places.items():
                with self.subTest(engine=engine, home=name):
                    self.refused(lambda: self.launch(engine, home, allow_inside_operator_home=True),
                                 detail="not_the_engine_home")
        self.launcher.assert_not_called()

    def test_the_engine_home_follows_the_parents_state_folder(self) -> None:
        state = self.route_home("st")
        moved = self.engine_home("opencode", base=state)
        default = self.engine_home("opencode")
        for value, accepted, refused in ((str(state), moved, default), ("", default, moved), (5, default, moved)):
            parent = self.parent(XDG_STATE_HOME=value)
            with self.subTest(XDG_STATE_HOME=value):
                self.launch("opencode", accepted, parent=parent, allow_inside_operator_home=True)
                self.refused(lambda: self.launch("opencode", refused, parent=parent, allow_inside_operator_home=True),
                             detail="not_the_engine_home")
        self.assertEqual(self.launcher.call_count, 3)

    def test_a_state_folder_reached_through_a_link_is_the_same_folder(self) -> None:
        # The daemon may be given XDG_STATE_HOME through a link: the engine's home is then the same folder.
        state = self.route_home("st")
        link = self.root / "state-link"
        link.symlink_to(state)
        self.engine_home("opencode", base=state)
        self.launch("opencode", link / "recursion" / "homes" / "opencode",
                    parent=self.parent(XDG_STATE_HOME=str(link)), allow_inside_operator_home=True)
        self.assertEqual(self.launcher.call_count, 1)

    def test_a_link_on_the_way_to_the_engine_home_refuses(self) -> None:
        ssh = self.route_home(".ssh")
        homes = self.engine_home("opencode").parent
        os.rmdir(homes / "opencode")
        (homes / "opencode").symlink_to(ssh)  # the engine's folder is a link to the operator's .ssh
        self.refused(lambda: self.launch("opencode", homes / "opencode", allow_inside_operator_home=True),
                     detail="not_the_engine_home")
        state = self.route_home("st")
        elsewhere = self.engine_home("pi", base=self.route_home("other"))
        (state / "recursion").symlink_to(elsewhere.parent.parent)  # st/recursion -> other/recursion
        self.refused(lambda: self.launch("pi", state / "recursion" / "homes" / "pi",
                                         parent=self.parent(XDG_STATE_HOME=str(state)),
                                         allow_inside_operator_home=True),
                     detail="not_the_engine_home")
        self.launcher.assert_not_called()

    def test_a_subscription_sign_in_takes_the_setting_too(self) -> None:
        home = self.engine_home("opencode")
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
        # DeepSeek r3 F1's sharpest case: the operator's own OpenCode login store as the route home.
        store = self.route_home(".local", "share", "opencode")
        own = {**common, "route_home": str(store)}
        self.refused(lambda: guard_then_spawn(self.launcher, argv, "opencode", profile, {**env, "HOME": str(store)},
                                              allow_inside_operator_home=True, **own), detail="not_the_engine_home")
        self.assertEqual(self.launcher.call_count, 1)

    def test_the_operators_home_itself_is_refused_whatever_the_setting(self) -> None:
        alias = self.root / "operator-alias"
        alias.symlink_to(self.operator)
        for engine in ENGINES_UNDER_TEST:
            for home in (self.operator, alias):
                for allow in (True, False):
                    with self.subTest(engine=engine, home=home.name, allow=allow):
                        self.refused(lambda: self.launch(engine, home, allow_inside_operator_home=allow),
                                     detail="operator_home")
        self.launcher.assert_not_called()

    def test_an_operator_home_that_is_also_an_engine_home_is_still_refused(self) -> None:
        # The parent's HOME is the folder the explicit operator home's launcher place would be: it is an
        # operator's home, so it refuses even with True.
        home = self.engine_home("opencode")
        self.refused(lambda: self.launch("opencode", home, parent=self.parent(HOME=str(home)),
                                         allow_inside_operator_home=True), detail="operator_home")
        self.launcher.assert_not_called()

    def test_opt_in_still_refuses_every_operator_home_source(self) -> None:
        # GPT-6.1 Sol's round-2 required test 2, as written in its evidence.
        parent_home = self.root / "parent-home"
        passwd_home = self.root / "passwd-home"
        parent_home.mkdir(mode=0o700)
        passwd_home.mkdir(mode=0o700)
        parent = self.parent(HOME=str(parent_home))
        with mock.patch("recursion_core.subscription_routes.policy.pwd.getpwuid",
                        return_value=SimpleNamespace(pw_dir=str(passwd_home))):
            for home in (self.operator, parent_home, passwd_home):
                with self.subTest(source=home.name):
                    with self.assertRaises(SubscriptionRouteRefused) as caught:
                        guard_then_spawn(self.launcher, ["opencode", "run", "anthropic/m"], "opencode", self.profile(),
                                         self.env(HOME=str(home)), route_home=str(home), parent_env=parent,
                                         operator_home=str(self.operator), allow_inside_operator_home=True)
                    self.assertEqual(caught.exception.cause, "route_home_is_operator_home")
                    self.launcher.assert_not_called()

    def test_a_route_home_whose_real_path_cannot_be_read_is_refused(self) -> None:
        # DeepSeek r3 F6: realpath raising left check_route as an OSError instead of a refusal.
        home = self.engine_home("opencode")
        real = os.path.realpath

        def failing(path, *args, **kwargs):
            if os.fspath(path) == str(home):
                raise OSError("synthetic realpath failure")
            return real(path, *args, **kwargs)

        with mock.patch("recursion_core.subscription_routes.policy.os.path.realpath", side_effect=failing):
            for allow in (True, False):
                with self.subTest(allow=allow):
                    self.refused(lambda: self.launch("opencode", home, allow_inside_operator_home=allow),
                                 cause="route_home_missing")
        self.launcher.assert_not_called()

    def test_anything_but_true_is_the_stricter_rule(self) -> None:
        home = self.engine_home("opencode")
        for value in (1, "true", "yes", None, [True], "inside_operator_home"):
            with self.subTest(value=value):
                self.refused(lambda: self.launch("opencode", home, allow_inside_operator_home=value),
                             detail="inside_operator_home")
        self.launcher.assert_not_called()

    def test_a_route_home_outside_the_operators_home_is_accepted_whatever_the_setting(self) -> None:
        for allow in (True, False):
            with self.subTest(allow=allow):
                self.launch("opencode", self.home, allow_inside_operator_home=allow)
        self.assertEqual(self.launcher.call_count, 2)

    def test_cursor_and_antigravity_take_the_setting_too(self) -> None:
        home = self.engine_home("cursor")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        common = {"route_home": str(home), "parent_env": self.parent(), "operator_home": str(self.operator)}
        check_cursor_route(env, wrapper=self.wrapper, allow_inside_operator_home=True, **common)
        guard_cursor_then_spawn(self.launcher, [self.wrapper.launcher_path], env, wrapper=self.wrapper,
                                allow_inside_operator_home=True, **common)
        self.assertEqual(self.launcher.call_count, 1)
        # Antigravity's own place, not Cursor's: True accepts only the engine's own home.
        self.refused(lambda: check_antigravity_route(env, version="1", binary_path="/x", launcher_path="/y",
                                                     pin=mock.Mock(), allow_inside_operator_home=True, **common),
                     detail="not_the_engine_home")
        ag_home = self.engine_home("antigravity")
        ag_env = {"PATH": "/usr/bin:/bin", "HOME": str(ag_home)}
        ag = {**common, "route_home": str(ag_home)}
        with mock.patch("recursion_core.subscription_routes.policy.verify_antigravity_pin",
                        side_effect=AssertionError("the pin is reached")):  # True: past the home, to the pin
            with self.assertRaisesRegex(AssertionError, "the pin is reached"):
                check_antigravity_route(ag_env, version="1", binary_path="/x", launcher_path="/y", pin=mock.Mock(),
                                        allow_inside_operator_home=True, **ag)
            with self.assertRaisesRegex(AssertionError, "the pin is reached"):
                guard_antigravity_then_spawn(self.launcher, ["/x"], ag_env, version="1", binary_path="/x",
                                             launcher_path="/y", pin=mock.Mock(), allow_inside_operator_home=True,
                                             **ag)
        for setting in ({}, {"allow_inside_operator_home": False}):
            with self.subTest(setting=setting):
                self.refused(lambda: check_cursor_route(env, wrapper=self.wrapper, **common, **setting))
                # The home is checked before Antigravity's pin, which is not reached here.
                pin = mock.Mock(name="pin")
                self.refused(lambda: check_antigravity_route(ag_env, version="1", binary_path="/x",
                                                             launcher_path="/y", pin=pin, **ag, **setting))
                self.refused(lambda: guard_antigravity_then_spawn(self.launcher, ["/x"], ag_env, version="1",
                                                                  binary_path="/x", launcher_path="/y", pin=pin,
                                                                  **ag, **setting))
        self.assertEqual(self.launcher.call_count, 1)

    def test_cursor_guard_keeps_default_and_explicit_false_strict(self) -> None:
        # GPT-6.1 Sol's round-2 required test 1, with the route home at Cursor's own place (the bound of F1).
        home = self.engine_home("cursor")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        common = {"route_home": str(home), "parent_env": self.parent(),
                  "operator_home": str(self.operator), "wrapper": self.wrapper}
        for setting in ({}, {"allow_inside_operator_home": False}):
            with self.subTest(setting=setting):
                with self.assertRaises(SubscriptionRouteRefused) as caught:
                    guard_cursor_then_spawn(self.launcher, [self.wrapper.launcher_path], env, **common, **setting)
                self.assertEqual(caught.exception.cause, "route_home_is_operator_home")
                self.launcher.assert_not_called()
        guard_cursor_then_spawn(self.launcher, [self.wrapper.launcher_path], env,
                                allow_inside_operator_home=True, **common)
        self.assertEqual(self.launcher.call_count, 1)

    def test_the_keyword_is_the_stricter_rule_by_default_on_every_entry(self) -> None:
        for function in (check_route, guard_then_spawn, check_cursor_route, guard_cursor_then_spawn,
                         check_antigravity_route, guard_antigravity_then_spawn):
            with self.subTest(function=function.__name__):
                parameter = inspect.signature(function).parameters["allow_inside_operator_home"]
                self.assertEqual((parameter.kind, parameter.default), (inspect.Parameter.KEYWORD_ONLY, False))

    def test_a_direct_profile_adapter_keeps_the_stricter_default(self) -> None:
        # GPT-6 Astra's round-2 R1 and DeepSeek r3 F2: the adapter built directly, without the keyword, refuses
        # the engine's own home inside the operator's home (it would launch with True).
        for engine in ENGINES_UNDER_TEST:
            with self.subTest(engine=engine):
                home = self.engine_home(engine)
                profile = self.profile(model=None, **({HERMES_ADOPT_EXTERNAL_LOGINS: False}
                                                      if engine == "hermes" else {}))
                wrapper = self.wrapper if engine in ("pi", "hermes") else None
                argv = [wrapper.launcher_path if wrapper else engine]
                adapter = ProfileRouteAdapter(
                    engine, self.launcher, lambda: 1.0, lambda *_: "synthetic", self.parent(),
                    argv=lambda req, argv=argv: argv, driver=lambda *args: {"code": None, "detail": "launched"},
                    operator_home=str(self.operator))  # deliberately omit allow_inside_operator_home
                req = {"request_id": "r1", "route": {"home": str(home), "profile": profile, "wrapper": wrapper,
                       "connection": {"kind": "api_key", "id": "fixture", "credential_env": "ANTHROPIC_API_KEY"}}}
                answer = adapter.run(req, 1000.0)
                self.assertEqual((answer["code"], answer["detail"]),
                                 ("subscription_route_refused", "route_home_is_operator_home"))
                self.launcher.assert_not_called()

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
