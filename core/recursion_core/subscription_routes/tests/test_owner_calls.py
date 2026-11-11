"""2026-11-09 owner calls on recursion #321: package links, pin shape, argv[0] binding, Hermes home reuse,
launch options, configured requirements, the full-access profile and the dropped Gemini CLI route.

Fakes only: a MagicMock launcher and temp trees. No engine binary is executed and no network is used.
"""

from __future__ import annotations

import errno
import importlib
import os
import shutil
import stat
import unittest
from pathlib import Path
from unittest import mock

import recursion_core.subscription_routes as routes
from recursion_core.subscription_routes import (
    FORWARDED_LAUNCH_OPTIONS,
    HERMES_ADOPT_EXTERNAL_LOGINS,
    PackagePin,
    RouteRequirements,
    SubscriptionRouteRefused,
    WrapperInstall,
    WrapperPinRefused,
    check_antigravity_route,
    check_cursor_route,
    check_route,
    guard_antigravity_then_spawn,
    guard_cursor_then_spawn,
    guard_then_spawn,
    measure_antigravity_pin,
    measure_package_pin,
    policy,
)
from recursion_core.subscription_routes.tests.test_subscription_routes import FAKE_KEY, FAKE_OAUTH, _Base

ADOPT_YAML = b"auth:\n  adopt_external_logins: false\n"
GUARDS = ("pi", "hermes", "cursor", "antigravity")


