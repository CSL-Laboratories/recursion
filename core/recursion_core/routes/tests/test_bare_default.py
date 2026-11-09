"""B20 (CSL-726): ``--bare`` as the ``-p`` default. Fakes and a recorded help excerpt only.

No provider call. The one optional test runs the local ``claude --help`` (offline) and is skipped when
``RECURSION_LIVE_CLAUDE_HELP`` is unset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path

from recursion_core.routes import bare_default as bd
from recursion_core.routes import contract as c
from recursion_core.routes.tests.fakes import claude_ok, request
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
    def test_recorded_2_1_312_help_is_absent(self) -> None:
        # 2.1.312 lists --bare and says nothing about it being the -p default.
        self.assertEqual(bd.classify_help(HEAD + FIXTURE.read_text()), "absent")

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
        self.assertEqual(bd.classify_help(help_with_bare("Minimal mode.", extra=extra)), "absent")

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
        run, calls = self._run(stdout=(HEAD + FIXTURE.read_text()).encode())
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
        h = Harness([])
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent", "bare_default": "present"}]
        # Validation accepts the key; the refusal happens when that route would launch.
        res = h.handle(request(bare_default="present", limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
