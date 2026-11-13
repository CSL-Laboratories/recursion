"""B20 (CSL-726): ``--bare`` as the ``-p`` default. Fakes and a recorded help excerpt only.

No provider call. The one optional test runs the local ``claude --help`` (offline) and is skipped when
``RECURSION_LIVE_CLAUDE_HELP`` is unset.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
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

    def test_present_default_survives_a_negated_future_aside(self) -> None:
        # A future-release word in the same sentence must not drop a present-tense default.
        text = help_with_bare("Minimal mode. --bare is the default for -p, not a future release.")
        self.assertEqual(bd.classify_help(text), "present")

    def test_future_qualifier_anywhere_blocks_present(self) -> None:
        # A future or planned word anywhere in the sentence blocks present, including
        # across a comma. A present claim joined to a future default with "and" is unknown.
        blocked = (
            "In a future release, --bare is the default for -p.",
            "--bare is the default for -p, in a future release.",
            "This is the default for -p, coming in a future release.",
            "planned, --bare is the default for -p.",
            "--bare is the default for -p and will become the default in a future release.",
        )
        for desc in blocked:
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "unknown")
        kept = {
            "--bare is the default for -p, not a future release.": "present",
            "This is the default for -p.": "present",
            # Not an exact present-tense sentence. Was present.
            "On by default in print mode.": "unknown",
            "--bare will become the default for -p in a future release.": "absent",
        }
        for desc, state in kept.items():
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), state)
        for version in ("2.1.312", "2.1.314", "2.1.321"):
            with self.subTest(version=version):
                text = (FIXTURE.parent / f"claude-{version}-help.txt").read_text()
                self.assertEqual(bd.classify_help(text), "unknown")

    def test_earlier_clause_negation_does_not_make_a_future_default_present(self) -> None:
        # "not" / "n't" in an earlier clause does not negate a later "will be".
        # Parent d25fab88 classified these absent. A 24-character window treated
        # the future verb as negated, and the present fallback then returned present.
        earlier_clause = (
            "Do not wait, it will be the default for -p.",
            "Do not worry, --bare will be the default for -p.",
            "Don't worry, it will be the default for -p.",
            "It is not slated, so --bare will be the default for -p.",
        )
        for desc in earlier_clause:
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "absent")

    def test_future_markers_and_line_qualifiers_are_not_present(self) -> None:
        # Adverbs between will/shall/'ll/going-to and be, and a future qualifier
        # anywhere on the help line, must not classify as the default now.
        future_only = (
            "--bare will soon be the default for -p.",
            "--bare will later be the default for -p.",
            "--bare will eventually be the default for -p.",
            "--bare will now be the default for -p.",
            "--bare will also be the default for -p.",
            "--bare shall be the default for -p.",
            "--bare shall soon be the default for -p.",
            "it'll be the default for -p.",
            "--bare is going to be the default for -p.",
            "--bare is going to soon be the default for -p.",
        )
        for desc in future_only:
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "absent")
        not_present = (
            "--bare is the default for -p in the next version.",
            "--bare is the default for -p in the next release.",
            "planned; --bare is the default for -p.",
            "--bare is the default for -p soon.",
        )
        for desc in not_present:
            with self.subTest(desc=desc):
                self.assertNotEqual(bd.classify_help(help_with_bare(desc)), "present")
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "unknown")
        # The exact trailer stays present. A longer negated aside is not that sentence.
        # Those three were present.
        self.assertEqual(bd.classify_help(help_with_bare(
            "--bare is the default for -p, not a future release.")), "present")
        ambiguous_aside = (
            "This is the default for -p, and it will not be optional.",
            "This is the default for -p, and it won't be optional.",
            "This is the default for -p, and it will never be optional.",
        )
        for desc in ambiguous_aside:
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "unknown")

    def test_present_and_future_default_claims_are_unknown(self) -> None:
        text = help_with_bare(
            "Minimal mode. --bare is the default for -p, or it will become the default in a future release.")
        self.assertEqual(bd.classify_help(text), "unknown")

    def test_present_tense_default_is_present(self) -> None:
        self.assertEqual(bd.classify_help(help_with_bare(
            "Minimal mode. This is the default for -p.")), "present")
        # Not exact present-tense sentences. Each was present.
        ambiguous = (
            "Minimal mode. Default for -p/--print; OAuth and keychain are never read.",
            "Minimal mode, on by default in print mode.",
            "Minimal mode. Now the default in headless runs.",
        )
        for desc in ambiguous:
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "unknown")

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
                flag + "                           Defaults to --bare.")
            self.assertEqual(bd.classify_help(text), "present")
            # "Runs in bare mode by default" was present. It is not an exact sentence.
            ambiguous = help_with_bare("Minimal mode.").replace(
                "-p, --print                           Print response and exit",
                flag + "                           Runs in bare mode by default")
            self.assertEqual(bd.classify_help(ambiguous), "unknown")

    def test_bare_help_saying_it_is_the_default_is_present(self) -> None:
        for desc in ("Minimal mode. --bare is the default.", "Minimal mode. Bare is now the default."):
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "present")
        # Not exact present-tense sentences. Each was present.
        for desc in ("Minimal mode (default).", "Minimal mode. Defaults to on."):
            with self.subTest(desc=desc):
                self.assertEqual(bd.classify_help(help_with_bare(desc)), "unknown")

    def test_only_print_help_saying_bare_is_default_is_present(self) -> None:
        for note in ("--bare is the default.", "Defaults to --bare."):
            with self.subTest(note=note):
                text = help_with_bare("Minimal mode.").replace("Print response and exit",
                                                               "Print response and exit. " + note)
                self.assertEqual(bd.classify_help(text), "present")
        # Not exact present-tense sentences. Each was present.
        for note in ("Bare mode is now the default.", "Implies --bare (default).", "Default is bare mode."):
            with self.subTest(note=note):
                text = help_with_bare("Minimal mode.").replace("Print response and exit",
                                                               "Print response and exit. " + note)
                self.assertEqual(bd.classify_help(text), "unknown")

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

    def test_reviewer_rows_are_not_a_present_default(self) -> None:
        # On 2f965a52 the apostrophe, punctuation, modal, past, and conditional rows
        # were present, and the four exact negated-future or "as soon as possible"
        # sentences were unknown. The state on each row is the allowlist result.
        right = "\u2019"
        modifier = "\u02bc"
        rows = (
            (f"--bare won{right}t be the default for -p.", "unknown"),
            (f"--bare won{modifier}t be the default for -p.", "unknown"),
            (f"it{right}ll be the default for -p.", "absent"),
            (f"it{modifier}ll be the default for -p.", "absent"),
            (f"--bare isn{right}t the default for -p.", "unknown"),
            (f"--bare isn{modifier}t the default for -p.", "unknown"),
            (f"Don{right}t worry, it{right}ll be the default for -p.", "absent"),
            (f"Don{modifier}t worry, it{modifier}ll be the default for -p.", "absent"),
            ("--bare will — be the default for -p.", "unknown"),
            ("--bare will (later) be the default for -p.", "unknown"),
            ("--bare will, as expected, be the default for -p.", "unknown"),
            ("--bare is slated to be the default for -p.", "unknown"),
            ("--bare is about to be the default for -p.", "unknown"),
            ("--bare may become the default for -p.", "unknown"),
            ("--bare would be the default for -p.", "unknown"),
            ("--bare was the default for -p.", "unknown"),
            ("Previously, --bare is the default for -p.", "unknown"),
            ("If you use print mode then --bare is the default for -p.", "unknown"),
            ("If it were released, --bare is the default for -p.", "unknown"),
            ("--bare is the default for -p in a later release.", "unknown"),
            ("--bare is the default for -p in a subsequent release.", "unknown"),
            ("--bare is the default for -p in a forthcoming release.", "unknown"),
            ("--bare is the default for -p sooner or later.", "unknown"),
            ("It is no longer experimental or optional for anyone, and --bare is the default for -p.",
             "unknown"),
            ("This is the default for -p as soon as the session starts.", "unknown"),
            ("This is the default for -p as soon as possible.", "present"),
            ("This is the default for -p, not planned for the future.", "present"),
            ("This is the default for -p, and it will not be a future release.", "present"),
            ("This is the default for -p, not coming soon.", "present"),
            ("This is the default for -p, not a future release.", "present"),
            ("default: true", "present"),
            ("Defaults to --bare.", "present"),
        )
        for desc, state in rows:
            with self.subTest(desc=desc):
                got = bd.classify_help(help_with_bare(desc))
                if state != "present":
                    self.assertNotEqual(got, "present")
                self.assertEqual(got, state)

    def test_seeded_marker_sentences_are_never_present(self) -> None:
        # A future, modal, past, or conditional marker is never present unless the
        # whole sentence is an exact allowlisted statement (those are not generated).
        # Seed 1112. On 2f965a52 many of these classified present.
        rng = random.Random(1112)
        markers = (
            "will", "won't", "shall", "it'll", "going to", "slated", "about to",
            "may", "might", "would", "could", "should", "was", "were", "previously",
            "if", "then", "later", "subsequent", "forthcoming", "sooner", "no longer",
            "soon", "planned", "future", "upcoming", "as soon as", "must",
        )
        cores = (
            "--bare is the default for -p",
            "this is the default for -p",
            "--bare is the default",
            "bare is now the default",
            "default: true",
            "defaults to --bare",
        )
        shapes = (
            "{core} {marker} zz",
            "{marker} zz {core}",
            "{core}, {marker} zz",
            "zz {marker}, {core}",
            "--bare {marker} zz the default for -p",
            "if it were zz, {core} {marker}",
        )
        sentences = [
            shapes[i % len(shapes)].format(core=cores[i % len(cores)], marker=markers[i % len(markers)])
            for i in range(len(markers) * len(cores))
        ]
        sentences.extend(rng.choice(shapes).format(core=rng.choice(cores), marker=rng.choice(markers))
                         for _ in range(80))
        sentences.extend((
            "--bare is slated to be the default for -p",
            "--bare will — be the default for -p",
            "--bare will (later) be the default for -p",
            "--bare will, as expected, be the default for -p",
            "--bare may become the default for -p",
            "--bare would be the default for -p",
            "--bare was the default for -p",
            "Previously, --bare is the default for -p",
            "If it were released, --bare is the default for -p",
            "--bare is the default for -p in a later release",
            "--bare is the default for -p sooner or later",
            f"it\u2019ll be the default for -p",
            f"--bare won\u2019t be the default for -p",
            f"--bare isn\u2019t the default for -p",
            f"Don\u2019t worry, it\u2019ll be the default for -p",
        ))
        self.assertGreaterEqual(len(sentences), 100)
        for sentence in sentences:
            with self.subTest(sentence=sentence):
                self.assertNotEqual(bd.classify_help(help_with_bare(sentence + ".")), "present")


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

    def test_alias_and_abbreviation_headers_are_read(self) -> None:
        # Real help lists aliases as `--bg, --background`. The same spelling of an
        # opt-out, including an `=<type>` suffix, is an advertised opt-out.
        cases = (
            "  --bare, --no-bare                     Minimal mode. default: false\n",
            "  --bare=<bool>, --no-bare=<bool>       Minimal mode. default: false\n",
        )
        for line in cases:
            with self.subTest(line=line):
                text = HEAD + line + "  -p, --print                           Print response and exit\n"
                self.assertEqual(bd.classify_help(text), "present")
                self.assertEqual(bd.auth_mode(text)["bare_opt_out"], "--no-bare")
        text = HEAD + "  -b|--bare                             This is the default for -p.\n" + (
            "  -p, --print                           Print response and exit\n")
        self.assertEqual(bd.classify_help(text), "present")

    def test_a_later_block_for_the_same_flag_is_kept(self) -> None:
        text = (HEAD
                + "  --bare                                Minimal mode. default: false\n"
                + "  --bare                                This is the default for -p.\n"
                + "  -p, --print                           Print response and exit\n")
        self.assertEqual(bd.classify_help(text), "present")

    def test_wrapped_argv_example_stays_in_the_block(self) -> None:
        # Description lines in the measured help are indented further than the header.
        # An argv example there is not a new option.
        text = (HEAD
                + "  --bare                                Minimal mode. default: false\n"
                + "      --bare is now the default for -p.\n"
                + "  -p, --print                           Print response and exit\n")
        # Whitespace collapse joins the two lines into one sentence, so it is not the
        # exact present statement. Unknown shows the continuation stayed in the block:
        # dropping it would leave "default: false" and classify absent. Was present.
        self.assertEqual(bd.classify_help(text), "unknown")
        dropped = (HEAD
                   + "  --bare                                Minimal mode. default: false\n"
                   + "  -p, --print                           Print response and exit\n")
        self.assertEqual(bd.classify_help(dropped), "absent")

    def test_unencodable_help_refuses_without_the_text(self) -> None:
        secret = "sk-live-SECRETVALUE"
        text = "help " + secret + " \ud800 tail"
        decision = bd.subscription_beta("2.1.321", text)
        self.assertEqual(decision["disposition"], "refuse")
        self.assertNotIn(secret, json.dumps(decision))
        self.assertEqual(decision["detail"], "claude 2.1.321 help is unknown; unknown is not safe")
        h = Harness([])
        res = h.handle(request(help_text=text))
        self.assertEqual(res["code"], c.LAUNCH_REFUSED)
        self.assertNotIn(secret, json.dumps(res))
        self.assertEqual(res["breaker_effect"], "none")
        self.assertEqual(h.launcher.calls, [])

    def test_oversized_build_token_is_not_copied_into_the_refusal(self) -> None:
        for size in (4000, 5000):
            with self.subTest(size=size):
                blob = "9" * size
                decision = bd.subscription_beta("0.1." + blob, MEASURED_HELP_TEXT)
                self.assertEqual(decision["disposition"], "refuse")
                self.assertNotIn(blob, json.dumps(decision))
                h = Harness([])
                res = h.handle(request(claude_build="2.1." + blob, help_text=MEASURED_HELP_TEXT))
                self.assertEqual(res["code"], c.LAUNCH_REFUSED)
                self.assertNotIn(blob, json.dumps(res))
                self.assertEqual(h.launcher.calls, [])

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
