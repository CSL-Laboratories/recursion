#!/usr/bin/env python3
"""Codex gate verdicts are accepted only from the genuine Codex App (CSL-365).

    python -X utf8 -B tools/test_codex_gate_identity.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_codex_gate import REPO, SHA, WORKFLOW_PATH, gate, gh_blob

GENUINE = {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 199175422}
FORGED = {
    "bare alias as user": {"login": "chatgpt-codex-connector", "type": "User", "id": 1},
    "bare alias claiming Bot": {"login": "chatgpt-codex-connector", "type": "Bot", "id": 199175422},
    "bare alias without metadata": {"login": "chatgpt-codex-connector"},
    "app login typed as user": {"login": "chatgpt-codex-connector[bot]", "type": "User", "id": 199175422},
    "app login with wrong id": {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": 7},
    "app login with string id": {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": "199175422"},
    "app login with bool id": {"login": "chatgpt-codex-connector[bot]", "type": "Bot", "id": True},
    "app login with case change": {"login": "ChatGPT-Codex-Connector[bot]", "type": "Bot", "id": 199175422},
    "app login with whitespace": {"login": " chatgpt-codex-connector[bot]", "type": "Bot", "id": 199175422},
    "other bot": {"login": "devin-ai-integration[bot]", "type": "Bot", "id": 158243242},
}


def review_event(user: dict, verdict: str) -> dict:
    return {
        "action": "submitted",
        "review": {"user": dict(user), "body": f"CODEX-GATE: {verdict}", "commit_id": SHA, "state": "commented"},
        "pull_request": {"number": 7, "draft": False, "head": {"sha": SHA, "repo": {"full_name": REPO}}},
        "sender": dict(user),
    }


def stored_review(user: dict, verdict: str = "READY") -> dict:
    return {"user": dict(user), "commit_id": SHA, "body": f"CODEX-GATE: {verdict}"}


class RecordingGH:
    """Answers the reads run_verdict makes; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, args, token, input_text=None):
        self.calls.append((list(args), input_text or ""))
        blob = gh_blob(args)
        if args[-1] == "user":
            return json.dumps({"login": "gate-writer", "id": 99, "type": "User"})
        if "/comments?" in blob or "/events?" in blob:
            return "[]"
        if args[-1] == f"repos/{REPO}/pulls/7":
            return json.dumps({"state": "open", "draft": False, "head": {"sha": SHA, "repo": {"full_name": REPO}}})
        return "{}"

    def writes(self) -> list[list[str]]:
        return [args for args, payload in self.calls
                if payload or args[:2] == ["workflow", "run"] or "DELETE" in args or args[-1] == "user"]


def helper_mock() -> mock.Mock:
    helper = mock.Mock()
    helper.pending_ci_intent.return_value = ""
    helper.label_event_id.return_value = 0
    helper.ci_label_decision.return_value = "allow"
    helper.readiness_blocked.return_value = False
    helper.begin_ci_intent.return_value = None
    return helper