class _Calls(_Base):
    def setUp(self) -> None:
        super().setUp()
        # Real paths, so a symlinked temp dir (macOS /tmp) does not blur inside and outside.
        self.root = Path(os.path.realpath(self.root))
        self.home, self.operator, self.install = self.root / "route-home", self.root / "operator", self.root / "pkg"
        self.launcher_path = str(self.install / "launcher.js")
        self.wrapper = self.pin()
        self.outside = self.root / "store"
        self.outside.mkdir()
        self.agy = self.root / "agy-install"
        self.agy.mkdir()
        (self.agy / "agy").write_text("agy-binary\n", encoding="ascii")
        (self.agy / "agy-launch").write_text("agy-launch\n", encoding="ascii")
        self.agy_pin = measure_antigravity_pin("1.2.3", str(self.agy / "agy"), str(self.agy / "agy-launch"))

    def pin(self, **inputs: object) -> WrapperInstall:
        return WrapperInstall(self.launcher_path, str(self.install),
                              measure_package_pin(self.launcher_path, str(self.install), **inputs))

    def bare(self) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}

    def cursor(self, **inputs: object):
        return check_cursor_route(self.bare(), route_home=str(self.home), wrapper=self.wrapper,
                                  parent_env=self.parent(), operator_home=str(self.operator), **inputs)

    def guards(self):
        """Each guarded engine in turn, in a route home with no Hermes state left by the one before."""

        for engine in GUARDS:
            shutil.rmtree(self.home / ".hermes", ignore_errors=True)
            yield engine

    def pinned_argv0(self, engine: str) -> str:
        return str(self.agy / "agy") if engine == "antigravity" else self.launcher_path

    def launch(self, engine: str, argv: list[str] | None = None, **inputs: object):
        """One guarded launch through the guard that owns ``engine``. API-key profile for Pi and Hermes."""

        argv = argv if argv is not None else [self.pinned_argv0(engine), "run", "anthropic/m"]
        common = {"route_home": str(self.home), "parent_env": inputs.pop("parent_env", self.parent()),
                  "operator_home": str(self.operator)}
        if engine == "cursor":
            return guard_cursor_then_spawn(self.launcher, argv, self.bare(), wrapper=self.wrapper, **common, **inputs)
        if engine == "antigravity":
            return guard_antigravity_then_spawn(
                self.launcher, argv, self.bare(), version="1.2.3", binary_path=str(self.agy / "agy"),
                launcher_path=str(self.agy / "agy-launch"), pin=self.agy_pin, **common, **inputs)
        profile = inputs.pop("use_profile", None) or self.profile()
        if engine == "hermes":
            profile = {**profile, HERMES_ADOPT_EXTERNAL_LOGINS: False}
        env = inputs.pop("use_env", None) or self.env()
        return guard_then_spawn(self.launcher, argv, engine, profile, env, wrapper=self.wrapper, **common, **inputs)

    def refused(self, cause: str, call, *args: object, **kwargs: object) -> SubscriptionRouteRefused:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            call(*args, **kwargs)
        self.assertEqual(cause, caught.exception.cause)
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        return caught.exception

    def pin_refused(self, cause: str, *args: object, **kwargs: object) -> None:
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_package_pin(*args, **kwargs)
        self.assertEqual(cause, caught.exception.cause)


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class SurrogateHomeTests(_Calls):
    def test_a_surrogate_home_is_refused_before_the_filesystem(self) -> None:
        homes = ["/tmp/review-home-\ud800"]
        root = os.fsencode(self.root)
        raw = os.path.join(root, b"\xff")
        os.mkdir(raw, 0o700)
        existing = os.fsdecode(raw)
        self.assertIn("\udcff", existing)
        homes.append(existing)
        for home in homes:
            env = {"PATH": "/usr/bin:/bin", "HOME": home, "OPENAI_API_KEY": "fake"}
            profile = {"provider": "openai", "auth": "api_key", "credential_env": "OPENAI_API_KEY"}
            self.refused(
                "home_unsafe", check_route, "opencode", profile, env, route_home=home,
                parent_env=self.parent(), operator_home=str(self.operator),
            )
            bare = {"PATH": "/usr/bin:/bin", "HOME": home}
            self.refused(
                "home_unsafe", check_cursor_route, bare, route_home=home, wrapper=self.wrapper,
                parent_env=self.parent(), operator_home=str(self.operator),
            )
            self.refused(
                "home_unsafe", check_antigravity_route, bare, version="1.2.3",
                binary_path=str(self.agy / "agy"), launcher_path=str(self.agy / "agy-launch"),
                pin=self.agy_pin, route_home=home, parent_env=self.parent(),
                operator_home=str(self.operator),
            )

    def test_a_passwd_surrogate_and_an_owned_hermes_home_are_refused_before_realpath(self) -> None:
        bad = "/tmp/\udcff"
        with mock.patch.object(policy.pwd, "getpwuid", return_value=mock.Mock(pw_dir=bad)), \
                mock.patch.object(policy.os.path, "realpath", side_effect=lambda path: path) as resolve:
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                policy._check_home("opencode", {"HOME": "/missing-route"}, "/missing-route", {}, None)
        self.assertEqual("home_unsafe", caught.exception.cause)
        self.assertFalse(any(call.args[0] == bad for call in resolve.call_args_list))
        checked = policy._checked_requirements(
            "hermes", RouteRequirements(owned_hermes_home="/tmp/\ud800"), False,
        )
        with mock.patch.object(policy.os.path, "realpath", side_effect=AssertionError("realpath")):
            with self.assertRaises(SubscriptionRouteRefused) as owned:
                policy._check_hermes_home("hermes", "/tmp/route", checked)
        self.assertEqual("requirements_malformed", owned.exception.cause)
        self.assertEqual("owned_hermes_home", owned.exception.detail)

    def test_an_unlistable_own_store_is_home_unsafe(self) -> None:
        with mock.patch.object(policy.os.path, "lexists", return_value=True), \
                mock.patch.object(policy.os.path, "islink", return_value=False), \
                mock.patch.object(policy.os.path, "isdir", return_value=True), \
                mock.patch.object(policy.os.path, "realpath", side_effect=lambda path: path), \
                mock.patch.object(policy.os, "scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                policy._check_own_store("opencode", "/route", "/route", ".local/share/opencode")
        self.assertEqual("home_unsafe", caught.exception.cause)


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class PackageLinkTests(_Calls):
    """Call 1: a link is followed and what it resolves to is hashed; a link out of the root refuses."""

    def test_in_root_directory_link_is_walked_and_a_change_behind_it_breaks_the_pin(self) -> None:
        (self.install / "store" / "dep").mkdir(parents=True)
        (self.install / "store" / "dep" / "index.js").write_text("dep-v1\n", encoding="ascii")
        (self.install / "node_modules").mkdir()
        os.symlink("../store/dep", self.install / "node_modules" / "dep")
        self.wrapper = self.pin()
        rows = dict(self.wrapper.pin.package_files)
        self.assertIn("node_modules/dep", rows)  # the link itself
        self.assertIn("node_modules/dep/index.js", rows)  # the file behind it
        self.assertEqual(rows["node_modules/dep/index.js"], rows["store/dep/index.js"])
        self.assertTrue(self.cursor().beta)
        (self.install / "store" / "dep" / "index.js").write_text("dep-v2\n", encoding="ascii")
        self.refused("wrapper_pin_mismatch", self.cursor)

    def test_retargeting_an_in_root_link_to_identical_content_breaks_the_pin(self) -> None:
        for name in ("a", "b"):
            (self.install / name).mkdir()
            (self.install / name / "index.js").write_text("same\n", encoding="ascii")
        (self.install / "a.js").write_text("same\n", encoding="ascii")
        (self.install / "b.js").write_text("same\n", encoding="ascii")
        os.symlink("a", self.install / "current")
        os.symlink("a.js", self.install / "current.js")
        self.wrapper = self.pin()
        self.assertTrue(self.cursor().beta)
        os.unlink(self.install / "current")
        os.symlink("b", self.install / "current")
        self.assertEqual("current", self.refused("wrapper_pin_mismatch", self.cursor).detail)
        os.unlink(self.install / "current")
        os.symlink("a", self.install / "current")
        self.assertTrue(self.cursor().beta)
        os.unlink(self.install / "current.js")
        os.symlink("b.js", self.install / "current.js")
        self.assertEqual("current.js", self.refused("wrapper_pin_mismatch", self.cursor).detail)

    def test_file_link_is_hashed_by_its_target_bytes(self) -> None:
        (self.install / "real.js").write_text("v1\n", encoding="ascii")
        os.symlink("real.js", self.install / "alias.js")
        before = dict(measure_package_pin(self.launcher_path, str(self.install)).package_files)
        (self.install / "real.js").write_text("v2\n", encoding="ascii")
        after = dict(measure_package_pin(self.launcher_path, str(self.install)).package_files)
        self.assertNotEqual(before["alias.js"], after["alias.js"])

    def test_link_out_of_the_root_refuses_by_default(self) -> None:
        (self.outside / "dep").mkdir()
        (self.outside / "dep" / "index.js").write_text("dep\n", encoding="ascii")
        for target in (self.outside / "dep", self.outside / "dep" / "index.js"):
            with self.subTest(target=target.name):
                link = self.install / "linked"
                os.symlink(target, link)
                self.pin_refused("package_link_outside_root", self.launcher_path, str(self.install))
                # The pin was measured before the link appeared: verifying refuses with the same code.
                self.assertEqual("linked", self.refused("package_link_outside_root", self.cursor).detail)
                self.refused("package_link_outside_root", self.launch, "pi")
                os.unlink(link)
        self.launcher.assert_not_called()

    def test_pnpm_style_store_link_is_followed_only_when_the_caller_allows_the_target(self) -> None:
        # Samir, review 5835212494: node_modules -> a store dir kept the pin while the store changed.
        (self.outside / "dep").mkdir()
        (self.outside / "dep" / "index.js").write_text("dep-v1\n", encoding="ascii")
        os.symlink(self.outside, self.install / "node_modules")
        allow = RouteRequirements(allowed_link_targets=(str(self.outside),))
        self.wrapper = self.pin(allowed_link_targets=allow.allowed_link_targets)
        self.assertIn("node_modules/dep/index.js", dict(self.wrapper.pin.package_files))
        self.assertTrue(self.cursor(requirements=allow).beta)
        self.assertTrue(self.cursor(requirements=RouteRequirements(allowed_link_targets=frozenset({str(self.outside)}))).beta)
        self.refused("package_link_outside_root", self.cursor)  # the same pin, without the input
        other = RouteRequirements(allowed_link_targets=(str(self.root / "elsewhere"),))
        self.refused("package_link_outside_root", self.cursor, requirements=other)
        (self.outside / "dep" / "index.js").write_text("dep-v2\n", encoding="ascii")
        self.assertEqual("node_modules/dep/index.js",
                         self.refused("wrapper_pin_mismatch", self.cursor, requirements=allow).detail)

    def test_full_access_follows_a_link_out_of_the_root_and_still_hashes_it(self) -> None:
        (self.outside / "index.js").write_text("dep-v1\n", encoding="ascii")
        os.symlink(self.outside, self.install / "node_modules")
        self.wrapper = self.pin(full_access_acknowledged=True)
        decision = self.cursor(full_access_acknowledged=True)
        self.assertIn("full_access_profile_acknowledged", decision.flags)
        self.refused("package_link_outside_root", self.cursor)
        (self.outside / "index.js").write_text("dep-v2\n", encoding="ascii")
        self.refused("wrapper_pin_mismatch", self.cursor, full_access_acknowledged=True)

    def test_a_link_inside_an_allowed_tree_may_not_leave_it(self) -> None:
        (self.root / "secret").mkdir()
        (self.root / "secret" / "x").write_text("x\n", encoding="ascii")
        os.symlink(self.root / "secret", self.outside / "escape")
        os.symlink(self.outside, self.install / "node_modules")
        self.pin_refused("package_link_outside_root", self.launcher_path, str(self.install),
                         allowed_link_targets=(str(self.outside),))

    def test_dangling_link_and_link_loop_refuse(self) -> None:
        os.symlink("missing.js", self.install / "gone.js")
        self.pin_refused("package_link_dangling", self.launcher_path, str(self.install))
        self.refused("package_link_dangling", self.cursor)
        self.pin_refused("package_link_dangling", self.launcher_path, str(self.install), full_access_acknowledged=True)
        os.unlink(self.install / "gone.js")
        os.symlink("loop-b", self.install / "loop-a")
        os.symlink("loop-a", self.install / "loop-b")
        self.pin_refused("package_link_dangling", self.launcher_path, str(self.install))

    def test_directory_link_cycle_ends_and_is_recorded(self) -> None:
        (self.install / "sub").mkdir()
        (self.install / "sub" / "f.js").write_text("f\n", encoding="ascii")
        os.symlink("..", self.install / "sub" / "up")  # a workspace self-link
        os.symlink(".", self.install / "self")
        rows = dict(measure_package_pin(self.launcher_path, str(self.install)).package_files)
        self.assertIn("sub/up", rows)
        self.assertIn("self", rows)
        self.assertFalse([path for path in rows if path.startswith(("sub/up/", "self/"))])

    def test_link_requirements_are_validated_fail_closed(self) -> None:
        for bad in ([str(self.outside)], str(self.outside), ("relative/dir",), ("/",), (b"/x",), (str(self.outside) + "/",),
                    ("/a/../b",), None):
            with self.subTest(bad=bad):
                self.pin_refused("requirements_malformed", self.launcher_path, str(self.install), allowed_link_targets=bad)
                self.refused("requirements_malformed", self.cursor,
                             requirements=RouteRequirements(allowed_link_targets=bad))


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class PinShapeTests(_Calls):
    """Call 2: a non-empty manifest, an absolute install root and a launcher under that root."""

    def test_empty_install_dir_is_not_a_pin(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        (empty / "only-dirs").mkdir()
        self.pin_refused("package_manifest_empty", self.launcher_path, str(empty))
        # A hand-built empty pin refuses too, before anything is measured.
        self.wrapper = WrapperInstall(self.launcher_path, str(empty), PackagePin(self.wrapper.pin.launcher_sha256, ()))
        self.refused("package_manifest_empty", self.cursor)
        self.refused("package_manifest_empty", self.launch, "pi")

    def test_relative_install_root_refuses(self) -> None:
        relative = os.path.relpath(self.install)
        self.pin_refused("install_root_not_absolute", self.launcher_path, relative)
        self.pin_refused("install_root_not_absolute", self.launcher_path, "")
        self.wrapper = WrapperInstall(self.launcher_path, relative, self.wrapper.pin)
        self.refused("install_root_not_absolute", self.cursor)

    def test_launcher_outside_the_root_refuses(self) -> None:
        stray = self.outside / "launcher.js"
        stray.write_text("launcher-v1\n", encoding="ascii")  # same bytes as the real one
        self.pin_refused("launcher_outside_root", str(stray), str(self.install))
        self.wrapper = WrapperInstall(str(stray), str(self.install), self.wrapper.pin)
        self.refused("launcher_outside_root", self.cursor)
        self.refused("launcher_outside_root", self.cursor, full_access_acknowledged=True)

    def test_launcher_link_that_resolves_into_the_root_is_hashed_by_its_target(self) -> None:
        shim = self.outside / "cursor"
        os.symlink(self.launcher_path, shim)
        direct = measure_package_pin(self.launcher_path, str(self.install))
        self.assertEqual(direct, measure_package_pin(str(shim), str(self.install)))
        os.unlink(shim)
        os.symlink(self.outside / "nowhere", shim)
        self.pin_refused("wrapper_launcher_missing", str(shim), str(self.install))


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class Argv0Tests(_Calls):
    """Call 3: argv[0] is the pinned path, not a PATH name and not another binary."""

    def test_path_name_and_other_binary_refuse_in_every_guard(self) -> None:
        other = self.outside / "other"
        other.write_text("launcher-v1\n", encoding="ascii")
        for engine in self.guards():
            for argv0 in (engine, "cursor", "agy", "./launcher.js", "launcher.js", str(other), str(self.install / "lib.js"), ""):
                with self.subTest(engine=engine, argv0=argv0):
                    self.refused("argv0_not_pinned", self.launch, engine, [argv0, "run", "anthropic/m"])
        self.launcher.assert_not_called()

    def test_relative_name_refuses_even_when_it_would_resolve_from_the_current_directory(self) -> None:
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.install)
        self.refused("argv0_not_pinned", self.launch, "cursor", ["launcher.js"])

    def test_pinned_path_launches_in_every_guard(self) -> None:
        for engine in self.guards():
            with self.subTest(engine=engine):
                self.launcher.reset_mock()
                decision, _ = self.launch(engine)
                self.assertTrue(decision.beta)
                self.assertEqual(self.pinned_argv0(engine), self.launcher.call_args.args[0][0])

    def test_antigravity_accepts_the_pinned_binary_or_the_pinned_launcher_only(self) -> None:
        self.launch("antigravity", [str(self.agy / "agy-launch"), "run"])
        self.assertEqual(1, self.launcher.call_count)
        (self.agy / "sibling").write_text("agy-binary\n", encoding="ascii")
        self.refused("argv0_not_pinned", self.launch, "antigravity", [str(self.agy / "sibling")])

    def test_a_link_to_the_pinned_path_resolves_to_it(self) -> None:
        shim = self.outside / "cursor"
        os.symlink(self.launcher_path, shim)
        self.launch("cursor", [str(shim)])
        self.assertEqual([str(shim)], self.launcher.call_args.args[0])  # argv is passed on unchanged
        os.unlink(shim)
        os.symlink(self.install / "lib.js", shim)
        self.refused("argv0_not_pinned", self.launch, "cursor", [str(shim)])

    def test_opencode_has_no_pin_and_no_argv0_binding(self) -> None:
        decision, _ = guard_then_spawn(self.launcher, ["opencode", "run"], "opencode", self.profile(), self.env(),
                                       route_home=str(self.home), parent_env=self.parent(),
                                       operator_home=str(self.operator))
        self.assertFalse(decision.beta)


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class HermesHomeReuseTests(_Calls):
    """Call 4: a second launch in the same route home works; any other config.yaml refuses."""

    def sub(self) -> dict:
        return {"provider": "grok", "auth": "subscription", "method": "grok_sign_in", "model": "grok/test-model"}

    def launch_sub(self, **inputs: object):
        return self.launch("hermes", [self.launcher_path, "run", "--model", "grok/test-model"],
                           use_profile=self.sub(), use_env=self.bare(), **inputs)

    def config(self) -> Path:
        return self.home / ".hermes" / "config.yaml"

    def test_consecutive_launches_in_one_home_both_succeed(self) -> None:
        for kind, launch in (("api_key", lambda: self.launch("hermes")), ("subscription", self.launch_sub)):
            with self.subTest(kind=kind):
                self.launcher.reset_mock()
                first, _ = launch()
                self.assertEqual(ADOPT_YAML, self.config().read_bytes())
                second, _ = launch()
                third, _ = launch()
                self.assertEqual(3, self.launcher.call_count)
                self.assertEqual(ADOPT_YAML, self.config().read_bytes())
                self.assertEqual(first, second)
                self.assertEqual(second, third)
                # Nothing but Recursion's own switch is there, so nothing is flagged as uninspected.
                self.assertFalse([flag for flag in second.flags if flag.startswith("own_login_store")])

    def test_api_key_then_sign_in_share_the_home(self) -> None:
        self.launch("hermes")
        self.launch_sub()
        self.launch("hermes")
        self.assertEqual(3, self.launcher.call_count)

    def test_tampered_config_refuses(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        same_size = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(same_size))
        for content in (same_size, ADOPT_YAML + b"model: x\n", b"", ADOPT_YAML[:-1]):
            with self.subTest(content=content):
                self.config().write_bytes(content)
                self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
                self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
                self.refused("hermes_config_blocks_adopt_override", check_route, "hermes",
                             {**self.sub(), HERMES_ADOPT_EXTERNAL_LOGINS: False}, self.bare(),
                             route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
                             wrapper=self.wrapper)
        self.launcher.assert_not_called()

    def test_config_that_is_a_link_or_not_a_regular_file_refuses(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        ours = self.home / "ours.yaml"  # inside the route home, so only the config check can refuse it
        ours.write_bytes(ADOPT_YAML)
        self.config().unlink()
        os.symlink(ours, self.config())  # even a link to the exact bytes
        self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.config().unlink()
        self.config().mkdir()
        self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.config().rmdir()
        os.mkfifo(self.config())
        self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.launcher.assert_not_called()

    def test_api_key_route_still_refuses_anything_else_in_the_hermes_dir(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        (self.home / ".hermes" / "auth.json").write_text(FAKE_OAUTH, encoding="ascii")
        self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.config().unlink()  # other state and no switch
        self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.launcher.assert_not_called()

    def stray(self) -> list[str]:
        return sorted(name for name in os.listdir(self.home) if name != ".hermes")

    def assert_switch_written(self) -> None:
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(37, len(ADOPT_YAML))
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.home / ".hermes").st_mode))
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(self.config()).st_mode))
        self.assertEqual(["config.yaml"], os.listdir(self.home / ".hermes"))
        self.assertEqual([], self.stray())  # no staging directory or temp file left behind

    def test_absent_hermes_dir_gets_a_staged_write_of_exactly_the_switch(self) -> None:
        real_rename = os.rename
        with mock.patch.object(policy.os, "rename", side_effect=real_rename) as rename:
            self.launch("hermes")
        (staging, dest), _ = rename.call_args
        self.assertEqual(str(self.home / ".hermes"), dest)
        self.assertEqual(str(self.home), os.path.dirname(staging))
        self.assertTrue(os.path.basename(staging).startswith(".hermes.recursion-"))
        self.assert_switch_written()
        self.assertEqual(1, self.launcher.call_count)

    def test_empty_hermes_dir_counts_as_absent_on_an_api_key_hermes_route(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o755)
        self.launch("hermes")
        self.assert_switch_written()  # the staged directory replaced the empty one, 0700
        self.assertEqual(1, self.launcher.call_count)

    def test_empty_hermes_dir_still_refuses_on_other_engines(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o700)
        for engine in ("pi", "cursor", "antigravity"):
            with self.subTest(engine=engine):
                self.refused("engine_store_in_route_home", self.launch, engine)
        self.refused("engine_store_in_route_home", guard_then_spawn, self.launcher, ["opencode", "run"], "opencode",
                     self.profile(), self.env(), route_home=str(self.home), parent_env=self.parent(),
                     operator_home=str(self.operator))
        self.assertEqual([], os.listdir(self.home / ".hermes"))
        self.launcher.assert_not_called()

    def test_hermes_entry_that_is_a_link_to_an_empty_dir_or_an_empty_file_is_not_absent(self) -> None:
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        os.symlink(elsewhere, self.home / ".hermes")
        self.refused("engine_store_in_route_home", self.launch, "hermes")
        os.unlink(self.home / ".hermes")
        (self.home / ".hermes").write_bytes(b"")
        self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.assertEqual([], os.listdir(elsewhere))
        self.launcher.assert_not_called()

    def test_rename_that_finds_the_hermes_dir_filled_refuses(self) -> None:
        for code in (errno.EEXIST, errno.ENOTEMPTY):
            for start in ("absent", "empty"):
                with self.subTest(code=errno.errorcode[code], start=start):
                    shutil.rmtree(self.home / ".hermes", ignore_errors=True)
                    if start == "empty":
                        (self.home / ".hermes").mkdir(mode=0o700)
                    with mock.patch.object(policy.os, "rename", side_effect=OSError(code, os.strerror(code))) as rename:
                        exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
                    self.assertEqual(".hermes", exc.detail)
                    self.assertEqual(1, rename.call_count)
                    self.assertEqual([], self.stray())  # the staging directory is removed
                    self.assertFalse(self.config().exists())
        self.launcher.assert_not_called()

    def test_hermes_dir_with_entries_and_no_config_gets_the_switch_by_link(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        with mock.patch.object(policy.os, "rename") as rename, mock.patch.object(policy.os, "replace") as replace:
            self.launch_sub()
        rename.assert_not_called()
        replace.assert_not_called()
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(self.config()).st_mode))
        self.assertEqual(1, os.lstat(self.config()).st_nlink)  # the temp name is gone
        self.assertEqual(b"hermes-state", (self.home / ".hermes" / "state.db").read_bytes())
        self.assertEqual([], self.stray())
        self.launch_sub()  # and the next launch reuses it
        self.assertEqual(2, self.launcher.call_count)

    def test_link_that_installs_a_symlink_does_not_launch(self) -> None:
        # The new hard-link path must not leave a symlink at config.yaml and then launch.
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")

        def plant(src: object, dst: object, **kwargs: object) -> None:
            os.symlink(src, dst, dir_fd=kwargs.get("dst_dir_fd"))

        with mock.patch.object(policy.os, "link", side_effect=plant):
            self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.assertFalse(self.config().exists())
        self.assertEqual(b"hermes-state", (self.home / ".hermes" / "state.db").read_bytes())
        self.assertEqual([], self.stray())
        self.launcher.assert_not_called()

    def test_link_failure_refuses_and_leaves_no_temp_file(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        for failing in ("link", "unlink"):
            with self.subTest(failing=failing):
                real = getattr(os, failing)
                calls = []

                def fail(*args: object, **kwargs: object) -> None:
                    calls.append(args)
                    if failing == "unlink":
                        real(*args, **kwargs)
                    raise OSError(errno.EEXIST, "File exists")

                with mock.patch.object(policy.os, failing, side_effect=fail):
                    exc = self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
                self.assertEqual("config.yaml", exc.detail)
                self.assertEqual(1, len(calls))
                self.assertEqual([], self.stray())
                self.config().unlink(missing_ok=True)
        self.launcher.assert_not_called()

    def test_config_that_appears_before_the_link_is_not_replaced(self) -> None:
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        real_link = os.link

        def racing_link(*args: object, **kwargs: object) -> None:
            self.config().write_bytes(b"model: theirs\n")  # written between the check and the link
            real_link(*args, **kwargs)

        with mock.patch.object(policy.os, "link", side_effect=racing_link):
            self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.assertEqual(b"model: theirs\n", self.config().read_bytes())
        self.assertEqual([], self.stray())
        self.launcher.assert_not_called()

    def test_zero_length_or_short_config_is_not_adopted(self) -> None:
        for content in (b"", b"auth:\n", ADOPT_YAML[:-1]):
            for others in (False, True):
                with self.subTest(content=content, others=others):
                    shutil.rmtree(self.home / ".hermes", ignore_errors=True)
                    (self.home / ".hermes").mkdir(mode=0o700)
                    self.config().write_bytes(content)
                    if others:
                        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
                    self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
                    if not others:
                        self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
                    self.assertEqual(content, self.config().read_bytes())
                    self.assertEqual([], self.stray())
        self.launcher.assert_not_called()

    def test_config_read_is_relative_to_the_real_hermes_dir_and_rechecks_the_owner(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        real_open, real_fstat, real_close = os.open, os.fstat, os.close
        opens: list[tuple[object, int, object]] = []
        closed: list[int] = []

        def spy_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            opens.append((path, flags, kwargs.get("dir_fd")))
            opens.append(("fd", fd, None))
            return fd

        def spy_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        with mock.patch.object(policy.os, "open", side_effect=spy_open), \
                mock.patch.object(policy.os, "close", side_effect=spy_close):
            self.assertTrue(policy._hermes_config_is_ours("hermes", str(self.home)))
        (dir_path, dir_flags, _), (_, dir_fd, _), (name, flags, used_dir_fd), (_, fd, _) = opens
        self.assertEqual(str(self.home / ".hermes"), dir_path)
        self.assertEqual(os.O_DIRECTORY | os.O_NOFOLLOW, dir_flags & (os.O_DIRECTORY | os.O_NOFOLLOW))
        self.assertEqual(("config.yaml", dir_fd), (name, used_dir_fd))
        self.assertEqual(os.O_NOCTTY | os.O_NOFOLLOW, flags & (os.O_NOCTTY | os.O_NOFOLLOW))
        self.assertEqual(sorted([dir_fd, fd]), sorted(closed))

        def other_owner(fd: int) -> os.stat_result:
            st = real_fstat(fd)
            return os.stat_result((*st[:4], st.st_uid + 1, *st[5:]))

        closed.clear()
        with mock.patch.object(policy.os, "fstat", side_effect=other_owner), \
                mock.patch.object(policy.os, "close", side_effect=spy_close):
            self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual(2, len(closed))
        self.launcher.assert_not_called()

    def test_other_engines_still_refuse_a_hermes_dir_that_holds_the_switch(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        self.refused("engine_store_in_route_home", self.launch, "pi")
        self.launcher.assert_not_called()

    def test_owned_hermes_home_must_be_the_route_home(self) -> None:
        owned = RouteRequirements(owned_hermes_home=str(self.home))
        self.launch("hermes", requirements=owned)
        self.launch("hermes", requirements=owned)
        self.assertEqual(2, self.launcher.call_count)
        self.launcher.reset_mock()
        elsewhere = self.root / "their-home"
        elsewhere.mkdir(mode=0o700)
        self.refused("hermes_home_not_owned", self.launch, "hermes",
                     requirements=RouteRequirements(owned_hermes_home=str(elsewhere)))
        self.assertFalse((elsewhere / ".hermes").exists())
        for bad in ("relative/home", b"/x", 7):
            self.refused("requirements_malformed", self.launch, "hermes",
                         requirements=RouteRequirements(owned_hermes_home=bad))
        self.launcher.assert_not_called()

    def test_owned_hermes_home_that_is_an_operator_home_still_refuses(self) -> None:
        # owned_hermes_home is a check, not a grant: naming the operator's home does not make it a route home.
        operator = self.operator / "hermes-home"
        operator.mkdir(mode=0o700)
        argv = [self.launcher_path, "run", "anthropic/m"]
        profile = {**self.profile(), HERMES_ADOPT_EXTERNAL_LOGINS: False}
        for home in (self.operator, operator):  # the operator's home itself, and a directory inside it
            with self.subTest(home=home.name):
                self.refused("route_home_is_operator_home", guard_then_spawn, self.launcher, argv, "hermes", profile,
                             {**self.env(), "HOME": str(home)}, route_home=str(home), parent_env=self.parent(),
                             operator_home=str(self.operator), wrapper=self.wrapper,
                             requirements=RouteRequirements(owned_hermes_home=str(home)))
                self.assertFalse((home / ".hermes").exists())
        self.launcher.assert_not_called()


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class LaunchOptionForwardTests(_Calls):
    """Call 5: allowed options reach the launcher; forbidden and unknown ones refuse."""

    def test_cwd_reaches_the_launcher_in_every_guard(self) -> None:
        self.assertEqual(frozenset({"cwd"}), FORWARDED_LAUNCH_OPTIONS)
        for engine in self.guards():
            with self.subTest(engine=engine):
                self.launcher.reset_mock()
                self.launch(engine, cwd=str(self.root))
                kwargs = self.launcher.call_args.kwargs
                self.assertEqual(str(self.root), kwargs["cwd"])
                self.assertIs(kwargs["shell"], False)
                self.assertEqual(str(self.home), kwargs["env"]["HOME"])
                self.launcher.reset_mock()
                self.launch(engine)
                self.assertNotIn("cwd", self.launcher.call_args.kwargs)

    def test_forbidden_options_refuse_in_every_guard_and_full_access_does_not_change_that(self) -> None:
        for engine in self.guards():
            for key, value in (("env", {"ANTHROPIC_OAUTH_TOKEN": FAKE_OAUTH}), ("shell", True), ("executable", "/bin/sh"),
                               ("preexec_fn", print), ("pass_fds", (0,)), ("user", 0), ("cwd_fd", 3),
                               ("model", "anthropic/other"), ("engine_config", ())):
                for full in (False, True):
                    with self.subTest(engine=engine, key=key, full=full):
                        exc = self.refused("launch_option_denied", self.launch, engine,
                                           full_access_acknowledged=full, **{key: value})
                        self.assertEqual(key, exc.detail)
                        self.assertNotIn(FAKE_OAUTH, str(exc))
        self.launcher.assert_not_called()

    def test_unknown_option_refuses_instead_of_being_dropped(self) -> None:
        for engine in self.guards():
            with self.subTest(engine=engine):
                exc = self.refused("launch_option_unknown", self.launch, engine, start_new_session=True, timeout=5)
                self.assertEqual("start_new_session,timeout", exc.detail)
        self.launcher.assert_not_called()

    def test_configured_extra_option_is_forwarded(self) -> None:
        for extra in (("start_new_session",), frozenset({"start_new_session"})):
            for engine in self.guards():
                with self.subTest(engine=engine, extra=extra):
                    self.launcher.reset_mock()
                    self.launch(engine, requirements=RouteRequirements(extra_launch_options=extra),
                                start_new_session=True, cwd=str(self.root))
                    kwargs = self.launcher.call_args.kwargs
                    self.assertIs(kwargs["start_new_session"], True)
                    self.assertEqual(str(self.root), kwargs["cwd"])
                    # Only the named option: another unknown one still refuses.
                    self.refused("launch_option_unknown", self.launch, engine,
                                 requirements=RouteRequirements(extra_launch_options=extra), umask=0)

    def test_caller_cannot_configure_a_forbidden_option(self) -> None:
        for engine in self.guards():
            for name in ("env", "shell", "executable", "preexec_fn", "close_fds", "model", "engine_config"):
                for full in (False, True):
                    with self.subTest(engine=engine, name=name, full=full):
                        requirements = RouteRequirements(extra_launch_options=("start_new_session", name))
                        exc = self.refused("requirements_forbidden_launch_option", self.launch, engine,
                                           requirements=requirements, full_access_acknowledged=full)
                        self.assertEqual(name, exc.detail)
        self.launcher.assert_not_called()

    def test_requirements_are_validated_fail_closed(self) -> None:
        for bad in (["cwd"], "cwd", (1,), ("not an identifier",), ("",), None):
            with self.subTest(bad=bad):
                self.refused("requirements_malformed", self.launch, "cursor",
                             requirements=RouteRequirements(extra_launch_options=bad))
        for bad in ({"extra_launch_options": ("umask",)}, ("umask",), "umask", True):
            with self.subTest(bad=bad):
                self.refused("requirements_malformed", self.launch, "cursor", requirements=bad)
                self.refused("requirements_malformed", self.cursor, requirements=bad)
        self.launcher.assert_not_called()


@unittest.skipIf(os.name == "nt", "POSIX links and home checks")
class FullAccessTests(_Calls):
    """Call C: the named full-access profile is an explicit bool the caller passes for this run."""

    def test_only_the_bool_true_counts_and_a_non_bool_refuses(self) -> None:
        for value in ("true", "True", 1, 0, None, "yes", [True], 1.0):
            with self.subTest(value=value):
                for engine in self.guards():
                    self.refused("route_full_access_malformed", self.launch, engine, full_access_acknowledged=value)
                self.refused("route_full_access_malformed", self.cursor, full_access_acknowledged=value)
                self.refused("route_full_access_malformed", check_antigravity_route, self.bare(), version="1.2.3",
                             binary_path=str(self.agy / "agy"), launcher_path=str(self.agy / "agy-launch"),
                             pin=self.agy_pin, route_home=str(self.home), full_access_acknowledged=value)
                self.refused("route_full_access_malformed", check_route, "opencode", self.profile(), self.env(),
                             route_home=str(self.home), full_access_acknowledged=value)
                self.pin_refused("route_full_access_malformed", self.launcher_path, str(self.install),
                                 full_access_acknowledged=value)
        self.launcher.assert_not_called()

    def test_full_access_refuses_an_unknown_option_and_is_recorded_on_the_decision(self) -> None:
        for engine in self.guards():
            with self.subTest(engine=engine):
                self.launcher.reset_mock()
                exc = self.refused("launch_option_unknown", self.launch, engine, full_access_acknowledged=True,
                                   start_new_session=True)
                self.assertEqual("start_new_session", exc.detail)
                self.launcher.assert_not_called()
                decision, _ = self.launch(engine, full_access_acknowledged=True)
                self.assertIn("full_access_profile_acknowledged", decision.flags)
                self.assertEqual({"env", "shell", "engine_config", "model"}, set(self.launcher.call_args.kwargs))
                self.launcher.reset_mock()
                decision, _ = self.launch(engine)
                self.assertNotIn("full_access_profile_acknowledged", decision.flags)

    def test_full_access_forwards_an_option_listed_in_extra_launch_options(self) -> None:
        listed = RouteRequirements(extra_launch_options=("start_new_session",))
        for engine in self.guards():
            with self.subTest(engine=engine):
                self.launcher.reset_mock()
                decision, _ = self.launch(engine, full_access_acknowledged=True, requirements=listed,
                                          start_new_session=True)
                self.assertIs(self.launcher.call_args.kwargs["start_new_session"], True)
                self.assertIn("full_access_profile_acknowledged", decision.flags)
                # Only the listed name: full access does not forward another one.
                self.refused("launch_option_unknown", self.launch, engine, full_access_acknowledged=True,
                             requirements=listed, start_new_session=True, umask=0)

    def test_parent_environment_cannot_turn_full_access_on(self) -> None:
        names = ("FULL_ACCESS_ACKNOWLEDGED", "full_access_acknowledged", "RECURSION_FULL_ACCESS",
                 "RECURSION_FULL_ACCESS_ACKNOWLEDGED", "RECURSION_ALLOWED_LINK_TARGETS", "RECURSION_EXTRA_LAUNCH_OPTIONS")
        loud = dict.fromkeys(names, "1")
        loud["RECURSION_ALLOWED_LINK_TARGETS"] = str(self.outside)
        loud["RECURSION_EXTRA_LAUNCH_OPTIONS"] = "start_new_session"
        (self.outside / "index.js").write_text("dep\n", encoding="ascii")
        with mock.patch.dict(os.environ, {**loud, "FULL_ACCESS_ACKNOWLEDGED": "true"}):
            parent = self.parent(**loud)
            for engine in self.guards():
                with self.subTest(engine=engine):
                    self.refused("launch_option_unknown", self.launch, engine, parent_env=parent, start_new_session=True)
                    decision, _ = self.launch(engine, parent_env=parent)
                    self.assertNotIn("full_access_profile_acknowledged", decision.flags)
            os.symlink(self.outside, self.install / "node_modules")
            self.pin_refused("package_link_outside_root", self.launcher_path, str(self.install))
            self.refused("package_link_outside_root", self.cursor)
            self.refused("package_link_outside_root", self.launch, "pi", parent_env=parent)

    def test_profile_and_child_env_cannot_turn_full_access_on(self) -> None:
        self.refused("profile_unknown_field", self.launch, "pi", use_profile=self.profile(full_access_acknowledged=True))
        self.refused("child_env_not_allowed", self.launch, "pi", use_env=self.env(FULL_ACCESS_ACKNOWLEDGED="true"))

    def test_full_access_never_relaxes_the_credential_and_binding_refusals(self) -> None:
        full = {"full_access_acknowledged": True}
        self.refused("claude_subscription_env", self.launch, "pi", use_env=self.env(ANTHROPIC_OAUTH_TOKEN=FAKE_OAUTH), **full)
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_cursor_then_spawn(self.launcher, [self.launcher_path], {**self.bare(), "CLAUDE_CODE_OAUTH_TOKEN": FAKE_OAUTH},
                                    route_home=str(self.home), wrapper=self.wrapper, parent_env=self.parent(),
                                    operator_home=str(self.operator), **full)
        self.assertEqual("claude_subscription_env", caught.exception.cause)
        self.refused("claude_subscription_provider", self.launch, "pi",
                     use_profile=self.profile(provider="claude-max"), **full)
        self.refused("google_subscription_refused", self.launch, "pi", use_env=self.bare(),
                     use_profile={"provider": "gemini", "auth": "subscription", "method": "x", "model": "gemini/m"}, **full)
        self.refused("argv0_not_pinned", self.launch, "pi", ["pi", "run", "anthropic/m"], **full)
        self.refused("launch_option_denied", self.launch, "pi", shell=True, **full)
        self.refused("child_env_not_allowed", self.launch, "pi", use_env=self.env(HTTPS_PROXY="http://127.0.0.1:1"), **full)
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_then_spawn(self.launcher, [self.launcher_path, "run"], "hermes", self.profile(), self.env(),
                             route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
                             wrapper=self.wrapper, **full)
        self.assertEqual("hermes_adopt_external_logins_not_off", caught.exception.cause)
        (self.install / "lib.js").write_text("lib-v2\n", encoding="ascii")
        self.refused("wrapper_pin_mismatch", self.launch, "pi", **full)
        self.launcher.assert_not_called()


class GeminiCliDroppedTests(_Calls):
    """Call A: there is no Gemini CLI route. Antigravity CLI is the Google route."""

    def test_the_module_and_its_names_are_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("recursion_core.subscription_routes.gemini_cli")
        for name in ("check_gemini_cli_sign_in", "GeminiCliRefused"):
            self.assertFalse(hasattr(routes, name))
            self.assertNotIn(name, routes.__all__)

    @unittest.skipIf(os.name == "nt", "POSIX home checks")
    def test_gemini_is_not_an_engine_and_google_sign_ins_still_refuse(self) -> None:
        for engine in ("gemini", "gemini_cli", "gemini-cli"):
            self.assertNotIn(engine, routes.ENGINES)
            self.refused("unknown_engine", check_route, engine, self.profile(), self.env(), route_home=str(self.home))
        for engine in ("pi", "hermes", "opencode"):
            for provider in ("google", "Gemini", "gemini-cli", "antigravity", "google_one", "google_ai_pro"):
                with self.subTest(engine=engine, provider=provider):
                    profile = {"provider": provider, "auth": "subscription", "method": "login_with_google",
                               "model": "gemini/m"}
                    if engine == "hermes":
                        profile[HERMES_ADOPT_EXTERNAL_LOGINS] = False
                    self.refused("google_subscription_refused", check_route, engine, profile, self.bare(),
                                 route_home=str(self.home), parent_env=self.parent(),
                                 operator_home=str(self.operator), wrapper=self.wrapper)
            # A Gemini API key is not on any engine's API-key list either.
            self.refused("provider_not_allowed", check_route, engine,
                         {**self.profile(provider="gemini", credential_env="GEMINI_API_KEY", model="gemini/m"),
                          **({HERMES_ADOPT_EXTERNAL_LOGINS: False} if engine == "hermes" else {})},
                         {**self.bare(), "GEMINI_API_KEY": FAKE_KEY}, route_home=str(self.home),
                         parent_env=self.parent(), operator_home=str(self.operator), wrapper=self.wrapper)

    @unittest.skipIf(os.name == "nt", "POSIX home checks")
    def test_antigravity_is_the_google_route(self) -> None:
        decision, _ = self.launch("antigravity")
        self.assertEqual(("antigravity", "antigravity", True), (decision.engine, decision.provider, decision.beta))


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class NoSecretInReprTests(_Calls):
    def test_decision_repr_does_not_print_the_child_env(self) -> None:
        decision, _ = self.launch("pi")
        self.assertEqual(FAKE_KEY, dict(decision.child_env)["ANTHROPIC_API_KEY"])  # still carried for the launch
        for text in (repr(decision), str(decision)):
            self.assertNotIn(FAKE_KEY, text)
            self.assertNotIn("child_env", text)


if __name__ == "__main__":
    unittest.main()
