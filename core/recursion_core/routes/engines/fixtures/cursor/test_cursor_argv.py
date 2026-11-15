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

    def test_every_spawn_disables_the_updater_first(self):
        for argv in (cursor.oneshot_argv("m"), cursor.acp_argv(), cursor.oneshot_argv("m", binary="/x/agent")):
            self.assertEqual(argv[1], "--disable-auto-update")

    def test_argv0_is_the_default_command_or_the_given_binary_and_nothing_else(self):
        self.assertEqual(cursor.DEFAULT_EXECUTABLE, "agent")
        self.assertEqual(cursor.oneshot_argv("m")[0], "agent")
        self.assertEqual(cursor.oneshot_argv("m", binary="/opt/c/agent")[0], "/opt/c/agent")

    def test_a_binary_must_be_an_absolute_path(self):
        for binary in ("agent", "./agent", "", 3, "/opt/a\x00b", "/opt/a\nb", "/opt/a\rb"):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.acp_argv(binary=binary)
            self.assertEqual(caught.exception.detail, "binary_not_absolute")

    def test_a_binary_must_be_a_plain_path_to_a_file(self):
        """Break-it read of #388, F9: a directory or an unnormalised path is not the vouched executable."""
        for binary in ("/", "/..", "/.", "//x", "/opt/cursor/", "/opt/./agent", "/opt/../agent", "/opt//agent"):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.acp_argv(binary=binary)
            self.assertEqual(caught.exception.detail, "binary_not_absolute", binary)
        self.assertEqual(cursor.acp_argv(binary="/opt/cursor/bin/agent.v2")[0], "/opt/cursor/bin/agent.v2")

    def test_a_model_that_could_be_read_as_an_option_is_refused(self):
        for model in ("", "-p", "--api-key", "m\nx", "m\rx", "m\x00", None, 4):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.oneshot_argv(model)
            self.assertEqual(caught.exception.detail, "model_invalid")

    def test_an_ordinary_model_name_is_taken_as_given(self):
        self.assertEqual(cursor.oneshot_argv("gpt-5.1 codex")[-1], "gpt-5.1 codex")


class CheckTest(unittest.TestCase):
    def test_the_updater_flag_stands_first(self):
        """Break-it read of #388, F1, in its stricter form (questions.md Q18): argv[1], on both drives."""
        for argv in (["agent", "acp", "--disable-auto-update"], ["agent", "acp", "--disable-auto-update", "x"],
                     ["agent", "-p", "--model", "m", "--disable-auto-update"],
                     ["agent", "-p", "--disable-auto-update", "--model", "m"], ["agent"]):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertEqual(caught.exception.detail, "updater_not_disabled", argv)
        cursor.check_argv(["agent", "--disable-auto-update", "acp"])
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--model", "m"])
        cursor.check_argv(("agent", "--disable-auto-update", "acp"))

    def test_a_credential_under_bundled_short_options_is_refused(self):
        """Break-it read of #388, F2: ``-vH V`` is ``-v`` and ``-H V``."""
        for bundle in ("-vH", "-pH", "-Hv", "-vpH"):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(["agent", "--disable-auto-update", bundle, "Authorization: Bearer made-up", "acp"])
            self.assertEqual(caught.exception.detail, "credential_in_argv:-H", bundle)

    def test_full_access_needs_its_acknowledgement(self):
        """Break-it read of #388, F3: refused unless the run's full access was acknowledged."""
        cases = {("--force",): "--force", ("--force=true",): "--force", ("--yolo",): "--yolo",
                 ("--approve-mcps",): "--approve-mcps", ("-f",): "-f", ("-pf",): "-f",
                 ("--dangerously-skip-permissions",): "--dangerously-skip-permissions",
                 ("--auto-approve",): "--auto-approve",
                 ("--sandbox", "disabled"): "--sandbox", ("--sandbox=disabled",): "--sandbox"}
        for extra, option in cases.items():
            argv = ["agent", "--disable-auto-update", "-p", *extra, "--", "say hi"]
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertEqual(caught.exception.detail, f"full_access_not_acknowledged:{option}", extra)
            cursor.check_argv(argv, full_access_acknowledged=True)
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--sandbox", "enabled", "--", "--force"])
        for credential in (["--api-key", "k"], ["-vH", "X: y"], ["--header=X: y"]):   # never acknowledged away
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(["agent", "--disable-auto-update", *credential, "--force"],
                                  full_access_acknowledged=True)
            self.assertTrue(caught.exception.detail.startswith("credential_in_argv:"), credential)

    def test_only_the_bool_true_acknowledges_full_access(self):
        """Review 2 of #388, change 1 (both reads): as the existing guards rule, ``"true"``, ``1`` and the like
        are not an acknowledgement, and a malformed one is refused whatever the argv."""
        argv = ["agent", "--disable-auto-update", "-p", "--force", "--", "say hi"]
        cursor.check_argv(argv, full_access_acknowledged=True)
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(argv, full_access_acknowledged=False)
        self.assertEqual(caught.exception.detail, "full_access_not_acknowledged:--force")
        for value in (1, 0, "true", "false", "", [True], {"ack": True}, None, 1.0):
            for target in (argv, cursor.acp_argv()):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(target, full_access_acknowledged=value)
                self.assertEqual(caught.exception.detail, "full_access_malformed", value)

    def test_the_argument_limit_counts_utf8_bytes(self):
        """Review 2 of #388, change 4 (Astra) / change 3 (Sol): two-byte characters at the boundary."""
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "\u00e9" * 65535])   # 131,070 bytes
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "\u00e9" * 65536])   # 131,072 bytes
        self.assertEqual(caught.exception.detail, "argv_invalid")

    def test_an_argument_that_cannot_be_passed_is_refused(self):
        """Break-it read of #388, F8: empty, over the one-argument limit, or a line break among the options."""
        self.assertEqual(cursor.MAX_ARG_BYTES, 131072 - 1)
        for argv in (["agent", "--disable-auto-update", "", "acp"],
                     ["agent", "--disable-auto-update", "-p", "--", "x" * 131072],
                     ["agent", "--disable-auto-update", "--model", "a\nb", "-p"],
                     ["agent", "--disable-auto-update", "--model", "a\rb", "-p"]):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertEqual(caught.exception.detail, "argv_invalid")
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "x" * 131071])
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "line one\nline two\r\n"])

    def test_the_updater_flag_counts_only_among_the_options(self):
        """Review 1 of #388, R1 (both reads): after ``--`` it is positional text, not the option."""
        for argv in (["agent", "-p", "--", "--disable-auto-update"],
                     ["agent", "-p", "--output-format", "stream-json", "--model", "m", "--", "--disable-auto-update"]):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertEqual(caught.exception.detail, "updater_not_disabled")
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "say hi"])
        cursor.check_argv([*cursor.oneshot_argv("m"), "--", "say hi"])

    def test_what_follows_the_double_dash_is_the_prompt_not_options(self):
        self.assertEqual(len(ARGV["accepted"]), 3)
        for argv in ARGV["accepted"]:
            cursor.check_argv(argv)
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["agent", "--api-key", "k", "--", "x", "--disable-auto-update"])
        self.assertEqual(caught.exception.detail, "credential_in_argv:--api-key")

    def test_an_argument_with_a_nul_is_refused(self):
        for argv in (["agent\x00", "--disable-auto-update", "acp"], ["agent", "--disable-auto-update", "--", "a\x00"]):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(argv)
            self.assertEqual(caught.exception.detail, "argv_invalid")

    def test_every_refused_argv_of_the_fixture(self):
        self.assertEqual(len(ARGV["refused"]), 21)
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
