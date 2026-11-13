"""2026-11-09 wrapper pins: Pi/Hermes/Cursor package pins, Antigravity binary pin.

Fakes only. No engine binary is executed. T3 Code has no route here.
"""

from __future__ import annotations

import hashlib
import os
import unittest
from unittest import mock

from recursion_core.subscription_routes import (
    SubscriptionRouteRefused,
    WrapperPinRefused,
    check_antigravity_route,
    check_cursor_route,
    check_route,
    guard_cursor_then_spawn,
    measure_antigravity_pin,
    measure_package_pin,
)
from recursion_core.subscription_routes.tests.test_subscription_routes import _Base

_MIB = 1024 * 1024
_GIB = 1024 * _MIB


@unittest.skipIf(os.name == "nt", "POSIX home checks")
class WrapperPinTests(_Base):
    def bare(self) -> dict:
        return {"PATH": "/usr/bin:/bin", "HOME": str(self.home)}

    def test_cursor_matches_as_beta_and_a_changed_file_refuses(self) -> None:
        decision = check_cursor_route(self.bare(), route_home=str(self.home), wrapper=self.wrapper,
                                      parent_env=self.parent(), operator_home=str(self.operator))
        self.assertTrue(decision.beta)
        self.assertEqual(decision.auth, "wrapper")
        self.assertEqual(dict(decision.child_env), self.bare())
        (self.install / "extra.js").write_text("extra\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_cursor_route(self.bare(), route_home=str(self.home), wrapper=self.wrapper,
                               parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "wrapper_pin_mismatch")

    def test_cursor_refuses_a_claude_subscription_token(self) -> None:
        env = self.bare()
        env["ANTHROPIC_OAUTH_TOKEN"] = "fake-oauth-token-not-a-credential"
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            guard_cursor_then_spawn(mock.Mock(), [self.wrapper.launcher_path], env, route_home=str(self.home), wrapper=self.wrapper,
                                    parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "claude_subscription_env")

    def test_opencode_refuses_a_package_pin(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("opencode", self.profile(), self.env(), route_home=str(self.home),
                        parent_env=self.parent(), operator_home=str(self.operator), wrapper=self.wrapper)
        self.assertEqual(caught.exception.cause, "wrapper_pin_not_applicable")

    def test_pi_without_a_pin_refuses(self) -> None:
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_route("pi", self.profile(), self.env(), route_home=str(self.home),
                        parent_env=self.parent(), operator_home=str(self.operator))
        self.assertEqual(caught.exception.cause, "wrapper_pin_missing")

    def test_antigravity_pin_is_version_binary_and_launcher_not_package_files(self) -> None:
        binary = self.install / "agy"
        launcher = self.install / "agy-launch"
        binary.write_text("agy-binary\n", encoding="ascii")
        launcher.write_text("agy-launch\n", encoding="ascii")
        pin = measure_antigravity_pin("1.2.3", str(binary), str(launcher))
        # A package file next to agy is not part of the pin.
        (self.install / "ignored.js").write_text("ignored\n", encoding="ascii")
        decision = check_antigravity_route(
            self.bare(), version="1.2.3", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
            route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
        )
        self.assertTrue(decision.beta)
        self.assertEqual(decision.pin_version, "1.2.3")
        self.assertEqual(decision.engine, "antigravity")
        binary.write_text("agy-binary-2\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="1.2.3", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_pin_mismatch", "binary"))
        binary.write_text("agy-binary\n", encoding="ascii")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="9.9.9", binary_path=str(binary), launcher_path=str(launcher), pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual(caught.exception.detail, "version")

    def test_measure_package_pin_changes_when_the_launcher_changes(self) -> None:
        before = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        (self.install / "launcher.js").write_text("launcher-v2\n", encoding="ascii")
        after = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        self.assertNotEqual(before.launcher_sha256, after.launcher_sha256)
        self.assertNotEqual(before.package_files, after.package_files)

    def sparse(self, name: str, size: int) -> str:
        # truncate leaves a hole: the file reads as zero bytes and uses no disk blocks.
        path = self.install / name
        with open(path, "wb") as handle:
            handle.truncate(size)
        return str(path)

    def small_launcher(self) -> str:
        launcher = self.install / "agy-launch"
        launcher.write_text("agy-launch\n", encoding="ascii")
        return str(launcher)

    def test_antigravity_measures_a_binary_over_the_package_file_cap(self) -> None:
        binary = self.sparse("agy", 40 * _MIB)
        pin = measure_antigravity_pin("1.5.5", binary, self.small_launcher())
        expected = hashlib.sha256()
        for _ in range(40):
            expected.update(bytes(_MIB))
        self.assertEqual(pin.binary_sha256, expected.hexdigest())
        self.assertEqual(pin.launcher_sha256, hashlib.sha256(b"agy-launch\n").hexdigest())

    def test_antigravity_refuses_a_binary_over_one_gib(self) -> None:
        binary = self.sparse("agy", _GIB + 1)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", binary, self.small_launcher())
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "binary"))

    def test_antigravity_refuses_a_launcher_over_one_gib(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-binary\n", encoding="ascii")
        launcher = self.sparse("agy-launch", _GIB + 1)
        with self.assertRaises(WrapperPinRefused) as caught:
            measure_antigravity_pin("1.5.5", str(binary), launcher)
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_file_too_large", "launcher"))

    def test_package_file_over_the_old_cap_measures(self) -> None:
        self.sparse("big.bin", 40 * _MIB)
        pin = measure_package_pin(str(self.install / "launcher.js"), str(self.install))
        expected = hashlib.sha256()
        for _ in range(40):
            expected.update(bytes(_MIB))
        self.assertEqual(dict(pin.package_files)["big.bin"], expected.hexdigest())

    def test_a_self_update_of_the_pinned_binary_fails_the_pin(self) -> None:
        binary = self.install / "agy"
        binary.write_text("agy-1.5.5\n", encoding="ascii")
        launcher = self.small_launcher()
        pin = measure_antigravity_pin("1.5.5", str(binary), launcher)
        # agy replaces its own bytes in place: same path, same stated version.
        with open(binary, "r+b") as handle:
            handle.write(b"agy-1.5.6\n")
        with self.assertRaises(SubscriptionRouteRefused) as caught:
            check_antigravity_route(
                self.bare(), version="1.5.5", binary_path=str(binary), launcher_path=launcher, pin=pin,
                route_home=str(self.home), parent_env=self.parent(), operator_home=str(self.operator),
            )
        self.assertEqual((caught.exception.cause, caught.exception.detail), ("wrapper_pin_mismatch", "binary"))


if __name__ == "__main__":
    unittest.main()
