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
from recursion_core.routes.tests.fakes import MEASURED_HELP_TEXT, claude_limit, claude_ok, request
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

    def test_measured_builds_read_unknown(self) -> None:
        # Classifier unknown is not an allow. Without claude_build/help_text these measured
        # builds still refuse. 2.1.320 was never published.
        for version in ("2.1.311", "2.1.312", "2.1.314", "2.1.319", "2.1.321"):
            with self.subTest(version=version):
                text = (FIXTURE.parent / f"claude-{version}-help.txt").read_text()
                metadata = bd.auth_mode(text)
                self.assertEqual(metadata, {"bare_default": "unknown", "bare_opt_out": None})
                h = Harness([])
                self.assertEqual(h.handle(request(measured=False, **metadata))["code"], c.LAUNCH_REFUSED)
                self.assertEqual(h.launcher.calls, [])

    def test_matching_measured_hash_runs_subscription_as_beta(self) -> None:
        # One entry, not a version allow list. 2.1.314, 2.1.319 and 2.1.321 share it.
        self.assertEqual(len(bd.MEASURED_HELP), 1)
        self.assertEqual(set(bd.MEASURED_HELP[0]), {"help_sha256", "label"})
        self.assertEqual(bd.MEASURED_HELP[0]["label"], "beta")
        hashes = set()
        for version in ("2.1.314", "2.1.319", "2.1.321"):
            text = (FIXTURE.parent / f"claude-{version}-help.txt").read_text()
            hashes.add(bd.help_sha256(text))
            decision = bd.subscription_beta(version, text)
            self.assertEqual(decision["disposition"], "beta")
            self.assertEqual(decision["label"], "beta")
            self.assertEqual(decision["detail"], "beta")
            h = Harness([claude_ok()])
            # Classifier unknown is still not an allow; the exact help text is.
            res = h.handle(request(claude_build=f"{version} (Claude Code)", help_text=text,
                                   bare_default="unknown"))
            self.assertEqual((res["status"], res["detail"]), ("ok", "beta"))
            self.assertNotIn("--bare", h.launcher.calls[0]["argv"])
            self.assertNotIn("--no-bare", h.launcher.calls[0]["argv"])
        self.assertEqual(hashes, {bd.MEASURED_HELP[0]["help_sha256"]})
        # A build that is not listed still runs as beta when the help hash matches.
        text = (FIXTURE.parent / "claude-2.1.314-help.txt").read_text()
        self.assertEqual(bd.subscription_beta("2.1.400", text)["label"], "beta")

    def test_changed_help_refuses_and_names_the_build(self) -> None:
        text = (FIXTURE.parent / "claude-2.1.321-help.txt").read_text() + "\n"
        self.assertNotEqual(bd.help_sha256(text), bd.MEASURED_HELP[0]["help_sha256"])
        decision = bd.subscription_beta("2.1.321", text)
        self.assertEqual(decision["disposition"], "refuse")
        self.assertIsNone(decision["label"])
        self.assertIn("2.1.321", decision["detail"])
        self.assertIn("needs re-measuring", decision["detail"])
        h = Harness([claude_ok()])
        # absent does not bypass a help text that is not the measured fixture.
        res = h.handle(request(claude_build="2.1.321 (Claude Code)", help_text=text, bare_default="absent"))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("2.1.321", res["detail"])
        self.assertIn("needs re-measuring", res["detail"])
        self.assertNotIn("beta", res["detail"])
        self.assertEqual(h.launcher.calls, [])

    def test_below_floor_refuses(self) -> None:
        measured = (FIXTURE.parent / "claude-2.1.314-help.txt").read_text()
        cases = (
            ("2.1.311", (FIXTURE.parent / "claude-2.1.311-help.txt").read_text()),
            ("2.1.312", (FIXTURE.parent / "claude-2.1.312-help.txt").read_text()),
            ("2.1.313", measured),  # same hash as the measured entry; the floor still refuses
        )
        for version, text in cases:
            with self.subTest(version=version):
                decision = bd.subscription_beta(version, text)
                self.assertEqual(decision["disposition"], "refuse")
                self.assertIsNone(decision["label"])
                self.assertIn(version, decision["detail"])
                self.assertIn("below the 2.1.314 floor", decision["detail"])
                self.assertNotIn("re-measuring", decision["detail"])
                h = Harness([claude_ok()])
                res = h.handle(request(claude_build=version, help_text=text, bare_default="absent"))
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertIn("below the 2.1.314 floor", res["detail"])
                self.assertEqual(h.launcher.calls, [])

    def test_unknown_build_or_help_is_not_beta(self) -> None:
        text = (FIXTURE.parent / "claude-2.1.314-help.txt").read_text()
        for build, help_text in (("latest", text), ("2.1.321-dev", text), ("2.1.321", ""), (None, text),
                                 ("2.1.321", None)):
            with self.subTest(build=build, help_text=help_text):
                decision = bd.subscription_beta(build, help_text)
                self.assertEqual(decision["disposition"], "refuse")
                self.assertNotEqual(decision["label"], "beta")
                self.assertNotIn("beta", decision["detail"])
        h = Harness([claude_ok()])
        res = h.handle(request(claude_build="latest", help_text=text, bare_default="absent"))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("unknown is not safe", res["detail"])
        self.assertEqual(h.launcher.calls, [])

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
        # Every measured build reads "unknown" (refuses); a later build may settle it either way.
        state = bd.probe(shutil.which("claude"), {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")})
        self.assertIn(state, bd.STATES)
        print(f"\nlocal claude --help: bare_default={state}")


class SubscriptionRouteBareDefaultTest(unittest.TestCase):
    def test_absent_without_measured_help_does_not_launch(self) -> None:
        # bare_default absent used to return [] and spawn with no hash check and no beta label.
        h = Harness([claude_ok()])
        res = h.handle(request(bare_default="absent", measured=False))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("unknown is not safe", res["detail"])
        self.assertNotEqual(res["detail"], "beta")
        self.assertEqual(h.launcher.calls, [])
        self.assertEqual(res["breaker_effect"], "none")

    def test_absent_with_measured_help_runs_as_beta(self) -> None:
        h = Harness([claude_ok()])
        res = h.handle(request(claude_build="2.1.321", help_text=MEASURED_HELP_TEXT, bare_default="absent"))
        self.assertEqual((res["status"], res["detail"]), ("ok", "beta"))
        self.assertNotIn("--bare", h.launcher.calls[0]["argv"])
        self.assertNotIn("--no-bare", h.launcher.calls[0]["argv"])

    def test_present_unknown_and_unreported_are_refused_before_launch(self) -> None:
        for state in ("present", "unknown", None):
            with self.subTest(state=state):
                h = Harness([])
                res = h.handle(request(bare_default=state, measured=False))
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertIn("unknown is not safe", res["detail"])
                self.assertNotEqual(res["detail"], "beta")
                self.assertEqual(h.launcher.calls, [])
                self.assertEqual(res["breaker_effect"], "none")  # a refusal never trips the login

    def test_documented_opt_out_does_not_launch_without_the_measured_hash(self):
        text = help_with_bare("Minimal mode.", extra="  --no-bare       Disable bare mode\n")
        metadata = bd.auth_mode(text)
        self.assertEqual(metadata, {"bare_default": "present", "bare_opt_out": "--no-bare"})
        h = Harness([claude_ok()])
        res = h.handle(request(measured=False, **metadata))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])
        # The synthetic help is not the measured fixture, so --no-bare is not passed.
        res = h.handle(request(claude_build="2.1.321", help_text=text, **metadata))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("needs re-measuring", res["detail"])
        self.assertNotEqual(res["detail"], "beta")
        self.assertEqual(h.launcher.calls, [])

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
                self.assertEqual(h.handle(request(measured=False, **metadata))["code"], c.LAUNCH_REFUSED)
                self.assertEqual(h.launcher.calls, [])

    def test_cli_reports_classification_route_and_help_lines(self) -> None:
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["timeout"], bd.HELP_TIMEOUT_S)
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0, stdout=b"2.1.321 (Claude Code)\n")
            return subprocess.CompletedProcess(argv, 0, stdout=help_with_bare("Minimal mode.").encode())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(bd.main(["--claude", "/opt/claude"], run), 0)
        self.assertEqual(calls, [["/opt/claude", "--version"], ["/opt/claude", "--help"]])
        text = out.getvalue()
        self.assertIn("bare_default: unknown", text)
        self.assertIn("subscription route: refuses", text)
        self.assertIn("2.1.321", text)
        self.assertIn("needs re-measuring", text)
        self.assertIn("  --bare", text)
        self.assertIn("  -p, --print", text)

    def test_unknown_still_refuses_even_with_opt_out(self):
        h = Harness([])
        self.assertEqual(h.handle(request(bare_default="unknown", bare_opt_out="--no-bare",
                                         measured=False))["code"], c.LAUNCH_REFUSED)
        self.assertEqual(h.launcher.calls, [])

    def test_fallback_without_measured_help_does_not_launch(self):
        h = Harness([claude_limit(), claude_ok()])
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent", "bare_default": "present", "bare_opt_out": "--no-bare"}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("unknown is not safe", res["detail"])
        self.assertNotEqual(res["detail"], "beta")
        self.assertEqual(len(h.launcher.calls), 1)
        self.assertNotIn("--no-bare", h.launcher.calls[0]["argv"])

    def test_fallback_with_measured_help_is_beta(self):
        h = Harness([claude_limit(), claude_ok()])
        order = [{"engine": "claude_code", "model": "m-b", "connection": {"kind": "subscription", "id": "login-B"},
                  "api_key_helper": "absent", "bare_default": "absent",
                  "claude_build": "2.1.321", "help_text": MEASURED_HELP_TEXT}]
        res = h.handle(request(limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual((res["status"], res["detail"]), ("ok", "beta"))
        self.assertEqual(len(h.launcher.calls), 2)
        self.assertNotIn("--bare", h.launcher.calls[1]["argv"])
        self.assertNotIn("--no-bare", h.launcher.calls[1]["argv"])

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
                  "api_key_helper": "absent", "bare_default": "absent"}]
        # The primary matches the measured help and hits a limit. The fallback has no help, so it refuses.
        res = h.handle(request(bare_default="absent", limits={"on_limit": "fallback", "fallback": order}))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertIn("unknown is not safe", res["detail"])
        self.assertNotEqual(res["detail"], "beta")
        self.assertEqual(len(h.launcher.calls), 1)
        self.assertEqual(res["limit_notice"]["attempts"][-1]["connection"], "subscription:login-B")

    def test_a_later_refusal_does_not_keep_the_beta_label(self) -> None:
        h = Harness([claude_ok(), claude_ok()])
        ok = h.handle(request(claude_build="2.1.321", help_text=MEASURED_HELP_TEXT, bare_default="absent"))
        self.assertEqual(ok["detail"], "beta")
        refused = h.handle(request(rid="r2", measured=False))
        self.assertEqual(refused["code"], c.LAUNCH_REFUSED)
        self.assertNotEqual(refused["detail"], "beta")
        self.assertEqual(len(h.launcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
