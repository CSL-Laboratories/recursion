"""2026-11-09 wrapper pins: Pi/Hermes/Cursor package pins, Antigravity binary pin.

Fakes only. No engine binary is executed. T3 Code has no route here.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from recursion_core.subscription_routes import (
    SubscriptionRouteRefused,
    check_antigravity_route,
    check_cursor_route,
    check_route,
    guard_cursor_then_spawn,
    measure_antigravity_pin,
    measure_package_pin,
)
from recursion_core.subscription_routes.tests.test_subscription_routes import _Base


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


if __name__ == "__main__":
    unittest.main()
