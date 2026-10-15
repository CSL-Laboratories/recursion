#!/usr/bin/env python3
"""Tests for the Codex CI/fixer gate (scripts/ci/codex_gate.py).

Acceptance:

    python -X utf8 -B tools/test_codex_gate.py
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "ci" / "codex_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "codex-gate.yml"
FIXER_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "claude.yml"
AGENTS_PATH = ROOT / "AGENTS.md"
CLAUDE_MD_PATH = ROOT / "CLAUDE.md"
SHA = "a" * 40
REPO = "acme/example"


def load_gate():
    spec = importlib.util.spec_from_file_location("codex_gate", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()


def review_event(*, login: str, body: str, sha: str = SHA, number: int = 7) -> dict:
    return {
        "review": {
            "user": {"login": login, "type": "Bot"},
            "body": body,
            "commit_id": sha,
            "state": "commented",
        },
        "pull_request": {
            "number": number,
            "draft": False,
            "head": {"sha": sha, "repo": {"full_name": REPO}},
        },
        "sender": {"login": login, "type": "Bot"},
    }


def pr_event(*, sha: str = SHA, fork: bool = False, draft: bool = False, number: int = 7) -> dict:
    head_repo = "evil/fork" if fork else REPO
    return {
        "pull_request": {
            "number": number,
            "draft": draft,
            "head": {"sha": sha, "ref": "feat/x", "repo": {"full_name": head_repo}},
        },
        "repository": {"default_branch": "master"},
    }


class MarkerAndVerdictTests(unittest.TestCase):
    def test_gate_marker_is_exact_html_comment(self) -> None:
        self.assertEqual(gate.gate_marker(SHA), f"<!-- codex-gate:{SHA} -->")

    def test_parse_verdict_last_line_wins(self) -> None:
        body = "notes\nCODEX-GATE: FIX\nmore\nCODEX-GATE: READY\n"
        self.assertEqual(gate.parse_verdict(body), "READY")
        self.assertEqual(gate.parse_verdict("CODEX-GATE: MEDIATE"), "MEDIATE")
        self.assertIsNone(gate.parse_verdict("CODEX-GATE: SKIP"))
        self.assertIsNone(gate.parse_verdict("said CODEX-GATE: FIX in prose"))
        self.assertIsNone(gate.parse_verdict(""))
        self.assertIsNone(gate.parse_verdict(None))

    def test_request_comment_contains_mention_and_marker(self) -> None:
        body = gate.request_comment_body(SHA)
        self.assertIn("@codex review", body)
        self.assertIn(gate.gate_marker(SHA), body)
        for verdict in ("FIX", "READY", "MEDIATE"):
            self.assertIn(f"CODEX-GATE: {verdict}", body)

    def test_fixer_run_cap(self) -> None:
        day = "2026-10-15"
        bodies = [gate.fixer_run_marker(day)] * 3
        self.assertTrue(gate.cap_reached(bodies, day))
        self.assertFalse(gate.cap_reached(bodies, day, bypass=True))
        self.assertFalse(gate.cap_reached(bodies[:2], day))
        self.assertFalse(gate.cap_reached([gate.fixer_run_marker("2026-10-14")] * 5, day))


class AdmitResolvePrTests(unittest.TestCase):
    def test_human_claude_on_pr_comment_is_admitted(self) -> None:
        event = {
            "sender": {"login": "alice", "type": "User"},
            "comment": {"body": "please @claude fix", "user": {"login": "alice", "type": "User"}},
            "issue": {"number": 7, "pull_request": {"url": "x"}},
        }
        self.assertTrue(gate.admit_resolve_pr("issue_comment", event))

    def test_bot_comment_is_rejected_even_with_claude_mention(self) -> None:
        for login, sender_type in (
            ("claude[bot]", "Bot"),
            ("cursor[bot]", "Bot"),
            ("github-actions[bot]", "Bot"),
            ("blacksmith-sh[bot]", "Bot"),
            ("devin-ai-integration[bot]", "Bot"),
        ):
            event = {
                "sender": {"login": login, "type": sender_type},
                "comment": {"body": "@claude please", "user": {"login": login, "type": "Bot"}},
                "issue": {"number": 7, "pull_request": {"url": "x"}},
            }
            self.assertFalse(gate.admit_resolve_pr("issue_comment", event), login)

    def test_plain_issue_is_rejected(self) -> None:
        event = {
            "sender": {"login": "alice", "type": "User"},
            "comment": {"body": "@claude", "user": {"login": "alice", "type": "User"}},
            "issue": {"number": 7},
        }
        self.assertFalse(gate.admit_resolve_pr("issue_comment", event))

    def test_bot_review_comment_is_rejected(self) -> None:
        event = {
            "sender": {"login": "devin-ai-integration[bot]", "type": "Bot"},
            "comment": {
                "body": "@claude fix this line",
                "user": {"login": "devin-ai-integration[bot]", "type": "Bot"},
            },
        }
        self.assertFalse(gate.admit_resolve_pr("pull_request_review_comment", event))

    def test_human_review_comment_without_mention_is_rejected(self) -> None:
        event = {
            "sender": {"login": "alice", "type": "User"},
            "comment": {"body": "nit", "user": {"login": "alice", "type": "User"}},
        }
        self.assertFalse(gate.admit_resolve_pr("pull_request_review_comment", event))

    def test_devin_and_codex_reviews_are_admitted(self) -> None:
        for login in ("devin-ai-integration[bot]", "chatgpt-codex-connector[bot]"):
            event = {"review": {"user": {"login": login}}}
            self.assertTrue(gate.admit_resolve_pr("pull_request_review", event), login)

    def test_other_bot_review_is_rejected(self) -> None:
        event = {"review": {"user": {"login": "cursor[bot]"}}}
        self.assertFalse(gate.admit_resolve_pr("pull_request_review", event))

    def test_dispatch_is_admitted(self) -> None:
        self.assertTrue(gate.admit_resolve_pr("workflow_dispatch", {}))
        self.assertTrue(gate.admit_resolve_pr("repository_dispatch", {}))


class DecisionTests(unittest.TestCase):
    def test_request_skips_when_pat_missing(self) -> None:
        got = gate.decide_request(
            pat="", same_repo=True, draft=False, sha=SHA, comments=[], reviews=[]
        )
        self.assertEqual(got["action"], "skip-pat")
        self.assertEqual(got["proceed"], "false")

    def test_request_skips_fork_and_draft(self) -> None:
        base = dict(pat="pat", sha=SHA, comments=[], reviews=[])
        self.assertEqual(
            gate.decide_request(same_repo=False, draft=False, **base)["action"],
            "skip",
        )
        self.assertEqual(
            gate.decide_request(same_repo=True, draft=True, **base)["action"],
            "skip",
        )

    def test_request_posts_once_per_sha(self) -> None:
        first = gate.decide_request(
            pat="pat", same_repo=True, draft=False, sha=SHA, comments=[], reviews=[]
        )
        self.assertEqual(first["action"], "post")
        second = gate.decide_request(
            pat="pat",
            same_repo=True,
            draft=False,
            sha=SHA,
            comments=[gate.request_comment_body(SHA)],
            reviews=[],
        )
        self.assertEqual(second["action"], "already")
        self.assertEqual(second["proceed"], "true")

    def test_request_does_not_wait_when_verdict_already_exists(self) -> None:
        reviews = [{"user": {"login": "chatgpt-codex-connector[bot]"}, "commit_id": SHA, "body": "CODEX-GATE: READY"}]
        got = gate.decide_request(
            pat="pat",
            same_repo=True,
            draft=False,
            sha=SHA,
            comments=[gate.request_comment_body(SHA)],
            reviews=reviews,
        )
        self.assertEqual(got["action"], "already-verdict")
        self.assertEqual(got["proceed"], "false")

    def test_verdict_routes(self) -> None:
        cases = {
            "FIX": "fix",
            "READY": "ready",
            "MEDIATE": "mediate",
        }
        for verdict, action in cases.items():
            got = gate.decide_verdict(
                login="chatgpt-codex-connector[bot]",
                body=f"looks good\nCODEX-GATE: {verdict}\n",
                sha=SHA,
                comments=[],
                day="2026-10-15",
            )
            self.assertEqual(got["action"], action, verdict)

    def test_verdict_ignores_non_codex_and_caps_fix(self) -> None:
        self.assertEqual(
            gate.decide_verdict(
                login="devin-ai-integration[bot]",
                body="CODEX-GATE: FIX",
                sha=SHA,
                comments=[],
                day="2026-10-15",
            )["action"],
            "skip",
        )
        comments = [gate.fixer_run_marker("2026-10-15")] * 3
        got = gate.decide_verdict(
            login="chatgpt-codex-connector[bot]",
            body="CODEX-GATE: FIX",
            sha=SHA,
            comments=comments,
            day="2026-10-15",
        )
        self.assertEqual(got["action"], "cap")
        bypassed = gate.decide_verdict(
            login="chatgpt-codex-connector[bot]",
            body="CODEX-GATE: FIX",
            sha=SHA,
            comments=comments,
            day="2026-10-15",
            bypass=True,
        )
        self.assertEqual(bypassed["action"], "fix")

    def test_fallback_fail_open_and_skips(self) -> None:
        day = "2026-10-15"
        self.assertEqual(
            gate.decide_fallback(sha=SHA, comments=[], reviews=[], day=day)["action"],
            "fix",
        )
        reviews = [{"user": {"login": "chatgpt-codex-connector[bot]"}, "commit_id": SHA, "body": "CODEX-GATE: READY"}]
        self.assertEqual(
            gate.decide_fallback(sha=SHA, comments=[], reviews=reviews, day=day)["action"],
            "has-verdict",
        )
        self.assertEqual(
            gate.decide_fallback(
                sha=SHA, comments=[gate.acted_marker(SHA)], reviews=[], day=day
            )["action"],
            "already-acted",
        )
        self.assertEqual(
            gate.decide_fallback(
                sha=SHA, comments=[gate.fixer_run_marker(day)] * 3, reviews=[], day=day
            )["action"],
            "cap",
        )


class NoticeAndIoTests(unittest.TestCase):
    def test_run_request_missing_pat_emits_notice_and_does_not_post(self) -> None:
        calls: list[list[str]] = []

        def gh(args, token, input_text=None):
            calls.append(args)
            self.assertNotIn("secret-pat", " ".join(args))
            if "/comments" in args[1] and not any(a == "--input" for a in args):
                return "[]"
            if "/reviews" in args[1]:
                return "[]"
            return ""

        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            os.environ["GITHUB_OUTPUT"] = str(out)
            try:
                from io import StringIO
                import contextlib

                buf = StringIO()
                with contextlib.redirect_stdout(buf):
                    got = gate.run_request(event, REPO, "", "read-token", gh=gh)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertEqual(got["proceed"], "false")
        self.assertEqual(got["action"], "skip-pat")
        self.assertIn("::notice::CODEX_TRIGGER_PAT is missing", buf.getvalue())
        self.assertNotIn("secret-pat", buf.getvalue())
        self.assertFalse(any(a[:2] == ["api", f"repos/{REPO}/issues/7/comments"] and "--input" in a for a in calls))

    def test_run_request_posts_once(self) -> None:
        posted: list[str] = []

        def gh(args, token, input_text=None):
            if args[:1] == ["workflow"]:
                return ""
            path = args[1] if len(args) > 1 else ""
            if path.endswith("/comments") and "--input" in args:
                posted.append(input_text or "")
                return "{}"
            if "/comments?" in path:
                return "[]"
            if "/reviews?" in path:
                return "[]"
            return "[]"

        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                got = gate.run_request(event, REPO, "pat", "read-token", gh=gh)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertEqual(got["action"], "post")
        self.assertEqual(got["proceed"], "true")
        self.assertEqual(len(posted), 1)
        self.assertIn("@codex review", posted[0])
        self.assertIn(gate.gate_marker(SHA), posted[0])

    def test_run_verdict_ready_labels_and_mediate_dispatches(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def gh(args, token, input_text=None):
            calls.append((list(args), input_text or ""))
            if "/comments?" in (args[1] if len(args) > 1 else ""):
                return "[]"
            return "{}"

        event = review_event(login="chatgpt-codex-connector[bot]", body="CODEX-GATE: READY")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_verdict(event, REPO, "pat", "read-token", "master", gh=gh, day="2026-10-15")
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertTrue(any("labels" in " ".join(args) for args, _ in calls))
        self.assertTrue(any("ci:full" in payload for _, payload in calls))

        calls.clear()
        event = review_event(login="chatgpt-codex-connector[bot]", body="CODEX-GATE: MEDIATE")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_verdict(event, REPO, "pat", "read-token", "master", gh=gh, day="2026-10-15")
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertTrue(
            any(args[:2] == ["workflow", "run"] and "stuck-thread-watch.yml" in args for args, _ in calls)
        )

    def test_run_verdict_fix_dispatches_claude_workflow(self) -> None:
        calls: list[list[str]] = []

        def gh(args, token, input_text=None):
            calls.append(list(args))
            if "/comments?" in (args[1] if len(args) > 1 else ""):
                return "[]"
            return "{}"

        event = review_event(login="chatgpt-codex-connector[bot]", body="CODEX-GATE: FIX")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_verdict(event, REPO, "pat", "read-token", "master", gh=gh, day="2026-10-15")
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertTrue(any(c[:2] == ["workflow", "run"] and "claude.yml" in c for c in calls))
        self.assertTrue(any("pr_number=7" in " ".join(c) for c in calls))

    def test_tokens_never_appear_in_argv(self) -> None:
        secret = "ghs_not_a_real_secret_value"

        def gh(args, token, input_text=None):
            blob = " ".join(args) + " " + (input_text or "")
            if secret in blob:
                raise AssertionError("secret leaked into gh argv")
            if "/comments?" in (args[1] if len(args) > 1 else ""):
                return "[]"
            if "/reviews?" in (args[1] if len(args) > 1 else ""):
                return "[]"
            return "{}"

        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_request(event, REPO, secret, secret, gh=gh)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)


class WorkflowStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.fixer = FIXER_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.agents = AGENTS_PATH.read_text(encoding="utf-8")

    def test_runs_on_ubuntu_latest_not_blacksmith(self) -> None:
        self.assertIn("runs-on: ubuntu-latest", self.text)
        self.assertNotIn("blacksmith", self.text)

    def test_wait_is_twenty_minutes(self) -> None:
        self.assertIn('WAIT_SECONDS: "1200"', self.text)
        self.assertIn('sleep "$WAIT_SECONDS"', self.text)

    def test_missing_secret_is_notice_not_failure(self) -> None:
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn('::notice::{message}', source)
        self.assertIn("CODEX_TRIGGER_PAT is missing", source)
        self.assertIn("secrets.CODEX_TRIGGER_PAT", self.text)
        self.assertNotIn("echo \"$CODEX_TRIGGER_PAT\"", self.text)
        self.assertNotIn("echo $CODEX_TRIGGER_PAT", self.text)

    def test_default_branch_checkout_without_credentials(self) -> None:
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.text)
        self.assertIn("persist-credentials: false", self.text)
        self.assertIn("scripts/ci/codex_gate.py", self.text)

    def test_codex_review_login_filter(self) -> None:
        self.assertIn("chatgpt-codex-connector[bot]", self.text)

    def test_fixer_dispatch_is_not_on_every_devin_review(self) -> None:
        start = self.fixer.index("  devin-review-fix:\n")
        nxt = self.fixer.find("\n  ", start + 4)
        while nxt != -1 and self.fixer[nxt + 3 : nxt + 4] in (" ", "\t"):
            nxt = self.fixer.find("\n  ", nxt + 1)
        job = self.fixer[start:] if nxt == -1 else self.fixer[start:nxt]
        self.assertIn("workflow_dispatch", job)
        self.assertIn("repository_dispatch", job)

    def test_agents_code_review_rules(self) -> None:
        self.assertIn("## Code Review Rules", self.agents)
        self.assertIn("CODEX-GATE: FIX", self.agents)
        self.assertIn("CODEX-GATE: READY", self.agents)
        self.assertIn("CODEX-GATE: MEDIATE", self.agents)
        self.assertEqual(self.agents, CLAUDE_MD_PATH.read_text(encoding="utf-8"))

    def test_human_mention_job_is_not_daily_capped(self) -> None:
        start = self.fixer.index("  claude-mention:\n")
        nxt = self.fixer.find("\n  ", start + 4)
        while nxt != -1 and self.fixer[nxt + 3 : nxt + 4] in (" ", "\t"):
            nxt = self.fixer.find("\n  ", nxt + 1)
        job = self.fixer[start:] if nxt == -1 else self.fixer[start:nxt]
        self.assertNotIn("codex-fixer-run", job)
        self.assertIn("needs.resolve-pr.outputs.mention == 'true'", job)


class ActionlintTests(unittest.TestCase):
    def test_actionlint_when_available(self) -> None:
        exe = shutil.which("actionlint")
        if not exe:
            self.skipTest("actionlint not on PATH")
        result = subprocess.run(
            [exe, "-color", str(WORKFLOW_PATH), str(FIXER_WORKFLOW_PATH)],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    if Path.cwd().resolve() != ROOT.resolve():
        raise SystemExit(f"run from the repository root ({ROOT})")
    unittest.main(verbosity=2)
