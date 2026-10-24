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
        self.assertEqual(got["devin"], "false")
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


class DevinMatrixTests(unittest.TestCase):
    def _review(self, **pr_kw) -> dict:
        return {
            "action": "submitted",
            "review": {
                "user": {"login": "devin-ai-integration[bot]", "type": "Bot"},
                "commit_id": "a" * 40,
                "state": "commented",
                "id": 1,
            },
            "pull_request": pr_payload(**pr_kw),
        }

    def test_devin_on_member_same_repo_pr(self) -> None:
        got = decide("pull_request_review", self._review())
        self.assertEqual(got["devin"], "true")
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["head_ref"], "feature/fix")

    def test_review_action_identity_and_head_are_required(self) -> None:
        for mutate in (
            lambda e: e.update(action="edited"),
            lambda e: e["review"]["user"].update(type="User"),
            lambda e: e["review"].update(commit_id="invalid"),
            lambda e: e["review"].pop("commit_id"),
            lambda e: e["review"].update(state="approved"),
        ):
            event = self._review()
            mutate(event)
            self.assertEqual(decide("pull_request_review", event)["devin"], "false")

    def test_main_fetches_current_head_and_fails_closed(self) -> None:
        from unittest import mock
        event = self._review()
        for payload in (pr_payload(), pr_payload(head_sha="b" * 40), None):
            with mock.patch.dict(os.environ, {
                "GITHUB_EVENT_NAME": "pull_request_review",
                "GITHUB_EVENT_PATH": "event.json",
                "GITHUB_REPOSITORY": REPO,
            }), mock.patch.object(gate, "load_event", return_value=event), mock.patch.object(
                gate, "fetch_pr", return_value=payload
            ) as fetch, mock.patch.object(gate, "gh_permission", return_value=lookup), mock.patch.object(gate, "emit") as emit:
                self.assertEqual(gate.main(), 0)
                fetch.assert_called_once_with(REPO, 42)
                self.assertEqual(emit.call_args.args[0]["devin"], "true" if payload is not None else "false")

    def test_current_fetched_pr_head_overrides_event_head(self) -> None:
        event = self._review()
        got = decide("pull_request_review", event, pr=pr_payload(head_sha="b" * 40))
        self.assertEqual(got["devin"], "true")
        self.assertEqual(got["head_sha"], "b" * 40)

    def test_owner_and_member_still_require_permission(self) -> None:
        for association in ("OWNER", "MEMBER"):
            self.assertEqual(decide("pull_request_review", self._review(author_association=association))["devin"], "true")
            self.assertEqual(decide("pull_request_review", self._review(author="reader", author_association=association))["devin"], "false")

    def test_devin_changes_requested(self) -> None:
        event = self._review()
        event["review"]["state"] = "changes_requested"
        self.assertEqual(decide("pull_request_review", event)["devin"], "true")

    def test_devin_on_fork_is_skipped(self) -> None:
        got = decide("pull_request_review", self._review(head_repo="evil/fork"))
        self.assertEqual(got["devin"], "false")
        self.assertEqual(got["same_repo"], "false")

    def test_devin_on_non_member_pr_is_skipped(self) -> None:
        got = decide(
            "pull_request_review",
            self._review(author="reader", author_association="MEMBER"),
        )
        self.assertEqual(got["devin"], "false")
        self.assertEqual(got["pr_author_permission"], "read")

    def test_private_member_contributor_with_write_is_allowed(self) -> None:
        got = decide("pull_request_review", self._review(author_association="CONTRIBUTOR"))
        self.assertEqual(got["devin"], "true")
        self.assertEqual(got["pr_author_association"], "CONTRIBUTOR")
        self.assertEqual(got["pr_author_permission"], "admin")

    def test_maintain_author_is_allowed(self) -> None:
        got = decide("pull_request_review", self._review(author="maint"))
        self.assertEqual(got["devin"], "true")

    def test_contributor_without_permission_is_rejected(self) -> None:
        got = decide("pull_request_review", self._review(author="mallory"))
        self.assertEqual(got["devin"], "false")

    def test_permission_lookup_error_fails_closed(self) -> None:
        got = decide("pull_request_review", self._review(author="boom"))
        self.assertEqual(got["devin"], "false")
        self.assertEqual(got["pr_author_permission"], "error")

    def test_no_lookup_fails_closed(self) -> None:
        got = decide("pull_request_review", self._review(), permission_lookup=None)
        self.assertEqual(got["devin"], "false")

    def test_unsafe_login_is_not_looked_up(self) -> None:
        seen: list[str] = []

        def spy(login: str) -> str:
            seen.append(login)
            return "admin"

        got = decide(
            "pull_request_review",
            self._review(author="../../evil"),
            permission_lookup=spy,
        )
        self.assertEqual(got["devin"], "false")
        self.assertEqual(seen, [])

    def test_other_bot_review_is_skipped(self) -> None:
        event = self._review()
        event["review"]["user"]["login"] = "other-bot[bot]"
        self.assertEqual(decide("pull_request_review", event)["devin"], "false")

    def test_human_review_is_skipped(self) -> None:
        event = self._review()
        event["review"]["user"]["login"] = "alice"
        self.assertEqual(decide("pull_request_review", event)["devin"], "false")

    def test_dispatch_on_member_same_repo_pr_sets_devin(self) -> None:
        event = {"inputs": {"pr_number": "42"}}
        got = decide("workflow_dispatch", event, pr=pr_payload())
        self.assertEqual(got["devin"], "true")
        self.assertEqual(got["mention"], "false")
        self.assertEqual(got["head_ref"], "feature/fix")

    def test_repository_dispatch_sets_devin(self) -> None:
        event = {"client_payload": {"pr_number": 42}}
        got = decide("repository_dispatch", event, pr=pr_payload())
        self.assertEqual(got["devin"], "true")

    def test_dispatch_on_fork_is_skipped(self) -> None:
        event = {"inputs": {"pr_number": "42"}}
        got = decide("workflow_dispatch", event, pr=pr_payload(head_repo="evil/fork"))
        self.assertEqual(got["devin"], "false")
        self.assertEqual(got["same_repo"], "false")

    def test_dispatch_pr_number_rejects_junk(self) -> None:
        self.assertEqual(
            gate.dispatch_pr_number("workflow_dispatch", {"inputs": {"pr_number": "42"}}),
            "42",
        )
        self.assertEqual(
            gate.dispatch_pr_number("repository_dispatch", {"client_payload": {"pr_number": 9}}),
            "9",
        )
        self.assertEqual(
            gate.dispatch_pr_number("workflow_dispatch", {"inputs": {"pr_number": "0"}}),
            "",
        )
        self.assertEqual(
            gate.dispatch_pr_number("workflow_dispatch", {"inputs": {"pr_number": "../7"}}),
            "",
        )

    def test_unsafe_ref_is_rejected(self) -> None:
        got = decide("pull_request_review", self._review(head_ref="feat/../master"))
        self.assertEqual(got["head_ref"], "")
        self.assertEqual(got["head_sha"], "")
        self.assertEqual(got["devin"], "false")
        self.assertEqual(got["reason"], "unsafe-or-empty-ref")

    def test_missing_sha_is_rejected(self) -> None:
        got = decide("pull_request_review", self._review(head_sha="not-a-sha"))
        self.assertEqual(got["head_sha"], "")
        self.assertEqual(got["devin"], "false")

    def test_default_branch_ref_is_rejected(self) -> None:
        for name in ("master", "main", "HEAD"):
            got = decide("pull_request_review", self._review(head_ref=name))
            self.assertEqual(got["head_ref"], "", name)
            self.assertEqual(got["devin"], "false", name)


