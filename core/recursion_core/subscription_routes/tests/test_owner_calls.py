"""2026-11-09 owner calls on recursion #321: package links, pin shape, argv[0] binding, Hermes home reuse,
launch options, configured requirements, the full-access profile and the dropped Gemini CLI route.

Fakes only: a MagicMock launcher and temp trees. No engine binary is executed and no network is used.
"""

from __future__ import annotations

import errno
import importlib
import inspect
import os
import shutil
import stat
import time
import unittest
from contextlib import contextmanager
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


def _stack_has(name: str) -> bool:
    frame = inspect.currentframe()
    while frame is not None:
        if frame.f_code.co_name == name:
            return True
        frame = frame.f_back
    return False


def _clone_stat(st: os.stat_result) -> os.stat_result:
    """A ``stat_result`` the recheck can be shown again, including the nanosecond stamps."""

    reduced = st.__reduce__()
    return reduced[0](*reduced[1])


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
        return self.launch("hermes", [self.launcher_path, "run", "grok/test-model"], use_profile=self.sub(),
                           use_env=self.bare(), **inputs)

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
        (staging, dest), kwargs = rename.call_args
        # The staged directory is renamed onto .hermes. A dirfd rename stays inside the held route home.
        if kwargs.get("dst_dir_fd") is None:
            self.assertEqual(str(self.home / ".hermes"), dest)
            self.assertEqual(str(self.home), os.path.dirname(staging))
        else:
            self.assertEqual(".hermes", os.path.basename(dest))
            self.assertEqual(kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd"))
            self.assertGreaterEqual(kwargs["dst_dir_fd"], 0)
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

    def test_a_temp_file_swapped_before_the_link_is_not_launched(self) -> None:
        # The link's success is not proof the new name holds the switch.
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        real_link = os.link

        def overwrite(src: str, dst: str, *args: object, **kwargs: object) -> None:
            src_dir_fd = kwargs.get("src_dir_fd")
            if src_dir_fd is None:
                with open(src, "wb") as handle:
                    handle.write(evil)
            else:
                fd = os.open(src, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=src_dir_fd)
                try:
                    os.write(fd, evil)
                finally:
                    os.close(fd)
            real_link(src, dst, *args, **kwargs)

        with mock.patch.object(policy.os, "link", side_effect=overwrite):
            self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.assertEqual(b"hermes-state", (self.home / ".hermes" / "state.db").read_bytes())
        self.launcher.assert_not_called()
        self.config().unlink(missing_ok=True)

        target = self.home / "not-the-switch.yaml"
        target.write_bytes(evil)

        def relink(src: str, dst: str, *args: object, **kwargs: object) -> None:
            src_dir_fd = kwargs.get("src_dir_fd")
            if src_dir_fd is None:
                os.unlink(src)
                os.symlink(target, src)
            else:
                os.unlink(src, dir_fd=src_dir_fd)
                os.symlink(os.path.realpath(target), src, dir_fd=src_dir_fd)
            real_link(src, dst, *args, **kwargs)

        with mock.patch.object(policy.os, "link", side_effect=relink):
            self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.launcher.assert_not_called()

    def test_a_staged_switch_swapped_before_the_rename_is_not_launched(self) -> None:
        # The rename's success is not proof the directory now holds the switch.
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        real_rename = os.rename

        def overwrite(src: str, dst: str, *args: object, **kwargs: object) -> None:
            src_dir_fd = kwargs.get("src_dir_fd")
            if src_dir_fd is None:
                with open(os.path.join(src, "config.yaml"), "wb") as handle:
                    handle.write(evil)
                real_rename(src, dst)
                return
            staging_fd = os.open(src, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=src_dir_fd)
            try:
                fd = os.open("config.yaml", os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, dir_fd=staging_fd)
                try:
                    os.write(fd, evil)
                finally:
                    os.close(fd)
            finally:
                os.close(staging_fd)
            real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=kwargs.get("dst_dir_fd"))

        with mock.patch.object(policy.os, "rename", side_effect=overwrite):
            self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.launcher.assert_not_called()

    def test_route_home_replaced_before_the_write_is_not_followed(self) -> None:
        real_pinned = policy._argv0_is_pinned

        outside = self.root / "outside-home"
        outside.mkdir(mode=0o700)

        def swap_for_symlink(argv: list[str], pinned: tuple[str, ...]) -> bool:
            os.rename(self.home, self.root / "route-home.real")
            os.symlink(outside, self.home)
            return real_pinned(argv, pinned)

        with mock.patch.object(policy, "_argv0_is_pinned", side_effect=swap_for_symlink):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.assertFalse((outside / ".hermes").exists())
        self.launcher.assert_not_called()

        # Put the checked directory back, then replace it with a different real directory.
        if self.home.is_symlink():
            self.home.unlink()
        moved = self.root / "route-home.real"
        if moved.exists() and not self.home.exists():
            os.rename(moved, self.home)
        decoy = self.root / "decoy-home"
        decoy.mkdir(mode=0o700)

        def swap_for_directory(argv: list[str], pinned: tuple[str, ...]) -> bool:
            os.rename(self.home, self.root / "route-home.real")
            os.rename(decoy, self.home)
            return real_pinned(argv, pinned)

        with mock.patch.object(policy, "_argv0_is_pinned", side_effect=swap_for_directory):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.root / "route-home.real" / ".hermes").exists())
        self.launcher.assert_not_called()

        if not self.home.exists() and (self.root / "route-home.real").exists():
            os.rename(self.root / "route-home.real", self.home)
        self.home.chmod(0o700)

        def widen(argv: list[str], pinned: tuple[str, ...]) -> bool:
            os.chmod(self.home, 0o777)
            return real_pinned(argv, pinned)

        with mock.patch.object(policy, "_argv0_is_pinned", side_effect=widen):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.assertFalse((self.home / ".hermes").exists())
        self.launcher.assert_not_called()

    def _route_home_fsync(self, directory: str | int) -> bool:
        if isinstance(directory, str):
            return os.path.abspath(directory) == str(self.home)
        if isinstance(directory, int):
            try:
                st, home = os.fstat(directory), os.lstat(self.home)
            except OSError:
                return False
            return stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == (home.st_dev, home.st_ino)
        return False

    def test_true_rewrite_during_route_home_fsync_after_the_link_check_does_not_launch(self) -> None:
        # The link check is not the last step: a same-length true switch written during the
        # route-home fsync that follows it must not launch, and must not be left behind.
        (self.home / ".hermes").mkdir(mode=0o700)
        (self.home / ".hermes" / "state.db").write_bytes(b"hermes-state")
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        real_fsync_dir = policy._fsync_dir

        def fsync_dir(directory: str | int) -> None:
            real_fsync_dir(directory)
            if self._route_home_fsync(directory) and self.config().is_file() and self.config().read_bytes() == ADOPT_YAML:
                self.config().write_bytes(evil)

        with mock.patch.object(policy, "_fsync_dir", side_effect=fsync_dir):
            self.refused("hermes_config_blocks_adopt_override", self.launch_sub)
        self.launcher.assert_not_called()
        self.assertEqual(b"hermes-state", (self.home / ".hermes" / "state.db").read_bytes())
        if self.config().exists():
            self.assertEqual(ADOPT_YAML, self.config().read_bytes())

    def test_true_rewrite_after_stage_returns_does_not_launch(self) -> None:
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        real_stage = policy._stage_hermes_dir

        def stage(engine: str, route_home: str, parent: str) -> None:
            real_stage(engine, route_home, parent)
            with open(os.path.join(parent, "config.yaml"), "wb") as handle:
                handle.write(evil)

        with mock.patch.object(policy, "_stage_hermes_dir", side_effect=stage):
            self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.launcher.assert_not_called()
        if self.config().exists():
            self.assertEqual(ADOPT_YAML, self.config().read_bytes())

    def test_replaced_switch_inode_is_not_launched(self) -> None:
        # Same bytes in a new inode are not the file this launch wrote.
        real_stage = policy._stage_hermes_dir

        def stage(engine: str, route_home: str, parent: str) -> None:
            real_stage(engine, route_home, parent)
            cfg = os.path.join(parent, "config.yaml")
            # Keep the written inode allocated so the kernel cannot hand that number out again.
            os.link(cfg, os.path.join(route_home, "kept-inode"))
            os.unlink(cfg)
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, ADOPT_YAML)
            finally:
                os.close(fd)

        with mock.patch.object(policy, "_stage_hermes_dir", side_effect=stage):
            self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.launcher.assert_not_called()

    def test_route_home_swapped_during_verify_is_not_stamped_or_launched(self) -> None:
        # A 0700 directory swapped in during the pin check is not the home that was checked.
        real_verify = policy.verify_package_pin
        decoy = self.root / "decoy-0700"
        decoy.mkdir(mode=0o700)
        os.chmod(decoy, 0o700)

        def swap_for_directory(*args: object, **kwargs: object):
            os.rename(self.home, self.root / "route-home.real")
            os.rename(decoy, self.home)
            return real_verify(*args, **kwargs)

        with mock.patch.object(policy, "verify_package_pin", side_effect=swap_for_directory):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.launcher.assert_not_called()
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.root / "route-home.real" / ".hermes").exists())

    def test_operator_home_swapped_in_during_verify_is_not_launched(self) -> None:
        real_verify = policy.verify_package_pin
        os.chmod(self.operator, 0o700)

        def swap_for_operator(*args: object, **kwargs: object):
            os.rename(self.home, self.root / "route-home.real")
            os.rename(self.operator, self.home)
            return real_verify(*args, **kwargs)

        with mock.patch.object(policy, "verify_package_pin", side_effect=swap_for_operator):
            self.refused("route_home_is_operator_home", self.launch, "hermes")
        self.launcher.assert_not_called()
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.root / "route-home.real" / ".hermes").exists())

    def test_swap_after_the_home_recheck_does_not_write_the_replacement(self) -> None:
        decoy = self.root / "decoy-apply"
        decoy.mkdir(mode=0o700)
        os.chmod(decoy, 0o700)
        real_apply = policy._apply_hermes_adopt_override

        def swap(decision: object, route_home: str) -> None:
            os.rename(self.home, self.root / "route-home.real")
            os.rename(decoy, self.home)
            return real_apply(decision, route_home)

        with mock.patch.object(policy, "_apply_hermes_adopt_override", side_effect=swap):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.launcher.assert_not_called()
        self.assertFalse((self.home / ".hermes").exists())
        self.assertFalse((self.root / "route-home.real" / ".hermes").exists())

    def test_decoy_home_renamed_during_final_reread_does_not_launch(self) -> None:
        # The final re-read opens the held dirfd. A 0700 directory with no
        # .hermes/config.yaml, renamed onto the route path at the start of that
        # read, is the HOME the child would get, and Hermes then defaults
        # adopt_external_logins to true.
        decoy = self.root / "decoy-0700-reread"
        decoy.mkdir(mode=0o700)
        os.chmod(decoy, 0o700)
        real = policy._reverify_hermes_switch
        moved = self.root / "route-home.real"

        def hook(engine: str, route_home: str):
            os.rename(self.home, moved)
            os.rename(decoy, self.home)
            return real(engine, route_home)

        with mock.patch.object(policy, "_reverify_hermes_switch", side_effect=hook):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.launcher.assert_not_called()
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.home).st_mode))
        self.assertFalse((self.home / ".hermes").exists())
        self.assertNotEqual(os.lstat(self.home).st_ino, os.lstat(moved).st_ino)
        self.assertEqual(ADOPT_YAML, (moved / ".hermes" / "config.yaml").read_bytes())

    def _private(self, path: Path) -> None:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)

    def _after_last_bind(self, mutate):
        """Run ``mutate`` after ``_bind_hermes_launch_path`` returns.

        That function's path ``lstat`` is the last check on 76a11a0c. A mutation
        here is invisible to that check and is what the child would open.
        """

        real = policy._bind_hermes_launch_path

        def wrapped(*args: object, **kwargs: object) -> None:
            real(*args, **kwargs)
            mutate()

        return mock.patch.object(policy, "_bind_hermes_launch_path", side_effect=wrapped)

    @contextmanager
    def _during_post_listing_walk(self, mutate):
        """Run ``mutate`` once, when the root walk after the ``.hermes`` listing starts.

        The listing has already pinned ``config.yaml``. The recheck runs after this walk returns.
        """

        real_list = policy._refuse_hermes_dir_entries
        real_walk = policy._bind_route_home_from_root
        listed = False
        done = False

        def listing(*args: object, **kwargs: object):
            nonlocal listed
            result = real_list(*args, **kwargs)
            listed = True
            return result

        def walk(engine: str, route_home: str) -> None:
            nonlocal done
            if listed and not done:
                done = True
                mutate()
            real_walk(engine, route_home)

        with mock.patch.object(policy, "_refuse_hermes_dir_entries", side_effect=listing), \
                mock.patch.object(policy, "_bind_route_home_from_root", side_effect=walk):
            yield

    def _hermes_at(self, home: Path, *, subscription: bool = False):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
        if subscription:
            profile = {**self.sub(), HERMES_ADOPT_EXTERNAL_LOGINS: False}
            argv = [self.launcher_path, "run", "grok/test-model"]
        else:
            profile = {**self.profile(), HERMES_ADOPT_EXTERNAL_LOGINS: False}
            argv = [self.launcher_path, "run", "anthropic/m"]
            env["ANTHROPIC_API_KEY"] = FAKE_KEY
        return guard_then_spawn(
            self.launcher, argv, "hermes", profile, env, route_home=str(home),
            parent_env=self.parent(), operator_home=str(self.operator), wrapper=self.wrapper,
        )

    def test_subscription_refuses_hermes_dotenv_file_symlink_and_directory(self) -> None:
        # load_hermes_dotenv() reads route_home/.hermes/.env (override) before config.yaml.
        # A pre-created file is only flagged own_login_store_not_inspected on the parent.
        # The symlink target stays inside the route home, so the own-store link walk allows it.
        # .op.env is the same loader's second file. A directory of either name refuses too.
        inside = self.home / "note.txt"
        inside.write_text("HERMES_HOME=/not-the-route\n", encoding="ascii")
        for name in (".env", ".op.env"):
            for kind in ("file", "symlink", "dir"):
                with self.subTest(name=name, kind=kind):
                    shutil.rmtree(self.home / ".hermes", ignore_errors=True)
                    self._private(self.home / ".hermes")
                    entry = self.home / ".hermes" / name
                    if kind == "file":
                        entry.write_text("HERMES_HOME=/not-the-route\nHERMES_DATA_DIR_SUFFIX=x\n", encoding="ascii")
                    elif kind == "symlink":
                        os.symlink(inside, entry)
                    else:
                        self._private(entry)
                    exc = self.refused("hermes_dotenv_in_home", self.launch_sub)
                    self.assertEqual(name, exc.detail)
                    self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        self.launcher.assert_not_called()

    def test_api_key_dotenv_planted_after_the_listing_does_not_launch(self) -> None:
        # The early listdir accepts a .hermes that holds only config.yaml. The file
        # created after that listing is what Hermes would load before the switch.
        def plant() -> None:
            fd = os.open(self.home / ".hermes" / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, b"HERMES_HOME=/not-the-route\n")
            finally:
                os.close(fd)

        with self._after_last_bind(plant):
            exc = self.refused("hermes_dotenv_in_home", self.launch, "hermes")
        self.assertEqual(".env", exc.detail)
        self.launcher.assert_not_called()

    def test_api_key_refuses_a_non_allowlisted_entry_planted_after_the_listing(self) -> None:
        def plant() -> None:
            fd = os.open(self.home / ".hermes" / "state.db", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)

        with self._after_last_bind(plant):
            exc = self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.assertEqual("state.db", exc.detail)
        self.launcher.assert_not_called()

    def test_route_home_replaced_after_last_lstat_does_not_launch(self) -> None:
        # Empty 0700 directory swapped onto the route path after the last lstat.
        # Hermes would default adopt_external_logins to true with no config.yaml.
        moved = self.root / "route-home.real"

        def swap() -> None:
            os.rename(self.home, moved)
            self._private(self.home)

        with self._after_last_bind(swap):
            self.refused("route_home_not_private", self.launch, "hermes")
        self.launcher.assert_not_called()
        self.assertFalse((self.home / ".hermes").exists())
        self.assertEqual(ADOPT_YAML, (moved / ".hermes" / "config.yaml").read_bytes())

    def test_parent_swapped_after_last_lstat_does_not_launch(self) -> None:
        parent = self.root / "parent"
        self._private(parent)
        home = parent / "home"
        self._private(home)
        moved = self.root / "parent.real"

        def swap() -> None:
            os.rename(parent, moved)
            self._private(parent)
            self._private(parent / "home")

        with self._after_last_bind(swap):
            self.refused("route_home_not_private", self._hermes_at, home)
        self.launcher.assert_not_called()
        self.assertFalse((parent / "home" / ".hermes").exists())
        self.assertEqual(ADOPT_YAML, (moved / "home" / ".hermes" / "config.yaml").read_bytes())

    def test_symlink_ancestor_retargeted_after_last_lstat_does_not_launch(self) -> None:
        real_parent = self.root / "real-parent"
        self._private(real_parent)
        home = real_parent / "home"
        self._private(home)
        link = self.root / "via-link"
        os.symlink(real_parent, link)
        decoy = self.root / "decoy-parent"
        self._private(decoy)
        self._private(decoy / "home")

        def swap() -> None:
            os.unlink(link)
            os.symlink(decoy, link)

        with self._after_last_bind(swap):
            self.refused("route_home_not_private", self._hermes_at, link / "home")
        self.launcher.assert_not_called()
        self.assertFalse((decoy / "home" / ".hermes").exists())
        self.assertEqual(ADOPT_YAML, (home / ".hermes" / "config.yaml").read_bytes())

    def test_search_only_ancestor_launches_and_the_root_walk_sees_no_adopt_write(self) -> None:
        # Mode 0111 is searchable. Opening it O_RDONLY needs read permission and
        # refuses route_home_not_private after the adopt switch was already written.
        parent = self.root / "noread"
        parent.mkdir(mode=0o700)
        home = parent / "route"
        self._private(home)
        os.chmod(parent, 0o111)
        try:
            seen: list[bool] = []
            real = policy._bind_route_home_from_root

            def wrapped(engine: str, route_home: str) -> None:
                seen.append(os.path.lexists(os.path.join(route_home, ".hermes", "config.yaml")))
                real(engine, route_home)

            refused: SubscriptionRouteRefused | None = None
            try:
                with mock.patch.object(policy, "_bind_route_home_from_root", side_effect=wrapped):
                    self._hermes_at(home)
            except SubscriptionRouteRefused as exc:
                refused = exc
            self.assertEqual((None, False), (None if refused is None else refused.cause, seen[0] if seen else None))
            self.assertEqual(ADOPT_YAML, (home / ".hermes" / "config.yaml").read_bytes())
            self.assertIn("route_home_fd", self.launcher.call_args.kwargs)
            self.assertEqual(1, self.launcher.call_count)
        finally:
            os.chmod(parent, 0o700)

    def test_new_inode_config_at_the_post_listing_root_walk_refuses(self) -> None:
        # The listing stats config.yaml. Replacing that inode at the start of the
        # walk after the listing must not launch: path and route_home_fd would
        # both see the new bytes.
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        listed = False
        swapped = False
        real_list = policy._refuse_hermes_dir_entries
        real_walk = policy._bind_route_home_from_root

        def listing(*args: object, **kwargs: object):
            nonlocal listed
            result = real_list(*args, **kwargs)
            listed = True
            return result

        def walk(engine: str, route_home: str) -> None:
            nonlocal swapped
            if listed and not swapped:
                swapped = True
                cfg = os.path.join(route_home, ".hermes", "config.yaml")
                os.link(cfg, os.path.join(route_home, "kept-config-inode"))
                os.unlink(cfg)
                fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, evil)
                finally:
                    os.close(fd)
            real_walk(engine, route_home)

        def spy(_argv: list[str], **kwargs: object) -> str:
            fd = kwargs["route_home_fd"]
            self.assertIsInstance(fd, int)
            hermes_fd = os.open(".hermes", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                cfg_fd = os.open("config.yaml", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=hermes_fd)
                try:
                    via_fd = os.read(cfg_fd, 64)
                finally:
                    os.close(cfg_fd)
            finally:
                os.close(hermes_fd)
            via_path = (self.home / ".hermes" / "config.yaml").read_bytes()
            raise AssertionError(f"launched path={via_path!r} fd={via_fd!r}")

        self.launcher.side_effect = spy
        with mock.patch.object(policy, "_refuse_hermes_dir_entries", side_effect=listing), \
                mock.patch.object(policy, "_bind_route_home_from_root", side_effect=walk):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(swapped)
        self.launcher.assert_not_called()

    def test_swapped_in_user_config_survives_the_post_listing_refusal(self) -> None:
        # A Hermes atomic-save rename lands a different inode after the pin.
        # The refusal must not unlink a file this launch did not read.
        user = (
            b"# hermes config\n"
            b"model:\n"
            b"  default: anthropic/claude-sonnet\n"
            b"  provider: anthropic\n"
            b"terminal:\n"
            b"  backend: local\n"
            b"auth:\n"
            b"  adopt_external_logins: false\n"
        )
        self.assertEqual(144, len(user))

        def swap() -> None:
            dest = self.config()
            tmp = dest.parent / "config.yaml.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                self.assertEqual(len(user), os.write(fd, user))
            finally:
                os.close(fd)
            os.rename(tmp, dest)

        with self._during_post_listing_walk(swap):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(self.config().is_file())
        self.assertEqual(user, self.config().read_bytes())
        self.assertFalse((self.home / ".hermes" / "config.yaml.tmp").exists())
        self.launcher.assert_not_called()

    def test_same_inode_rewrite_after_listing_refuses(self) -> None:
        # O_TRUNC keeps the inode. Both live stats then share dev, ino, size and
        # mtime_ns, including when the write stays inside one timestamp tick.
        same = b"auth:\n  adopt_external_logins: true \n"
        grown = b"auth:\n  adopt_external_logins: true\nmodel: xyz\n"
        appended = ADOPT_YAML + b"# comment\n"
        self.assertEqual((37, 47, 47), (len(same), len(grown), len(appended)))
        for label, payload in (("same-length", same), ("size-change", grown), ("appended", appended)):
            with self.subTest(label=label):
                shutil.rmtree(self.home / ".hermes", ignore_errors=True)

                def rewrite(payload: bytes = payload) -> None:
                    cfg = self.config()
                    before = os.lstat(cfg)
                    fd = os.open(cfg, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
                    try:
                        self.assertEqual(len(payload), os.write(fd, payload))
                    finally:
                        os.close(fd)
                    os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns))
                    after = os.lstat(cfg)
                    self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
                    self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
                    self.assertEqual(len(payload), after.st_size)

                with self._during_post_listing_walk(rewrite):
                    exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
                self.assertEqual("config.yaml", exc.detail)
                self.assertEqual(payload, self.config().read_bytes())
                self.launcher.assert_not_called()
                self.launcher.reset_mock()

    def test_new_inode_after_recheck_name_stat_refuses_and_is_left_in_place(self) -> None:
        # The recheck's name stat of config.yaml used to run before pread and fstat.
        # A rename onto a new inode after that stat returned still launched: the fd
        # still held the pinned bytes. The name is sampled again after the content
        # checks, and a mismatch refuses without unlinking the new file.
        user = b"# owner config, left in place\nmodel: theirs\n"
        planted = self.root / "user-config.yaml"
        planted.write_bytes(user)
        swapped = False
        real_stat = os.stat

        def racing_stat(path: object, *args: object, **kwargs: object):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if swapped or path != "config.yaml" or not _stack_has("_recheck_pinned_hermes_config"):
                return result
            swapped = True
            os.rename(planted, self.config())
            live = os.lstat(self.config())
            self.assertNotEqual(result.st_ino, live.st_ino)
            self.assertEqual(user, self.config().read_bytes())
            return result

        with mock.patch.object(policy.os, "stat", side_effect=racing_stat):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(swapped)
        self.assertEqual(user, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_same_size_trunc_between_recheck_pread_and_fstat_refuses(self) -> None:
        # pread used to be the last look at the bytes. A same-size O_TRUNC before
        # fstat, with mtime_ns restored, kept dev, ino, size and mtime. ctime_ns
        # still changes. The second fstat must see that and refuse, and not unlink.
        same = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(same))
        rewritten = False
        real_pread = os.pread

        def racing_pread(fd: int, count: int, offset: int, *args: object, **kwargs: object):
            nonlocal rewritten
            data = real_pread(fd, count, offset, *args, **kwargs)
            if rewritten or not _stack_has("_recheck_pinned_hermes_config"):
                return data
            rewritten = True
            cfg = self.config()
            before = os.lstat(cfg)
            # Inode ctime follows the coarse clock. Retry across that tick so the
            # restored mtime is not paired with an unchanged ctime.
            after = before
            for _ in range(50):
                write = os.open(cfg, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
                try:
                    self.assertEqual(len(same), os.write(write, same))
                finally:
                    os.close(write)
                os.utime(cfg, ns=(before.st_atime_ns, before.st_mtime_ns))
                after = os.lstat(cfg)
                if after.st_ctime_ns != before.st_ctime_ns:
                    break
                time.sleep(0.01)
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
            return data

        with mock.patch.object(policy.os, "pread", side_effect=racing_pread):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(rewritten)
        self.assertEqual(same, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_hermes_dir_swapped_after_recheck_open_refuses(self) -> None:
        # os.open of .hermes returns an fd. fstat and listdir on that fd still see
        # the pinned directory after the name is renamed aside and a new .hermes
        # with .env is put in its place. The hardlink that keeps the config inode
        # raises its link count, so the descriptor check refuses config.yaml. The
        # new directory is left where it is.
        swapped = False
        real_open = os.open

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            fd = real_open(path, flags, *args, **kwargs)
            if swapped or path != ".hermes" or not _stack_has("_recheck_pinned_hermes_config"):
                return fd
            swapped = True
            aside = self.home / ".hermes.aside"
            os.rename(self.home / ".hermes", aside)
            self._private(self.home / ".hermes")
            os.link(aside / "config.yaml", self.config())
            env = real_open(
                self.home / ".hermes" / ".env",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(env, b"HERMES_HOME=/not-the-route\n")
            finally:
                os.close(env)
            return fd

        with mock.patch.object(policy.os, "open", side_effect=racing_open):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(swapped)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        aside = self.home / ".hermes.aside"
        self.assertEqual(ADOPT_YAML, (aside / "config.yaml").read_bytes())
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(os.lstat(aside / "config.yaml").st_ino, os.lstat(self.config()).st_ino)
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_dotenv_planted_after_recheck_listdir_refuses(self) -> None:
        # listdir's names omit a .env created after it returns. Absence has to be
        # stat'd again after that listing, and the planted file is not removed.
        planted = False
        real_listdir = os.listdir

        def racing_listdir(path: object, *args: object, **kwargs: object):
            nonlocal planted
            names = real_listdir(path, *args, **kwargs)
            if planted or not isinstance(path, int) or not _stack_has("_recheck_pinned_hermes_config"):
                return names
            planted = True
            env = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=path)
            try:
                os.write(env, b"HERMES_HOME=/not-the-route\n")
            finally:
                os.close(env)
            return names

        with mock.patch.object(policy.os, "listdir", side_effect=racing_listdir):
            exc = self.refused("hermes_dotenv_in_home", self.launch, "hermes")
        self.assertEqual(".env", exc.detail)
        self.assertTrue(planted)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_same_tick_same_size_trunc_between_pread_and_second_fstat_refuses(self) -> None:
        # Between pread and the fstat after it, a same-size O_TRUNC that restores
        # mtime_ns often keeps ctime_ns (one coarse tick; no sleep). The recheck
        # is shown that pre-trunc stamp. The bytes on the inode are not the pin.
        # The second read has to refuse, and the rewritten file stays.
        same = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(same))
        rewritten = False
        frozen: os.stat_result | None = None
        real_pread, real_fstat = os.pread, os.fstat

        def racing_pread(fd: int, count: int, offset: int, *args: object, **kwargs: object):
            nonlocal rewritten, frozen
            data = real_pread(fd, count, offset, *args, **kwargs)
            if rewritten or not _stack_has("_recheck_pinned_hermes_config"):
                return data
            rewritten = True
            before = real_fstat(fd)
            write = os.open(self.config(), os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            try:
                self.assertEqual(len(same), os.write(write, same))
            finally:
                os.close(write)
            os.utime(self.config(), ns=(before.st_atime_ns, before.st_mtime_ns))
            after = os.lstat(self.config())
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(len(same), after.st_size)
            # Publish the pre-trunc stamp. A tick that did move ctime_ns is not
            # what this window shows the recheck.
            frozen = _clone_stat(before)
            self.assertEqual(before.st_ctime_ns, frozen.st_ctime_ns)
            return data

        def racing_fstat(fd: int, *args: object, **kwargs: object):
            st = real_fstat(fd, *args, **kwargs)
            if (frozen is None or not _stack_has("_recheck_pinned_hermes_config")
                    or (st.st_dev, st.st_ino) != (frozen.st_dev, frozen.st_ino)):
                return st
            return frozen

        with mock.patch.object(policy.os, "pread", side_effect=racing_pread), \
                mock.patch.object(policy.os, "fstat", side_effect=racing_fstat):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(rewritten)
        self.assertIsNotNone(frozen)
        self.assertEqual(same, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_same_size_rewrite_after_second_fstat_refuses(self) -> None:
        # After the second fstat of the pinned descriptor, later checks used to
        # compare dev/ino only. A same-size rewrite there still launched. No sleep:
        # the next pread has to see the new bytes, and the file is not removed.
        same = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(same))
        rewritten = False
        fstats = 0
        config_id: tuple[int, int] | None = None
        real_fstat = os.fstat

        def racing_fstat(fd: int, *args: object, **kwargs: object):
            nonlocal rewritten, fstats, config_id
            st = real_fstat(fd, *args, **kwargs)
            if not stat.S_ISREG(st.st_mode) or not _stack_has("_recheck_pinned_hermes_config"):
                return st
            if config_id is None:
                live = os.lstat(self.config())
                config_id = (live.st_dev, live.st_ino)
            if (st.st_dev, st.st_ino) != config_id:
                return st
            fstats += 1
            if fstats != 2 or rewritten:
                return st
            rewritten = True
            before = os.lstat(self.config())
            write = os.open(self.config(), os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
            try:
                self.assertEqual(len(same), os.write(write, same))
            finally:
                os.close(write)
            os.utime(self.config(), ns=(before.st_atime_ns, before.st_mtime_ns))
            after = os.lstat(self.config())
            self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(len(same), after.st_size)
            return st

        with mock.patch.object(policy.os, "fstat", side_effect=racing_fstat):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(rewritten)
        self.assertGreaterEqual(fstats, 2)
        self.assertEqual(same, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_new_inode_after_second_config_stat_refuses(self) -> None:
        # The second stat of config.yaml used to be the last look at the name.
        # A new inode after it returned still launched: the pinned fd held the
        # old bytes. The new file is left where it is.
        user = b"# owner config, left in place\nmodel: theirs\n"
        planted = self.root / "user-config.yaml"
        planted.write_bytes(user)
        swapped = False
        stats = 0
        real_stat = os.stat

        def racing_stat(path: object, *args: object, **kwargs: object):
            nonlocal swapped, stats
            result = real_stat(path, *args, **kwargs)
            if path != "config.yaml" or not _stack_has("_recheck_pinned_hermes_config"):
                return result
            stats += 1
            if stats != 2 or swapped:
                return result
            swapped = True
            os.rename(planted, self.config())
            live = os.lstat(self.config())
            self.assertNotEqual(result.st_ino, live.st_ino)
            self.assertEqual(user, self.config().read_bytes())
            return result

        with mock.patch.object(policy.os, "stat", side_effect=racing_stat):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(swapped)
        self.assertGreaterEqual(stats, 2)
        self.assertEqual(user, self.config().read_bytes())
        self.launcher.assert_not_called()

    def test_hermes_replaced_between_by_name_stat_and_dir_fstat_refuses(self) -> None:
        # stat(".hermes") on the home fd returns, then the name is replaced,
        # then fstat of the fd still sees the directory that was opened. The
        # returned stat matches that fd. The new directory is what a path open
        # gets, and it is left in place, .env included.
        swapped = False
        real_stat = os.stat
        evil = b"auth:\n  adopt_external_logins: true \n"

        def racing_stat(path: object, *args: object, **kwargs: object):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if swapped or path != ".hermes" or not _stack_has("_recheck_pinned_hermes_config"):
                return result
            swapped = True
            aside = self.home / ".hermes.aside"
            os.rename(self.home / ".hermes", aside)
            self._private(self.home / ".hermes")
            fd = os.open(self.config(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                self.assertEqual(len(evil), os.write(fd, evil))
            finally:
                os.close(fd)
            env = os.open(
                self.home / ".hermes" / ".env",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(env, b"HERMES_HOME=/not-the-route\n")
            finally:
                os.close(env)
            self.assertNotEqual(result.st_ino, os.lstat(self.home / ".hermes").st_ino)
            return result

        with mock.patch.object(policy.os, "stat", side_effect=racing_stat):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual(".hermes", exc.detail)
        self.assertTrue(swapped)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        aside = self.home / ".hermes.aside"
        self.assertEqual(ADOPT_YAML, (aside / "config.yaml").read_bytes())
        self.assertEqual(evil, self.config().read_bytes())
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_dotenv_created_after_enoent_stat_refuses(self) -> None:
        # stat(".env") returns ENOENT, then the name is created. The next
        # absence check has to see it. The planted file is not removed.
        planted = False
        real_stat = os.stat

        def racing_stat(path: object, *args: object, **kwargs: object):
            nonlocal planted
            try:
                return real_stat(path, *args, **kwargs)
            except OSError as exc:
                if (planted or path != ".env" or exc.errno != errno.ENOENT
                        or not _stack_has("_recheck_pinned_hermes_config")):
                    raise
                planted = True
                dir_fd = kwargs.get("dir_fd")
                env = os.open(
                    ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd,
                )
                try:
                    os.write(env, b"HERMES_HOME=/not-the-route\n")
                finally:
                    os.close(env)
                raise

        with mock.patch.object(policy.os, "stat", side_effect=racing_stat):
            exc = self.refused("hermes_dotenv_in_home", self.launch, "hermes")
        self.assertEqual(".env", exc.detail)
        self.assertTrue(planted)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_api_key_state_db_created_after_listdir_refuses(self) -> None:
        # The API-key extra-name check used the listdir snapshot. state.db
        # created after that snapshot returned was not in it, so the launch
        # proceeded. The next listing has to refuse, and the file stays.
        planted = False
        real_listdir = os.listdir

        def racing_listdir(path: object, *args: object, **kwargs: object):
            nonlocal planted
            names = real_listdir(path, *args, **kwargs)
            if planted or not isinstance(path, int) or not _stack_has("_recheck_pinned_hermes_config"):
                return names
            planted = True
            fd = os.open("state.db", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=path)
            try:
                os.write(fd, b"x")
            finally:
                os.close(fd)
            return names

        with mock.patch.object(policy.os, "listdir", side_effect=racing_listdir):
            exc = self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.assertEqual("state.db", exc.detail)
        self.assertTrue(planted)
        self.assertEqual(b"x", (self.home / ".hermes" / "state.db").read_bytes())
        self.launcher.assert_not_called()

    def test_replaced_hermes_dir_keeping_the_config_inode_refuses(self) -> None:
        # Rename .hermes aside, mkdir a new one, hardlink config.yaml, add .env.
        # The link keeps the inode and changes ctime_ns. The recheck samples that
        # stamp before it opens the directory, so it refuses config.yaml. The new
        # directory is a different inode and is left in place, .env included.
        aside = self.home / ".hermes.aside"

        def swap() -> None:
            os.rename(self.home / ".hermes", aside)
            before = os.lstat(aside / "config.yaml")
            self._private(self.home / ".hermes")
            after = before
            for _ in range(50):
                if self.config().exists():
                    os.unlink(self.config())
                os.link(aside / "config.yaml", self.config())
                after = os.lstat(self.config())
                if after.st_ctime_ns != before.st_ctime_ns:
                    break
                time.sleep(0.01)
            self.assertEqual(before.st_ino, after.st_ino)
            self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
            self.assertNotEqual(os.lstat(aside).st_ino, os.lstat(self.home / ".hermes").st_ino)
            fd = os.open(self.home / ".hermes" / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, b"HERMES_HOME=/not-the-route\n")
            finally:
                os.close(fd)

        with self._during_post_listing_walk(swap):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        self.assertEqual(ADOPT_YAML, (aside / "config.yaml").read_bytes())
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(os.lstat(aside / "config.yaml").st_ino, os.lstat(self.config()).st_ino)
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_names_added_to_the_listed_hermes_dir_after_listing_refuse(self) -> None:
        cases = (
            (".env", "hermes_dotenv_in_home", False),
            (".op.env", "hermes_dotenv_in_home", False),
            ("state.db", "engine_store_in_route_home", False),
            (".env", "hermes_dotenv_in_home", True),
        )
        for name, cause, subscription in cases:
            with self.subTest(name=name, subscription=subscription):
                shutil.rmtree(self.home / ".hermes", ignore_errors=True)

                def plant(name: str = name) -> None:
                    fd = os.open(
                        self.home / ".hermes" / name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                    )
                    try:
                        os.write(fd, b"HERMES_HOME=/not-the-route\n")
                    finally:
                        os.close(fd)

                launch = self.launch_sub if subscription else lambda: self.launch("hermes")
                with self._during_post_listing_walk(plant):
                    exc = self.refused(cause, launch)
                self.assertEqual(name, exc.detail)
                self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
                self.assertTrue((self.home / ".hermes" / name).is_file())
                self.launcher.assert_not_called()
                self.launcher.reset_mock()

    def test_subscription_extra_name_added_after_listing_still_launches(self) -> None:
        def plant() -> None:
            fd = os.open(
                self.home / ".hermes" / "state.db",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(fd)

        with self._during_post_listing_walk(plant):
            self.launch_sub()
        self.assertEqual(b"", (self.home / ".hermes" / "state.db").read_bytes())
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(1, self.launcher.call_count)

    def test_launcher_gets_the_held_route_home_fd_and_not_a_proc_self_home(self) -> None:
        # The fake launcher does not spawn. HOME=/proc/self/fd/N would name this
        # process, not a child. The contract is the held directory fd, valid
        # during the call. A swap inside the launcher is still after every check:
        # the path in HOME is then the empty directory, and the fd is not.
        seen: dict[str, object] = {}

        def spy(_argv: list[str], **kwargs: object) -> str:
            self.assertIn("route_home_fd", kwargs)
            fd = kwargs["route_home_fd"]
            self.assertIsInstance(fd, int)
            env = kwargs["env"]
            self.assertIsInstance(env, dict)
            self.assertEqual(str(self.home), env["HOME"])
            self.assertNotIn("/proc/", env["HOME"])
            before = os.fstat(fd)
            home_st = os.lstat(self.home)
            self.assertEqual((home_st.st_dev, home_st.st_ino), (before.st_dev, before.st_ino))
            os.rename(self.home, self.root / "route-home.real")
            self._private(self.home)
            self.assertEqual((before.st_dev, before.st_ino), (os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            self.assertNotEqual(os.lstat(self.home).st_ino, before.st_ino)
            self.assertFalse((self.home / ".hermes").exists())
            hermes_fd = os.open(".hermes", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                cfg = os.open("config.yaml", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=hermes_fd)
                try:
                    seen["yaml"] = os.read(cfg, 64)
                finally:
                    os.close(cfg)
            finally:
                os.close(hermes_fd)
            return "launched"

        self.launcher.side_effect = spy
        self.launch("hermes")
        self.assertEqual(ADOPT_YAML, seen["yaml"])
        self.assertEqual(1, self.launcher.call_count)

    def test_config_renamed_inside_final_open_before_fstat_does_not_launch(self) -> None:
        # The final check fstats the fd it opened. Another file renamed onto
        # config.yaml inside that open, before fstat, is the name the child opens.
        evil = b"auth:\n  adopt_external_logins: true \n"
        self.assertEqual(len(ADOPT_YAML), len(evil))
        planted = self.root / "adopt-true.yaml"
        planted.write_bytes(evil)
        real_open = os.open
        swapped = False

        def called_from_reverify() -> bool:
            frame = inspect.currentframe()
            while frame is not None:
                if frame.f_code.co_name == "_reverify_hermes_switch":
                    return True
                frame = frame.f_back
            return False

        def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            nonlocal swapped
            fd = real_open(path, flags, *args, **kwargs)
            if not swapped and path == "config.yaml" and called_from_reverify():
                os.rename(planted, self.config())
                swapped = True
            return fd

        with mock.patch.object(policy.os, "open", side_effect=racing_open):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.assertTrue(swapped)
        self.launcher.assert_not_called()
        self.assertEqual(evil, self.config().read_bytes())

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
        opened_fds: list[int] = []

        def spy_open_fd(path: object, flags: int, *args: object, **kwargs: object) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            opened_fds.append(fd)
            return fd

        with mock.patch.object(policy.os, "fstat", side_effect=other_owner), \
                mock.patch.object(policy.os, "open", side_effect=spy_open_fd), \
                mock.patch.object(policy.os, "close", side_effect=spy_close):
            self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        # The owner mismatch refuses while reading the existing switch. Every descriptor
        # this launch opened, including the ancestor walk and the route home, is closed.
        self.assertTrue(opened_fds)
        self.assertEqual(sorted(opened_fds), sorted(closed))
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

    # os.* calls counted while _recheck_pinned_hermes_config is on the stack.
    # On 0f1fa676 that trace is 37 long: pass 2 listdir is 22, its ENOENT stats are
    # 23 (.env) and 24 (.op.env), the final .hermes name stat is 30, and the final
    # config.yaml name stat is 32.
    _RECHECK_HOOKS = ("open", "stat", "fstat", "listdir", "pread", "close")
    # os.* name, and the import-captured function the seal calls for that name.
    _EXTENDED_HOOKS = (
        ("open", "_LIBC_OPEN"),
        ("stat", "_LIBC_STAT"),
        ("fstat", "_LIBC_FSTAT"),
        ("listdir", "_LIBC_LISTDIR"),
        ("pread", "_LIBC_PREAD"),
        ("close", "_LIBC_CLOSE"),
        ("lstat", "_LIBC_LSTAT"),
    )

    def _restore_route_home_path(self) -> None:
        # A symlink planted in HOME's place must not be followed. Put the real directory back.
        if self.home.is_symlink():
            self.home.unlink()
        real = self.root / "route-home.real"
        if not self.home.exists() and real.exists():
            os.rename(real, self.home)
        elif real.exists():
            shutil.rmtree(real)

    def _reset_hermes_tree(self) -> None:
        self._pread_lie = False
        self._restore_route_home_path()
        for name in (".hermes", ".hermes.aside"):
            path = self.home / name
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        swapped = self.root / "swapped-config.yaml"
        if swapped.exists():
            swapped.unlink()
        self.launcher.reset_mock()

    @contextmanager
    def _hook_recheck_syscalls(self, index: int | None, mutate):
        """Count recheck os calls. After call ``index`` returns, run ``mutate`` once.

        ``mutate`` receives a dict it may fill with ``id`` and ``st`` so later hooked
        ``fstat`` results for that inode keep the pre-mutation stamp. The final name
        seal calls the functions captured at import, so it still sees the real inode.
        """

        counter = {"n": 0}
        events: list[str] = []
        guard = {"on": False}
        freeze: dict[str, object] = {"id": None, "st": None}

        def make(name: str):
            real = getattr(os, name)

            def wrapped(*args: object, **kwargs: object):
                if guard["on"]:
                    return real(*args, **kwargs)
                try:
                    result = real(*args, **kwargs)
                    err: BaseException | None = None
                except BaseException as exc:
                    result, err = None, exc
                frozen = freeze["st"]
                frozen_id = freeze["id"]
                if (err is None and name == "fstat" and frozen is not None
                        and frozen_id == (result.st_dev, result.st_ino)):
                    result = frozen
                if _stack_has("_recheck_pinned_hermes_config"):
                    seen = counter["n"]
                    counter["n"] += 1
                    events.append(name)
                    if index is not None and seen == index:
                        guard["on"] = True
                        try:
                            mutate(freeze)
                        finally:
                            guard["on"] = False
                if err is not None:
                    raise err
                return result

            return wrapped

        patches = [mock.patch.object(policy.os, name, side_effect=make(name)) for name in self._RECHECK_HOOKS]
        for patcher in patches:
            patcher.start()
        try:
            yield counter, events
        finally:
            for patcher in patches:
                patcher.stop()

    def _trace_recheck(self) -> list[str]:
        with self._hook_recheck_syscalls(None, lambda _freeze: None) as (_counter, events):
            self.launch("hermes")
        self._reset_hermes_tree()
        return events

    def _plant_new_config_inode(self, freeze: dict[str, object]) -> None:
        live = os.lstat(self.config())
        freeze["id"] = (live.st_dev, live.st_ino)
        freeze["st"] = _clone_stat(live)
        fd = os.open(self.root / "swapped-config.yaml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"# owner config, left in place\nmodel: theirs\n")
        finally:
            os.close(fd)
        os.rename(self.root / "swapped-config.yaml", self.config())
        self.assertNotEqual(live.st_ino, os.lstat(self.config()).st_ino)

    def _replace_hermes_dir(self, _freeze: dict[str, object]) -> None:
        aside = self.home / ".hermes.aside"
        os.rename(self.home / ".hermes", aside)
        self._private(self.home / ".hermes")
        fd = os.open(self.config(), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, b"auth:\n  adopt_external_logins: true \n")
        finally:
            os.close(fd)
        env = os.open(self.home / ".hermes" / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(env, b"HERMES_HOME=/not-the-route\n")
        finally:
            os.close(env)

    def _plant_named(self, name: str, payload: bytes):
        def plant(_freeze: dict[str, object]) -> None:
            fd = os.open(
                self.home / ".hermes" / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
            )
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        return plant

    def _assert_mutation_left_in_place(self, kind: str) -> None:
        if kind == "inode":
            self.assertEqual(b"# owner config, left in place\nmodel: theirs\n", self.config().read_bytes())
        elif kind == "hermes":
            self.assertEqual(ADOPT_YAML, (self.home / ".hermes.aside" / "config.yaml").read_bytes())
            self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        elif kind in (".env", ".op.env"):
            self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / kind).read_bytes())
        elif kind == "state.db":
            self.assertEqual(b"x", (self.home / ".hermes" / "state.db").read_bytes())
        elif kind == "pread":
            self.assertEqual(b"auth:\n  adopt_external_logins: true \n", self.config().read_bytes())
        elif kind == "home":
            self.assertTrue(self.home.is_symlink())
            self.assertEqual(ADOPT_YAML, (self.root / "route-home.real" / ".hermes" / "config.yaml").read_bytes())

    def test_new_config_inode_after_final_name_stat_refuses(self) -> None:
        # Index 32 on 0f1fa676 is the last stat of config.yaml. The fstat after it
        # compared device and inode only, so a same-tick replacement (nlink 0, ctime
        # unchanged) still launched. Hooked fstats keep that pre-unlink stamp.
        with self._hook_recheck_syscalls(32, self._plant_new_config_inode):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self._assert_mutation_left_in_place("inode")
        self.launcher.assert_not_called()

    def test_hermes_replaced_after_final_name_stat_refuses(self) -> None:
        # Index 30 on 0f1fa676 is the last stat of .hermes. Later config checks used
        # the directory fd opened before that stat, so the new directory still launched.
        with self._hook_recheck_syscalls(30, self._replace_hermes_dir):
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual(".hermes", exc.detail)
        self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
        self._assert_mutation_left_in_place("hermes")
        self.launcher.assert_not_called()

    def test_dotenv_after_pass_two_enoent_refuses(self) -> None:
        # Pass 2's absence stats are indices 23 (.env) and 24 (.op.env). The old final
        # phase never stat'd them again.
        for index, name in ((23, ".env"), (24, ".op.env"), (24, ".env")):
            with self.subTest(index=index, name=name):
                self._reset_hermes_tree()
                with self._hook_recheck_syscalls(index, self._plant_named(name, b"HERMES_HOME=/not-the-route\n")):
                    exc = self.refused("hermes_dotenv_in_home", self.launch, "hermes")
                self.assertEqual(name, exc.detail)
                self.assertNotIn("HERMES_HOME=/not-the-route", str(exc))
                self._assert_mutation_left_in_place(name)
                self.launcher.assert_not_called()

    def test_api_key_state_db_after_pass_two_listdir_refuses(self) -> None:
        # Index 22 is pass 2's listdir. The old final phase did not list again.
        with self._hook_recheck_syscalls(22, self._plant_named("state.db", b"x")):
            exc = self.refused("engine_store_in_route_home", self.launch, "hermes")
        self.assertEqual("state.db", exc.detail)
        self._assert_mutation_left_in_place("state.db")
        self.launcher.assert_not_called()

    def test_subscription_state_db_after_pass_two_listdir_still_launches(self) -> None:
        # Subscription extra names other than .env / .op.env stay allowed.
        with self._hook_recheck_syscalls(22, self._plant_named("state.db", b"x")):
            self.launch_sub()
        self.assertEqual(b"x", (self.home / ".hermes" / "state.db").read_bytes())
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(1, self.launcher.call_count)

    def test_mutation_after_every_recheck_syscall_except_the_final_pread_refuses(self) -> None:
        events = self._trace_recheck()
        self.assertEqual(("listdir", "stat", "stat"), (events[22], events[23], events[24]))
        self.assertGreater(len(events), 32)
        # os.* lookups end at the first re-verify's close. The final pread and the
        # seal call the functions captured at import, so they are not in this trace.
        self.assertEqual("close", events[-1])
        self.assertIn("pread", events)
        mutations = (
            ("inode", self._plant_new_config_inode, "hermes"),
            ("hermes", self._replace_hermes_dir, "hermes"),
            (".env", self._plant_named(".env", b"HERMES_HOME=/not-the-route\n"), "hermes"),
            (".op.env", self._plant_named(".op.env", b"HERMES_HOME=/not-the-route\n"), "hermes"),
            ("state.db", self._plant_named("state.db", b"x"), "hermes"),
        )
        for kind, mutate, _engine in mutations:
            for index in range(len(events) - 1):
                with self.subTest(kind=kind, index=index):
                    self._reset_hermes_tree()
                    with self._hook_recheck_syscalls(index, mutate):
                        with self.assertRaises(SubscriptionRouteRefused) as caught:
                            self.launch("hermes")
                    self.assertNotIn("HERMES_HOME=/not-the-route", str(caught.exception))
                    self._assert_mutation_left_in_place(kind)
                    self.launcher.assert_not_called()

    @contextmanager
    def _hook_extended(self, index: int | None, mutate, *, when: str = "after"):
        """Count every recheck syscall, including the functions captured at import.

        ``when="after"`` runs ``mutate`` once the indexed call has returned.
        ``when="before"`` runs it before that call. A patched ``os.pread`` lies only
        on the ``os.pread`` binding; ``_LIBC_PREAD`` stays the real read. Hooked
        ``fstat`` keeps a stamp ``mutate`` stored on the freeze dict, so a same-tick
        rewrite cannot refuse through ``ctime_ns`` alone.
        """

        counter = {"n": 0}
        events: list[str] = []
        guard = {"on": False}
        freeze: dict[str, object] = {"id": None, "st": None}
        reals = {name: getattr(os, name) for name, _attr in self._EXTENDED_HOOKS}

        def make(name: str, *, captured: bool):
            real = reals[name]

            def wrapped(*args: object, **kwargs: object):
                if guard["on"]:
                    return real(*args, **kwargs)
                seen: int | None = None
                if _stack_has("_recheck_pinned_hermes_config"):
                    seen = counter["n"]
                    counter["n"] += 1
                    events.append(name)
                    if index is not None and seen == index and when == "before":
                        guard["on"] = True
                        try:
                            mutate(freeze)
                        finally:
                            guard["on"] = False
                if name == "pread" and not captured and getattr(self, "_pread_lie", False):
                    result, err = ADOPT_YAML, None
                else:
                    try:
                        result = real(*args, **kwargs)
                        err = None
                    except BaseException as exc:
                        result, err = None, exc
                frozen = freeze["st"]
                frozen_id = freeze["id"]
                if (err is None and name == "fstat" and frozen is not None
                        and frozen_id == (result.st_dev, result.st_ino)):
                    result = frozen
                if (seen is not None and index is not None and seen == index and when == "after"):
                    guard["on"] = True
                    try:
                        mutate(freeze)
                    finally:
                        guard["on"] = False
                if err is not None:
                    raise err
                return result

            return wrapped

        patchers = []
        for name, attr in self._EXTENDED_HOOKS:
            patchers.append(mock.patch.object(policy.os, name, side_effect=make(name, captured=False)))
            if hasattr(policy, attr):
                patchers.append(mock.patch.object(
                    policy, attr, side_effect=make(name, captured=True),
                ))
        for patcher in patchers:
            patcher.start()
        try:
            yield counter, events
        finally:
            for patcher in patchers:
                patcher.stop()

    def _trace_extended(self, launch=None) -> list[str]:
        if launch is None:
            launch = lambda: self.launch("hermes")
        with self._hook_extended(None, lambda _freeze: None) as (_counter, events):
            launch()
        self._reset_hermes_tree()
        return events

    def _final_pread(self, events: list[str]) -> int:
        return max(index for index, name in enumerate(events) if name == "pread")

    def _nth_after(self, events: list[str], start: int, name: str, nth: int) -> int | None:
        seen = 0
        for index in range(start + 1, len(events)):
            if events[index] == name:
                seen += 1
                if seen == nth:
                    return index
        return None

    def _last_after(self, events: list[str], start: int, name: str) -> int | None:
        found: int | None = None
        for index in range(start + 1, len(events)):
            if events[index] == name:
                found = index
        return found

    def _seal_observer(self, events: list[str], final_pread: int, kind: str) -> int:
        """Index the seal uses to see ``kind``. Absent on a build with no post-pread seal.

        The range is every index strictly before that call, which includes the final
        pread and the seal steps that run before the check. A missing check means every
        index, including the final pread, is still a hole.
        """

        if kind == "pread":
            return final_pread + 1
        if kind in (".env", ".op.env", "state.db"):
            found = self._nth_after(events, final_pread, "listdir", 1)
        elif kind == "hermes":
            # HOME is the first lstat. The .hermes name is the second.
            found = self._nth_after(events, final_pread, "lstat", 2)
        elif kind == "home":
            found = self._nth_after(events, final_pread, "lstat", 1)
        elif kind == "inode":
            found = self._last_after(events, final_pread, "lstat")
        else:
            found = None
        return len(events) if found is None else found

    def _symlink_home(self, _freeze: dict[str, object]) -> None:
        real = self.root / "route-home.real"
        if self.home.is_symlink():
            return
        os.rename(self.home, real)
        os.symlink(real, self.home)

    def _lie_about_pread(self, freeze: dict[str, object]) -> None:
        # Same inode, same size, mtime restored. A patched os.pread returns the pin.
        # The captured pread reads these bytes.
        live = os.lstat(self.config())
        freeze["id"] = (live.st_dev, live.st_ino)
        freeze["st"] = _clone_stat(live)
        evil = b"auth:\n  adopt_external_logins: true \n"
        fd = os.open(self.config(), os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
        try:
            self.assertEqual(len(evil), os.write(fd, evil))
        finally:
            os.close(fd)
        os.utime(self.config(), ns=(live.st_atime_ns, live.st_mtime_ns))
        self._pread_lie = True

    def _refuse_at(self, index: int, mutate, *, when: str) -> None:
        with self._hook_extended(index, mutate, when=when):
            with self.assertRaises(SubscriptionRouteRefused) as caught:
                self.launch("hermes")
        self.assertNotIn("HERMES_HOME=/not-the-route", str(caught.exception))
        self.launcher.assert_not_called()

    def test_attacks_at_every_extended_index_including_after_pread_refuse(self) -> None:
        # (a) plant .env / .op.env / state.db after the captured listdir.
        # (b) replace .hermes after the last directory fstat.
        # (c) replace config.yaml between the final fstat and pread, and after pread.
        # (d) a patched os.pread returns the pin while the inode bytes changed.
        # (e) HOME becomes a symlink as the recheck runs.
        # The seal is the last look, so each of these refuses at every index before
        # the seal call that sees it, including the final pread and the calls after it.
        events = self._trace_extended()
        final_pread = self._final_pread(events)
        if len(events) == final_pread + 1:
            # Nothing runs after pread. A name planted as that read returns must still refuse.
            self._reset_hermes_tree()
            self._refuse_at(
                final_pread, self._plant_named(".env", b"HERMES_HOME=/not-the-route\n"), when="after",
            )
        self.assertGreater(len(events), final_pread + 1)
        self.assertIn("lstat", events[final_pread + 1:])
        self.assertIn("listdir", events[final_pread + 1:])
        attacks = (
            (".env", self._plant_named(".env", b"HERMES_HOME=/not-the-route\n"), "after"),
            (".op.env", self._plant_named(".op.env", b"HERMES_HOME=/not-the-route\n"), "after"),
            ("state.db", self._plant_named("state.db", b"x"), "after"),
            ("hermes", self._replace_hermes_dir, "after"),
            ("inode", self._plant_new_config_inode, "after"),
            ("home", self._symlink_home, "after"),
            ("pread", self._lie_about_pread, "before"),
        )
        for kind, mutate, when in attacks:
            observer = self._seal_observer(events, final_pread, kind)
            self.assertGreater(observer, final_pread)
            for index in range(observer):
                with self.subTest(kind=kind, index=index, when=when):
                    self._reset_hermes_tree()
                    self._refuse_at(index, mutate, when=when)
                    self._assert_mutation_left_in_place(kind)

    def test_editor_rename_of_identical_bytes_then_a_new_launch_succeeds(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        tmp = self.home / ".hermes" / "config.yaml.editor"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            self.assertEqual(len(ADOPT_YAML), os.write(fd, ADOPT_YAML))
        finally:
            os.close(fd)
        os.rename(tmp, self.config())
        self.launch("hermes")
        self.assertEqual(1, self.launcher.call_count)
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertFalse(tmp.exists())

    def test_steady_extra_hard_link_still_launches(self) -> None:
        self.launch("hermes")
        self.launcher.reset_mock()
        alias = self.home / "config.yaml.alias"
        os.link(self.config(), alias)
        self.assertGreaterEqual(os.lstat(self.config()).st_nlink, 2)
        self.launch("hermes")
        self.assertEqual(1, self.launcher.call_count)
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(os.lstat(alias).st_ino, os.lstat(self.config()).st_ino)

    def test_different_st_dev_on_the_sealed_names_refuses(self) -> None:
        # A bind mount of another filesystem over the path changes st_dev. The seal's
        # lstat must refuse that. Patching os.lstat does not reach the captured lstat.
        real = os.lstat

        def bump(path: object, *args: object, **kwargs: object):
            st = real(path, *args, **kwargs)
            if path == str(self.home) or path == "config.yaml":
                return os.stat_result((*st[:2], st.st_dev + 1, *st[3:]))
            return st

        captured = getattr(policy, "_LIBC_LSTAT", None)
        if captured is None:
            patcher = mock.patch.object(policy.os, "lstat", side_effect=bump)
        else:
            patcher = mock.patch.object(policy, "_LIBC_LSTAT", side_effect=bump)
        with patcher:
            self.refused("route_home_not_private", self.launch, "hermes")
        self.launcher.assert_not_called()

        def bump_config(path: object, *args: object, **kwargs: object):
            st = real(path, *args, **kwargs)
            if path == "config.yaml":
                return os.stat_result((*st[:2], st.st_dev + 1, *st[3:]))
            return st

        self._reset_hermes_tree()
        if captured is None:
            patcher = mock.patch.object(policy.os, "lstat", side_effect=bump_config)
        else:
            patcher = mock.patch.object(policy, "_LIBC_LSTAT", side_effect=bump_config)
        with patcher:
            exc = self.refused("hermes_config_blocks_adopt_override", self.launch, "hermes")
        self.assertEqual("config.yaml", exc.detail)
        self.launcher.assert_not_called()

    def test_seal_does_not_leak_a_directory_fd(self) -> None:
        def fds() -> set[str]:
            return set(os.listdir("/proc/self/fd"))

        before = fds()
        self.launch("hermes")
        self.assertEqual(before, fds())
        self.launcher.reset_mock()
        events = self._trace_extended()
        final_pread = self._final_pread(events)
        before = fds()
        with self._hook_extended(final_pread, self._plant_named(".env", b"HERMES_HOME=/not-the-route\n")):
            self.refused("hermes_dotenv_in_home", self.launch, "hermes")
        self.assertEqual(before, fds())
        self.assertEqual(b"HERMES_HOME=/not-the-route\n", (self.home / ".hermes" / ".env").read_bytes())
        self.launcher.assert_not_called()

    def test_subscription_state_db_after_final_pread_still_launches(self) -> None:
        events = self._trace_extended(self.launch_sub)
        final_pread = self._final_pread(events)
        with self._hook_extended(final_pread, self._plant_named("state.db", b"x")):
            self.launch_sub()
        self.assertEqual(b"x", (self.home / ".hermes" / "state.db").read_bytes())
        self.assertEqual(ADOPT_YAML, self.config().read_bytes())
        self.assertEqual(1, self.launcher.call_count)

    def test_thirty_honest_api_key_and_subscription_launches(self) -> None:
        for kind, launch in (("api_key", lambda: self.launch("hermes")), ("subscription", self.launch_sub)):
            with self.subTest(kind=kind):
                self._reset_hermes_tree()
                for _ in range(30):
                    launch()
                self.assertEqual(30, self.launcher.call_count)
                self.assertEqual(ADOPT_YAML, self.config().read_bytes())
                self.assertEqual(["config.yaml"], sorted(os.listdir(self.home / ".hermes")))


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
                               ("route_home_fd", 3), ("model", "anthropic/other"), ("engine_config", ())):
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
            for name in ("env", "shell", "executable", "preexec_fn", "close_fds", "model", "engine_config",
                          "route_home_fd"):
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
                expected = {"env", "shell", "engine_config", "model"}
                if engine == "hermes":
                    expected.add("route_home_fd")
                self.assertEqual(expected, set(self.launcher.call_args.kwargs))
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
