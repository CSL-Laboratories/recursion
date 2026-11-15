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


def argv_cost(argv) -> int:
    """What the whole-argv bound counts: each argument's UTF-8 bytes, its NUL and its 8-byte pointer."""
    return sum(len(a.encode("utf-8")) + 1 + 8 for a in argv)


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

    def test_an_argument_with_a_lone_surrogate_is_refused(self):
        """Break-it read 5 of #388, F5-1: UTF-8 cannot write a lone surrogate, so execve could not be given it
        (Python raises UnicodeEncodeError). Refused wherever it stands, the escape range ``U+DC80``..``U+DCFF``
        included, in an argument, the model or the binary."""
        for code in (0xD800, 0xDBFF, 0xDC00, 0xDC80, 0xDFFF):
            lone = chr(code)
            for argv in (["agent", "--disable-auto-update", lone, "acp"],
                         ["agent", "--disable-auto-update", "-p", "--model", "m" + lone],
                         ["agent", "--disable-auto-update", "-p", "--", "say " + lone],
                         ["agent" + lone, "--disable-auto-update", "acp"]):
                with self.subTest(code=hex(code), argv=argv):
                    with self.assertRaises(cursor.ArgvRefused) as caught:
                        cursor.check_argv(argv)
                    self.assertEqual(caught.exception.detail, "argv_invalid")
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.oneshot_argv("m" + lone)
            self.assertEqual(caught.exception.detail, "model_invalid")
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.acp_argv(binary="/opt/" + lone + "/agent")
            self.assertEqual(caught.exception.detail, "binary_not_absolute")
        astral = chr(0x1F600)   # a character beyond the BMP is text UTF-8 writes: taken
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--", "say " + astral])
        self.assertEqual(cursor.oneshot_argv("m" + astral)[-1], "m" + astral)
        self.assertEqual(cursor.acp_argv(binary="/opt/" + astral + "/agent")[0], "/opt/" + astral + "/agent")

    def test_the_whole_argv_is_bounded(self):
        """Break-it read 5 of #388, F5-2, and 7, F7-2: execve also bounds the argv as a whole (ARG_MAX), and
        charges each argument its bytes, its NUL and its 8-byte pointer. Exactly at MAX_ARGV_BYTES passes; one
        byte more is refused."""
        self.assertEqual(cursor.MAX_ARGV_BYTES, 1024 * 1024)
        self.assertEqual(cursor.ARG_POINTER_BYTES, 8)
        head = ["agent", "--disable-auto-update", "-p", "--"]
        room = cursor.MAX_ARGV_BYTES - argv_cost(head)
        tail = []
        while room:
            size = min(cursor.MAX_ARG_BYTES, room - 9)
            self.assertGreater(size, 0)
            tail.append("x" * size)
            room -= size + 9
        argv = head + tail
        self.assertEqual(argv_cost(argv), cursor.MAX_ARGV_BYTES)
        self.assertTrue(all(len(a) <= cursor.MAX_ARG_BYTES for a in argv))
        cursor.check_argv(argv)
        for longer in (argv[:-1] + [argv[-1] + "x"], argv + ["x"], head + ["x" * cursor.MAX_ARG_BYTES] * 20):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.check_argv(longer)
            self.assertEqual(caught.exception.detail, "argv_invalid")

    def test_the_whole_argv_bound_counts_each_arguments_pointer(self):
        """Break-it read 7 of #388, F7-2: 250,000 one-byte arguments are 500,034 bytes with their NULs but two
        million more in pointers, and execve refuses them under the default stack. Each argument costs its
        bytes, its NUL and 8 bytes of pointer, so one-byte arguments fill the bound exactly at 104,851 after the
        head, and one more is refused."""
        head = ["agent", "--disable-auto-update", "-p", "--"]
        self.assertEqual(argv_cost(head), 66)
        many = head + ["x"] * 250000
        self.assertLess(sum(len(a) + 1 for a in many), cursor.MAX_ARGV_BYTES)
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(many)
        self.assertEqual(caught.exception.detail, "argv_invalid")
        exact = head + ["x"] * 104851
        self.assertEqual(66 + 10 * 104851, cursor.MAX_ARGV_BYTES)
        cursor.check_argv(exact)
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(exact + ["x"])
        self.assertEqual(caught.exception.detail, "argv_invalid")

    def test_every_argv_invalid_case_comes_before_executable_mismatch(self):
        """Break-it read 7 of #388, F7-1: the docstring lists every ``argv_invalid`` case, a line break among
        the options included, before ``executable_mismatch``, and the code checks them in that order."""
        for argv in (["wrong", "--disable-auto-update", "-p", "--model", "a\nb"],
                     ["wrong", "--disable-auto-update", "-p", "--model", "a\rb"],
                     ["wrong", "--disable-auto-update", "", "acp"]):
            with self.subTest(argv=argv):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(argv)
                self.assertEqual(caught.exception.detail, "argv_invalid")
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["wrong", "--disable-auto-update", "-p", "--", "a\nb"])   # the prompt may break lines
        self.assertEqual(caught.exception.detail, "executable_mismatch")
        doc = cursor.check_argv.__doc__
        self.assertLess(doc.index("* ``argv_invalid``"), doc.index("* ``executable_mismatch``"))

    def test_the_whole_argv_bound_counts_utf8_bytes_not_characters(self):
        """Review 6 of #388 (Astra change 1, Sol change 1): each argument 120,000 UTF-8 bytes, within its own
        limit. Eight pass; nine are over 1 MiB in bytes, though well under it in characters, and are refused.
        Both reads' forms: two-byte characters (Sol) and four-byte characters (Astra)."""
        head = ["agent", "--disable-auto-update", "-p", "--"]
        for part in (chr(0xE9) * 60000, chr(0x1F642) * 30000):
            with self.subTest(width=len(part[0].encode("utf-8"))):
                self.assertEqual(len(part.encode("utf-8")), 120000)
                self.assertLess(len(part.encode("utf-8")), cursor.MAX_ARG_BYTES)
                cursor.check_argv(head + [part] * 8)
                nine = head + [part] * 9
                self.assertLess(sum(len(a) + 1 for a in nine), cursor.MAX_ARGV_BYTES)
                self.assertGreater(sum(len(a.encode("utf-8")) + 1 for a in nine), cursor.MAX_ARGV_BYTES)
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(nine)
                self.assertEqual(caught.exception.detail, "argv_invalid")

    def test_the_bound_holds_only_over_the_argv_it_is_given(self):
        """Break-it read 6 of #388, F6-1: the builder's argv stops before the prompt, so the driver checks the
        complete argv, ``--`` and the prompt included. The builder's part alone passes; with a prompt that takes
        it over MAX_ARGV_BYTES it is refused."""
        built = cursor.oneshot_argv("synthetic-model")
        cursor.check_argv(built)
        prompts = []
        room = cursor.MAX_ARGV_BYTES - argv_cost([*built, "--"]) + 1   # one byte over, each within its own limit
        while room > 0:
            size = max(1, min(cursor.MAX_ARG_BYTES, room - 9))
            prompts.append("p" * size)
            room -= size + 9
        complete = [*built, "--", *prompts]
        self.assertGreater(argv_cost(complete), cursor.MAX_ARGV_BYTES)
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(complete)
        self.assertEqual(caught.exception.detail, "argv_invalid")
        self.assertIn("complete argv", cursor.check_argv.__doc__)

    def test_the_drive_stands_right_after_the_updater_flag(self):
        """Break-it read 5 of #388, F5-3: an argv that names no drive would start the interactive agent."""
        self.assertEqual(cursor.DRIVES, ("-p", "acp"))
        for argv in (["agent", "--disable-auto-update"], ["agent", "--disable-auto-update", "--"],
                     ["agent", "--disable-auto-update", "--", "acp"], ["agent", "--disable-auto-update", "--", "-p"],
                     ["agent", "--disable-auto-update", "--model", "m", "-p"],
                     ["agent", "--disable-auto-update", "--print"], ["agent", "--disable-auto-update", "-pv"],
                     ["agent", "--disable-auto-update", "ACP"], ["agent", "--disable-auto-update", "chat"]):
            with self.subTest(argv=argv):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(argv)
                self.assertEqual(caught.exception.detail, "drive_missing")
        with self.assertRaises(cursor.ArgvRefused) as caught:   # acknowledged full access still needs a drive
            cursor.check_argv(["agent", "--disable-auto-update", "--force"], full_access_acknowledged=True)
        self.assertEqual(caught.exception.detail, "drive_missing")
        cursor.check_argv(["agent", "--disable-auto-update", "-p"])
        cursor.check_argv(["agent", "--disable-auto-update", "acp"])
        cursor.check_argv(("agent", "--disable-auto-update", "acp"))

    def test_a_subclass_of_list_or_str_is_refused_not_trusted(self):
        """Break-it read 8 of #388, F8-1: every rule reads the argv through the container's and the arguments'
        own methods, so a subclass could show the rules one argv and execve another. The argv must be exactly
        a list or a tuple, and each argument, the model and the binary exactly a str. A subclass is refused
        with the rule's own code, never with anything but ArgvRefused."""

        class BenignView(list):   # all() and sum() see a short argv; slicing and indexing see the real one
            def __iter__(self):
                return iter(["agent", "--disable-auto-update", "-p"])

        class LyingLength(str):   # encode() reports one byte, whatever the text
            def encode(self, *args, **kwargs):
                return b"x"

        class HiddenSurrogate(str):   # iterating it hides the lone surrogate from the surrogate rule
            def __iter__(self):
                return iter("ok")

        head = ["agent", "--disable-auto-update", "-p", "--"]
        big = BenignView(head + ["x" * cursor.MAX_ARG_BYTES] * 60)
        self.assertGreater(argv_cost(list.__iter__(big)), cursor.MAX_ARGV_BYTES)
        self.assertLessEqual(argv_cost(big), cursor.MAX_ARGV_BYTES)
        for argv in (big, head + [LyingLength("x" * 200000)], head + [HiddenSurrogate("say " + chr(0xD800))],
                     [LyingLength("agent"), "--disable-auto-update", "acp"],
                     type("Row", (tuple,), {})(("agent", "--disable-auto-update", "acp"))):
            with self.subTest(argv=type(argv).__name__):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(argv)
                self.assertEqual(caught.exception.detail, "argv_invalid")
        for model in (HiddenSurrogate("m" + chr(0xD800)), LyingLength("m")):
            with self.assertRaises(cursor.ArgvRefused) as caught:
                cursor.oneshot_argv(model)
            self.assertEqual(caught.exception.detail, "model_invalid")
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.acp_argv(binary=HiddenSurrogate("/opt/" + chr(0xD800) + "/agent"))
        self.assertEqual(caught.exception.detail, "binary_not_absolute")
        with self.assertRaises(cursor.ArgvRefused) as caught:
            cursor.check_argv(["/opt/c/agent", "--disable-auto-update", "acp"], binary=LyingLength("/opt/c/agent"))
        self.assertEqual(caught.exception.detail, "binary_not_absolute")
        cursor.check_argv(["agent", "--disable-auto-update", "acp"])   # the same text, plain, is taken
        cursor.check_argv(("agent", "--disable-auto-update", "acp"))
        cursor.check_argv(["/opt/c/agent", "--disable-auto-update", "acp"], binary="/opt/c/agent")

    def test_a_bad_binary_is_refused_after_argv_invalid_and_before_executable_mismatch(self):
        """Break-it read 8 of #388, F8-3: ``binary_not_absolute`` is raised as argv[0] is compared, so after
        every ``argv_invalid`` case (a line break among the options included, since round 7) and before
        ``executable_mismatch``, and the docstring lists it there."""
        for argv, detail in ((["agent", "--disable-auto-update", "-p", "--model", "a\nb"], "argv_invalid"),
                             (["agent", "--disable-auto-update", "acp"], "binary_not_absolute"),
                             (["wrong", "--disable-auto-update", "acp"], "binary_not_absolute"),
                             (["agent", "--disable-auto-update", "--api-key", "k", "acp"], "binary_not_absolute")):
            with self.subTest(argv=argv):
                with self.assertRaises(cursor.ArgvRefused) as caught:
                    cursor.check_argv(argv, binary="relative/agent")
                self.assertEqual(caught.exception.detail, detail)
        doc = cursor.check_argv.__doc__
        self.assertLess(doc.index("* ``argv_invalid``"), doc.index("* ``binary_not_absolute``"))
        self.assertLess(doc.index("* ``binary_not_absolute``"), doc.index("* ``executable_mismatch``"))

    def test_every_refused_argv_of_the_fixture(self):
        self.assertEqual(len(ARGV["refused"]), 24)
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
        """The drive now stands right after the updater flag (F5-3), so these options follow it."""
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--api-keys-doc"])
        cursor.check_argv(["agent", "--disable-auto-update", "-p", "--headers-only"])

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