class WholeWordTests(unittest.TestCase):
    def test_patterns(self) -> None:
        self.assertTrue(gate.is_whole_word_mention("@claude"))
        self.assertTrue(gate.is_whole_word_mention("hey @claude!"))
        self.assertTrue(gate.is_whole_word_mention("`@claude`"))
        self.assertFalse(gate.is_whole_word_mention("@claude123"))
        self.assertFalse(gate.is_whole_word_mention("claude"))
        self.assertFalse(gate.is_whole_word_mention(""))
        self.assertFalse(gate.is_whole_word_mention(None))


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
    def test_all_four_shared_writers_use_bounded_fifo_queue(self) -> None:
        validate_shared_writer_queues(WORKFLOW_PATH.read_text(), ["claude-${{ needs.resolve-pr.outputs.pr_number }}"] * 2)
        validate_shared_writer_queues(SCAN_WORKFLOW_PATH.read_text(), ["claude-${{ matrix.pr.number }}"])
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
                validate_shared_writer_queues(bad, ["claude-${{ needs.resolve-pr.outputs.pr_number }}"] * 2)

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
        self.assertEqual(config.count("      - '" + pattern + "'"), 3)
        self.assertEqual(config.count("    ignore:"), 3)
        self.assertIn("  .github/workflows/claude.yml:", config)
        self.assertIn("  .github/workflows/conflict-scan.yml:", config)
        self.assertIn("  .github/workflows/codex-gate.yml:", config)
        # CSL-408: every job is GitHub-hosted, so no custom runner labels are declared.
        self.assertNotIn("self-hosted-runner", config)
        self.assertNotIn("blacksmith", config)


