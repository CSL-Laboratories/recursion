#!/usr/bin/env python3
"""Event-matrix and structure tests for the Claude PR Fixer workflow.

Acceptance gate for .github/workflows/claude.yml:

    python -X utf8 -B tools/test_claude_pr_fixer.py

Optionally runs actionlint when it is on PATH.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "ci" / "claude_pr_fixer_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "claude.yml"
SCAN_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "conflict-scan.yml"
# CSL-414 Port C: conflict-scan.yml's conflict-resolve job calls this reusable writer per PR.
WRITER_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "claude-conflict-writer.yml"
WRITER_LOCK = "claude-${{ inputs.pr_number }}"
CONFLICT_STAGES = ("prepare", "model", "publish")
HEREDOC = "CLAUDE_PR_FIXER_GATE"
SCAN_CRON = 'cron: "11 */2 * * *"'
REPO = "acme/example"


def load_gate():
    spec = importlib.util.spec_from_file_location("claude_pr_fixer_gate", GATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load_gate()


def pr_payload(
    *,
    number: int = 42,
    state: str = "open",
    head_repo: str = REPO,
    head_ref: str = "feature/fix",
    head_sha: str = "a" * 40,
    author_association: str = "CONTRIBUTOR",
    author: str = "lucas",
) -> dict:
    return {
        "number": number,
        "state": state,
        "author_association": author_association,
        "user": {"login": author, "type": "User"},
        "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": head_repo}},
    }


def comment(
    *,
    body: str,
    association: str = "CONTRIBUTOR",
    user_type: str = "User",
    login: str = "alice",
) -> dict:
    return {
        "body": body,
        "author_association": association,
        "user": {"type": user_type, "login": login},
    }


# Repository permissions as the collaborators API would report them.
PERMS = {
    "lucas": "admin",
    "alice": "write",
    "maint": "maintain",
    "reader": "read",
    "mallory": "none",
}


def lookup(login: str) -> str:
    if login == "boom":
        raise RuntimeError("api down")
    return PERMS.get(login, "none")


def decide(
    event_name: str,
    event: dict,
    repository: str = REPO,
    pr: dict | None = None,
    permission_lookup=lookup,
) -> dict:
    return gate.evaluate(event_name, event, repository, pr=pr, permission_lookup=permission_lookup)


class MentionMatrixTests(unittest.TestCase):
    def test_member_whole_word_on_same_repo_pr_comment(self) -> None:
        event = {
            "comment": comment(body="please @claude fix the lint"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "true")
        self.assertNotIn("devin", got)  # CSL-645: the Devin path and its output are gone.
        self.assertEqual(got["same_repo"], "true")
        self.assertEqual(got["head_ref"], "feature/fix")
        self.assertEqual(got["head_sha"], "a" * 40)

    def test_member_mention_on_outsider_pr_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude please"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide(
            "issue_comment",
            event,
            pr=pr_payload(author="reader", author_association="MEMBER"),
        )
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["same_repo"], "true")

    def test_contributor_commenter_with_write_is_allowed(self) -> None:
        event = {
            "comment": comment(body="@claude fix", association="CONTRIBUTOR", login="alice"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "true")
        self.assertEqual(got["commenter_association"], "CONTRIBUTOR")
        self.assertEqual(got["commenter_permission"], "write")

    def test_read_only_commenter_is_rejected(self) -> None:
        event = {
            "comment": comment(body="@claude fix", association="MEMBER", login="reader"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["commenter_permission"], "read")

    def test_review_comment_mention(self) -> None:
        event = {
            "comment": comment(body="@claude"),
            "pull_request": pr_payload(),
        }
        got = decide("pull_request_review_comment", event)
        self.assertEqual(got["mention"], "true")
        self.assertEqual(got["head_ref"], "feature/fix")

    def test_plain_issue_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude fix this"),
            "issue": {"number": 7, "state": "open"},
        }
        got = decide("issue_comment", event)
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["reason"], "not-a-pr")

    def test_fork_pr_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude please"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload(head_repo="evil/fork"))
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["same_repo"], "false")

    def test_substring_mention_is_skipped(self) -> None:
        event = {
            "comment": comment(body="see @claude123 and claude"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "false")

    def test_email_like_mention_is_skipped(self) -> None:
        event = {
            "comment": comment(body="mail@claude.com"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "false")

    def test_bot_mention_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude", user_type="Bot"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "false")

    def test_outsider_mention_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude", association="MEMBER", login="mallory"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload())
        self.assertEqual(got["mention"], "false")

    def test_closed_pr_mention_is_skipped(self) -> None:
        event = {
            "comment": comment(body="@claude"),
            "issue": {"number": 42, "state": "closed", "pull_request": {"url": "x"}},
        }
        got = decide("issue_comment", event, pr=pr_payload(state="closed"))
        self.assertEqual(got["mention"], "false")


class MentionSafetyMatrixTests(unittest.TestCase):
    """Generic PR-author and head safety on the @claude path (formerly exercised via Devin, CSL-645)."""

    def _mention(self, *, commenter: str = "alice", **pr_kw) -> dict:
        return {
            "comment": comment(body="@claude please fix", login=commenter),
            "pull_request": pr_payload(**pr_kw),
        }

    def _decide(self, event: dict, **kw) -> dict:
        return decide("pull_request_review_comment", event, **kw)

    def test_main_fetches_current_pr_for_issue_comment_and_fails_closed(self) -> None:
        event = {
            "comment": comment(body="@claude please fix"),
            "issue": {"number": 42, "state": "open", "pull_request": {"url": "x"}},
        }
        env = {"GITHUB_EVENT_NAME": "issue_comment", "GITHUB_EVENT_PATH": "event.json", "GITHUB_REPOSITORY": REPO}
        for payload in (pr_payload(), pr_payload(head_sha="b" * 40), None):
            with mock.patch.dict(os.environ, env), mock.patch.object(gate, "load_event", return_value=event), \
                    mock.patch.object(gate, "fetch_pr", return_value=payload) as fetch, \
                    mock.patch.object(gate, "gh_permission", return_value=lookup), \
                    mock.patch.object(gate, "emit") as emit:
                self.assertEqual(gate.main(), 0)
                fetch.assert_called_once_with(REPO, 42)
                decision = emit.call_args.args[0]
                self.assertEqual(decision["mention"], "true" if payload is not None else "false")
                if payload is None:
                    self.assertEqual(decision["reason"], "pr-fetch-failed")
                else:
                    self.assertEqual(decision["head_sha"], payload["head"]["sha"])
        bad = {**event, "issue": {**event["issue"], "number": "x"}}
        with mock.patch.dict(os.environ, env), mock.patch.object(gate, "load_event", return_value=bad), \
                mock.patch.object(gate, "fetch_pr") as fetch, mock.patch.object(gate, "emit") as emit:
            self.assertEqual(gate.main(), 0)
            fetch.assert_not_called()
            self.assertEqual(emit.call_args.args[0]["reason"], "bad-pr-number")
            self.assertEqual(emit.call_args.args[0]["mention"], "false")

    def test_current_fetched_pr_head_overrides_event_head(self) -> None:
        got = self._decide(self._mention(), pr=pr_payload(head_sha="b" * 40))
        self.assertEqual(got["mention"], "true")
        self.assertEqual(got["head_sha"], "b" * 40)

    def test_owner_and_member_still_require_permission(self) -> None:
        for association in ("OWNER", "MEMBER"):
            self.assertEqual(self._decide(self._mention(author_association=association))["mention"], "true")
            self.assertEqual(
                self._decide(self._mention(author="reader", author_association=association))["mention"], "false"
            )

    def test_private_member_contributor_with_write_is_allowed(self) -> None:
        got = self._decide(self._mention(author_association="CONTRIBUTOR"))
        self.assertEqual(got["mention"], "true")
        self.assertEqual(got["pr_author_association"], "CONTRIBUTOR")
        self.assertEqual(got["pr_author_permission"], "admin")

    def test_maintain_author_is_allowed(self) -> None:
        self.assertEqual(self._decide(self._mention(author="maint"))["mention"], "true")

    def test_contributor_without_permission_is_rejected(self) -> None:
        got = self._decide(self._mention(author="mallory"))
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["pr_author_permission"], "none")

    def test_permission_lookup_error_fails_closed(self) -> None:
        got = self._decide(self._mention(author="boom"))
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["pr_author_permission"], "error")
        got = self._decide(self._mention(commenter="boom"))
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["commenter_permission"], "error")

    def test_no_lookup_fails_closed(self) -> None:
        self.assertEqual(self._decide(self._mention(), permission_lookup=None)["mention"], "false")

    def test_unsafe_login_is_not_looked_up(self) -> None:
        seen: list[str] = []

        def spy(login: str) -> str:
            seen.append(login)
            return "admin"

        got = self._decide(self._mention(author="../../evil"), permission_lookup=spy)
        self.assertEqual(got["mention"], "false")
        self.assertEqual(seen, [])
        got = self._decide(self._mention(commenter="../../evil"), permission_lookup=spy)
        self.assertEqual(got["mention"], "false")
        self.assertEqual(seen, ["lucas"])

    def test_unsafe_ref_is_rejected(self) -> None:
        got = self._decide(self._mention(head_ref="feat/../master"))
        self.assertEqual(got["head_ref"], "")
        self.assertEqual(got["head_sha"], "")
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["reason"], "unsafe-or-empty-ref")

    def test_missing_sha_is_rejected(self) -> None:
        for sha in ("not-a-sha", ""):
            got = self._decide(self._mention(head_sha=sha))
            self.assertEqual(got["head_sha"], "", sha)
            self.assertEqual(got["mention"], "false", sha)
            self.assertEqual(got["reason"], "unsafe-or-empty-ref", sha)

    def test_default_branch_ref_is_rejected(self) -> None:
        for name in ("master", "main", "HEAD"):
            got = self._decide(self._mention(head_ref=name))
            self.assertEqual(got["head_ref"], "", name)
            self.assertEqual(got["mention"], "false", name)

    def test_retired_devin_and_dispatch_events_start_nothing(self) -> None:
        # CSL-645: a Devin review or a dispatch no longer starts a fixer, even on a writer's PR.
        review = {
            "action": "submitted",
            "review": {"user": {"login": "devin-ai-integration[bot]", "type": "Bot"},
                       "commit_id": "a" * 40, "state": "commented", "id": 1},
            "pull_request": pr_payload(),
        }
        for event_name, event in (("pull_request_review", review),
                                  ("workflow_dispatch", {"inputs": {"pr_number": "42"}}),
                                  ("repository_dispatch", {"client_payload": {"pr_number": 42}})):
            with self.subTest(event=event_name):
                got = decide(event_name, event, pr=pr_payload())
                self.assertEqual(got["mention"], "false")
                self.assertNotIn("devin", got)
        for name in ("DEVIN_BOT", "DEVIN_STATES", "DISPATCH_EVENTS", "SAFE_PR_RE", "dispatch_pr_number"):
            self.assertFalse(hasattr(gate, name), name)


class WholeWordTests(unittest.TestCase):
    def test_patterns(self) -> None:
        self.assertTrue(gate.is_whole_word_mention("@claude"))
        self.assertTrue(gate.is_whole_word_mention("hey @claude!"))
        self.assertTrue(gate.is_whole_word_mention("`@claude`"))
        self.assertFalse(gate.is_whole_word_mention("@claude123"))
        self.assertFalse(gate.is_whole_word_mention("claude"))
        self.assertFalse(gate.is_whole_word_mention(""))
        self.assertFalse(gate.is_whole_word_mention(None))


# CSL-414 (the CSL-377 split): each writer is a trusted prepare, an untrusted model job and a trusted publish.
# CSL-645: the Devin writer (fixer-prepare, devin-review-fix, fixer-publish) is gone; only the @claude writer remains.
WRITER_STAGES = ("mention-prepare", "claude-mention", "mention-publish")
PREPARE_JOBS = ("mention-prepare",)
MODEL_JOBS = ("claude-mention",)
PUBLISH_JOBS = ("mention-publish",)
CLAUDE_JOBS = ("resolve-pr", "event-paths") + WRITER_STAGES
PR_LOCK = "claude-${{ needs.resolve-pr.outputs.pr_number }}"


def job_block(text: str, name: str) -> str:
    """One top-level job of a workflow, from its key to the next job."""
    marker = f"\n  {name}:\n"
    start = text.index(marker) + 1
    nxt = re.search(r"\n  \S", text[start + len(marker) - 1 :])
    return text[start:] if nxt is None else text[start : start + len(marker) - 1 + nxt.start()]


def step_script(text: str, marker: str) -> str:
    """The dedented `run: |` script of the step containing marker, ending at the next step or job."""
    start = text.index(marker)
    run = text.index("        run: |\n", start) + len("        run: |\n")
    end = re.search(r"\n      - |\n\n  \S|\n  \S", text[run:])
    return textwrap.dedent(text[run:] if end is None else text[run : run + end.start()])


def validate_shared_writer_queues(text: str, expected_groups: list[str]) -> None:
    """Strict independent gate for the one syntax feature actionlint cannot parse."""
    groups = []
    for match in re.finditer(r"(?m)^    concurrency:\n((?:      .*\n|\n)*)", text):
        entries = re.findall(r"(?m)^      ([a-z-]+): (.+)$", match.group(1))
        values = dict(entries)
        if values.get("group", "").startswith("claude-${{") or "queue" in values:
            if len(entries) != 3 or len(values) != 3 or set(values) != {"group", "queue", "cancel-in-progress"}:
                raise ValueError("writer concurrency must have three unique literal keys")
            if values["queue"] != "max" or values["cancel-in-progress"] != "false":
                raise ValueError("writer must retain up to 100 pending jobs and one active job")
            groups.append(values["group"])
    if sorted(groups) != sorted(expected_groups):
        raise ValueError("all writers must use their shared per-PR concurrency group")
    if len(re.findall(r"(?m)^\s*queue:", text)) != len(expected_groups):
        raise ValueError("unexpected or incorrectly indented queue key")


class SharedQueuePolicyTests(unittest.TestCase):
    def test_all_three_shared_writers_use_bounded_fifo_queue(self) -> None:
        # CSL-645: the @claude writer, the conflict writer and the Codex journal writer are the
        # only jobs that take the claude-<PR> group; the Devin writer is gone.
        claude = WORKFLOW_PATH.read_text()
        validate_shared_writer_queues(claude, [PR_LOCK] * len(WRITER_STAGES))
        locked = sorted(name for name in CLAUDE_JOBS if "group: claude-${{" in job_block(claude, name))
        self.assertEqual(locked, sorted(WRITER_STAGES))
        # Port C: the scan caller holds no lock (it would deadlock its inner jobs); the called writer's
        # prepare, model and publish each take the shared per-PR lock.
        validate_shared_writer_queues(SCAN_WORKFLOW_PATH.read_text(), [])
        validate_shared_writer_queues(WRITER_WORKFLOW_PATH.read_text(), [WRITER_LOCK] * len(CONFLICT_STAGES))
        validate_shared_writer_queues((ROOT / ".github/workflows/codex-gate.yml").read_text(), ["claude-${{ github.event.pull_request.number }}"])

    def test_mutations_cannot_hide_behind_actionlint_compatibility_exception(self) -> None:
        original = WORKFLOW_PATH.read_text()
        for bad in (
            original.replace("queue: max", "queue: single", 1),
            original.replace("cancel-in-progress: false", "cancel-in-progress: true", 1),
            original.replace("      queue: max\n", "", 1),
            original.replace("      queue: max\n", "      queue: max\n      queue: single\n", 1),
            original.replace("group: claude-${{", "group: independent-${{", 1),
        ):
            with self.assertRaises(ValueError):
                validate_shared_writer_queues(bad, [PR_LOCK] * len(WRITER_STAGES))

    def test_every_split_writer_stage_takes_the_per_pr_lock(self) -> None:
        # GitHub releases the group between jobs, so prepare, model and publish each queue
        # for it again; publish then rechecks the live head and pause itself.
        text = WORKFLOW_PATH.read_text()
        for name in WRITER_STAGES:
            with self.subTest(job=name):
                job = job_block(text, name)
                validate_shared_writer_queues(job, [PR_LOCK])
                head = job.split("    steps:", 1)[0]
                self.assertEqual(head.count("    concurrency:\n"), 1)
        for name in ("resolve-pr", "event-paths"):
            self.assertNotIn("concurrency:", job_block(text, name), name)
        writer = WRITER_WORKFLOW_PATH.read_text()
        for name in CONFLICT_STAGES:
            with self.subTest(job=name):
                job = job_block(writer, name)
                validate_shared_writer_queues(job, [WRITER_LOCK])
                self.assertEqual(job.split("    steps:", 1)[0].count("    concurrency:\n"), 1)
        for bad in (writer.replace("queue: max", "queue: single", 1),
                    writer.replace("cancel-in-progress: false", "cancel-in-progress: true", 1),
                    writer.replace("group: claude-${{ inputs.pr_number }}", "group: other-${{ inputs.pr_number }}", 1)):
            with self.assertRaises(ValueError):
                validate_shared_writer_queues(bad, [WRITER_LOCK] * len(CONFLICT_STAGES))
        self.assertNotIn("concurrency:", job_block(SCAN_WORKFLOW_PATH.read_text(), "conflict-resolve"))

    def test_codex_journal_writer_lock_rejects_queue_and_group_drift(self):
        workflow = (ROOT / ".github/workflows/codex-gate.yml").read_text()
        group = "claude-${{ github.event.pull_request.number }}"
        for bad in (workflow.replace("queue: max", "queue: min"),
                    workflow.replace("cancel-in-progress: false", "cancel-in-progress: true"),
                    workflow.replace(group, "codex-independent")):
            with self.assertRaises(ValueError):
                validate_shared_writer_queues(bad, [group])
        verdict = workflow.split("  verdict:", 1)[1]
        self.assertIn("github.event_name == 'pull_request_review'", verdict)
        self.assertNotIn("gh run watch", verdict)
        self.assertNotIn("workflow_run", verdict)

    def test_actionlint_exception_is_exact_and_path_scoped(self) -> None:
        config = (ROOT / ".github/actionlint.yaml").read_text()
        pattern = '^unexpected key "queue" for "concurrency" section\\. expected one of "cancel-in-progress", "group"$'
        self.assertEqual(config.count("      - '" + pattern + "'"), 4)
        self.assertEqual(config.count("    ignore:"), 4)
        self.assertIn("  .github/workflows/claude.yml:", config)
        self.assertIn("  .github/workflows/conflict-scan.yml:", config)
        self.assertIn("  .github/workflows/claude-conflict-writer.yml:", config)
        self.assertIn("  .github/workflows/codex-gate.yml:", config)
        # CSL-408: every job is GitHub-hosted, so no custom runner labels are declared.
        self.assertNotIn("self-hosted-runner", config)
        self.assertNotIn("blacksmith", config)


class WorkflowStructureTests(unittest.TestCase):
    def test_light_jobs_use_github_hosted_runners(self) -> None:
        for name in CLAUDE_JOBS:
            job = self._job(name)
            self.assertIn("runs-on: ubuntu-latest", job.split("timeout-minutes", 1)[0], name)

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_no_workflow_level_concurrency(self) -> None:
        head = self.text.split("jobs:", 1)[0]
        self.assertNotIn("\nconcurrency:", head)
        self.assertFalse(head.lstrip().startswith("concurrency:"))

    def test_job_level_concurrency_does_not_cancel(self) -> None:
        self.assertIn("group: claude-${{ needs.resolve-pr.outputs.pr_number }}", self.text)
        self.assertIn("cancel-in-progress: false", self.text)
        for name in WRITER_STAGES:
            self.assertIn("concurrency:", self._job(name), name)

    def test_only_the_mention_writer_jobs_remain(self) -> None:
        # CSL-645: fixer-prepare, devin-review-fix and fixer-publish were deleted with the Devin path.
        self.assertEqual(re.findall(r"(?m)^  ([a-z][a-z0-9-]*):$", self.text.split("\njobs:\n", 1)[1]), list(CLAUDE_JOBS))
        for name in ("fixer-prepare", "devin-review-fix", "fixer-publish"):
            self.assertNotIn(f"\n  {name}:\n", self.text, name)
            self.assertNotIn(f"needs.{name}.", self.text, name)
        self.assertNotIn("needs.resolve-pr.outputs.devin", self.text)

    def test_privileged_jobs_require_gate_outputs(self) -> None:
        mention = self._job("claude-mention")
        self.assertIn("needs.resolve-pr.outputs.mention == 'true'", mention)
        self.assertIn("needs.resolve-pr.outputs.same_repo == 'true'", mention)
        # CSL-414: the trusted prepare job admits the same events; every later stage needs a
        # successful, unskipped prepare from this run attempt.
        prepares = {"mention-prepare": ("needs.resolve-pr.outputs.mention == 'true'",)}
        for name, needles in prepares.items():
            header = self._job(name).split("    steps:", 1)[0]
            for needle in needles + ("needs.resolve-pr.outputs.same_repo == 'true'",):
                self.assertIn(needle, header, name)
        for prepare, model, publish in zip(PREPARE_JOBS, MODEL_JOBS, PUBLISH_JOBS):
            for name in (model, publish):
                header = self._job(name).split("    steps:", 1)[0]
                self.assertIn(f"needs.{prepare}.result == 'success'", header, name)
                self.assertIn(f"needs.{prepare}.outputs.skip == 'false'", header, name)
                self.assertIn(f"needs.{prepare}.outputs.run_attempt == github.run_attempt", header, name)
            header = self._job(publish).split("    steps:", 1)[0]
            self.assertIn("!cancelled()", header)
            self.assertIn(f"needs.{model}.result != 'skipped'", header)

    def test_resolve_pr_job_if_skips_bots(self) -> None:
        resolve = self._job("resolve-pr")
        header = resolve.split("runs-on:", 1)[0]
        self.assertIn("github.event.sender.type != 'Bot'", header)
        self.assertIn("github.event.comment.user.type != 'Bot'", header)
        self.assertIn("contains(github.event.comment.body, '@claude')", header)
        self.assertNotIn("chatgpt-codex-connector[bot]", header)
        self.assertNotIn(
            "github.event_name == 'pull_request_review_comment' ||",
            header,
        )

    def test_every_privileged_checkout_uses_pr_head_sha(self) -> None:
        mention = self._job("claude-mention")
        needle = "ref: ${{ needs.resolve-pr.outputs.head_sha }}"
        self.assertIn("ref: ${{ needs.mention-prepare.outputs.head_sha }}", mention)
        self.assertIn("head_sha: ${{ steps.live.outputs.head_sha }}", self._job("mention-prepare"))
        self.assertNotIn(needle, mention)
        # Only the untrusted model jobs check out PR files, without a persisted credential.
        for name in MODEL_JOBS:
            job = self._job(name)
            self.assertEqual(job.count("uses: actions/checkout@v4"), 1, name)
            self.assertIn("persist-credentials: false", job, name)
        for name in PREPARE_JOBS + PUBLISH_JOBS:
            self.assertNotIn("actions/checkout", self._job(name), name)
        self.assertNotIn("ref: ${{ needs.resolve-pr.outputs.head_ref }}", mention)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.ref }}", mention)

    def test_privileged_jobs_attach_branch_before_push(self) -> None:
        attach = 'git checkout -B "$HEAD_REF" "$HEAD_SHA"'
        # CSL-414: the model job cannot push; only the trusted publisher pushes.
        for name in MODEL_JOBS:
            job = self._job(name)
            self.assertIn(attach, job, name)
            self.assertLess(job.index(attach), job.index("uses: anthropics/claude-code-action@"), name)
            allow = job.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
            deny = job.split("--disallowedTools", 1)[1].split("\n", 1)[0]
            self.assertNotIn("git push", allow, name)
            for rule in ("Bash(git push:*)", "Bash(git config:*)", "Bash(git diff --no-index:*)"):
                self.assertIn(rule, deny, name)
            self.assertNotIn("HEAD:refs/heads/", job, name)

    def test_prompt_treats_review_and_files_as_untrusted(self) -> None:
        self.assertIn("UNTRUSTED DATA", self.text)
        self.assertIn("untrusted", self.text.lower())

    def test_tool_allowlist_and_denials(self) -> None:
        self.assertIn("--allowedTools", self.text)
        self.assertIn("--disallowedTools", self.text)
        self.assertIn("WebSearch", self.text)
        self.assertIn("WebFetch", self.text)
        self.assertIn(".github/workflows", self.text)
        # CSL-645: no job reads Devin review comments any more.
        self.assertNotIn("include_comments_by_actor", self.text)

    def test_devin_and_dispatch_triggers_are_removed(self) -> None:
        # CSL-645: Devin retires; only human @claude comments (plus the event-paths self-test) trigger.
        on = self.text.split("\njobs:\n", 1)[0]
        on_block = re.split(r"\n(?=\S)", on.split("\non:\n", 1)[1], maxsplit=1)[0]
        triggers = re.findall(r"(?m)^  ([a-z_]+):", on_block)
        self.assertEqual(triggers, ["issue_comment", "pull_request_review_comment", "pull_request"])
        for trigger in ("pull_request_review", "workflow_dispatch", "repository_dispatch"):
            self.assertNotIn(f"\n  {trigger}:\n", on, trigger)
        header = self._job("resolve-pr").split("runs-on:", 1)[0]
        self.assertNotIn("devin-ai-integration", header)
        for event in ("pull_request_review'", "workflow_dispatch", "repository_dispatch"):
            self.assertNotIn(event, header, event)
        # The @claude path stays permission-gated by the collaborators API, not author_association.
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn("collaborators/{login}/permission", source)
        self.assertNotIn("MEMBER_ASSOC", source)
        self.assertNotIn("DISPATCH_EVENTS", source)
        self.assertNotIn("DEVIN", source)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self._job("resolve-pr"))

    ACTION_SHA = "a5a423f75c251dcbff8a74e99f83b2cb03c72bfa"
    ACTION_TAG = "v1.0.256"
    ACTION_USES = f"anthropics/claude-code-action@{ACTION_SHA}"

    def test_model_jobs_install_and_run_no_repository_code(self) -> None:
        # CSL-414: the model job holds the model credential, so it installs no PR-controlled
        # dependencies and runs no repository code; the required linux job tests the pushed commit.
        for name in MODEL_JOBS:
            job = self._job(name)
            for needle in ("actions/setup-python", "pip install", "requirements-ci.txt", "tools/ci_gate.py --force",
                           "python -X utf8", "python3 -X utf8"):
                self.assertNotIn(needle, job, name)
            self.assertNotIn("continue-on-error", job, name)
            action = job.index("uses: anthropics/claude-code-action@")
            self.assertIn("--setting-sources user", job, name)
            self.assertIn('CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"', job[action:], name)
            self.assertIn("DISABLE_UPDATES", job[action:], name)
            self.assertLess(job.index("command -v bwrap"), action, name)
            self.assertNotIn("anthropics/claude-code-action@v1", job)
            self.assertIn("anthropics/claude-code-action@a5a423f75c251dcbff8a74e99f83b2cb03c72bfa", job)
            self.assertIn("path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude", job)
            self.assertIn("Install hash-pinned Claude Code CLI", job, name)

    def test_privileged_pip_isolates_sys_path_and_reads_trusted_requirements(self) -> None:
        # CSL-414: model jobs hold the model credential and must not pip.
        # CSL-471: linux CI jobs that still run pip do it isolated (python -P).
        for name in MODEL_JOBS:
            job = self._job(name)
            with self.subTest(job=name):
                self.assertNotIn("pip install", job)
                self.assertNotIn("actions/setup-python", job)
                self.assertNotIn("requirements-ci.txt", job)
                self.assertNotIn("cache: pip", job)
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('python -P -m pip install --only-binary=:all: -r "$REQUIREMENTS_CI"', ci)
        self.assertNotIn("python -m pip install", ci)
        self.assertNotIn("cache: pip", ci)

    def test_claude_code_action_is_pinned_to_resolved_v1_commit(self) -> None:
        pin = f"uses: {self.ACTION_USES} # {self.ACTION_TAG}"
        self.assertEqual(self.text.count(pin), len(MODEL_JOBS))
        self.assertNotIn("uses: anthropics/claude-code-action@v1\n", self.text)
        writer = WRITER_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn(pin, writer)
        self.assertNotIn("uses: anthropics/claude-code-action@v1\n", writer)
        scan = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("anthropics/claude-code-action@", scan)
        contract = (ROOT / "docs" / "ci-event-contract.md").read_text(encoding="utf-8")
        self.assertIn(self.ACTION_USES, contract)
        self.assertNotIn("anthropics/claude-code-action@v1", contract)

    def test_acceptance_gates_before_push(self) -> None:
        for name in MODEL_JOBS:
            job = self._job(name)
            self.assertIn("acceptance gate", job.lower(), name)
            self.assertIn("tools/ci_gate.py", job, name)
            allow = job.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
            self.assertNotIn("ci_gate", allow, name)
            self.assertIn("do not run tests, linters or any repository code", job.lower(), name)
            self.assertIn("linux CI job runs the acceptance gate on the pushed commit", job, name)

    def test_embedded_gate_matches_source_file(self) -> None:
        start = f"python3 - <<'{HEREDOC}'\n"
        self.assertIn(start, self.text)
        after = self.text.split(start, 1)[1]
        closer = after.find(HEREDOC)
        self.assertGreater(closer, 0)
        embedded = textwrap.dedent(after[:closer]).rstrip("\n")
        source = GATE_PATH.read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual(embedded, source)

    def test_plain_issues_are_not_resolved(self) -> None:
        resolve = self._job("resolve-pr")
        self.assertIn("github.event.issue.pull_request", resolve)
        self.assertIn("head_sha: ${{ steps.pr.outputs.head_sha }}", resolve)

    def _job(self, name: str) -> str:
        text = getattr(self, "text", None) or WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn(f"\n  {name}:\n", text, name)
        return job_block(text, name)


class GateCliTests(unittest.TestCase):
    """Run the gate as a process with a stub gh that checks token and login."""

    def _run_cli(self, *, token: str | None, comment_login: str = "alice") -> str:
        event = {
            "comment": comment(body="@claude please", login=comment_login),
            "issue": {"number": 9, "state": "open", "pull_request": {"url": "x"}},
            "pull_request": pr_payload(number=9),
        }
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            output_path = Path(tmp) / "out.txt"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                '[ "$GH_TOKEN" = "stub-token" ] || exit 2\n'
                'case "$*" in\n'
                f'  "api repos/{REPO}/collaborators/lucas/permission") '
                "echo '{\"permission\": \"admin\"}' ;;\n"
                f'  "api repos/{REPO}/collaborators/alice/permission") '
                "echo '{\"permission\": \"write\"}' ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            env = {
                k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")
            }
            env.update(
                {
                    "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                    "GITHUB_EVENT_NAME": "issue_comment",
                    "GITHUB_REPOSITORY": REPO,
                    "GITHUB_EVENT_PATH": str(event_path),
                    "GITHUB_OUTPUT": str(output_path),
                }
            )
            if token is not None:
                env["GH_TOKEN"] = token
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(GATE_PATH)],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return output_path.read_text(encoding="utf-8")

    def test_cli_writes_outputs_for_writer_mention(self) -> None:
        body = self._run_cli(token="stub-token")
        self.assertIn("mention=true", body)
        self.assertIn("head_ref=feature/fix", body)
        self.assertIn("head_sha=" + "a" * 40, body)
        self.assertIn("pr_author_association=CONTRIBUTOR", body)
        self.assertIn("pr_author_permission=admin", body)
        self.assertIn("commenter_permission=write", body)

    def test_cli_fails_closed_without_token(self) -> None:
        body = self._run_cli(token=None)
        self.assertIn("mention=false", body)
        self.assertIn("pr_author_permission=error", body)

    def test_cli_looks_up_the_actual_commenter(self) -> None:
        body = self._run_cli(token="stub-token", comment_login="mallory")
        self.assertIn("mention=false", body)
        self.assertIn("commenter=mallory", body)
        self.assertIn("commenter_permission=error", body)


DENIALS_PATH = ROOT / "scripts" / "ci" / "claude_denials.py"
DENIALS_HEREDOC = "CLAUDE_DENIALS_REPORT"


def load_denials():
    spec = importlib.util.spec_from_file_location("claude_denials", DENIALS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FixerGateAndPromptTests(unittest.TestCase):
    GATES = ("Bash(python -X utf8 -B tools/ci_gate.py --force)", "Bash(python3 -X utf8 -B tools/ci_gate.py --force)")
    HELPER = "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/model-api --:*)"

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        return WorkflowStructureTests._job(self, name)  # type: ignore[arg-type]

    def test_gates_are_single_commands_allowlisted_exactly(self) -> None:
        for job in MODEL_JOBS:
            body = self._job(job)
            # CSL-414: the fixed root-owned helper is the only Bash rule; the model job runs no gate.
            allow = body.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
            self.assertEqual(re.findall(r"Bash\([^)]*\)", allow), [self.HELPER], job)
            for gate_tool in self.GATES:
                self.assertNotIn(gate_tool, body, job)
            self.assertNotIn("Bash(cd ", body, job)
            self.assertNotIn("cd app &&", body, job)
            self.assertNotIn("ci_gate.py:*", body, job)
            self.assertNotIn("pytest:*", body, job)
            self.assertNotIn("ruff check:*", body, job)

    def test_fixer_prompt_requires_sha_replies_and_failure_reply(self) -> None:
        for job in MODEL_JOBS:
            body = self._job(job)
            self.assertIn("prompt: |", body, job)
            self.assertIn("commit SHA", body, job)
            self.assertIn("do NOT resolve the thread", body, job)
            self.assertIn("claude-fixer-guard/model-api -- reply COMMENT_ID BODY_FILE", body, job)
            allow = body.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
            self.assertIn(self.HELPER, allow, job)
            self.assertNotIn("resolveReviewThread", allow, job)
            self.assertNotIn("graphql", allow, job)
            # CSL-414: the model resolves no thread at all; the watcher or a human does.
            self.assertIn("Do not resolve threads or run GraphQL mutations", body, job)
            self.assertIn("do NOT push", body, job)
            self.assertIn("PUBLISHING: you have no push access and must not try", body, job)
            self.assertIn("NEVER push or force-push", body, job)
            self.assertIn("Never exit without either committing a fix or queuing that failure reply", body, job)
            self.assertIn("UNTRUSTED DATA", body, job)
            self.assertIn("(one operation per call)", body, job)
            self.assertIn("no heredocs, loops, or &&", body, job)
            self.assertNotIn("git commit -m", body, job)
            self.assertIn("model-api -- commit .claude-fixer-replies/NAME.txt", body, job)
            # CSL-414 B': the commit is built after the job, so the SHA is a token the publisher fills in
            # only from its verified push; the model never writes a SHA of its own.
            self.assertIn("It records your one commit intent and never changes .git; a second commit is refused.", body, job)
            self.assertIn("write {{CLAUDE_COMMIT_SHA}}: the workflow replaces it with the full pushed SHA, or drops that record",
                          " ".join(body.split()), job)
            self.assertNotIn("prints the full 40/64-character commit SHA", body, job)
            self.assertIn("use Edit/Write for files", body, job)
            self.assertIn("linux CI job checks the pushed commit", body, job)
            self.assertIn("Linear-first (CSL-88)", body, job)
            self.assertIn("has no Linear, Context7 or Parallel tools", body, job)
            self.assertIn("Context7", body, job)
            self.assertIn("Parallel", body, job)
            self.assertIn("PR author relays status and blockers to Linear", body, job)
            self.assertIn("exact helper commands below", body, job)
            self.assertIn("Do not install tools, request access", body, job)

    def test_denials_are_reported_after_each_fixer(self) -> None:
        for job in MODEL_JOBS:
            body = self._job(job)
            self.assertIn("id: claude", body, job)
            self.assertIn("Report tool-permission denials", body, job)
            self.assertIn("steps.claude.outputs.execution_file", body, job)
            self.assertNotIn("show_full_output", body, job)

    def test_embedded_denials_match_source(self) -> None:
        start = f"python3 - \"$EXECUTION_FILE\" <<'{DENIALS_HEREDOC}'\n"
        combined = self.text + "\n" + WRITER_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(self.text.count(start), len(MODEL_JOBS))
        self.assertEqual(combined.count(start), len(MODEL_JOBS) + 1)
        # Port C: the denials report runs in the conflict writer's model job, not in the scan caller.
        self.assertNotIn(start, SCAN_WORKFLOW_PATH.read_text(encoding="utf-8"))
        source = DENIALS_PATH.read_text(encoding="utf-8").rstrip("\n")
        for chunk in combined.split(start)[1:]:
            embedded = textwrap.dedent(chunk[: chunk.find(DENIALS_HEREDOC)]).rstrip("\n")
            self.assertEqual(embedded, source)


class DenialsReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_denials()

    def _write(self, tmp: str, data) -> str:
        path = Path(tmp) / "exec.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def _report(self, data) -> list:
        with tempfile.TemporaryDirectory() as tmp:
            return self.mod.report(self._write(tmp, data))

    def _bash(self, command: str) -> dict:
        return {"type": "result", "permission_denials": [
            {"tool_name": "Bash", "tool_input": {"command": command}}]}

    def test_reports_tool_and_command_shape(self) -> None:
        lines = self._report([
            {"type": "assistant", "message": "secret-ish text"},
            {
                "type": "result",
                "permission_denials": [
                    {"tool_name": "Bash", "tool_input": {"command": "cd app && ruff check ."}},
                    {"tool_name": "Edit", "tool_input": {"file_path": ".github/workflows/x.yml"}},
                ],
            },
        ])
        self.assertEqual(lines[0], "claude-denials: count=2")
        self.assertIn("tool=Bash input=cd app && ruff check .", lines[1])
        self.assertIn("tool=Edit input=.github/workflows/x.yml", lines[2])
        self.assertNotIn("secret-ish", "\n".join(lines))

    def test_collects_denials_from_every_result(self) -> None:
        lines = self._report([self._bash("cd app"), {"type": "result", "permission_denials": []}])
        self.assertEqual(lines[0], "claude-denials: count=1")

    def test_masks_arguments_and_credentials(self) -> None:
        token = "ghp_" + "A" * 36
        for command, secret in (
            (f"git push https://x:{token}@github.com main", token),
            ("mysql -p hunter2 db", "hunter2"),
            ("curl -H Authorization:pw123 https://example.com", "pw123"),
            ("export PASS=s3cr3t", "s3cr3t"),
        ):
            out = "\n".join(self._report(self._bash(command)))
            self.assertNotIn(secret, out, command)
            self.assertIn("[arg]", out, command)

    def test_known_gate_commands_stay_readable(self) -> None:
        for command in (
            "python3 -X utf8 -B tools/ci_gate.py --force",
            "python -m pytest -q app/tests",
            "ruff check app",
        ):
            self.assertIn("input=" + command, self._report(self._bash(command))[1])

    def test_truncates_long_input(self) -> None:
        line = self._report(self._bash("echo " + "x " * 300))[1]
        self.assertIn("...(+", line)
        self.assertLess(len(line), 160)

    def test_summary_cannot_inject_markup(self) -> None:
        bad = {"type": "result", "permission_denials": [
            {"tool_name": "Bash`![x](http://e)", "tool_input": {"command": "`![img](http://e)`"}}]}
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
            subprocess.run(
                [sys.executable, "-X", "utf8", "-B", str(DENIALS_PATH), self._write(tmp, bad)],
                env=env, check=True, capture_output=True, text=True,
            )
            text = summary.read_text(encoding="utf-8")
        self.assertIn("```text", text)
        self.assertNotIn("](http", text)
        self.assertEqual(text.count("`"), 6)

    def test_absolute_file_paths_show_repo_relative_path(self) -> None:
        root = "/home/runner/work/r/r"
        self.assertEqual(
            self.mod.path_shape(root + "/.github/workflows/claude.yml", root),
            ".github/workflows/claude.yml",
        )
        self.assertEqual(self.mod.path_shape("/tmp/app/config.py", root), "[path]")
        self.assertEqual(self.mod.path_shape("/home/runner/.ssh/id_rsa", root), "[path]")
        self.assertEqual(self.mod.path_shape(root + "/../app/x.py", root), "[path]")
        self.assertEqual(self.mod.path_shape(root + "/.github/x.yml", ""), "[path]")
        self.assertEqual(self.mod.path_shape("app/core/x.py", root), "app/core/x.py")

    def test_missing_or_bad_file_is_safe(self) -> None:
        self.assertEqual(self.mod.report("/nonexistent/x.json"), ["claude-denials: execution file unreadable"])
        self.assertEqual(self._report([]), ["claude-denials: count=0"])


SCAN_PATH = ROOT / "scripts" / "ci" / "claude_conflict_scan.py"
SCAN_HEREDOC = "CLAUDE_CONFLICT_SCAN"


def load_scan():
    spec = importlib.util.spec_from_file_location("claude_conflict_scan", SCAN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def conflicted_pr(**kw) -> dict:
    pr = {
        "number": kw.get("number", 7),
        "state": kw.get("state", "open"),
        "mergeable": kw.get("mergeable", False),
        "mergeable_state": kw.get("mergeable_state", "dirty"),
        "user": {"login": kw.get("author", "lucas")},
        "labels": [{"name": n} for n in kw.get("labels", [])],
        "head": {
            "ref": kw.get("head_ref", "feature/x"),
            "sha": kw.get("head_sha", "b" * 40),
            "repo": {"full_name": kw.get("head_repo", REPO)},
        },
        "base": {"ref": kw.get("base_ref", "main")},
    }
    return pr


class ConflictScanTests(unittest.TestCase):
    def test_rotation_matches_two_hour_schedule(self) -> None:
        self.assertEqual(self.mod.ROTATION_SECONDS, 7200)

    def setUp(self) -> None:
        self.mod = load_scan()

    def _select(self, prs, lookup_fn=lookup, cap=3):
        return self.mod.select(prs, REPO, lookup_fn, max_prs=cap)

    def test_selects_conflicted_same_repo_writer_pr(self) -> None:
        chosen, audit = self._select([conflicted_pr()])
        self.assertEqual(
            chosen,
            [{"number": "7", "head_ref": "feature/x", "head_sha": "b" * 40, "base_ref": "main"}],
        )
        self.assertIn("select author_permission=admin", audit[0])

    def test_skips_each_unsafe_or_ineligible_case(self) -> None:
        cases = {
            "not-open": conflicted_pr(state="closed"),
            "fork": conflicted_pr(head_repo="evil/fork"),
            "not-conflicted": conflicted_pr(mergeable=True, mergeable_state="clean"),
            "unknown-mergeable": conflicted_pr(mergeable=None, mergeable_state="unknown"),
            "protected-head": conflicted_pr(head_ref="main"),
            "release-head": conflicted_pr(head_ref="release/1.0"),
            "unsafe-head": conflicted_pr(head_ref="feat/../main"),
            "unsafe-base": conflicted_pr(base_ref="a b"),
            "unsafe-sha": conflicted_pr(head_sha="zz"),
            "labelled": conflicted_pr(labels=["needs-decision"]),
            "reader": conflicted_pr(author="reader"),
            "no-access": conflicted_pr(author="mallory"),
            "lookup-error": conflicted_pr(author="boom"),
            "unsafe-login": conflicted_pr(author="../x"),
        }
        for name, pr in cases.items():
            chosen, audit = self._select([pr])
            self.assertEqual(chosen, [], name)
            self.assertIn("skip=", audit[0], name)

    def test_contributor_association_with_write_permission_is_selected(self) -> None:
        pr = conflicted_pr(author="alice")
        pr["author_association"] = "CONTRIBUTOR"
        self.assertEqual(len(self._select([pr])[0]), 1)

    def test_no_lookup_fails_closed(self) -> None:
        self.assertEqual(self._select([conflicted_pr()], lookup_fn=None)[0], [])

    def test_cap_bounds_runs(self) -> None:
        prs = [conflicted_pr(number=n, head_ref=f"f/{n}") for n in range(1, 6)]
        chosen, audit = self._select(prs, cap=2)
        self.assertEqual([c["number"] for c in chosen], ["1", "2"])
        self.assertEqual(sum("skip=over-cap" in a for a in audit), 3)

    def test_head_targeted_by_another_open_pr_is_protected(self) -> None:
        stacked = conflicted_pr(number=8, head_ref="feature/y", base_ref="feature/x")
        chosen, audit = self._select([conflicted_pr(), stacked])
        self.assertEqual([c["number"] for c in chosen], ["8"])
        self.assertIn("pr=7 skip=protected-head", audit[0])

    def test_listing_follows_pagination(self) -> None:
        pages = {1: [{"number": n} for n in range(1, 101)], 2: [{"number": 101}]}
        calls = []

        def get(path):
            calls.append(path)
            return pages.get(int(path.rsplit("page=", 1)[1]), [])

        numbers = self.mod.list_open_numbers(REPO, get)
        self.assertEqual(len(numbers), 101)
        self.assertEqual(numbers[-1], "101")
        self.assertEqual(len(calls), 2)

    def test_pending_mergeable_shares_bounded_budget(self) -> None:
        now = [0.0]
        slept = []

        def sleep(sec):
            slept.append(sec)
            now[0] += sec

        def get(path):
            if "?state=open" in path:
                return [{"number": n} for n in range(1, 41)] if "page=1" in path else []
            return conflicted_pr(number=int(path.rsplit("/", 1)[1]), mergeable=None, mergeable_state="unknown")

        prs, _bases = self.mod.fetch_prs(REPO, "", get=get, sleep=sleep, clock=lambda: now[0])
        self.assertEqual(len(prs), 40)
        self.assertLessEqual(sum(slept), self.mod.MERGEABLE_BUDGET_SECONDS)

    def test_no_sleep_after_last_attempt(self) -> None:
        slept = []

        def get(path):
            return conflicted_pr(mergeable=None, mergeable_state="unknown")

        self.mod.fetch_prs(REPO, "7", get=get, sleep=slept.append, clock=lambda: 0.0)
        self.assertEqual(len(slept), self.mod.MERGEABLE_ATTEMPTS - 1)

    def test_every_pr_fetched_once_and_retries_capped(self) -> None:
        calls = []

        def get(path):
            calls.append(path)
            if "?state=open" in path:
                page = int(path.rsplit("page=", 1)[1])
                return [{"number": n, "state": "open"} for n in range((page - 1) * 100 + 1, page * 100 + 1)]
            return conflicted_pr(mergeable=None, mergeable_state="unknown")

        prs, _ = self.mod.fetch_prs(REPO, "", get=get, sleep=lambda _s: None, clock=lambda: 0.0, now=lambda: 0.0)
        self.assertEqual(len(prs), self.mod.MAX_DETAIL_PRS)
        details = [c for c in calls if "?state=open" not in c]
        self.assertEqual(len(details), self.mod.MAX_DETAIL_PRS + self.mod.MAX_RETRY_REQUESTS)

    def test_window_rotation_covers_every_pr(self) -> None:
        numbers = [str(n) for n in range(1, 1001)]
        seen = set()
        for step in range(5):
            seen.update(self.mod.rotate_window(numbers, step * self.mod.ROTATION_SECONDS))
        self.assertEqual(seen, set(numbers))
        self.assertEqual(self.mod.rotate_window(numbers[:5], 12345.0), numbers[:5])

    def test_fixer_busy_blocks_scan(self) -> None:
        def make(runs):
            return lambda path: {"workflow_runs": runs if "in_progress" in path else []}

        self.assertTrue(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "issue_comment"}])))
        self.assertTrue(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "pull_request_review"}])))
        self.assertTrue(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "workflow_dispatch"}])))
        self.assertTrue(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "repository_dispatch"}])))
        self.assertFalse(self.mod.fixer_busy(REPO, "1", make([{"id": 1, "event": "issue_comment"}])))
        self.assertFalse(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "schedule"}])))
        self.assertFalse(self.mod.fixer_busy(REPO, "1", make([{"id": 2, "event": "pull_request"}])))
        self.assertTrue(self.mod.fixer_busy(REPO, "1", lambda path: {"message": "error"}))
        self.assertIn("workflow_dispatch", self.mod.FIXER_EVENTS)
        self.assertIn("repository_dispatch", self.mod.FIXER_EVENTS)

    def test_fixer_busy_follows_run_pages(self) -> None:
        def get(path):
            if "in_progress" not in path:
                return {"workflow_runs": []}
            page = int(path.rsplit("page=", 1)[1])
            if page == 1:
                return {"workflow_runs": [{"id": 10 + n, "event": "schedule"} for n in range(100)]}
            return {"workflow_runs": [{"id": 999, "event": "issue_comment"}]}

        self.assertTrue(self.mod.fixer_busy(REPO, "1", get))

    def test_single_pr_dispatch_keeps_stacked_protection(self) -> None:
        listing = [
            {"number": 7, "state": "open", "base": {"ref": "main"}},
            {"number": 8, "state": "open", "base": {"ref": "feature/x"}},
        ]

        def get(path):
            if "?state=open" in path:
                return listing if "page=1" in path else []
            return conflicted_pr()

        prs, bases = self.mod.fetch_prs(REPO, "7", get=get, sleep=lambda _s: None, clock=lambda: 0.0)
        self.assertEqual(len(prs), 1)
        chosen, audit = self.mod.select(prs, REPO, lookup, review_bases=bases)
        self.assertEqual(chosen, [])
        self.assertIn("skip=protected-head", audit[0])

    def test_paused_or_unreadable_labels_are_never_selected(self) -> None:
        for labels in (["claude:pause"], ["hold"], ["bug", "Claude:Pause"], ["HOLD"]):
            with self.subTest(labels=labels):
                chosen, audit = self._select([conflicted_pr(labels=labels)])
                self.assertEqual(chosen, [])
                self.assertIn("skip=paused", audit[0])
        for raw in (None, "hold", {"name": "hold"}, ["hold"], [None]):
            with self.subTest(raw=raw):
                pr = conflicted_pr()
                pr["labels"] = raw
                chosen, audit = self._select([pr])
                self.assertEqual(chosen, [])
                self.assertIn("skip=paused", audit[0])
        pr = conflicted_pr()
        del pr["labels"]
        self.assertIn("skip=paused", self._select([pr])[1][0])
        self.assertIn("skip=needs-decision", self._select([conflicted_pr(labels=["Needs-Decision"])])[1][0])
        chosen, _ = self._select([conflicted_pr(labels=["bug", "holding", "do-not-merge"])])
        self.assertEqual(len(chosen), 1)

    def test_human_pause_stops_the_conflict_writer(self) -> None:
        for labels in (["claude:pause"], ["Hold"]):
            chosen, audit = self._select([conflicted_pr(labels=labels)])
            self.assertEqual(chosen, [])
            self.assertIn("skip=paused", audit[0])
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPO, "CLAUDE_FIXER_PAUSE": "true", "GITHUB_OUTPUT": str(Path(d) / "out")}), \
                mock.patch.object(self.mod, "fixer_busy") as busy, mock.patch.object(self.mod, "fetch_prs") as fetch, \
                mock.patch("builtins.print"):
            self.assertEqual(self.mod.main(), 0)
            output = (Path(d) / "out").read_text()
        busy.assert_not_called()
        fetch.assert_not_called()
        self.assertIn("count=0", output)
        text = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(text.count("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}"), 1)
        # CSL-414 Port C: the per-PR recheck is the trusted prepare job; the model job
        # (checkout, merge, model) runs only after prepare proceeds, and the
        # publish job rereads the pause before any write.
        writer = WRITER_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(writer.count("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}"), 2)
        prep = writer.split("\n  prepare:\n", 1)[1].split("id: prep", 1)[1].split("\n  model:\n", 1)[0]
        self.assertLess(prep.index('"${CLAUDE_FIXER_PAUSE:-}" = "true"'), prep.index('echo "skip=false"'))
        self.assertLess(prep.index('select(. == "claude:pause" or . == "hold")'), prep.index('echo "skip=false"'))
        self.assertIn('if (.labels | type) != "array" or any(.labels[]; type != "object") then "unreadable"', prep)
        self.assertNotIn("git checkout", prep)
        self.assertNotIn("git merge", prep)
        model = writer.split("\n  model:\n", 1)[1].split("    steps:", 1)[0]
        self.assertIn("needs.prepare.outputs.skip == 'false'", model)

    def test_repo_pause_variable_selects_nothing_without_reads(self) -> None:
        env = {"GITHUB_REPOSITORY": REPO, "PR_NUMBER": "", "CLAUDE_FIXER_PAUSE": "true", "GITHUB_OUTPUT": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.mod, "_gh_json", side_effect=AssertionError("no reads")), \
                mock.patch("builtins.print") as printed:
            self.assertEqual(self.mod.main(), 0)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertIn("conflict-scan: skip=paused (repo variable CLAUDE_FIXER_PAUSE)", lines)
        self.assertIn("count=0", lines)
        for value in ("false", "TRUE", ""):
            with mock.patch.dict(os.environ, env | {"CLAUDE_FIXER_PAUSE": value}), \
                    mock.patch.object(self.mod, "fixer_busy", return_value=True), \
                    mock.patch("builtins.print") as printed:
                self.mod.main()
            self.assertTrue(any("fixer-busy" in call.args[0] for call in printed.call_args_list), value)

    def test_embedded_scan_matches_source(self) -> None:
        text = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        start = f"python3 - <<'{SCAN_HEREDOC}'\n"
        self.assertEqual(text.count(start), 1)
        chunk = text.split(start, 1)[1]
        embedded = textwrap.dedent(chunk[: chunk.find(SCAN_HEREDOC)]).rstrip("\n")
        self.assertEqual(embedded, SCAN_PATH.read_text(encoding="utf-8").rstrip("\n"))


class ConflictJobStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.fixer = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        return WorkflowStructureTests._job(self, name)  # type: ignore[arg-type]

    def test_schedule_lives_in_dedicated_workflow(self) -> None:
        fixer_on = self.fixer.split("jobs:", 1)[0]
        scan_on = self.text.split("jobs:", 1)[0]
        self.assertNotIn("schedule:", fixer_on)
        self.assertNotIn("\n  conflict-scan:\n", self.fixer)
        self.assertNotIn("\n  conflict-resolve:\n", self.fixer)
        self.assertIn(SCAN_CRON, scan_on)
        self.assertIn("workflow_dispatch:", scan_on)
        self.assertNotIn("\n  issue_comment:\n", scan_on)
        self.assertNotIn("\n  pull_request_review:\n", scan_on)
        self.assertNotIn("\n  pull_request_review_comment:\n", scan_on)
        self.assertNotIn("\n  pull_request:\n", scan_on)

    def test_triggers_are_bounded(self) -> None:
        self.assertIn(SCAN_CRON, self.text)
        self.assertIn("workflow_dispatch:", self.text)
        scan = self._job("conflict-scan")
        self.assertIn("contents: read", scan)
        self.assertNotIn("write", scan.split("steps:")[0])
        self.assertIn("GH_TOKEN: ${{ github.token }}", scan)
        self.assertIn("actions: read", scan)
        self.assertIn("runs-on: ubuntu-latest", scan.split("timeout-minutes", 1)[0])
        # Port C: the resolver is a reusable-workflow caller (it runs nothing itself); every job of
        # the called writer runs on a GitHub-hosted runner.
        resolve = self._job("conflict-resolve")
        self.assertNotIn("runs-on:", resolve)
        self.assertIn("    uses: ./.github/workflows/claude-conflict-writer.yml\n", resolve)
        for name in CONFLICT_STAGES:
            self.assertIn("runs-on: ubuntu-latest", self._writer(name).split("timeout-minutes", 1)[0], name)

    def _writer(self, name: str) -> str:
        return job_block(WRITER_WORKFLOW_PATH.read_text(encoding="utf-8"), name)

    # The conflict writer's prompt and publisher protected-path list (same as claude.yml's split writers).
    PROTECTED = (".github/ and any .git* or .env* path, .claude/, .cursor/, .claude-fixer-out/, .claude-fixer-replies/, "
                 "scripts/ci/, scripts/tests/**/fixtures/, tools/ci_gate.py, tools/test_ci_gate.py, the loop test suites "
                 "tools/test_claude_*, tools/test_codex_*, tools/test_fixer_* and tools/test_stuck_*, CONTROL_PLANE.md, "
                 "ANCHOR.md, state/DECISIONS.md, CODEOWNERS, AGENTS.md, CLAUDE.md, .mcp.json, credential files")

    def test_resolver_keeps_protections(self) -> None:
        caller = self._job("conflict-resolve")
        self.assertIn("github.event_name != 'push'", caller.split("strategy:", 1)[0])
        self.assertIn("max-parallel: 1", caller)
        self.assertIn("fail-fast: false", caller)
        self.assertIn("uses: ./.github/workflows/claude-conflict-writer.yml", caller)
        self.assertNotIn("concurrency:", caller)  # A caller lock would deadlock its inner jobs.
        self.assertNotIn("steps:", caller)
        prepare = self._writer("prepare")
        job = self._writer("model")
        publish = self._writer("publish")
        for part in (prepare, job, publish):
            self.assertIn("runs-on: ubuntu-latest", part.split("steps:")[0])
            self.assertIn("group: claude-${{ inputs.pr_number }}", part)
            self.assertIn("cancel-in-progress: false", part)
        self.assertIn("ref: ${{ needs.prepare.outputs.head_sha }}", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn('git checkout -B "$HEAD_REF" "$HEAD_SHA"', job)
        self.assertIn("merge --no-ff --no-commit", job)
        self.assertNotIn("--no-edit", job)
        self.assertIn('git config user.name "claude[bot]"', job)
        self.assertIn('git config user.email "209825114+claude[bot]@users.noreply.github.com"', job)
        self.assertIn("needs.prepare.outputs.skip == 'false'", job.split("steps:")[0])
        self.assertIn("needs.prepare.outputs.skip == 'false'", publish.split("steps:")[0])
        self.assertIn("proceed=true", prepare)
        self.assertIn('"open $HEAD_SHA dirty $REPO $BASE_REF"', prepare)
        self.assertIn(".head.repo.full_name", prepare)
        self.assertIn(".base.ref", prepare)
        for path in (".github/**", "AGENTS.md", "CLAUDE.md", "CODEOWNERS", "scripts/ci/**", "tools/ci_gate.py",
                     "tools/test_claude_pr_fixer.py", ".git/**", ".env*", ".claude/**"):
            self.assertIn(f'"Edit({path})"', job)
            self.assertIn(f'"Write({path})"', job)
        self.assertIn('"Read(.env)"', job)
        # The protected list is in the prompt twice (judgment path and publishing rules), and the
        # publisher refuses a merge touching any of them.
        prompt = job.split("prompt: |", 1)[1].split("claude_args:", 1)[0]
        flat = " ".join(prompt.split())
        self.assertEqual(flat.count(self.PROTECTED), 2)
        mention = " ".join(job_block(WORKFLOW_PATH.read_text(encoding="utf-8"), "claude-mention").split())
        self.assertIn(self.PROTECTED, mention)  # same list as the @claude writer
        allow = job.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
        disallow = job.split("--disallowedTools", 1)[1].split('"', 2)[1]
        self.assertNotIn("pytest", allow)
        self.assertNotIn("tools/test_claude_pr_fixer.py", allow)
        self.assertNotIn("ci_gate", allow)
        self.assertNotIn("comments/*/replies", allow)
        self.assertNotIn("resolveReviewThread", allow)
        # CSL-414: no push from the model job; the publish job pushes.
        self.assertNotIn("git push", allow)
        self.assertNotIn("HEAD:refs/heads/", job)
        for rule in ("Bash(git push:*)", "Bash(git rebase:*)", "Bash(git reset:*)", "Bash(git config:*)",
                     "Bash(git diff --no-index:*)", "Bash(gh api:*)"):
            self.assertIn(rule, disallow.split(","))
        self.assertEqual(re.findall(r"Bash\([^)]*\)", allow), [FixerGateAndPromptTests.HELPER])
        self.assertIn("--setting-sources user", job)
        self.assertIn("Report tool-permission denials", job)
        self.assertIn("UNTRUSTED DATA", job)
        self.assertIn("no heredocs, loops, or &&", job)
        # The model runs no gate or repository code; the required linux job checks the pushed commit.
        self.assertNotIn("exactly as written above", job)
        self.assertNotIn("tools/ci_gate.py --force", job)
        self.assertIn("Do not run tests, linters or any repository code", job)
        self.assertIn("linux CI job runs the acceptance gate on the pushed commit", flat)
        self.assertIn("model-api -- status and python3 ${{ runner.temp }}/claude-fixer-guard/model-api -- conflicts", job)
        self.assertNotIn("actions/setup-python", job)
        self.assertNotIn("pip install", job)
        self.assertIn("Linear-first (CSL-88)", job)
        self.assertIn("PR-sync comment", job)
        self.assertIn("Context7", job)
        self.assertIn("Parallel", job)
        self.assertNotIn("web research (Parallel)", job)
        self.assertIn("any deep-research source the team has", job)
        self.assertIn("Open Deep Research", job)
        self.assertIn("when those tools are available", job)
        self.assertIn("not a denied command that blocks the push", job)
        self.assertIn("CSL-Laboratories/csl-research", job)
        self.assertIn("Research bodies go", job)
        self.assertIn("submissions/<record_id>/", job)
        self.assertIn("through a PR there", job)
        self.assertNotIn("submission record", job)
        self.assertIn("never to this repo", job)
        self.assertIn("digest on the Linear issue", job)
        self.assertIn("New non-public docs", job)
        self.assertIn("Existing files stay (CSL-536)", job)

    def _prep(self, live: object, pause_var: str | None = None, env_extra: dict | None = None,
              commit: object = None, reviews: object = None, ref: object = None,
              repo: object = None, publisher_ref: object = None, calls: list | None = None) -> tuple[str, str]:
        if shutil.which("bash") is None or shutil.which("jq") is None:
            self.skipTest("bash/jq not available")
        job = self._writer("prepare")
        start = job.index("        id: prep\n")
        run = job.index("        run: |\n", start) + len("        run: |\n")
        script = textwrap.dedent(job[run:])
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Each endpoint reads its own fixture; the PR view is the default.
            fixtures = {"pr": live, "commit": commit if commit is not None else {},
                        "reviews": reviews if reviews is not None else [],
                        "ref": ref if ref is not None else {"object": {"sha": "c" * 40}},
                        "repo": repo if repo is not None else {"default_branch": "master"},
                        "publisher_ref": publisher_ref if publisher_ref is not None else {"object": {"sha": "d" * 40}}}
            for key, value in fixtures.items():
                (d / f"{key}.json").write_text(json.dumps(value), encoding="utf-8")
            gh = d / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{d}/calls"\n'
                'expr=""; prev=""; file=pr\n'
                'for a in "$@"; do if [ "$prev" = "--jq" ]; then expr="$a"; fi; prev="$a";\n'
                '  case "$a" in\n'
                '    repos/o/r) file=repo;;\n'
                '    */git/ref/heads/master) file=publisher_ref;;\n'
                '    */git/ref/heads/*) file=ref;;\n'
                '    */commits/*) file=commit;;\n'
                '    */reviews*) file=reviews;;\n'
                '    */pulls/*) file=pr;;\n'
                '  esac; done\n'
                f'jq -r "$expr" "{d}/$file.json"\n',
                encoding="utf-8",
            )
            gh.chmod(0o755)
            out = d / "out"
            out.write_text("", encoding="utf-8")
            env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ.get('PATH', '')}", GITHUB_OUTPUT=str(out),
                       REPO="o/r", PR_NUMBER="1", HEAD_SHA="a" * 40, HEAD_REF="feature/x", BASE_REF="main", GH_TOKEN="x")
            env.pop("CLAUDE_FIXER_PAUSE", None)
            if pause_var is not None:
                env["CLAUDE_FIXER_PAUSE"] = pause_var
            env.update(env_extra or {})
            result = subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True, cwd=tmp)
            if calls is not None and (d / "calls").exists():
                calls.extend((d / "calls").read_text(encoding="utf-8").splitlines())
            return out.read_text(encoding="utf-8"), result.stdout

    def test_resolver_rechecks_pause_under_lock_before_merge(self) -> None:
        moved = {"state": "open", "head": {"sha": "b" * 40, "repo": {"full_name": "o/r"}}, "mergeable_state": "dirty"}
        for live, pause_var, message in (
            (moved | {"labels": [{"name": "claude:pause"}]}, None, "paused by PR label (claude:pause)"),
            (moved | {"labels": [{"name": "HOLD"}]}, None, "paused by PR label (hold)"),
            (moved | {"labels": None}, None, "paused by PR label (unreadable)"),
            (moved | {"labels": ["hold"]}, None, "paused by PR label (unreadable)"),
            (moved, None, "paused by PR label (unreadable)"),
            (moved | {"labels": []}, "true", "paused by repo variable CLAUDE_FIXER_PAUSE"),
        ):
            with self.subTest(message=message, live=live):
                out, stdout = self._prep(live, pause_var)
                self.assertEqual(out, "proceed=false\n")
                self.assertIn("::notice::conflict-resolve: " + message, stdout)
        # Not paused: falls through to the existing stale-head recheck (head moved to b...).
        out, stdout = self._prep(moved | {"labels": [{"name": "needs-review"}]}, "false")
        self.assertEqual(out, "proceed=false\n")
        self.assertIn("stale scan result", stdout)
        self.assertNotIn("paused", stdout)

    LIVE = {"state": "open", "head": {"sha": "a" * 40, "repo": {"full_name": "o/r"}}, "mergeable_state": "dirty",
            "base": {"ref": "main"}, "labels": [{"name": "needs-review"}]}

    def test_prepare_records_the_base_and_proceeds_only_when_live(self) -> None:
        out, _ = self._prep(self.LIVE, "false")
        self.assertEqual(out, "proceed=true\nskip=false\nhead_sha=" + "a" * 40 + "\nhead_ref=feature/x\n"
                         "base_sha=" + "c" * 40 + "\npublisher_sha=" + "d" * 40 + "\n")
        for live in (self.LIVE | {"state": "closed"}, self.LIVE | {"mergeable_state": "clean"},
                     self.LIVE | {"head": {"sha": "a" * 40, "repo": {"full_name": "evil/r"}}},
                     self.LIVE | {"base": {"ref": "release/v2"}}):
            with self.subTest(live=live):
                out, stdout = self._prep(live)
                self.assertEqual(out, "proceed=false\n")
                self.assertIn("stale scan result", stdout)
        for ref in ({"object": {"sha": "short"}}, {}, {"object": {"sha": "a" * 40}}):
            with self.subTest(ref=ref):
                out, stdout = self._prep(self.LIVE, ref=ref)
                self.assertEqual(out, "proceed=false\n")
                self.assertIn("base branch unreadable", stdout)

    def test_prepare_records_default_branch_sha_on_a_stacked_base(self) -> None:
        # BASE_REF is another PR branch; the publisher still comes from master.
        live = {**self.LIVE, "base": {"ref": "feature/lower"}}
        out, _ = self._prep(live, "false", env_extra={"BASE_REF": "feature/lower"})
        self.assertEqual(out, "proceed=true\nskip=false\nhead_sha=" + "a" * 40 + "\nhead_ref=feature/x\n"
                         "base_sha=" + "c" * 40 + "\npublisher_sha=" + "d" * 40 + "\n")

    def test_prepare_skips_when_the_default_branch_is_unreadable(self) -> None:
        for repo, publisher_ref in (({"default_branch": ""}, None),
                                    ({"default_branch": "master..x"}, None),
                                    (None, {"object": {"sha": "short"}}),
                                    (None, {"object": {}})):
            with self.subTest(repo=repo, publisher_ref=publisher_ref):
                out, stdout = self._prep(self.LIVE, "false", repo=repo, publisher_ref=publisher_ref)
                self.assertEqual(out, "proceed=false\n")
                self.assertIn("default branch unreadable", stdout)

    def test_prepare_stringifies_label_names_like_the_scan_and_publisher(self) -> None:
        # Master's `| tostring` keeps the label check total: the scan and the publisher compare
        # str(name), so a non-string name is an ordinary non-pause label there. Without it jq exits 5
        # on ascii_downcase and `set -e` fails the prepare step instead of making the same decision.
        spec = importlib.util.spec_from_file_location("publisher", ROOT / "scripts/ci/claude_fixer_publish.py")
        publisher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publisher)
        scan = load_scan()
        for names, paused in (([5], ""), ([True], ""), ([["hold"]], ""), ([{"name": "hold"}], ""), ([1.5, "bug"], ""),
                              ([5, "Hold"], "hold"), ([True, "CLAUDE:PAUSE"], "claude:pause")):
            with self.subTest(names=names):
                labels = [{"name": name} for name in names]
                out, stdout = self._prep(self.LIVE | {"labels": labels}, "false")
                pr = conflicted_pr(head_sha="a" * 40)
                pr["labels"] = labels
                chosen, _audit = scan.select([pr], REPO, lookup, max_prs=3)
                refusal = publisher.pause_refusal({"labels": labels}, {})
                if paused:
                    self.assertEqual(out, "proceed=false\n")
                    self.assertIn(f"paused by PR label ({paused})", stdout)
                    self.assertEqual(chosen, [])
                    self.assertTrue(refusal)
                else:
                    self.assertTrue(out.startswith("proceed=true\nskip=false\n"), out)
                    self.assertEqual(len(chosen), 1)
                    self.assertEqual(refusal, "")

    def test_prepare_validates_the_scan_context_before_any_read(self) -> None:
        for key, value in (("PR_NUMBER", "0"), ("PR_NUMBER", "1;x"), ("PR_NUMBER", ""), ("HEAD_SHA", "A" * 40),
                           ("HEAD_SHA", "a" * 39), ("HEAD_REF", "feature x"), ("HEAD_REF", "a..b"),
                           ("BASE_REF", "main$(id)"), ("BASE_REF", ""), ("BASE_REF", "../x")):
            with self.subTest(key=key, value=value):
                # Paused labels would also stop it; the invalid context must stop it first.
                out, stdout = self._prep(self.LIVE | {"labels": [{"name": "hold"}]}, env_extra={key: value})
                self.assertEqual(out, "proceed=false\n")
                self.assertIn("invalid scan context", stdout)
                self.assertNotIn("paused", stdout)

    def test_prepare_proceeds_on_a_claude_head_without_a_review_lookup(self) -> None:
        # CSL-645: with no Devin fixer there is no race to wait for, so a claude[bot] head (by
        # author or committer) proceeds like any other, and prepare reads no commit or review.
        devin = [{"user": {"login": "devin-ai-integration[bot]"}, "commit_id": "a" * 40}]
        for commit in ({"author": {"login": "claude[bot]"}}, {"commit": {"author": {"name": "claude"}}},
                       {"committer": {"login": "claude[bot]"}, "author": {"login": "alice"}},
                       {"commit": {"committer": {"name": "claude[bot]"}, "author": {"name": "alice"}}},
                       {"author": {"login": "alice"}, "committer": {"login": "web-flow"}}):
            for reviews in ([], devin):
                with self.subTest(commit=commit, reviews=reviews):
                    calls: list[str] = []
                    out, stdout = self._prep(self.LIVE, commit=commit, reviews=reviews, calls=calls)
                    self.assertTrue(out.startswith("proceed=true\nskip=false\n"), out)
                    self.assertNotIn("Devin", stdout)
                    self.assertTrue(calls)
                    for call in calls:
                        self.assertNotIn("/reviews", call)
                        self.assertNotIn("/commits/", call)
        prepare = self._writer("prepare")
        for needle in ("devin", "Devin", "/reviews", "/commits/"):
            self.assertNotIn(needle, prepare, needle)
        self.assertIn("      - name: Recheck pause and branch under the PR lock\n", prepare)

    def test_pause_variable_wired_into_scan_and_resolve(self) -> None:
        scan = self._job("conflict-scan")
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", scan)
        prep = self._writer("prepare").split("        id: prep\n", 1)[1]
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", prep)
        self.assertLess(prep.index("CLAUDE_FIXER_PAUSE:-"), prep.index('echo "skip=false"'))
        # The merge runs only in the model job, which needs an unskipped prepare; the publisher
        # rereads the variable and the live labels before any push or write.
        self.assertNotIn("git merge", prep)
        self.assertIn("git merge --no-ff --no-commit", self._writer("model"))
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", self._writer("publish"))
        self.assertIn('PAUSE_LABELS = frozenset({"claude:pause", "hold"})',
                      (ROOT / "scripts/ci/claude_fixer_publish.py").read_text(encoding="utf-8"))
        self.assertIn("PUBLISHER_SHA", self._writer("publish"))
        self.assertIn("scripts/ci/claude_fixer_publish.py", self._writer("publish"))
        self.assertNotIn(".base.sha", self._writer("publish"))

    def test_resolver_uses_fixed_helper_not_raw_gh_api(self) -> None:
        job = self._writer("model")
        allow = job.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
        self.assertNotIn("gh api", allow)
        self.assertIn("Bash(python3 ${{ runner.temp }}/claude-fixer-guard/model-api --:*)", allow)
        self.assertIn("Bash(gh api:*)", job.split("--disallowedTools", 1)[1].split('"', 2)[1])
        prompt = job.split("prompt: |", 1)[1].split("claude_args:", 1)[0]
        self.assertNotIn("gh api repos/", prompt)
        self.assertNotIn("labels[]=", prompt)
        self.assertIn("model-api -- comment BODY_FILE", prompt)
        self.assertIn("model-api -- label", prompt)
        self.assertIn(".claude-fixer-replies/NAME.txt", prompt)
        # Conflict mode alone enables the fixed needs-decision handoff label in model-api.
        helper = job.split("id: outbox", 1)[1].split("      - id: claude", 1)[0]
        self.assertIn("WRITER_MODE: conflict\n", helper)
        self.assertNotIn("RECEIPT_CONTEXT:", helper.split("        run: |", 1)[0])  # conflict mode has no receipts
        self.assertIn('python3 "$RUNNER_TEMP/claude-fixer-guard/model-api" install', helper)
        chunk = job.split("<<'CLAUDE_FIXER_OUTBOX'\n", 1)[1].split("          CLAUDE_FIXER_OUTBOX", 1)[0]
        self.assertEqual(textwrap.dedent(chunk).rstrip(),
                         (ROOT / "scripts/ci/claude_fixer_outbox.py").read_text(encoding="utf-8").rstrip())
        self.assertLess(job.index("id: outbox"), job.index("      - id: claude"))
        self.assertLess(job.index("Start merge of the recorded base"), job.index("id: outbox"))

    def test_resolver_comments_or_labels(self) -> None:
        job = self._writer("model")
        self.assertIn("merge commit SHA", job)
        self.assertIn("needs-decision", job)
        self.assertIn("push NOTHING", job)
        self.assertIn("model-api -- merge-abort", job)
        self.assertIn("model-api -- commit .claude-fixer-replies/NAME.txt", job)
        self.assertIn("Never exit without either committing the merge or queuing that comment", job)
        self.assertNotIn("git merge --abort", job.split("prompt: |", 1)[1])  # the helper aborts, not raw git

    def test_comment_and_review_jobs_ignore_conflict_events(self) -> None:
        original = self.text
        self.text = self.fixer
        try:
            resolve = self._job("resolve-pr")
            self.assertNotIn("schedule", resolve)
            self.assertNotIn("'push'", resolve)
        finally:
            self.text = original


class ActionlintTests(unittest.TestCase):
    def test_actionlint_when_available(self) -> None:
        exe = shutil.which("actionlint")
        if not exe:
            self.skipTest("actionlint not on PATH")
        result = subprocess.run(
            [
                exe,
                "-color",
                str(WORKFLOW_PATH),
                str(SCAN_WORKFLOW_PATH),
                str(WRITER_WORKFLOW_PATH),
                str(ROOT / ".github" / "workflows" / "codex-gate.yml"),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class MentionLiveHeadTests(unittest.TestCase):
    """Behavioral tests for the trusted mention-prepare live refresh (pause, auth, eight-commit guard)."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW_PATH.read_text()
        cls.script = step_script(job_block(cls.text, "mention-prepare"), "        id: live\n")

    def _run(
        self,
        commits: list[tuple[str, str]],
        comments: list[str],
        *,
        current_sha: str = "a" * 40,
        current_state: str = "open",
        current_ref: str = "feature/fix",
        current_repo: str = "o/r",
        permission: str = "write",
        commenter_permission: str = "write",
        listing_error: bool = False,
        labels: object = (),
        pause_var: str | None = None,
        draft: object = False,
    ) -> tuple[str, str]:
        if shutil.which("bash") is None or shutil.which("tac") is None:
            self.skipTest("bash/tac not available")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Rows are (author login, author name[, committer login, committer name]);
            # 2-column rows leave the committer columns empty, as before.
            (d / "commits.tsv").write_text("".join("\t".join(row) + "\n" for row in commits), encoding="utf-8")
            (d / "comments.txt").write_text("".join(c + "\n" for c in comments), encoding="utf-8")
            live = {"state": current_state, "head": {"sha": current_sha, "ref": current_ref, "repo": {"full_name": current_repo}}, "user": {"login": "alice"}}
            if draft is not None:
                live["draft"] = draft
            if labels is not None:
                # GitHub always returns a labels array; raw non-list values model unreadable labels.
                live["labels"] = [{"name": n} for n in labels] if isinstance(labels, tuple) else labels
            (d / "pr.json").write_text(json.dumps(live))
            gh = d / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *" -f body="*) echo posted >> "{d}/posted";;\n'
                f'  *"collaborators/bob/permission"*) echo {commenter_permission};;\n'
                f'  *"collaborators"*) echo {permission};;\n'
                f'  *"repos/o/r --jq .default_branch"*) echo master;;\n'
                f'  *"git/ref/heads/"*) echo {"d" * 40};;\n'
                f'  "api repos/o/r/pulls/1") cat "{d}/pr.json";;\n'
                f'  */commits*) {"exit 1" if listing_error else "cat" + chr(32) + str(d / "commits.tsv")};;\n'
                f'  */comments*) cat "{d}/comments.txt";;\n'
                "esac\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            out = d / "out"
            out.write_text("", encoding="utf-8")
            env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ.get('PATH', '')}", GITHUB_OUTPUT=str(out),
                       REPO="o/r", PR_NUMBER="1", HEAD_SHA="a" * 40, HEAD_REF="feature/fix", GH_TOKEN="x", COMMENTER="bob")
            env.pop("CLAUDE_FIXER_PAUSE", None)
            if pause_var is not None:
                env["CLAUDE_FIXER_PAUSE"] = pause_var
            subprocess.run(["bash", "-c", self.script], env=env, check=True, capture_output=True)
            posted = (d / "posted").read_text(encoding="utf-8") if (d / "posted").exists() else ""
            return out.read_text(encoding="utf-8"), posted

    def test_queued_human_request_refreshes_head_after_earlier_writer(self):
        out, posts = self._run([], [], current_sha="b" * 40)
        self.assertIn("head_sha=" + "b" * 40, out)
        self.assertIn("head_ref=feature/fix", out)
        self.assertIn("publisher_sha=" + "d" * 40, out)
        self.assertIn("skip=false", out)
        self.assertEqual(posts, "")

    def test_live_authorization_and_branch_boundary_rechecked(self):
        for kwargs in ({"permission": "read"}, {"commenter_permission": "read"},
                       {"current_state": "closed"}, {"current_repo": "evil/fork"},
                       {"current_ref": "renamed"}, {"current_sha": "invalid"}):
            out, _ = self._run([], [], **kwargs)
            self.assertIn("skip=true", out)

    def test_mention_honors_eight_commit_streak(self):
        claude, human, other = ("claude[bot]", "claude[bot]"), ("alice", "Alice"), ("github-actions[bot]", "github-actions[bot]")
        cases = (
            ([human] + [claude] * 7, "false"),
            ([human] + [claude] * 8, "true"),
            ([human] + [claude] * 4 + [other] + [claude] * 4, "true"),
            ([human] + [claude] * 4 + [other] * 5 + [claude] * 3, "false"),
            ([claude] * 8 + [("bob", "Bob")] + [claude] * 7, "false"),
            ([human] + [("x", "claude")] * 8, "true"),
            ([], "false"),
        )
        for commits, skip in cases:
            with self.subTest(commits=commits):
                out, posts = self._run(commits, [])
                self.assertIn("skip=" + skip, out)
                self.assertEqual("head_sha=" in out, skip == "false")
                self.assertEqual(posts, "")

    def test_mention_counts_live_fixer_attribution_as_claude(self):
        # Real fixer pushes are authored by github-actions[bot] with git author name claude[bot].
        live = ("github-actions[bot]", "claude[bot]")
        for n, skip in ((7, "false"), (8, "true")):
            with self.subTest(n=n):
                out, _ = self._run([("alice", "Alice")] + [live] * n, [])
                self.assertIn("skip=" + skip, out)
        self.assertIn("A human commit cannot reset this, so a human has to take over this PR.", self.script)

    def test_mention_forged_author_does_not_reset_and_committer_is_checked(self):
        claude, human = ("claude[bot]", "claude[bot]"), ("alice", "Alice")
        cases = (
            ([human] + [("alice", "Alice", "-", "claude[bot]")] * 8, "true"),
            ([human] + [("alice", "Alice", "claude[bot]", "claude[bot]")] * 8, "true"),
            ([human] + [("alice", "Alice", "claude[bot]", "claude[bot]")] * 7, "false"),
            ([claude] * 8 + [("bob", "Bob", "bob", "Bob")] + [claude] * 7, "false"),
            ([claude] * 8 + [("bob", "Bob", "web-flow", "GitHub")] + [claude] * 2, "false"),
            ([human] + [claude] * 4 + [("bob", "Bob", "github-actions[bot]", "github-actions[bot]")] + [claude] * 4, "true"),
        )
        for commits, skip in cases:
            with self.subTest(commits=commits[-9:]):
                out, _ = self._run(commits, [])
                self.assertIn("skip=" + skip, out)
        self.assertIn('(.committer.login // "-"), (.commit.committer.name // "-")', self.script)
        self.assertIn("A human commit resets the streak, except on a PR listing 250+ commits, where a human has to take over.",
                      self.script)

    def test_mention_cap_fails_closed_on_truncated_commit_list(self):
        # GitHub's PR commits endpoint lists at most 250 commits, oldest first.
        human = ("alice", "Alice")
        for n, skip in ((249, "false"), (250, "true"), (300, "true")):
            with self.subTest(n=n):
                out, _ = self._run([human] * n, [])
                self.assertIn("skip=" + skip, out)

    def test_mention_commit_listing_error_fails_closed(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self._run([], [], listing_error=True)

    def test_mention_human_pause_skips(self):
        for kwargs in ({"labels": ("claude:pause",)}, {"labels": ("Hold",)}, {"labels": None},
                       {"labels": "hold"}, {"pause_var": "true"}, {"labels": ("bug", "hold")},
                       {"labels": ("Claude:Pause",)}, {"labels": ("HOLD",)}, {"labels": {"name": "hold"}},
                       {"labels": ["hold"]}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                out, posts = self._run([("alice", "Alice")], [], **kwargs)
                self.assertIn("skip=true", out)
                self.assertNotIn("head_sha", out)
                self.assertEqual(posts, "")
        for kwargs in ({"labels": ("needs-decision",), "pause_var": "false"},
                       {"labels": ("bug", "needs-decision", "do-not-merge", "holding")},
                       {"pause_var": "TRUE"}, {"pause_var": ""}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                out, _ = self._run([("alice", "Alice")], [], **kwargs)
                self.assertIn("skip=false", out)

    def test_mention_keeps_draft_prs(self):
        # A human @claude mention may act on a draft PR (the retired Devin path skipped drafts).
        for draft in (True, False, None):
            with self.subTest(draft=draft):
                out, _ = self._run([("alice", "Alice")], [], draft=draft)
                self.assertIn("skip=false", out)

    def test_query_variables_stay_literal_with_scoped_shellcheck_directives(self):
        self.assertEqual(self.script.count("# shellcheck disable=SC2016"), 1)
        self.assertIn("# shellcheck disable=SC2016\nif ! jq", self.script)
        self.assertIn('.head.ref == $ref and .head.repo.full_name == $repo', self.script)

    def test_pause_variable_and_notice_wired_into_writer_jobs(self):
        # CSL-414: the pause is checked first in each trusted prepare job, which checks out nothing.
        for name in PREPARE_JOBS:
            job = job_block(self.text, name)
            first = job.split("\n      - ", 2)[1]
            self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", first, name)
            self.assertIn('[ "${CLAUDE_FIXER_PAUSE:-}" = "true" ]', first, name)
            self.assertIn("::notice::Claude PR writer paused by", first, name)
            self.assertIn('select(. == "claude:pause" or . == "hold")', first, name)
            self.assertNotIn("actions/checkout", job, name)
        # Publish rechecks the variable and the live labels right before the push.
        for name in PUBLISH_JOBS:
            step = job_block(self.text, name).split("      - name: Publish validated candidate", 1)[1]
            self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", step, name)
            self.assertIn('python3 -I "$RUNNER_TEMP/claude-fixer-publish/publish.py"', step, name)

    def test_original_comment_and_fresh_branch_remain_bound(self):
        # CSL-414: the live refresh runs in the trusted mention-prepare job, which checks out
        # nothing; the model job runs only on its unskipped outputs.
        prepare = job_block(self.text, "mention-prepare")
        model = job_block(self.text, "claude-mention")
        self.assertIn("COMMENTER: ${{ github.event.comment.user.login }}", prepare)
        self.assertIn("COMMENT ID: ${{ github.event.comment.id }}", model)
        self.assertIn("head_ref: ${{ steps.live.outputs.head_ref }}", prepare)
        self.assertIn("BRANCH: ${{ needs.mention-prepare.outputs.head_ref }}", model)
        self.assertIn("skip: ${{ steps.live.outputs.skip || 'true' }}", prepare)
        self.assertNotIn("actions/checkout", prepare)
        self.assertIn("needs.mention-prepare.outputs.skip == 'false'", model)
        self.assertLess(model.index("actions/checkout"), model.index("uses: anthropics/claude-code-action@"))


class ClaudeActionInlineCommentTests(unittest.TestCase):
    """CSL-383: no claude-code-action step publishes the model-writable comment buffer."""

    def test_every_claude_action_step_disables_buffered_inline_comments(self) -> None:
        import re

        steps = []
        for path in sorted(WORKFLOW_PATH.parent.glob("*.y*ml")):
            for step in path.read_text(encoding="utf-8").split("uses: anthropics/claude-code-action@")[1:]:
                steps.append((path.name, re.split(r"\n      - ", step, maxsplit=1)[0]))
        # CSL-645: claude.yml's claude-mention and the conflict writer's model job.
        self.assertGreaterEqual(len(steps), 2)
        for index, (name, inputs) in enumerate(steps):
            with self.subTest(workflow=name, step=index):
                self.assertIn('\n          classify_inline_comments: "false"\n', inputs)


class ClaudeCodeInstallerPinTests(unittest.TestCase):
    """CSL-473 interim E3-1: SHA-pin the action and skip its unpinned installer."""

    PIN = ROOT / "scripts/ci/claude-code-2.1.306-linux-x64.pin"
    FINGERPRINT = ROOT / "scripts/ci/claude-code-release-signing.fingerprint"
    SCRIPT = ROOT / "scripts/ci/install_pinned_claude_code.sh"
    ACTION = "anthropics/claude-code-action@a5a423f75c251dcbff8a74e99f83b2cb03c72bfa"

    def test_committed_pin_and_fingerprint_match_the_install_script(self) -> None:
        pin = {}
        for line in self.PIN.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                pin[key] = value
        fingerprint = next(line.strip() for line in self.FINGERPRINT.read_text(encoding="utf-8").splitlines()
                           if line and not line.startswith("#"))
        self.assertEqual(pin["sha256"], "520bc6c1f40af466bd03a278b5ae1af1a2938bc7de5b9e5cb15526da20d71cfe")
        self.assertEqual(pin["bytes"], "290532720")
        self.assertEqual(pin["version"], "2.1.306")
        self.assertEqual(fingerprint, "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE")
        script = self.SCRIPT.read_text(encoding="utf-8")
        for value in (pin["sha256"], pin["bytes"], pin["version"], fingerprint):
            self.assertIn(value, script)
        self.assertIn('DEST_DIR="/opt/csl-claude/${CLAUDE_VERSION}"', script)
        self.assertIn("sha256sum -c", script)
        self.assertIn('CURL=/usr/bin/curl', script)
        self.assertIn('GPG=/usr/bin/gpg', script)
        self.assertIn('SUDO=/usr/bin/sudo', script)
        self.assertIn('"$GPG" --batch --status-fd 1 --verify', script)
        self.assertIn("[GNUPG:] VALIDSIG ${EXPECTED_FPR}", script)
        self.assertIn("[GNUPG:] GOODSIG", script)
        self.assertIn("EXPKEYSIG", script)
        self.assertIn("REVKEYSIG", script)
        self.assertIn('mkdir -m 0700 "$STAGING"', script)
        self.assertNotIn("mkdir -m 0700 -p", script)
        self.assertNotIn('mkdir -p "$STAGING"', script)
        self.assertIn("install path is a symlink", script)
        self.assertIn("install dir contains extra files", script)
        self.assertIn("installClaudeCode()", script)
        self.assertIn("python3 -I -c", script)
        self.assertNotIn("python3 -c '", script)
        self.assertIn("reference copy", script)
        self.assertNotIn("| bash", script)

    def test_every_claude_action_use_is_sha_pinned_and_skips_the_installer(self) -> None:
        import re

        steps = []
        for path in sorted(WORKFLOW_PATH.parent.glob("*.y*ml")):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("claude.ai/install", text)
            self.assertNotRegex(text, r"uses:\s*[\"']?anthropics/claude-code-action@v\d")
            self.assertNotRegex(text, r"install\.sh\s*\|\s*bash")
            self.assertNotIn("mkdir -m 0700 -p", text)
            uses = re.findall(r"uses:\s*[\"']?(anthropics/claude-code-action@[^\s\"'#]*)", text)
            if uses:
                self.assertEqual(set(uses), {self.ACTION})
                self.assertIn("Install hash-pinned Claude Code CLI", text)
            for step in re.split(r"uses:\s*[\"']?anthropics/claude-code-action@", text)[1:]:
                steps.append((path.name, re.split(r"\n      - ", step, maxsplit=1)[0]))
        # CSL-645: claude.yml's claude-mention and the conflict writer's model job.
        self.assertGreaterEqual(len(steps), 2)
        for index, (name, inputs) in enumerate(steps):
            with self.subTest(workflow=name, step=index):
                self.assertIn("\n          path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude\n", inputs)
                self.assertIn("DISABLE_UPDATES", inputs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
