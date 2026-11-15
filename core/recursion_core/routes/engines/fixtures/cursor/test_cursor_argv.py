# Written by Claude Opus 5.5 (claude-opus-5-5), builder lane ENGINE-E6, for Lucas.
"""The drives' argv (routes/engines/cursor.py): the updater flag on every spawn, no credential in argv, and
argv[0] the vouched executable (the binary option composition gave, else ``agent``)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recursion_core.routes import contract
from recursion_core.routes.engines import cursor

HERE = Path(__file__).resolve().parent
ARGV = json.loads((HERE / "argv.json").read_text(encoding="utf-8"))


class BuildTest(unittest.TestCase):
    def test_the_one_shot_argv(self):
        self.assertEqual(cursor.oneshot_argv(ARGV["oneshot"]["model"]), ARGV["oneshot"]["argv"])

    def test_the_one_shot_argv_with_the_pinned_binary(self):
        case = ARGV["oneshot_pinned"]
        self.assertEqual(cursor.oneshot_argv(case["model"], binary=case["binary"]), case["argv"])

    def test_the_acp_argv_carries_the_updater_flag_before_the_command(self):
        self.assertEqual(cursor.acp_argv(), ARGV["acp"]["argv"])
        self.assertEqual(cursor.acp_argv(binary="/opt/c/agent"), ["/opt/c/agent", "--disable-auto-update", "acp"])

    def test_every_built_argv_passes_the_check(self):
        cursor.check_argv(cursor.oneshot_argv("m"))
        cursor.check_argv(cursor.acp_argv())
        cursor.check_argv(cursor.oneshot_argv("m", binary="/opt/c/agent"), binary="/opt/c/agent")
        cursor.check_argv(cursor.acp_argv(binary="/opt/c/agent"), binary="/opt/c/agent")

    def test_every_spawn_disables_the_updater(self):
        for argv in (cursor.oneshot_argv("m"), cursor.acp_argv(), cursor.oneshot_argv("m", binary="/x/agent")):
            self.assertIn("--disable-auto-update", argv[1:])

    def test_argv0_is_the_default_command_or_the_given_binary_and_nothing_else(self):
        self.assertEqual(cursor.DEFAULT_EXECUTABLE, "agent")
        self.assertEqual(cursor.oneshot_argv("m")[0], "agent")
        self.assertEqual(cursor.oneshot_argv("m", binary="/opt/c/agent")[0], "/opt/c/agent")

    def test_a_binary_must_be_an_absolute_path(self):
        for binary in ("agent", "./agent", "", 3, "/opt/a\x00b", "/opt/a\nb"):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.acp_argv(binary=binary)
            self.assertEqual(caught.exception.detail, "binary_not_absolute")

    def test_a_model_that_could_be_read_as_an_option_is_refused(self):
        for model in ("", "-p", "--api-key", "m\nx", "m\x00", None, 4):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.oneshot_argv(model)
            self.assertEqual(caught.exception.detail, "model_invalid")


class CheckTest(unittest.TestCase):
    def test_every_refused_argv_of_the_fixture(self):
        self.assertEqual(len(ARGV["refused"]), 9)
        for case in ARGV["refused"]:
            with self.subTest(argv=case["argv"]):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(case["argv"])
                self.assertEqual(caught.exception.detail, case["detail"])

    def test_the_refusal_is_the_route_contracts_launch_refused(self):
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["agent", "acp"])
        self.assertEqual(caught.exception.code, contract.LAUNCH_REFUSED)
        self.assertIn(caught.exception.code, contract.CODES)

    def test_argv0_must_be_the_vouched_executable(self):
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(cursor.acp_argv(), binary="/opt/c/agent")
        self.assertEqual(caught.exception.detail, "executable_mismatch")
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["/usr/bin/agent", "--disable-auto-update", "acp"])
        self.assertEqual(caught.exception.detail, "executable_mismatch")

    def test_a_credential_is_refused_wherever_it_stands(self):
        for extra in (["--api-key", "k"], ["--api-key=k"], ["--auth-token", "t"], ["--auth-token=t"],
                      ["-H", "X: y"], ["-HX: y"], ["--header", "X: y"], ["--header=X: y"]):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv([*cursor.oneshot_argv("m"), *extra])
            self.assertTrue(caught.exception.detail.startswith("credential_in_argv:"))
            with self.assertRaises(cursor.ArgvRefused):
                cursor.check_argv(["agent", *extra, "--disable-auto-update", "acp"])

    def test_options_that_only_look_alike_are_not_refused(self):
        cursor.check_argv(["agent", "--disable-auto-update", "--api-keys-doc", "acp"])
        cursor.check_argv(["agent", "--disable-auto-update", "--headers-only", "acp"])

    def test_the_updater_flag_must_be_an_option_not_argv0(self):
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["--disable-auto-update"], binary=None)
        self.assertEqual(caught.exception.detail, "executable_mismatch")

    def test_an_argv_that_is_not_a_list_of_text_is_refused(self):
        for argv in (None, "agent acp", [], ["agent", 3], ("agent",)):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertIn(caught.exception.detail, ("argv_invalid", "updater_not_disabled"))
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv("agent acp")
        self.assertEqual(caught.exception.detail, "argv_invalid")


if __name__ == "__main__":
    unittest.main()