class IdentityTests(unittest.TestCase):
    def test_only_the_app_identity_is_a_codex_reviewer(self) -> None:
        self.assertEqual(GENUINE["id"], gate.CODEX_REVIEW_ID)
        self.assertTrue(gate.is_codex_reviewer(GENUINE))
        # Fixtures without REST metadata keep the unforgeable [bot] login.
        self.assertTrue(gate.is_codex_reviewer({"login": "chatgpt-codex-connector[bot]"}))
        self.assertTrue(gate.is_codex_reviewer({"login": "chatgpt-codex-connector[bot]", "type": "Bot"}))
        for name, user in FORGED.items():
            with self.subTest(name):
                self.assertFalse(gate.is_codex_reviewer(user))
        for junk in (None, {}, "chatgpt-codex-connector[bot]", [], {"login": None}):
            with self.subTest(junk=junk):
                self.assertFalse(gate.is_codex_reviewer(junk))
        for login in ("chatgpt-codex-connector", "CHATGPT-CODEX-CONNECTOR[BOT]", "chatgpt-codex-connector[bot] ", "", None):
            with self.subTest(login=login):
                self.assertFalse(gate.is_codex_login(login))
        self.assertTrue(gate.is_codex_login("chatgpt-codex-connector[bot]"))

    def test_forged_reviews_never_yield_a_verdict(self) -> None:
        for verdict in ("READY", "MEDIATE"):
            for name, user in FORGED.items():
                with self.subTest(verdict=verdict, forged=name):
                    self.assertFalse(gate.sha_has_verdict([stored_review(user, verdict)], SHA))
                    got = gate.decide_request(pat="pat", same_repo=True, draft=False, sha=SHA,
                                              comments=[gate.request_comment_body(SHA)],
                                              reviews=[stored_review(user, verdict)])
                    self.assertEqual(got["action"], "already")
                    self.assertEqual(gate.decide_verdict(login=user["login"], user=user, body=f"CODEX-GATE: {verdict}",
                                                         sha=SHA, comments=[])["action"], "skip")
            self.assertTrue(gate.sha_has_verdict([stored_review(GENUINE, verdict)], SHA))
            self.assertEqual(gate.decide_verdict(login=GENUINE["login"], user=GENUINE, body=f"CODEX-GATE: {verdict}",
                                                 sha=SHA, comments=[])["action"], verdict.lower())

    def test_supplied_user_object_overrides_the_login_argument(self) -> None:
        # A matching login string never rescues a user object that fails the type/id check.
        for name in ("app login typed as user", "app login with wrong id", "app login with string id"):
            with self.subTest(name):
                got = gate.decide_verdict(login=GENUINE["login"], user=FORGED[name], body="CODEX-GATE: READY", sha=SHA, comments=[])
                self.assertEqual(got["action"], "skip")

    def test_forged_review_event_never_labels_dispatches_or_reads_the_pat_user(self) -> None:
        for verdict in ("READY", "MEDIATE"):
            for name, user in FORGED.items():
                with self.subTest(verdict=verdict, forged=name):
                    gh = RecordingGH()
                    helper = helper_mock()
                    with mock.patch.object(gate, "fixer_helpers", return_value=helper), mock.patch.object(gate, "emit"):
                        got = gate.run_verdict(review_event(user, verdict), REPO, "pat", "read", "master", gh=gh)
                    self.assertEqual(got["action"], "skip")
                    self.assertEqual(gh.writes(), [])
                    helper.begin_ci_intent.assert_not_called()
                    helper.revoke_owned_ci.assert_not_called()

    def test_genuine_review_event_still_routes(self) -> None:
        for verdict in ("READY", "MEDIATE"):
            with self.subTest(verdict=verdict):
                gh = RecordingGH()
                with mock.patch.object(gate, "fixer_helpers", return_value=helper_mock()), mock.patch.object(gate, "emit"):
                    got = gate.run_verdict(review_event(GENUINE, verdict), REPO, "pat", "read", "master", gh=gh)
                self.assertEqual(got["action"], verdict.lower())
                if verdict == "READY":
                    self.assertTrue(any(f"repos/{REPO}/issues/7/labels" in args and "ci:full" in payload for args, payload in gh.calls))
                else:
                    self.assertTrue(any(args[:2] == ["workflow", "run"] and "stuck-thread-watch.yml" in args
                                        for args, _ in gh.calls))

    def test_workflow_filter_requires_the_exact_app_login_and_bot_type(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        verdict = text.split("\n  verdict:\n", 1)[1]
        condition = verdict.split("    if: >-\n", 1)[1].split("    runs-on:", 1)[0]
        self.assertIn("github.event.review.user.login == 'chatgpt-codex-connector[bot]'", condition)
        self.assertIn("github.event.review.user.type == 'Bot'", condition)
        self.assertNotIn("'chatgpt-codex-connector'", condition)
        self.assertNotIn("||", condition)

    def test_look_alikes_still_never_count_as_human_requests(self) -> None:
        # Bot-typed Codex identities (genuine or forged) never admit a human @claude
        # request, and no Codex identity is admitted as a Devin finding.
        for user in (GENUINE, *(u for u in FORGED.values() if u.get("type") == "Bot")):
            with self.subTest(user=user):
                event = {"sender": dict(user), "comment": {"body": "@claude fix", "user": dict(user)},
                         "issue": {"number": 7, "pull_request": {"url": "x"}}}
                self.assertFalse(gate.admit_resolve_pr("issue_comment", event))
                self.assertFalse(gate.admit_resolve_pr("pull_request_review_comment", event))
        for user in (GENUINE, *FORGED.values()):
            if user.get("login") == gate.DEVIN_REVIEW_LOGIN:
                continue
            with self.subTest(review=user):
                event = review_event(user, "READY")
                self.assertFalse(gate.admit_resolve_pr("pull_request_review", event))


if __name__ == "__main__":
    unittest.main()