class WorkflowStructureTests(unittest.TestCase):
    def test_light_jobs_use_github_hosted_runners(self) -> None:
        resolve = self._job("resolve-pr")
        mention = self._job("claude-mention")
        paths = self._job("event-paths")
        fixer = self._job("devin-review-fix")
        self.assertIn("runs-on: ubuntu-latest", resolve.split("timeout-minutes", 1)[0])
        self.assertIn("runs-on: ubuntu-latest", mention.split("timeout-minutes", 1)[0])
        self.assertIn("runs-on: ubuntu-latest", paths.split("timeout-minutes", 1)[0])
        self.assertIn("runs-on: ubuntu-latest", fixer.split("timeout-minutes", 1)[0])

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
        mention = self._job("claude-mention")
        devin = self._job("devin-review-fix")
        self.assertIn("concurrency:", mention)
        self.assertIn("concurrency:", devin)

    def test_privileged_jobs_require_gate_outputs(self) -> None:
        mention = self._job("claude-mention")
        devin = self._job("devin-review-fix")
        self.assertIn("needs.resolve-pr.outputs.mention == 'true'", mention)
        self.assertIn("needs.resolve-pr.outputs.same_repo == 'true'", mention)
        self.assertIn("needs.resolve-pr.outputs.devin == 'true'", devin)
        self.assertIn("needs.resolve-pr.outputs.same_repo == 'true'", devin)
        self.assertIn("github.event_name == 'workflow_dispatch'", devin)
        self.assertIn("github.event_name == 'repository_dispatch'", devin)

    def test_resolve_pr_job_if_skips_bots(self) -> None:
        resolve = self._job("resolve-pr")
        header = resolve.split("runs-on:", 1)[0]
        self.assertIn("github.event.sender.type != 'Bot'", header)
        self.assertIn("github.event.comment.user.type != 'Bot'", header)
        self.assertIn("contains(github.event.comment.body, '@claude')", header)
        self.assertIn("devin-ai-integration[bot]", header)
        self.assertNotIn("chatgpt-codex-connector[bot]", header)
        self.assertIn("github.event_name == 'pull_request_review' &&", header)
        self.assertIn("workflow_dispatch", header)
        self.assertIn("repository_dispatch", header)
        self.assertNotIn(
            "github.event_name == 'pull_request_review_comment' ||",
            header,
        )

    def test_every_privileged_checkout_uses_pr_head_sha(self) -> None:
        mention = self._job("claude-mention")
        devin = self._job("devin-review-fix")
        needle = "ref: ${{ needs.resolve-pr.outputs.head_sha }}"
        self.assertIn("ref: ${{ steps.live.outputs.head_sha }}", mention)
        self.assertIn("ref: ${{ steps.cap.outputs.head_sha }}", devin)
        self.assertNotIn(needle, devin)
        self.assertNotIn("ref: ${{ needs.resolve-pr.outputs.head_ref }}", mention)
        self.assertNotIn("ref: ${{ github.event.pull_request.head.ref }}", mention)

    def test_privileged_jobs_attach_branch_before_push(self) -> None:
        mention = self._job("claude-mention")
        devin = self._job("devin-review-fix")
        attach = 'git checkout -B "$HEAD_REF" "$HEAD_SHA"'
        self.assertIn(attach, mention)
        self.assertIn(attach, devin)
        push = (
            "Bash(git push origin HEAD:refs/heads/"
            "${{ needs.resolve-pr.outputs.head_ref }})"
        )
        self.assertIn(push.replace("needs.resolve-pr.outputs.head_ref", "steps.live.outputs.head_ref"), mention)
        self.assertIn(push.replace("needs.resolve-pr.outputs.head_ref", "steps.cap.outputs.head_ref"), devin)
        self.assertNotIn("HEAD:refs/heads/*", mention)

    def test_prompt_treats_review_and_files_as_untrusted(self) -> None:
        self.assertIn("UNTRUSTED DATA", self.text)
        self.assertIn("untrusted", self.text.lower())

    def test_tool_allowlist_and_denials(self) -> None:
        self.assertIn("--allowedTools", self.text)
        self.assertIn("--disallowedTools", self.text)
        self.assertIn("WebSearch", self.text)
        self.assertIn("WebFetch", self.text)
        self.assertIn(".github/workflows", self.text)
        self.assertIn("include_comments_by_actor: devin-ai-integration[bot]", self.text)

    def test_devin_auto_trigger_stays_but_is_permission_gated(self) -> None:
        self.assertIn("devin-ai-integration[bot]", self.text)
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn("collaborators/{login}/permission", source)
        self.assertNotIn("MEMBER_ASSOC", source)
        self.assertIn("GH_TOKEN: ${{ github.token }}", self._job("resolve-pr"))
        self.assertIn("DISPATCH_EVENTS", source)

    ACTION_SHA = "a5a423f75c251dcbff8a74e99f83b2cb03c72bfa"
    ACTION_TAG = "v1.0.256"
    ACTION_USES = f"anthropics/claude-code-action@{ACTION_SHA}"

    def test_dependencies_are_installed_before_model_invocation(self) -> None:
        for name in ("claude-mention", "devin-review-fix"):
            job = self._job(name)
            self.assertLess(job.index("actions/setup-python@v5"), job.index(self.ACTION_USES))
            self.assertLess(
                job.index("python -P -m pip install --disable-pip-version-check --only-binary=:all:"),
                job.index(self.ACTION_USES),
            )
            self.assertNotIn("anthropics/claude-code-action@v1", job)
            self.assertIn("anthropics/claude-code-action@a5a423f75c251dcbff8a74e99f83b2cb03c72bfa", job)
            self.assertIn("path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude", job)
            self.assertIn("DISABLE_UPDATES", job)
            self.assertIn("Install hash-pinned Claude Code CLI", job)
            self.assertIn('python-version: "3.12"', job)
            self.assertNotIn("continue-on-error", job)

    def test_privileged_pip_isolates_sys_path_and_reads_trusted_requirements(self) -> None:
        for name in ("claude-mention", "devin-review-fix"):
            job = self._job(name)
            with self.subTest(job=name):
                self.assertIn("working-directory: ${{ runner.temp }}", job)
                self.assertNotIn("fetch --no-tags --depth=1", job)
                self.assertNotIn("--depth=1", job)
                self.assertIn(
                    'git -C "$GITHUB_WORKSPACE" show origin/master:requirements-ci.txt > "$RUNNER_TEMP/requirements-ci.txt"',
                    job,
                )
                self.assertIn(
                    'python -P -m pip install --disable-pip-version-check --only-binary=:all: -r "$RUNNER_TEMP/requirements-ci.txt"',
                    job,
                )
                self.assertNotIn("python -m pip install", job)
                self.assertNotIn("cache: pip", job)

    def test_claude_code_action_is_pinned_to_resolved_v1_commit(self) -> None:
        pin = f"uses: {self.ACTION_USES} # {self.ACTION_TAG}"
        self.assertGreaterEqual(self.text.count(pin), 2)
        self.assertNotIn("uses: anthropics/claude-code-action@v1\n", self.text)
        scan = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn(pin, scan)
        self.assertNotIn("uses: anthropics/claude-code-action@v1\n", scan)
        contract = (ROOT / "docs" / "ci-event-contract.md").read_text(encoding="utf-8")
        self.assertIn(self.ACTION_USES, contract)
        self.assertNotIn("anthropics/claude-code-action@v1", contract)

    def test_acceptance_gates_before_push(self) -> None:
        devin = self._job("devin-review-fix")
        self.assertIn("acceptance gate", devin.lower())
        self.assertIn("tools/ci_gate.py", devin)

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
        marker = f"  {name}:"
        start = self.text.find(marker)
        self.assertGreaterEqual(start, 0, name)
        nxt = self.text.find("\n  ", start + len(marker))
        while nxt != -1 and self.text[nxt + 3 : nxt + 4] in (" ", "\t"):
            nxt = self.text.find("\n  ", nxt + 1)
        return self.text[start:] if nxt == -1 else self.text[start:nxt]


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

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW_PATH.read_text(encoding="utf-8")

    def _job(self, name: str) -> str:
        return WorkflowStructureTests._job(self, name)  # type: ignore[arg-type]

    def test_gates_are_single_commands_allowlisted_exactly(self) -> None:
        for job in ("claude-mention", "devin-review-fix"):
            body = self._job(job)
            for gate_tool in self.GATES:
                self.assertIn(gate_tool, body, job)
            self.assertNotIn("Bash(cd ", body, job)
            self.assertNotIn("cd app &&", body, job)
            self.assertNotIn("ci_gate.py:*", body, job)
            self.assertNotIn("pytest:*", body, job)
            self.assertNotIn("ruff check:*", body, job)

    def test_both_prompts_require_sha_replies_and_failure_reply(self) -> None:
        for job in ("claude-mention", "devin-review-fix"):
            body = self._job(job)
            self.assertIn("prompt: |", body, job)
            self.assertIn("commit SHA", body, job)
            self.assertIn("do NOT resolve the thread", body, job)
            self.assertIn("pre-push model-api -- reply ID BODY_FILE", body, job)
            self.assertIn(
                "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/pre-push model-api --:*)",
                body,
                job,
            )
            self.assertIn(
                "Bash(python3 ${{ runner.temp }}/claude-fixer-guard/pre-push model-api --:*)",
                body,
                job,
            )
            self.assertNotIn("Bash(gh api graphql:*resolveReviewThread*)", body, job)
            self.assertIn("existing trusted watcher or human resolution", body, job)
            self.assertIn("Do not resolve a thread you have not answered", body, job)
            self.assertIn("Do not use graphql for any other mutation", body, job)
            self.assertIn("do NOT push", body, job)
            self.assertIn("Never exit without either pushing or posting that failure reply", body, job)
            self.assertIn("UNTRUSTED DATA", body, job)
            self.assertIn("exactly as written above: no pipes, redirects, or extra words", body, job)
            self.assertIn("no heredocs, loops, or &&", body, job)
            self.assertIn("git commit -m with a short message", body, job)
            self.assertIn("use Edit/Write for files", body, job)
            self.assertIn("the gate result", body, job)
            self.assertIn("Linear-first (CSL-88)", body, job)
            self.assertIn("has no Linear, Context7 or Parallel tools", body, job)
            self.assertIn("Context7", body, job)
            self.assertIn("Parallel", body, job)
            self.assertIn("PR author relays status and blockers to Linear", body, job)
            self.assertIn("exact GitHub/gate commands below", body, job)
            self.assertIn("Do not install tools, request access", body, job)

    def test_denials_are_reported_after_each_fixer(self) -> None:
        for job in ("claude-mention", "devin-review-fix"):
            body = self._job(job)
            self.assertIn("id: claude", body, job)
            self.assertIn("Report tool-permission denials", body, job)
            self.assertIn("steps.claude.outputs.execution_file", body, job)
            self.assertNotIn("show_full_output", body, job)

    def test_embedded_denials_match_source(self) -> None:
        start = f"python3 - \"$EXECUTION_FILE\" <<'{DENIALS_HEREDOC}'\n"
        combined = self.text + "\n" + SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertEqual(self.text.count(start), 2)
        self.assertEqual(combined.count(start), 3)
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
        self.assertIn("workflow_dispatch:", fixer_on)
        self.assertIn("repository_dispatch:", fixer_on)
        self.assertIn("devin-review-fix", fixer_on)
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
        resolve = self._job("conflict-resolve")
        self.assertIn("runs-on: ubuntu-latest", resolve.split("timeout-minutes", 1)[0])

    def test_resolver_keeps_protections(self) -> None:
        job = self._job("conflict-resolve")
        self.assertIn("github.event_name != 'push'", job.split("steps:", 1)[0])
        self.assertIn("group: claude-${{ matrix.pr.number }}", job)
        self.assertIn("cancel-in-progress: false", job)
        self.assertIn("max-parallel: 1", job)
        self.assertIn("ref: ${{ matrix.pr.head_sha }}", job)
        self.assertIn('git checkout -B "$HEAD_REF" "$HEAD_SHA"', job)
        self.assertIn("merge --no-ff --no-commit", job)
        self.assertNotIn("--no-edit", job)
        self.assertIn('git config user.name "claude[bot]"', job)
        self.assertIn("if: steps.prep.outputs.proceed == 'true'", job)
        self.assertIn('"open $HEAD_SHA dirty $REPO"', job)
        self.assertIn(".head.repo.full_name", job)
        for path in (".github/**", "AGENTS.md", "CLAUDE.md", "CODEOWNERS", "scripts/ci/**", "tools/ci_gate.py"):
            self.assertIn(f'"Edit({path})"', job)
            self.assertIn(f'"Write({path})"', job)
        self.assertIn("tools/test_claude_pr_fixer.py.", job.split("--allowedTools", 1)[0])
        self.assertIn("Bash(git push origin HEAD:refs/heads/${{ matrix.pr.head_ref }})", job)
        self.assertNotIn("HEAD:refs/heads/*", job)
        self.assertIn("Bash(git rebase:*)", job.split("--disallowedTools", 1)[1])
        self.assertIn("Bash(git reset:*)", job.split("--disallowedTools", 1)[1])
        self.assertIn("Report tool-permission denials", job)
        self.assertIn("UNTRUSTED DATA", job)
        self.assertIn("no heredocs, loops, or &&", job)
        self.assertIn("exactly as written above", job)
        self.assertIn("tools/ci_gate.py --force", job)
        self.assertIn("Linear-first (CSL-88)", job)
        self.assertIn("PR-sync comment", job)
        self.assertIn("Context7", job)
        self.assertIn("Parallel", job)
        self.assertIn("when those tools are available", job)
        self.assertIn("not a denied command that blocks the push", job)

    def _prep(self, live: object, pause_var: str | None = None) -> tuple[str, str]:
        if shutil.which("bash") is None or shutil.which("jq") is None:
            self.skipTest("bash/jq not available")
        job = self._job("conflict-resolve")
        start = job.index("        id: prep\n")
        run = job.index("        run: |\n", start) + len("        run: |\n")
        script = textwrap.dedent(job[run:job.index("\n      - ", run)])
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "pr.json").write_text(json.dumps(live), encoding="utf-8")
            gh = d / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                'expr=""; prev=""\n'
                'for a in "$@"; do if [ "$prev" = "--jq" ]; then expr="$a"; fi; prev="$a"; done\n'
                f'jq -r "$expr" "{d}/pr.json"\n',
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
            result = subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True, cwd=tmp)
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

    def test_pause_variable_wired_into_scan_and_resolve(self) -> None:
        scan = self._job("conflict-scan")
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", scan)
        prep = self._job("conflict-resolve").split("        id: prep\n", 1)[1].split("\n      - ", 1)[0]
        self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", prep)
        self.assertLess(prep.index("CLAUDE_FIXER_PAUSE:-"), prep.index("git merge --no-ff --no-commit"))

    def test_resolver_uses_fixed_helper_not_raw_gh_api(self) -> None:
        job = self._job("conflict-resolve")
        allow = job.split("--allowedTools", 1)[1].split("--disallowedTools", 1)[0]
        self.assertNotIn("gh api", allow)
        self.assertIn("Bash(python3 ${{ runner.temp }}/claude-fixer-guard/pre-push model-api --:*)", allow)
        self.assertIn("Bash(gh api:*)", job.split("--disallowedTools", 1)[1].split('"', 2)[1])
        prompt = job.split("prompt: |", 1)[1].split("claude_args:", 1)[0]
        self.assertNotIn("gh api repos/", prompt)
        self.assertNotIn("labels[]=", prompt)
        self.assertIn("model-api -- comment BODY_FILE", prompt)
        self.assertIn("model-api -- label", prompt)
        self.assertIn(".claude-fixer-replies/NAME.txt", prompt)
        guard = job.split("id: push_guard", 1)[1].split("      - id: claude", 1)[0]
        self.assertIn("MODEL_API_LABEL: needs-decision", guard)
        self.assertLess(job.index("id: prep"), job.index("id: push_guard"))
        self.assertLess(job.index("id: push_guard"), job.index("      - id: claude"))

    def test_resolver_comments_or_labels(self) -> None:
        job = self._job("conflict-resolve")
        self.assertIn("merge commit SHA", job)
        self.assertIn("needs-decision", job)
        self.assertIn("push NOTHING", job)
        self.assertIn("git merge --abort", job)
        self.assertIn("Never exit without either pushing or posting that comment", job)

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
                str(ROOT / ".github" / "workflows" / "codex-gate.yml"),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class FixerLoopCapTests(unittest.TestCase):
    """Behavioral tests for the devin-review-fix loop cap and conflict-resolve head guard."""

    @classmethod
    def setUpClass(cls) -> None:
        import textwrap

        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        cls.text = text
        start = text.index("        id: cap\n")
        run = text.index("        run: |\n", start) + len("        run: |\n")
        end = text.index("\n      - ", run)
        cls.script = textwrap.dedent(text[run:end])

    def _run(
        self,
        commits: list[tuple[str, str]],
        comments: list[str],
        *,
        bypass_cap: str | None = None,
        current_sha: str = "a" * 40,
        current_state: str = "open",
        current_ref: str = "feature/fix",
        current_repo: str = "o/r",
        permission: str = "write",
        commenter_permission: str = "write",
        findings: str = "PRRT_1",
        listing_error: bool = False,
        threads: list[dict] | None = None,
        labels: object = (),
        pause_var: str | None = None,
    ) -> tuple[str, str]:
        import os
        import shutil
        import subprocess
        import tempfile

        if shutil.which("bash") is None or shutil.which("tac") is None:
            self.skipTest("bash/tac not available")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # Rows are (author login, author name[, committer login, committer name]);
            # 2-column rows leave the committer columns empty, as before.
            (d / "commits.tsv").write_text("".join("\t".join(row) + "\n" for row in commits), encoding="utf-8")
            (d / "comments.txt").write_text("".join(c + "\n" for c in comments), encoding="utf-8")
            live = {"state": current_state, "head": {"sha": current_sha, "ref": current_ref, "repo": {"full_name": current_repo}}, "user": {"login": "alice"}}
            if labels is not None:
                # GitHub always returns a labels array; raw non-list values model unreadable labels.
                live["labels"] = [{"name": n} for n in labels] if isinstance(labels, tuple) else labels
            (d / "pr.json").write_text(json.dumps(live))
            (d / "threads.json").write_text(json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": threads or []}}}}}))
            graphql_reply = ('jq -r "${!#}" "' + str(d / "threads.json") + '"') if threads is not None else ('printf "%s\\n" "' + findings + '"')
            gh = d / "gh"
            gh.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *" -f body="*) echo posted >> "{d}/posted";;\n'
                f'  *"graphql"*) {"exit 1" if listing_error else graphql_reply};;\n'
                f'  *"collaborators/bob/permission"*) echo {commenter_permission};;\n'
                f'  *"collaborators"*) echo {permission};;\n'
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
            if bypass_cap is not None:
                env["BYPASS_CAP"] = bypass_cap
            env.pop("CLAUDE_FIXER_PAUSE", None)
            if pause_var is not None:
                env["CLAUDE_FIXER_PAUSE"] = pause_var
            subprocess.run(["bash", "-c", self.script], env=env, check=True, capture_output=True)
            posted = (d / "posted").read_text(encoding="utf-8") if (d / "posted").exists() else ""
            return out.read_text(encoding="utf-8"), posted

    def test_eight_claude_commits_are_passed_to_pending_finding_guard(self) -> None:
        out, posted = self._run([("alice", "Alice")] + [("claude[bot]", "claude[bot]")] * 8, [])
        self.assertIn("loop_commit_count=8", out)
        self.assertIn("skip=false", out)
        self.assertEqual(posted, "")

    def test_seven_claude_commits_runs(self) -> None:
        out, posted = self._run([("alice", "Alice")] + [("claude[bot]", "claude[bot]")] * 7, [])
        self.assertIn("skip=false", out)
        self.assertIn("loop_commit_count=7", out)
        self.assertEqual(posted, "")

    def test_daily_fixer_markers_do_not_cap_runs(self) -> None:
        from datetime import datetime, timezone

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker = f"<!-- codex-fixer-run:{day} -->"
        out, posted = self._run(
            [("alice", "Alice")],
            [marker] * 8,
        )
        self.assertIn("skip=false", out)
        self.assertEqual(posted, "")

    def test_bypass_cap_ignores_daily_markers(self) -> None:
        from datetime import datetime, timezone

        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        marker = f"<!-- codex-fixer-run:{day} -->"
        out, posted = self._run(
            [("alice", "Alice")],
            [marker] * 8,
            bypass_cap="true",
        )
        self.assertIn("skip=false", out)
        self.assertEqual(posted, "")

    def test_human_commit_resets_streak(self) -> None:
        c = [("claude[bot]", "claude[bot]")] * 8 + [("bob", "Bob")] + [("claude[bot]", "claude[bot]")] * 7
        out, _ = self._run(c, [])
        self.assertIn("skip=false", out)
        self.assertIn("loop_commit_count=7", out)

    def test_other_bot_does_not_reset_streak(self) -> None:
        c = [("alice", "Alice")] + [("claude[bot]", "claude[bot]")] * 4 + [("github-actions[bot]", "github-actions[bot]")] + [("claude[bot]", "claude[bot]")] * 4
        out, _ = self._run(c, [])
        self.assertIn("loop_commit_count=8", out)

    def test_queued_job_refreshes_head_after_prior_fix_push(self) -> None:
        # The previous run checked its threads, then another finding arrived,
        # then that run pushed B. The queued wake-up for A must fix the remainder on B.
        out, posted = self._run([("claude[bot]", "claude[bot]")], [], current_sha="b" * 40, findings="PRRT_late")
        self.assertIn("skip=false", out)
        self.assertIn("head_sha=" + "b" * 40, out)
        self.assertEqual(posted, "")

    def test_invalid_live_sha_skips(self) -> None:
        out, _ = self._run([], [], current_sha="not-a-sha")
        self.assertIn("skip=true", out)

    def test_queued_authorization_is_rechecked(self) -> None:
        for kwargs in ({"current_state": "closed"}, {"current_ref": "renamed"}, {"current_repo": "evil/fork"}, {"permission": "read"}):
            out, _ = self._run([], [], **kwargs)
            self.assertIn("skip=true", out)

    def test_listing_error_fails_closed(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            self._run([], [], listing_error=True)

    def test_legacy_bypass_does_not_disable_eight_commit_guard(self) -> None:
        out, _ = self._run([("claude[bot]", "claude[bot]")] * 8, [], bypass_cap="true")
        self.assertIn("loop_commit_count=8", out)

    def test_historical_event_markers_do_not_block_new_or_failed_findings(self) -> None:
        out, _ = self._run([], ["<!-- claude-fixer-event:123:abc123 -->"])
        self.assertIn("skip=false", out)

    def test_query_variables_stay_literal_with_scoped_shellcheck_directives(self) -> None:
        self.assertEqual(self.script.count("# shellcheck disable=SC2016"), 1)
        self.assertIn("# shellcheck disable=SC2016\nif ! jq", self.script)
        self.assertIn('.head.ref == $ref and .head.repo.full_name == $repo', self.script)

    def test_receipt_helper_embedded_from_trusted_workflow_before_checkout(self) -> None:
        source = (WORKFLOW_PATH.parents[2] / "scripts/ci/claude_fixer_receipts.py").read_text()
        job = self.text[self.text.index("  devin-review-fix:"):]
        begin = job.index("          #!/usr/bin/env python3", job.index("id: eligibility"))
        end = job.index("          CLAUDE_FIXER_RECEIPTS", begin)
        import textwrap
        self.assertEqual(textwrap.dedent(job[begin:end]).rstrip(), source.rstrip())
        self.assertLess(job.index("id: eligibility"), job.index("uses: actions/checkout"))
        self.assertIn("MANUAL_RETRY: ${{ github.event_name == 'workflow_dispatch' }}", job)
        self.assertIn("steps.eligibility.outputs.receipt_context", job)
        self.assertIn("LOOP_COMMIT_COUNT: ${{ steps.cap.outputs.loop_commit_count }}", job)
        self.assertIn("needs-human handoffs must remain unresolved", job)
        for step in job.split("      - uses: actions/checkout@v4", 1)[1].split("\n      - "):
            self.assertIn("steps.eligibility.outputs.skip != 'true'", step)

    def test_fixer_steps_gated_on_cap(self) -> None:
        start = self.text.index("  devin-review-fix:\n")
        job = self.text[start:]
        steps = job.split("        id: cap\n", 1)[1].split("\n      - ")[1:]
        self.assertTrue(steps)
        for step in steps:
            self.assertIn("steps.cap.outputs.skip != 'true'", step)

    def test_truncated_commit_list_counts_as_at_cap(self) -> None:
        # GitHub lists at most 250 PR commits, oldest first; the newest may be missing.
        human = [("alice", "Alice")]
        for commits, expected in ((human * 249, "0"), (human * 250, "8"), (human * 300, "8"),
                                  (human * 240 + [("claude[bot]", "claude[bot]")] * 12, "12")):
            with self.subTest(n=len(commits)):
                out, posted = self._run(commits, [])
                self.assertIn("loop_commit_count=" + expected, out)
                self.assertEqual(posted, "")

    def test_live_fixer_attribution_counts_as_claude(self) -> None:
        # Real fixer pushes are authored by github-actions[bot] with git author name claude[bot].
        live = ("github-actions[bot]", "claude[bot]")
        for commits, expected in (([("alice", "Alice")] + [live] * 8, "8"), ([("alice", "Alice")] + [live] * 7, "7"),
                                  ([live] * 3 + [("bob", "Bob")] + [live] * 2, "2")):
            with self.subTest(commits=commits):
                out, _ = self._run(commits, [])
                self.assertIn("loop_commit_count=" + expected, out)

    def test_forged_author_does_not_reset_and_committer_is_checked(self) -> None:
        # The model can pass `git commit --author`, but its committer comes from the job's git config.
        claude, human = ("claude[bot]", "claude[bot]"), ("alice", "Alice")
        cases = (
            ([human] + [("alice", "Alice", "-", "claude[bot]")] * 8, "8"),
            ([human] + [("alice", "Alice", "claude[bot]", "claude[bot]")] * 8, "8"),
            ([human] + [("-", "Alice", "-", "claude")] * 8, "8"),
            ([claude] * 8 + [("bob", "Bob", "bob", "Bob")] + [claude] * 7, "7"),
            ([claude] * 8 + [("bob", "Bob", "web-flow", "GitHub")] + [claude] * 2, "2"),
            ([human] + [claude] * 4 + [("bob", "Bob", "github-actions[bot]", "github-actions[bot]")] + [claude] * 4, "8"),
            ([human] + [claude] * 4 + [("bob", "Bob", "-", "renovate[bot]")] + [claude] * 4, "8"),
        )
        for commits, expected in cases:
            with self.subTest(commits=commits[-9:]):
                out, _ = self._run(commits, [])
                self.assertIn("loop_commit_count=" + expected + "\n", out)
        self.assertIn('(.committer.login // "-"), (.commit.committer.name // "-")', self.script)

    def test_truncated_listing_notice_says_a_human_must_take_over(self) -> None:
        self.assertIn("A human commit cannot reset this, so a human has to take over this PR.", self.script)

    def test_human_pause_skips_before_reservation(self) -> None:
        for kwargs in ({"labels": ("claude:pause",)}, {"labels": ("bug", "hold")}, {"labels": ("Claude:Pause",)},
                       {"labels": ("HOLD",)}, {"labels": None}, {"labels": {"name": "hold"}},
                       {"labels": ["hold"]}, {"pause_var": "true"}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                out, posted = self._run([("alice", "Alice")], [], **kwargs)
                self.assertIn("skip=true", out)
                self.assertNotIn("loop_commit_count", out)
                self.assertNotIn("head_sha", out)
                self.assertEqual(posted, "")
        for kwargs in ({"labels": ("bug", "needs-decision", "do-not-merge", "holding")},
                       {"pause_var": "false"}, {"pause_var": "TRUE"}, {"pause_var": ""}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                out, _ = self._run([("alice", "Alice")], [], **kwargs)
                self.assertIn("skip=false", out)

    def test_pause_variable_and_notice_wired_into_writer_jobs(self) -> None:
        for name in ("claude-mention", "devin-review-fix"):
            job = WorkflowStructureTests._job(self, name)
            first = job.split("\n      - ", 2)[1]
            self.assertIn("CLAUDE_FIXER_PAUSE: ${{ vars.CLAUDE_FIXER_PAUSE }}", first, name)
            self.assertIn('[ "${CLAUDE_FIXER_PAUSE:-}" = "true" ]', first, name)
            self.assertIn("::notice::Claude PR writer paused by", first, name)
            self.assertIn('select(. == "claude:pause" or . == "hold")', first, name)
            self.assertLess(job.index("CLAUDE_FIXER_PAUSE"), job.index("uses: actions/checkout"), name)

    def test_conflict_resolve_waits_for_devin_on_claude_head(self) -> None:
        text = SCAN_WORKFLOW_PATH.read_text(encoding="utf-8")
        job = text[text.index("  conflict-resolve:\n"):]
        self.assertIn("devin-ai-integration[bot]", job)
        self.assertIn(".commit_id == ", job)
        self.assertIn("Devin review of head is pending", job)


class MentionLiveHeadTests(unittest.TestCase):
    _run = FixerLoopCapTests._run

    @classmethod
    def setUpClass(cls):
        import textwrap
        cls.text = WORKFLOW_PATH.read_text()
        start = cls.text.index("        id: live\n")
        run = cls.text.index("        run: |\n", start) + len("        run: |\n")
        end = cls.text.index("\n      - ", run)
        cls.script = textwrap.dedent(cls.text[run:end])

    def test_queued_human_request_refreshes_head_after_earlier_writer(self):
        out, posts = self._run([], [], current_sha="b" * 40)
        self.assertIn("head_sha=" + "b" * 40, out)
        self.assertIn("head_ref=feature/fix", out)
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
                       {"labels": "hold"}, {"pause_var": "true"}):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}):
                out, posts = self._run([], [], **kwargs)
                self.assertIn("skip=true", out)
                self.assertNotIn("head_sha", out)
                self.assertEqual(posts, "")
        out, _ = self._run([], [], labels=("needs-decision",), pause_var="false")
        self.assertIn("skip=false", out)

    def test_original_comment_and_fresh_branch_remain_bound(self):
        job = self.text.split("  claude-mention:", 1)[1].split("  devin-review-fix:", 1)[0]
        self.assertIn("COMMENTER: ${{ github.event.comment.user.login }}", job)
        self.assertIn("COMMENT ID: ${{ github.event.comment.id }}", job)
        self.assertIn("BRANCH: ${{ steps.live.outputs.head_ref }}", job)
        self.assertLess(job.index("id: live"), job.index("actions/checkout"))
        for step in job.split("      - uses: actions/checkout@v4", 1)[1].split("\n      - "):
            self.assertIn("steps.live.outputs.skip != 'true'", step)


class ClaudeActionInlineCommentTests(unittest.TestCase):
    """CSL-383: no claude-code-action step publishes the model-writable comment buffer."""

    def test_every_claude_action_step_disables_buffered_inline_comments(self) -> None:
        import re

        steps = []
        for path in sorted(WORKFLOW_PATH.parent.glob("*.y*ml")):
            for step in path.read_text(encoding="utf-8").split("uses: anthropics/claude-code-action@")[1:]:
                steps.append((path.name, re.split(r"\n      - ", step, maxsplit=1)[0]))
        self.assertGreaterEqual(len(steps), 3)
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
        self.assertGreaterEqual(len(steps), 3)
        for index, (name, inputs) in enumerate(steps):
            with self.subTest(workflow=name, step=index):
                self.assertIn("\n          path_to_claude_code_executable: /opt/csl-claude/2.1.306/claude\n", inputs)
                self.assertIn("DISABLE_UPDATES", inputs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
