"""B20 (CSL-726): ``--bare`` as the ``-p`` default. Fakes and a recorded help excerpt only.

No provider call. The one optional test runs the local ``claude --help`` (offline) and is skipped when
``RECURSION_LIVE_CLAUDE_HELP`` is unset.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from recursion_core.routes import bare_default as bd
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import claude_limit, claude_ok, request
from recursion_core.routes.tests.test_driver_and_limits import Harness

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude-2.1.312-help-excerpt.txt"
HEAD = ("Usage: claude [options] [command] [prompt]\n\n"
        "Claude Code - starts an interactive session by default, use -p/--print for\nnon-interactive output\n\n"
        "Options:\n")


def help_with_bare(description: str, extra: str = "") -> str:
    lines = textwrap.wrap(description, 40)
    body = "  --bare                                " + ("\n" + " " * 40).join(lines) + "\n"
    return (HEAD + "  --add-dir <directories...>            Additional directories\n" + body
            + "  -p, --print                           Print response and exit\n" + extra)


class ClassifyHelpTest(unittest.TestCase):
    def test_recorded_incomplete_help_is_unknown(self) -> None:
        # The recorded excerpt omits -p/--print; it cannot establish its default.
        self.assertEqual(bd.classify_help(HEAD + FIXTURE.read_text()), "unknown")

    def test_future_tense_note_is_absent(self) -> None:
        text = help_with_bare("Minimal mode. --bare will become the default for -p in a future release.")
        self.assertEqual(bd.classify_help(text), "absent")

    def test_present_tense_default_is_present(self) -> None:
        for desc in ("Minimal mode. This is the default for -p.",
                     "Minimal mode. Default for -p/--print; OAuth and keychain are never read.",
                     "Minimal mode, on by default in print mode.",
                     "Minimal mode. Now the default in headless runs."):
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "present")

    def test_opt_out_option_means_bare_can_be_on_without_the_flag(self) -> None:
        text = help_with_bare("Minimal mode.", extra="  --no-bare                             Turn bare off\n")
        self.assertEqual(bd.classify_help(text), "present")

    def test_default_wording_in_another_option_does_not_count(self) -> None:
        # 2.1.312 has "default, except under --bare" inside a different option's block.
        extra = ("  --system-prompt-mode <mode>           Rendered on the first request (the\n"
                 "                                        default, except under --bare outside\n"
                 "                                        cloud sessions); default for -p too\n")
        self.assertEqual(bd.classify_help(help_with_bare("Minimal mode.", extra=extra)), "unknown")

    def test_print_help_announces_default_independently(self):
        for flag in ("-p, --print", "--print"):
            text = help_with_bare("Minimal mode.").replace(
                "-p, --print                           Print response and exit",
                flag + "                           Runs in bare mode by default")
            self.assertEqual(bd.classify_help(text), "present")

    def test_bare_help_saying_it_is_the_default_is_present(self) -> None:
        for desc in ("Minimal mode. --bare is the default.", "Minimal mode (default).",
                     "Minimal mode. Defaults to on.", "Minimal mode. Bare is now the default."):
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "present")

    def test_only_print_help_saying_bare_is_default_is_present(self) -> None:
        for note in ("--bare is the default.", "Bare mode is now the default.", "Defaults to --bare.",
                     "Implies --bare (default).", "Default is bare mode."):
            with self.subTest(note=note):
                text = help_with_bare("Minimal mode.").replace("Print response and exit",
                                                               "Print response and exit. " + note)
                self.assertEqual(bd.classify_help(text), "present")

    def test_future_or_negated_print_wording_keeps_its_classification(self) -> None:
        cases = {"--bare will become the default for -p in a future release.": "absent",
                 "--bare is not the default.": "unknown",
                 "The default system prompt is skipped with --bare.": "unknown"}
        for note, state in cases.items():
            with self.subTest(note=note):
                text = help_with_bare("Minimal mode.").replace("Print response and exit",
                                                               "Print response and exit. " + note)
                self.assertEqual(bd.classify_help(text), state)

    def test_neutral_or_ambiguous_help_is_unknown(self):
        for description in ("Minimal mode.", "Minimal bare mode is planned."):
            self.assertEqual(bd.classify_help(help_with_bare(description)), "unknown")

    def test_missing_or_unreadable_help_is_unknown(self) -> None:
        for text in (None, "", "   ", b"--bare", HEAD + "  --print   Print response\n",
                     "                                        --bare on a continuation line only\n"):
            with self.subTest(text=text):
                self.assertEqual(bd.classify_help(text), "unknown")


class ReadHelpTest(unittest.TestCase):
    def _run(self, returncode=0, stdout=b"", exc=None):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout)
        return run, calls

    def test_only_help_with_a_small_fixed_env(self) -> None:
        run, calls = self._run(stdout=help_with_bare("Bare default: false").encode())
        parent = {"PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": "k",
                  "CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_SIMPLE": "1"}
        self.assertEqual(bd.probe("/opt/claude/bin/claude", parent, run), "absent")
        (argv, kwargs), = calls
        self.assertEqual(argv, ["/opt/claude/bin/claude", "--help"])
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin", "HOME": "/home/op", "LANG": "C.UTF-8"})
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], bd.HELP_TIMEOUT_S)

    def test_failures_read_as_unknown(self) -> None:
        cases = [self._run(returncode=1, stdout=b"--bare")[0],
                 self._run(exc=FileNotFoundError())[0],
                 self._run(exc=subprocess.TimeoutExpired(["claude"], 10))[0],
                 self._run(stdout=b"\xff\xfe not utf-8")[0],
                 self._run(stdout=b"x" * (bd.HELP_MAX_BYTES + 1))[0]]
        for run in cases:
            with self.subTest(run=run):
                self.assertEqual(bd.probe("claude", {"PATH": "/usr/bin"}, run), "unknown")

    @unittest.skipUnless(os.environ.get("RECURSION_LIVE_CLAUDE_HELP") == "1" and shutil.which("claude"),
                         "set RECURSION_LIVE_CLAUDE_HELP=1 with claude on PATH")
    def test_local_claude_help(self) -> None:
        state = bd.probe(shutil.which("claude"), {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")})
        self.assertIn(state, ("absent", "present"))
        print(f"\nlocal claude --help: bare_default={state}")


class SubscriptionRouteBareDefaultTest(unittest.TestCase):
    def test_absent_launches_without_bare(self) -> None:
        h = Harness([claude_ok()])
        self.assertEqual(h.handle(request(bare_default="absent"))["status"], "ok")
        self.assertNotIn("--bare", h.launcher.calls[0]["argv"])

    def test_present_unknown_and_unreported_are_refused_before_launch(self) -> None:
        for state in ("present", "unknown", None):
            with self.subTest(state=state):
                h = Harness([])
                res = h.handle(request(bare_default=state))
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertIn("--bare", res["detail"])
                self.assertEqual(h.launcher.calls, [])
                self.assertEqual(res["breaker_effect"], "none")  # a refusal never trips the login

    def test_documented_opt_out_is_passed_explicitly(self):
        text = help_with_bare("Minimal mode.", extra="  --no-bare       Disable bare mode\n")
        metadata = bd.auth_mode(text)
        self.assertEqual(metadata, {"bare_default": "present", "bare_opt_out": "--no-bare"})
        h = Harness([claude_ok()])
        self.assertEqual(h.handle(request(**metadata))["status"], "ok")
        self.assertEqual(h.launcher.calls[0]["argv"][:3], ["claude", "--no-bare", "-p"])

    def test_unreadable_or_unparseable_help_refuses_the_route(self) -> None:
        def unreadable(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, bd.HELP_TIMEOUT_S)

        def unparseable(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=b"Usage: claude [options]\n")
        for run in (unreadable, unparseable):
            with self.subTest(run=run.__name__):
                metadata = bd.probe_auth_mode("claude", {"PATH": "/usr/bin"}, run)
                self.assertEqual(metadata, {"bare_default": "unknown", "bare_opt_out": None})
                h = Harness([])
                self.assertEqual(h.handle(request(**metadata))["code"], c.LAUNCH_REFUSED)
                self.assertEqual(h.launcher.calls, [])

    def test_cli_reports_classification_route_and_help_lines(self) -> None:
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=help_with_bare("Minimal mode.").encode())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bd.main(["--claude", "/opt/claude"], run), 0)
        self.assertEqual(calls, [["/opt/claude", "--help"]])
        text = out.getvalue()
        self.assertIn("bare_default: unknown", text)
        self.assertIn("subscription route: refuses", text)
        self.assertIn("  --bare", text)
        self.assertIn("  -p, --print", text)

    def test_unknown_still_refuses_even_with_opt_out(self):
        h = Harness([])
        self.assertEqual(h.handle(request(bare_default="unknown", bare_opt_out="--no-bare"))["code"],
                         c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])

    def test_fallback_passes_documented_auth_mode(self):
        h = Harness([claude_limit(), claude_ok()])
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent", "bare_default": "present", "bare_opt_out": "--no-bare"}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(h.launcher.calls), 2)
        self.assertIn("--no-bare", h.launcher.calls[1]["argv"])

    def test_invalid_value_is_request_invalid(self) -> None:
        h = Harness([])
        self.assertEqual(h.handle(request(bare_default="maybe"))["code"], c.REQUEST_INVALID)
        self.assertEqual(h.launcher.calls, [])

    def test_api_key_and_passthrough_routes_do_not_need_it(self) -> None:
        h = Harness([claude_ok(), claude_ok()])
        res = h.handle(request(kind="api_key", conn_id="key-2", credential_env="ANTHROPIC_API_KEY",
                               bare=True, bare_default="present"))
        self.assertEqual(res["status"], "ok")
        req = request(rid="r2", kind="passthrough", bare_default=None)
        req["route"]["connection"]["method"] = "bedrock"
        self.assertEqual(h.handle(req)["status"], "ok")

    def test_fallback_entry_carries_the_same_check(self) -> None:
        h = Harness([claude_limit()])
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent", "bare_default": "present"}]
        # Validation accepts the key; the refusal happens when that route would launch.
        res = h.handle(request(bare_default="absent", limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(len(h.launcher.calls), 1)
        self.assertEqual(res["limit_notice"]["attempts"][-1]["connection"], "subscription:login-B")


if __name__ == "__main__":
    unittest.main()
