#!/usr/bin/env python3
"""Tests for the Codex CI/fixer gate (scripts/ci/codex_gate.py).

Acceptance:

    python -X utf8 -B tools/test_codex_gate.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
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


def gh_blob(args: list) -> str:
    return " ".join(str(a) for a in args)


READY_EVENT_READS = 0

def ready_api_reply(args):
    global READY_EVENT_READS
    if args[-1] == "user":
        return json.dumps({"login": "gate-writer", "id": 99, "type": "User"})
    if "/events?" in gh_blob(args):
        READY_EVENT_READS += 1
        if READY_EVENT_READS <= 2:
            return "[]"
        return json.dumps([{"id": 5, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": "gate-writer"}}])
    if args[-1] == f"repos/{REPO}/pulls/7":
        return json.dumps({"state": "open", "draft": False, "head": {"sha": SHA, "repo": {"full_name": REPO}}})
    return "{}"


def review_event(*, login: str, body: str, sha: str = SHA, number: int = 7) -> dict:
    return {
        "action": "submitted",
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
        self.assertNotIn("@claude", body)
        self.assertIn(gate.gate_marker(SHA), body)
        for verdict in ("READY", "MEDIATE"):
            self.assertIn(f"CODEX-GATE: {verdict}", body)

    def test_request_verdict_instruction_is_conditional(self) -> None:
        body = gate.request_comment_body(SHA)
        self.assertIn("When ready or needing human mediation, end with exactly one", body)
        self.assertIn("report pending without a verdict", body)
        self.assertNotIn("End this review with exactly", body)

    def test_only_terminal_actions_deduplicate(self) -> None:
        for body in (gate.acted_marker(SHA), gate.acted_comment_body(SHA, "FIX"), f"{gate.acted_marker(SHA)}\nCodex gate: dispatching batched Devin review fixer (timeout) on 2026-10-15."):
            self.assertFalse(gate.has_terminal_action(SHA, [body]))
        for verdict in ("READY", "MEDIATE"):
            self.assertEqual(gate.has_terminal_action(SHA, [gate.acted_comment_body(SHA, verdict)]), verdict == "MEDIATE")


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

    def test_submitted_devin_findings_are_admitted(self) -> None:
        for state in ("commented", "changes_requested"):
            event = review_event(login=gate.DEVIN_REVIEW_LOGIN, body="finding")
            event["review"]["state"] = state
            self.assertTrue(gate.admit_resolve_pr("pull_request_review", event), state)

    def test_review_admission_requires_exact_bot_identity_action_and_state(self) -> None:
        cases = [
            ("login", gate.CODEX_REVIEW_LOGIN),
            ("login", "cursor[bot]"),
            ("login", "devin-ai-integration"),
            ("login", "Devin-ai-integration[bot]"),
            ("login", "devin-ai-integration[bot] "),
            ("type", "User"),
            ("type", "bot"),
            ("type", None),
            ("action", "edited"),
            ("action", "dismissed"),
            ("action", None),
            ("state", "approved"),
            ("state", "pending"),
            ("state", None),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                event = review_event(login=gate.DEVIN_REVIEW_LOGIN, body="finding")
                target = event if key == "action" else event["review"]
                if key in {"login", "type"}:
                    target = event["review"]["user"]
                target[key] = value
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
            "FIX": "ignored-fix",
            "READY": "ready",
            "MEDIATE": "mediate",
        }
        for verdict, action in cases.items():
            got = gate.decide_verdict(
                login="chatgpt-codex-connector[bot]",
                body=f"looks good\nCODEX-GATE: {verdict}\n",
                sha=SHA,
                comments=[],
            )
            self.assertEqual(got["action"], action, verdict)

    def test_verdict_ignores_non_codex(self) -> None:
        self.assertEqual(
            gate.decide_verdict(
                login=gate.DEVIN_REVIEW_LOGIN,
                body="CODEX-GATE: FIX",
                sha=SHA,
                comments=[],
            )["action"],
            "skip",
        )

    def test_historical_fix_markers_allow_ready_and_mediate(self) -> None:
        comments = [gate.acted_comment_body(SHA, "FIX"), f"{gate.acted_marker(SHA)}\nCodex gate: dispatching batched Devin review fixer (timeout) on 2026-10-15."]
        for verdict in ("READY", "MEDIATE"):
            got = gate.decide_verdict(login=gate.CODEX_REVIEW_LOGIN, body=f"CODEX-GATE: {verdict}", sha=SHA, comments=comments)
            self.assertEqual(got["action"], verdict.lower())

    def test_verdict_deduplicates_only_the_acted_head(self) -> None:
        for verdict in ("READY", "MEDIATE"):
            for head, expected in ((SHA, "already-acted" if verdict == "MEDIATE" else "ready"), ("b" * 40, verdict.lower())):
                with self.subTest(verdict=verdict, head=head):
                    got = gate.decide_verdict(
                        login=gate.CODEX_REVIEW_LOGIN,
                        body=f"CODEX-GATE: {verdict}",
                        sha=head,
                        comments=[gate.acted_comment_body(SHA, verdict)],
                    )
                    self.assertEqual(got["action"], expected)

    def test_legacy_fallback_is_inert(self) -> None:
        gh = mock.Mock(side_effect=AssertionError("fallback must not access GitHub"))
        with mock.patch.object(gate, "emit"):
            self.assertEqual(gate.run_fallback(REPO, "7", SHA, "pat", "token", "master", gh=gh), {"action": "ignored-fallback"})
        gh.assert_not_called()


class MainModeTests(unittest.TestCase):
    def check_mode(self, mode, argv, expected, *, process_argv=None) -> None:
        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_EVENT_PATH": str(event_path),
                "CODEX_TRIGGER_PAT": "test-pat",
                "GH_TOKEN": "test-read-token",
                "DEFAULT_BRANCH": "master",
                "PR_NUMBER": "7",
                "HEAD_SHA": SHA,
            }
            if mode is not None:
                env["CODEX_GATE_MODE"] = mode
            with (
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch.object(sys, "argv", process_argv or ["codex_gate.py"]),
                mock.patch.object(gate, "run_request") as request,
                mock.patch.object(gate, "run_verdict") as verdict,
                mock.patch.object(gate, "run_fallback") as fallback,
                mock.patch.object(gate, "emit") as emit,
            ):
                self.assertEqual(gate.main(argv), 0)
                handlers = {"request": request, "verdict": verdict, "fallback": fallback}
                for name, handler in handlers.items():
                    self.assertEqual(handler.call_count, int(name == expected), name)
                if expected == "request":
                    request.assert_called_once_with(event, REPO, "test-pat", "test-read-token")
                elif expected == "verdict":
                    verdict.assert_called_once_with(event, REPO, "test-pat", "test-read-token", "master")
                elif expected == "fallback":
                    fallback.assert_called_once_with(REPO, "7", SHA, "test-pat", "test-read-token", "master")
                else:
                    emit.assert_called_once_with({"action": "skip", "proceed": "false"})
                if expected:
                    emit.assert_not_called()

    def test_environment_selects_each_full_mode(self) -> None:
        for mode in ("request", "verdict", "fallback"):
            with self.subTest(mode=mode):
                self.check_mode(mode, [], mode)

    def test_cli_selects_each_mode_without_environment_override(self) -> None:
        for mode in ("request", "verdict", "fallback"):
            with self.subTest(mode=mode):
                self.check_mode(None, [mode], mode)

    def test_environment_takes_precedence_over_cli(self) -> None:
        self.check_mode("request", ["verdict"], "request")
        self.check_mode("unknown", ["request"], None)

    def test_missing_and_unknown_modes_skip(self) -> None:
        self.check_mode(None, [], None)
        self.check_mode(None, ["unknown"], None)
        self.check_mode("unknown", [], None)

    def test_empty_environment_falls_back_to_cli(self) -> None:
        self.check_mode("", ["fallback"], "fallback")

    def test_none_argv_reads_process_arguments_but_empty_argv_does_not(self) -> None:
        self.check_mode(None, None, "request", process_argv=["codex_gate.py", "request"])
        self.check_mode(None, [], None, process_argv=["codex_gate.py", "request"])


class ListingParseTests(unittest.TestCase):
    def test_parse_json_list_pages_and_fails_closed(self) -> None:
        self.assertEqual(gate._parse_json_list(""), [])
        self.assertEqual(gate._parse_json_list("[]"), [])
        self.assertEqual(
            gate._parse_json_list('[{"a": 1}]\n[{"a": 2}]'),
            [{"a": 1}, {"a": 2}],
        )
        self.assertEqual(
            gate._parse_json_list('[{"a": 1}][{"a": 2}]'),
            [{"a": 1}, {"a": 2}],
        )
        with self.assertRaises(gate.ListingError):
            gate._parse_json_list('{"not": "a list"}')
        with self.assertRaises(gate.ListingError):
            gate._parse_json_list('[{"a": 1}] trash')
        with self.assertRaises(gate.ListingError):
            gate._parse_json_list("not-json")

    def test_retry_listing_retries_transport_then_succeeds(self) -> None:
        n = {"c": 0}

        def fn():
            n["c"] += 1
            if n["c"] < 3:
                raise subprocess.CalledProcessError(1, ["gh"])
            return ["ok"]

        got = gate._retry_listing(fn, sleep=lambda _s: None)
        self.assertEqual(got, ["ok"])
        self.assertEqual(n["c"], 3)

    def test_retry_listing_does_not_retry_malformed(self) -> None:
        n = {"c": 0}

        def fn():
            n["c"] += 1
            raise gate.ListingError("malformed JSON listing")

        with self.assertRaises(gate.ListingError):
            gate._retry_listing(fn, sleep=lambda _s: None)
        self.assertEqual(n["c"], 1)


class ReadyHeadTests(unittest.TestCase):
    def test_only_live_same_repo_open_head_can_be_ready(self):
        valid = {"state": "open", "draft": False, "head": {"sha": SHA, "repo": {"full_name": REPO}}}
        self.assertTrue(gate.current_ready_head(REPO, "7", SHA, "read", gh=lambda *args: json.dumps(valid)))
        for pr in (valid | {"state": "closed"}, valid | {"draft": True},
                   valid | {"head": {"sha": "b" * 40, "repo": {"full_name": REPO}}},
                   valid | {"head": {"sha": SHA, "repo": {"full_name": "evil/fork"}}}):
            self.assertFalse(gate.current_ready_head(REPO, "7", SHA, "read", gh=lambda *args: json.dumps(pr)))


class NoticeAndIoTests(unittest.TestCase):
    def setUp(self):
        global READY_EVENT_READS
        READY_EVENT_READS = 0
        # Existing I/O fixtures isolate routing; current-head validation has its
        # own behavioral controls below and can be overridden per scenario.
        patcher = mock.patch.object(gate, "current_ready_head", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = mock.patch.object(gate, "readiness_blocked", return_value=False)
        ready.start()
        self.addCleanup(ready.stop)

    def test_run_request_missing_pat_emits_notice_and_does_not_post(self) -> None:
        calls: list[list[str]] = []

        def gh(args, token, input_text=None):
            calls.append(args)
            blob = gh_blob(args)
            self.assertNotIn("secret-pat", blob)
            if "/comments" in blob and "--input" not in args:
                return "[]"
            if "/reviews" in blob:
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
            blob = gh_blob(args)
            if "/comments" in blob and "--input" in args:
                posted.append(input_text or "")
                return ready_api_reply(args)
            if "/comments?" in blob:
                return "[]"
            if "/reviews?" in blob:
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

    def test_paused_findings_block_ready_even_with_old_ready_marker(self) -> None:
        comments = [{"id": 1, "body": gate.acted_comment_body(SHA, "READY")},
                    {"id": 2, "body": "trusted pause"}]
        calls = []
        def gh(args, token, input_text=None):
            calls.append((args, input_text))
            return json.dumps(comments) if "/comments?" in gh_blob(args) else ready_api_reply(args)
        helper = mock.Mock()
        helper.pending_ci_intent.return_value = ""
        helper.label_event_id.return_value = 0
        helper.latest_pause_id.return_value = 2
        helper.latest_ready_check.return_value = 0
        helper.marker.return_value = "checked"
        helper.readiness_blocked.return_value = True
        with mock.patch.object(gate, "fixer_helpers", return_value=helper), mock.patch.object(gate, "readiness_blocked", return_value=True), mock.patch.object(gate, "emit"):
            result = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: READY"), REPO, "pat", "read", "master", gh=gh)
        self.assertEqual(result["action"], "paused-findings")
        self.assertEqual(len(calls), 3)

    def test_pause_invalidates_old_ready_for_mediate_and_recovered_ready(self) -> None:
        comments = [{"id": 1, "body": gate.acted_comment_body(SHA, "READY")},
                    {"id": 2, "body": "trusted pause"}]
        for verdict in ("MEDIATE", "READY"):
            calls = []
            def gh(args, token, input_text=None):
                calls.append((args, input_text))
                return json.dumps(comments) if "/comments?" in gh_blob(args) else ready_api_reply(args)
            helper = mock.Mock()
            helper.pending_ci_intent.return_value = ""
            helper.label_event_id.return_value = 0
            helper.latest_pause_id.return_value = 2
            helper.latest_ready_check.return_value = 0
            helper.bot.return_value = False
            helper.marker.return_value = "checked"
            helper.readiness_blocked.return_value = False
            with mock.patch.object(gate, "fixer_helpers", return_value=helper), mock.patch.object(gate, "emit"):
                result = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body=f"CODEX-GATE: {verdict}"), REPO, "pat", "read", "master", gh=gh)
            self.assertEqual(result["action"], verdict.lower())
            self.assertGreater(len(calls), 1)
            if verdict == "MEDIATE":
                helper.readiness_blocked.assert_not_called()

    def test_pause_or_head_change_after_label_revokes_without_terminal_ready(self) -> None:
        for changed_head in (False, True):
            calls = []
            def gh(args, token, input_text=None):
                calls.append((args, input_text or ""))
                return "[]" if "/comments?" in gh_blob(args) else ready_api_reply(args)
            helper = mock.Mock()
            helper.pending_ci_intent.return_value = ""
            helper.label_event_id.return_value = 0
            helper.latest_pause_id.return_value = 0
            helper.readiness_blocked.side_effect = [False, True]
            helper.marker.return_value = "checked"
            helper.records.return_value = [{"event": 5}]
            helper.begin_ci_intent.return_value = {"nonce": "a" * 32}
            helper.record_ci_ownership.side_effect = lambda *args, **kwargs: args[-1](REPO, "7", "owned")
            head_checks = [True, False] if changed_head else [True, True]
            with mock.patch.object(gate, "fixer_helpers", return_value=helper), \
                 mock.patch.object(gate, "current_ready_head", side_effect=head_checks), \
                 mock.patch.object(gate, "readiness_blocked", side_effect=[False, True]), \
                 mock.patch.object(gate, "emit"):
                result = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: READY"), REPO, "pat", "read", "master", gh=gh)
            self.assertEqual(result["action"], "paused-findings")
            self.assertTrue(any("labels" in gh_blob(args) for args, _ in calls))
            helper.rollback_ci_event.assert_called_once()
            self.assertFalse(any("Codex gate acted: READY" in body for _, body in calls))

    def test_ready_failure_rolls_back_only_proven_event_and_reports_unknown_cleanup(self) -> None:
        for failure in ("ownership", "postcheck", "terminal", "cleanup", "human-override"):
            calls, event_reads, comment_reads = [], 0, 0
            def gh(args, token, input_text=None):
                nonlocal event_reads, comment_reads
                calls.append((args, input_text or ""))
                blob = gh_blob(args)
                if "/events?" in blob:
                    event_reads += 1
                    if event_reads <= 2:
                        return "[]"
                    if failure == "cleanup" and event_reads >= 4:
                        raise OSError("unavailable during cleanup")
                    identity = 6 if failure == "human-override" and event_reads >= 4 else 5
                    return json.dumps([{"id": identity, "event": "labeled", "label": {"name": "ci:full"}, "actor": {"login": "gate-writer" if identity == 5 else "human"}}])
                if "/comments?" in blob:
                    comment_reads += 1
                    if failure == "postcheck" and comment_reads > 2:
                        raise OSError("postcheck failed")
                    return "[]"
                if input_text:
                    body = json.loads(input_text).get("body", "")
                    if ((failure in {"ownership", "cleanup", "human-override"} and "claude-fixer-ci-owned:" in body)
                            or (failure == "checked" and "claude-fixer-ci-checked:" in body)
                            or (failure == "terminal" and "Codex gate acted: READY" in body)):
                        raise OSError("receipt post failed")
                return ready_api_reply(args)
            with mock.patch.object(gate, "emit"), mock.patch.object(gate, "notice") as notice:
                result = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: READY"), REPO, "pat", "read", "master", gh=gh)
            self.assertEqual(result["action"], "skip-readiness", failure)
            deletes = [args for args, _ in calls if "DELETE" in args]
            self.assertEqual(len(deletes), 0 if failure in {"cleanup", "human-override"} else 1, failure)
            if failure == "cleanup":
                self.assertTrue(any("inspect ci:full manually" in str(call) for call in notice.call_args_list))

    def test_run_verdict_ready_labels_and_mediate_dispatches(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def gh(args, token, input_text=None):
            calls.append((list(args), input_text or ""))
            blob = gh_blob(args)
            if "/comments?" in blob:
                return "[]"
            return ready_api_reply(args)

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

    def test_run_verdict_fix_never_writes_or_dispatches(self) -> None:
        calls = []
        def gh(args, token, input_text=None):
            calls.append(list(args))
            self.assertIsNone(input_text)
            self.assertEqual(args[:2], ["api", "--paginate"])
            return "[]"
        event = review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: FIX")
        with mock.patch.object(gate, "emit"):
            got = gate.run_verdict(event, REPO, "pat", "token", "master", gh=gh)
        self.assertEqual(got["action"], "ignored-fix")
        self.assertFalse(any("workflow" in c for c in calls))

    def test_fix_and_silence_have_no_side_effects(self) -> None:
        for body, expected in (("CODEX-GATE: FIX", "ignored-fix"), ("pending findings", "no-verdict"), ("", "no-verdict")):
            gh = mock.Mock(return_value="[]")
            with mock.patch.object(gate, "emit"):
                got = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body=body), REPO, "pat", "token", "master", gh=gh)
            self.assertEqual(got["action"], expected)
            for call in gh.call_args_list:
                self.assertEqual(call.args[0][:2], ["api", "--paginate"])
                self.assertLess(len(call.args), 3)

    def test_ready_and_mediate_work_after_legacy_fix_marker(self) -> None:
        for verdict in ("READY", "MEDIATE"):
            calls = []
            def gh(args, token, input_text=None):
                calls.append((args, input_text))
                if "/comments?" in gh_blob(args):
                    return json.dumps([{"body": gate.acted_comment_body(SHA, "FIX")}])
                return ready_api_reply(args)
            with mock.patch.object(gate, "emit"):
                got = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body=f"CODEX-GATE: {verdict}"), REPO, "pat", "token", "master", gh=gh)
            self.assertEqual(got["action"], verdict.lower())
            self.assertTrue(any(("/labels" in gh_blob(a)) if verdict == "READY" else ("stuck-thread-watch.yml" in a) for a, _ in calls))
            self.assertFalse(any("claude.yml" in a for a, _ in calls))

    def test_failed_dispatch_does_not_post_run_or_acted_marker(self) -> None:
        posted: list[str] = []

        def gh(args, token, input_text=None):
            if args[:2] == ["workflow", "run"]:
                raise subprocess.CalledProcessError(1, args)
            blob = gh_blob(args)
            if "/comments?" in blob:
                return "[]"
            if "--input" in args:
                posted.append(input_text or "")
            return ready_api_reply(args)

        event = review_event(login="chatgpt-codex-connector[bot]", body="CODEX-GATE: FIX")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_verdict(event, REPO, "pat", "read-token", "master", gh=gh, day="2026-10-15")
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertEqual(posted, [])

    def test_run_verdict_skips_fork_and_does_not_label(self) -> None:
        calls: list[list[str]] = []

        def gh(args, token, input_text=None):
            calls.append(list(args))
            return ready_api_reply(args)

        event = review_event(login="chatgpt-codex-connector[bot]", body="CODEX-GATE: READY")
        event["pull_request"]["head"]["repo"]["full_name"] = "evil/fork"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                got = gate.run_verdict(
                    event, REPO, "pat", "read-token", "master", gh=gh, day="2026-10-15"
                )
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertEqual(got["action"], "skip-fork")
        self.assertFalse(calls)

    def test_run_request_listing_failure_skips(self) -> None:
        def gh(args, token, input_text=None):
            return "not-json"

        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                got = gate.run_request(event, REPO, "pat", "read-token", gh=gh)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)
        self.assertEqual(got["action"], "skip-listing")
        self.assertEqual(got["proceed"], "false")

    def test_tokens_never_appear_in_argv(self) -> None:
        secret = "ghs_not_a_real_secret_value"

        def gh(args, token, input_text=None):
            blob = " ".join(args) + " " + (input_text or "")
            if secret in blob:
                raise AssertionError("secret leaked into gh argv")
            joined = gh_blob(args)
            if "/comments?" in joined:
                return "[]"
            if "/reviews?" in joined:
                return "[]"
            return ready_api_reply(args)

        event = pr_event()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GITHUB_OUTPUT"] = str(Path(tmp) / "out")
            try:
                gate.run_request(event, REPO, secret, secret, gh=gh)
            finally:
                os.environ.pop("GITHUB_OUTPUT", None)


class RealHelperLifecycleTests(unittest.TestCase):
    def test_gh_retains_rejection_for_classifier_without_printing_stderr(self):
        result = subprocess.CompletedProcess(["gh"], 1, "", "Forbidden (HTTP 403)")
        with mock.patch.object(gate.subprocess, "run", return_value=result), mock.patch("builtins.print") as printed:
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                gate._gh(["api", "example"], "token")
        self.assertEqual(gate.fixer_helpers().known_label_rejection(raised.exception), 403)
        printed.assert_not_called()

    def test_real_helper_lifecycle_race_recovery_and_human_override(self):
        helper = gate.fixer_helpers()
        state = {"head": SHA, "pending": [], "comments": [], "events": [], "label": False, "race": False}
        writer = {"login": "gate-writer", "id": 99, "type": "User"}
        actions = {"login": "github-actions[bot]", "id": helper.ACTIONS_ID, "type": "Bot"}
        item = {"root": 1, "thread": "PRRT_1", "fingerprint": "f" * 64}
        clock = 0
        def stamp():
            nonlocal clock
            clock += 1
            if state.get("same_stamp"):
                return "2026-10-16T00:00:00Z"
            return f"2026-10-16T00:00:00.{clock:06d}Z"
        def event(kind, actor):
            state["events"].append({"id": len(state["events"]) + 1, "event": kind, "created_at": stamp(), "label": {"name": "ci:full"}, "actor": actor})
        def gh(args, token, input_text=None):
            path = next((a for a in args if str(a).startswith("repos/")), "")
            if args[-1] == "user":
                return json.dumps(writer)
            if "/events?" in path:
                if state.get("lag_reads", 0):
                    state["lag_reads"] -= 1
                    return "[]"
                return json.dumps(state["events"])
            if "/comments?" in path:
                if state.pop("fail_next_comments", False):
                    raise OSError("transient postcheck")
                return json.dumps(state["comments"])
            if path == f"repos/{REPO}/pulls/7":
                return json.dumps({"state": "open", "draft": False, "head": {"sha": state["head"], "repo": {"full_name": REPO}}, "labels": [{"name": "ci:full"}] if state["label"] else []})
            if "/labels/ci:full" in path and "DELETE" in args:
                state["label"] = False
                event("unlabeled", actions)
                return "{}"
            if args[:2] == ["workflow", "run"]:
                return "{}"
            if path.endswith("/labels"):
                if state.pop("reject_label_post", False) or state.get("fail_rejected_receipt"):
                    raise subprocess.CalledProcessError(1, ["gh", "api"], stderr="Forbidden (HTTP 403)")
                if state.pop("fail_label_post", False):
                    raise OSError("POST failed before mutation")
                if not state["label"]:
                    state["label"] = True
                    event("labeled", writer)
                    if state.pop("lag_after_label", False):
                        state["lag_reads"] = 3
                return "{}"
            if path.endswith("/comments") and input_text:
                body = json.loads(input_text)["body"]
                if "claude-fixer-ci-rejected:" in body and state.pop("fail_rejected_receipt", False):
                    raise OSError("rejection receipt POST unavailable")
                if "claude-fixer-ci-owned:" in body:
                    if state.pop("fail_owned_post", False):
                        raise OSError("transient receipt POST")
                    if state.pop("fail_after_owned", False):
                        state["fail_next_comments"] = True
                # Pause after final live check, before terminal comment is written.
                if state["race"] and "Codex gate acted: READY" in body:
                    state["race"] = False
                    state["pending"] = [item]
                    helper.revoke_owned_ci(REPO, "7", state["head"], get_pages=pages, delete=delete, post_comment=post)
                state["comments"].append({"id": len(state["comments"]) + 1, "body": body, "created_at": stamp(), "user": writer if token == "pat" else actions})
                return "{}"
            raise AssertionError(args)
        def pages(path):
            return gate._parse_json_list(gh(["api", "--paginate", path + "?per_page=100"], "read"))
        def delete(repo, number):
            gh(["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/ci:full"], "read")
        def post(repo, number, body):
            gate.post_comment(repo, number, body, "read", gh=gh)
        def ready():
            return gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: READY", sha=state["head"]), REPO, "pat", "read", "master", gh=gh)
        with mock.patch.object(gate, "fixer_helpers", return_value=helper), mock.patch.object(helper, "live_state", side_effect=lambda *args: (state["pending"], state["pending"])), mock.patch.object(gate, "emit"):
            self.assertEqual(ready()["action"], "ready")
            self.assertTrue(state["label"])
            before = (len(state["comments"]), len(state["events"]))
            self.assertEqual(ready()["action"], "ready-present")
            self.assertEqual(before, (len(state["comments"]), len(state["events"])))
            state["pending"] = [item]
            self.assertEqual(ready()["action"], "paused-findings")
            self.assertFalse(state["label"])
            mediate = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: MEDIATE", sha=state["head"]), REPO, "pat", "read", "master", gh=gh)
            self.assertEqual(mediate["action"], "mediate")
            # A human can resolve the handoff without changing the commit.
            # Authenticated MEDIATE history must not prevent fresh READY.
            state["pending"] = []
            state["race"] = True
            self.assertEqual(ready()["action"], "ready")
            self.assertFalse(state["label"])
            state["pending"] = []
            self.assertEqual(ready()["action"], "ready")
            self.assertTrue(state["label"])
            for failure in ("fail_after_owned", "fail_owned_post"):
                state["pending"] = [item]
                self.assertEqual(ready()["action"], "paused-findings")
                state["pending"] = []
                state[failure] = True
                state["same_stamp"] = True
                self.assertEqual(ready()["action"], "skip-readiness")
                self.assertFalse(state["label"])
                self.assertEqual(ready()["action"], "ready")
                self.assertTrue(state["label"])
                state["same_stamp"] = False
            state["pending"] = [item]
            self.assertEqual(ready()["action"], "paused-findings")
            state["pending"] = []
            state["reject_label_post"] = True
            with mock.patch.object(gate, "notice") as notices:
                self.assertEqual(ready()["action"], "skip-readiness")
            self.assertTrue(any("safely retry" in str(call) for call in notices.call_args_list))
            self.assertFalse(any("inspect ci:full manually" in str(call) for call in notices.call_args_list))
            self.assertFalse(state["label"])
            self.assertEqual(ready()["action"], "ready")
            for failure in ("fail_label_post", "lag_after_label", "fail_rejected_receipt"):
                state["pending"] = [item]
                self.assertEqual(ready()["action"], "paused-findings")
                state["pending"] = []
                state[failure] = True
                self.assertEqual(ready()["action"], "skip-readiness")
                # Visibility can recover, but an uncertain old transaction is
                # never silently adopted as a new owned label on the next run.
                state["lag_reads"] = 0
                before_owned = sum("claude-fixer-ci-owned:" in c["body"] for c in state["comments"])
                self.assertEqual(ready()["action"], "unverified-ci-label")
                self.assertEqual(before_owned, sum("claude-fixer-ci-owned:" in c["body"] for c in state["comments"]))
                # Explicit human remove/readd settles the uncertainty. If POST
                # never took effect, the human first adds then removes it.
                human = {"login": "human", "id": 123, "type": "User"}
                if not state["label"]:
                    event("labeled", human)
                event("unlabeled", human)
                event("labeled", human)
                state["label"] = True
                self.assertEqual(ready()["action"], "ready-present")
                self.assertEqual(before_owned, sum("claude-fixer-ci-owned:" in c["body"] for c in state["comments"]))
                # Restore a fresh, unowned starting state for the next scenario.
                state["events"] = []
                state["comments"] = []
                state["label"] = False
                self.assertEqual(ready()["action"], "ready")
            state["head"] = "b" * 40
            stale = gate.run_verdict(review_event(login=gate.CODEX_REVIEW_LOGIN, body="CODEX-GATE: READY", sha=SHA), REPO, "pat", "read", "master", gh=gh)
            self.assertEqual(stale["action"], "skip-stale-head")
            self.assertTrue(state["label"])
            state["pending"] = [item]
            self.assertEqual(ready()["action"], "paused-findings")
            self.assertFalse(state["label"])
            state["pending"] = []
            self.assertEqual(ready()["action"], "ready")
            # A verified human removal remains authoritative on its observed head.
            state["label"] = False
            event("unlabeled", {"login": "human", "id": 123, "type": "User"})
            state["head"] = "c" * 40
            self.assertEqual(ready()["action"], "preserve-human-removal")
            self.assertFalse(state["label"])
            # Repeated same-head READY preserves the removal without comment spam.
            count = len(state["comments"])
            self.assertEqual(ready()["action"], "preserve-human-removal")
            self.assertEqual(len(state["comments"]), count)
            # A fresh READY for a subsequent head has a new authorization scope.
            state["head"] = "d" * 40
            self.assertEqual(ready()["action"], "ready")
            self.assertTrue(state["label"])

    def test_terminal_dedup_requires_authenticated_gate_writer(self):
        helper = gate.fixer_helpers()
        writer = {"login": "gate-writer", "id": 99, "type": "User"}
        for verdict in ("READY", "MEDIATE"):
            body = gate.acted_comment_body(SHA, verdict)
            forged = {"id": 1, "body": body, "user": {"login": "gate-writer", "id": 1, "type": "User"}}
            self.assertEqual(gate.comments_after_pause(SHA, [forged], writer), [])
            self.assertEqual(gate.comments_after_pause(SHA, [forged | {"user": writer}], writer), [] if verdict == "READY" else [body])


class WorkflowStructureTests(unittest.TestCase):
    def test_only_verdict_job_gets_authenticated_receipt_write_permission(self):
        text = WORKFLOW_PATH.read_text()
        request, verdict = text.split("  verdict:", 1)
        self.assertNotIn("issues: write", request)
        self.assertEqual(verdict.count("issues: write"), 1)
        self.assertIn("contents: read", verdict)
        self.assertIn("pull-requests: read", verdict)
        self.assertIn("scripts/ci/claude_fixer_receipts.py", verdict)

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.fixer = FIXER_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.agents = AGENTS_PATH.read_text(encoding="utf-8")

    def test_runs_on_ubuntu_latest_not_blacksmith(self) -> None:
        self.assertIn("runs-on: ubuntu-latest", self.text)
        self.assertNotIn("blacksmith", self.text)

    def test_no_wait_or_timeout_dispatch_job(self) -> None:
        self.assertNotIn("WAIT_SECONDS", self.text)
        self.assertNotIn("  wait:", self.text)
        self.assertNotIn("  fallback:", self.text)
        self.assertNotIn("sleep ", self.text)
        self.assertNotIn("FIXER_WORKFLOW", GATE_PATH.read_text())

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

    def test_concurrency_isolates_verdict_from_request_wait(self) -> None:
        self.assertIn(
            "codex-gate-${{ github.event.pull_request.number }}-${{ github.event_name == 'pull_request_review' && format('verdict-{0}', github.event.review.id) || 'request' }}",
            self.text,
        )
        self.assertIn(
            "cancel-in-progress: ${{ github.event_name != 'pull_request_review' }}",
            self.text,
        )

    def test_fixer_accepts_direct_devin_and_dispatch_triggers(self) -> None:
        start = self.fixer.index("  devin-review-fix:\n")
        nxt = self.fixer.find("\n  ", start + 4)
        while nxt != -1 and self.fixer[nxt + 3 : nxt + 4] in (" ", "\t"):
            nxt = self.fixer.find("\n  ", nxt + 1)
        job = self.fixer[start:] if nxt == -1 else self.fixer[start:nxt]
        self.assertIn("workflow_dispatch", job)
        self.assertIn("repository_dispatch", job)
        self.assertIn("pull_request_review", job)

    def test_agents_code_review_rules(self) -> None:
        self.assertIn("## Code Review Rules", self.agents)
        self.assertIn("CODEX-GATE: FIX", self.agents)
        self.assertIn("CODEX-GATE: READY", self.agents)
        self.assertIn("CODEX-GATE: MEDIATE", self.agents)
        self.assertIn("Parallel Task API deep research", self.agents)
        self.assertIn("addition to Search/Extract", self.agents)
        self.assertIn("options, tradeoffs,", self.agents)
        self.assertIn("target metrics", self.agents)
        agents_flat = " ".join(self.agents.split())
        self.assertIn("The developer decides", agents_flat)
        self.assertIn("a coding agent @mentions its developer", agents_flat)
        self.assertIn("does not decide", agents_flat)
        self.assertNotIn("then decide or @mention", agents_flat)
        self.assertIn("not an immediate `@codex review`", agents_flat)
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
