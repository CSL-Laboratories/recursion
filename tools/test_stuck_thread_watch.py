#!/usr/bin/env python3
"""Provider-free tests for the stuck-thread watch / Codex mediator.

Acceptance gate:

    python -X utf8 -B tools/test_stuck_thread_watch.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "stuck_thread_watch.py"
WORKFLOW = ROOT / ".github" / "workflows" / "stuck-thread-watch.yml"
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

DEVIN = "devin-ai-integration[bot]"
CLAUDE_BOT = "claude[bot]"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def load_mod():
    spec = importlib.util.spec_from_file_location("stuck_thread_watch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_mod()


def ts(hours_ago: float) -> datetime:
    return NOW - timedelta(hours=hours_ago)


def comment(login: str, body: str, hours_ago: float = 1, url: str = "") -> mod.Comment:
    return mod.Comment(login=login, body=body, created_at=ts(hours_ago), url=url)


def thread(
    *,
    tid: str = "PRRT_1",
    path: str = "AGENTS.md",
    line: int | None = 104,
    comments: list[mod.Comment] | None = None,
    resolved: bool = False,
) -> mod.Thread:
    return mod.Thread(
        id=tid,
        path=path,
        line=line,
        comments=tuple(comments or ()),
        is_resolved=resolved,
    )


def pull(
    *,
    number: int = 186,
    threads: list[mod.Thread] | None = None,
    issue_comments: list[mod.Comment] | None = None,
    draft: bool = False,
    state: str = "OPEN",
    author: str = "",
    title: str = "",
    head_ref: str = "",
    review_decision: str = "",
) -> mod.Pull:
    return mod.Pull(
        number=number,
        url=f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
        is_draft=draft,
        state=state,
        threads=tuple(threads or ()),
        issue_comments=tuple(issue_comments or ()),
        author=author,
        title=title,
        head_ref=head_ref,
        review_decision=review_decision,
    )


def cfg(**kwargs: Any) -> mod.Config:
    values = dict(
        repo="CSL-Laboratories/recursion",
        github_token="gh-read",
        codex_pat="codex-pat",
        linear_api_key="lin_api_test",
        now=NOW,
    )
    values.update(kwargs)
    return mod.Config(**values)


def stuck_pair(**kwargs: Any) -> list[mod.Comment]:
    return [
        comment(DEVIN, "Please clarify this wording.", hours_ago=8, url="https://devin.example"),
        comment(
            CLAUDE_BOT,
            "I couldn't fix this; it needs a human.",
            hours_ago=7,
            url="https://claude.example",
        ),
    ]


class ClassifyTests(unittest.TestCase):
    def test_needs_human_phrases(self) -> None:
        self.assertTrue(mod.needs_human("Claude couldn't fix this"))
        self.assertTrue(mod.needs_human("This needs a human"))
        self.assertTrue(mod.needs_human("A human needs to look at the remaining findings"))
        self.assertTrue(mod.needs_human("I cannot fix the protected-path edit"))
        self.assertTrue(mod.needs_human("I can't fix this"))
        self.assertFalse(mod.needs_human("Fixed in abc123. Gate passed."))
        self.assertFalse(mod.needs_human(""))

    def test_same_login_strips_bot_suffix(self) -> None:
        self.assertTrue(mod.same_login("claude[bot]", "claude"))
        self.assertTrue(mod.same_login("devin-ai-integration", "devin-ai-integration[bot]"))
        self.assertFalse(mod.same_login("claude[bot]", "devin-ai-integration[bot]"))
        self.assertFalse(mod.same_login("", ""))

    def test_graphql_bot_logins_match_rest_form(self) -> None:
        t = thread(
            comments=[
                comment("devin-ai-integration", "Please clarify this wording.", hours_ago=8),
                comment("claude", "I couldn't fix this; it needs a human.", hours_ago=7),
            ]
        )
        self.assertEqual(mod.classify_thread(t, (), cfg()), "stuck-human")

    def test_stuck_when_claude_needs_human(self) -> None:
        t = thread(comments=stuck_pair())
        self.assertEqual(mod.classify_thread(t, (), cfg()), "stuck-human")
        self.assertTrue(mod.is_stuck(t, (), cfg()))

    def test_stuck_when_timed_out_after_claude_reply(self) -> None:
        t = thread(
            comments=[
                comment(DEVIN, "nit: rename this", hours_ago=10),
                comment(CLAUDE_BOT, "Fixed in abc123. Gate passed.", hours_ago=7),
            ]
        )
        self.assertEqual(mod.classify_thread(t, (), cfg()), "stuck-timeout")

    def test_later_fix_reply_clears_earlier_needs_human(self) -> None:
        t = thread(
            comments=[
                comment(DEVIN, "please fix", hours_ago=3),
                comment(CLAUDE_BOT, "needs a human", hours_ago=2),
                comment(CLAUDE_BOT, "Fixed in abc123. Gate passed.", hours_ago=1),
            ]
        )
        self.assertEqual(mod.classify_thread(t, (), cfg()), "waiting")

    def test_waiting_when_claude_replied_recently(self) -> None:
        t = thread(
            comments=[
                comment(DEVIN, "nit: rename this", hours_ago=2),
                comment(CLAUDE_BOT, "Fixed in abc123. Gate passed.", hours_ago=1),
            ]
        )
        self.assertEqual(mod.classify_thread(t, (), cfg()), "waiting")
        self.assertFalse(mod.is_stuck(t, (), cfg()))

    def test_no_claude_is_not_stuck(self) -> None:
        t = thread(comments=[comment(DEVIN, "please fix", hours_ago=24)])
        self.assertEqual(mod.classify_thread(t, (), cfg()), "no-claude")

    def test_not_devin_is_skipped(self) -> None:
        t = thread(
            comments=[
                comment("alice", "please look", hours_ago=8),
                comment(CLAUDE_BOT, "needs a human", hours_ago=7),
            ]
        )
        self.assertEqual(mod.classify_thread(t, (), cfg()), "not-devin")

    def test_resolved_is_skipped(self) -> None:
        t = thread(comments=stuck_pair(), resolved=True)
        self.assertEqual(mod.classify_thread(t, (), cfg()), "resolved")

    def test_already_mediated_is_skipped(self) -> None:
        t = thread(tid="PRRT_9", comments=stuck_pair())
        existing = [comment("watcher", mod.mediation_marker("PRRT_9"), hours_ago=0.5)]
        self.assertEqual(
            mod.classify_thread(t, existing, cfg(mediator_login="watcher")),
            "mediated",
        )

    def test_spoofed_marker_ignored_when_mediator_login_set(self) -> None:
        t = thread(tid="PRRT_9", comments=stuck_pair())
        existing = [comment("mallory", mod.mediation_marker("PRRT_9"), hours_ago=0.5)]
        self.assertEqual(
            mod.classify_thread(t, existing, cfg(mediator_login="pat-user")),
            "stuck-human",
        )
        trusted = [comment("pat-user", mod.mediation_marker("PRRT_9"), hours_ago=0.5)]
        self.assertEqual(
            mod.classify_thread(t, trusted, cfg(mediator_login="pat-user")),
            "mediated",
        )

    def test_timeout_threshold_is_exclusive(self) -> None:
        t = thread(
            comments=[
                comment(DEVIN, "x", hours_ago=10),
                comment(CLAUDE_BOT, "still looking", hours_ago=6),
            ]
        )
        # exactly 6 hours is not yet past the default window
        self.assertEqual(mod.classify_thread(t, (), cfg()), "waiting")
        t2 = thread(
            comments=[
                comment(DEVIN, "x", hours_ago=10),
                comment(CLAUDE_BOT, "still looking", hours_ago=6.01),
            ]
        )
        self.assertEqual(mod.classify_thread(t2, (), cfg()), "stuck-timeout")


class CapAndBodyTests(unittest.TestCase):
    def test_daily_cap_counts_todays_markers_only(self) -> None:
        comments = [
            comment("w", "<!-- codex-mediation:a -->", hours_ago=1),
            comment("w", "<!-- codex-mediation:b -->", hours_ago=2),
            comment("w", "<!-- codex-mediation:c -->", hours_ago=30),
            comment("w", "unrelated", hours_ago=1),
        ]
        self.assertEqual(mod.mediations_today(comments, cfg(mediator_login="w")), 2)

    def test_mediation_body_has_marker_and_codex_mention(self) -> None:
        t = thread(tid="PRRT_abc", comments=stuck_pair())
        p = pull(threads=[t])
        body = mod.mediation_body(p, t, cfg(), reason="stuck-human", linear_url="https://linear.app/x")
        self.assertIn("<!-- codex-mediation:PRRT_abc -->", body)
        self.assertIn("@codex review", body)
        self.assertIn("Do **not** push code", body)
        self.assertIn("Please clarify this wording.", body)
        self.assertIn("couldn't fix", body)
        self.assertIn("UNTRUSTED DATA", body)
        self.assertIn("Linear record: https://linear.app/x", body)
        self.assertIn("Linear-first (CSL-88)", body)
        self.assertIn("PR-sync comment", body)
        self.assertIn("Context7", body)
        self.assertIn("Parallel", body)
        self.assertLess(body.index("@codex review"), body.index("Linear-first (CSL-88)"))
        self.assertNotIn("git push", body.lower())

    def test_quote_truncates(self) -> None:
        quoted = mod.quote_block("n" * 5000, limit=20)
        self.assertTrue(quoted.startswith("> "))
        self.assertLessEqual(len(quoted), 24)

    def test_rollup_title_is_one_issue_per_pr(self) -> None:
        self.assertEqual(
            mod.rollup_title("CSL-Laboratories/recursion", 186),
            "Stuck PR threads: recursion#186",
        )
        line = mod.checklist_line(thread(path="AGENTS.md", line=104, tid="PRRT_t"), cfg(), "stuck-human")
        self.assertIn("AGENTS.md:104", line)
        self.assertIn("<!-- codex-mediation:PRRT_t -->", line)


class FakeGitHub:
    def __init__(self, pulls: list[mod.Pull]) -> None:
        self.pulls = {p.number: p for p in pulls}
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, list[str]]] = []
        self.user_ids: dict[str, str] = {}

    def list_open_pr_numbers(self) -> list[int]:
        return [
            n
            for n, p in sorted(self.pulls.items())
            if p.state == "OPEN" and not p.is_draft
        ]

    def load_pr(self, number: int) -> mod.Pull | None:
        return self.pulls.get(number)

    def post_comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.labels.append((number, labels))

    def pat_login(self) -> str:
        return "pat-user"

    def user_id(self, login: str) -> str:
        return self.user_ids.get(login, "")


class FakeLinear:
    def __init__(self, rollups: list[mod.LinearIssue] | None = None) -> None:
        self.rollups = list(rollups or [])
        self.created: list[tuple[str, str, str]] = []
        self.updated: list[tuple[str, str | None, str]] = []
        self.relations: list[tuple[str, str]] = []
        self.github_users = {"99": "linear-user"}

    def find_rollups(self, repo: str, number: int) -> list[mod.LinearIssue]:
        return [
            issue
            for issue in self.rollups
            if mod.is_rollup_for_pr(issue, repo, number)
        ]

    def assignee_for_github_user(self, github_user_id: str) -> str:
        return self.github_users.get(str(github_user_id), "")

    def create_rollup(self, *, title: str, description: str, assignee_id: str = "") -> mod.LinearIssue:
        self.created.append((title, description, assignee_id))
        issue = mod.LinearIssue(
            id=f"created-{len(self.created)}",
            identifier="CSL-NEW",
            url=f"https://linear.app/csl-laboratories/issue/CSL-NEW/{len(self.created)}",
            title=title,
            description=description,
            state_type="triage",
            assignee_id=assignee_id,
        )
        self.rollups.append(issue)
        return issue

    def update_rollup(
        self,
        issue_id: str,
        *,
        description: str | None = None,
        assignee_id: str = "",
    ) -> None:
        self.updated.append((issue_id, description, assignee_id))

    def link_related(self, issue_id: str, other_key: str) -> None:
        self.relations.append((issue_id, other_key))


class ScanTests(unittest.TestCase):
    def test_mediates_stuck_thread_and_files_linear(self) -> None:
        t = thread(tid="PRRT_x", comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(counters.linear, 1)
        self.assertEqual(len(gh.comments), 1)
        self.assertIn("@codex review", gh.comments[0][1])
        self.assertIn("<!-- codex-mediation:PRRT_x -->", gh.comments[0][1])
        self.assertEqual(len(lin.created), 1)
        self.assertEqual(lin.created[0][0], "Stuck PR threads: recursion#186")
        self.assertIn("<!-- codex-mediation:PRRT_x -->", lin.created[0][1])
        self.assertIn("<!-- stuck-pr-threads:CSL-Laboratories/recursion#186 -->", lin.created[0][1])
        self.assertEqual(gh.labels, [])

    def test_missing_pat_skips_without_writes(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        lin = FakeLinear()
        counters = mod.scan(cfg(codex_pat=""), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.skipped_pat, 1)
        self.assertEqual(gh.comments, [])
        self.assertEqual(lin.created, [])

    def test_daily_cap_stops_at_three(self) -> None:
        threads = [
            thread(tid=f"PRRT_{i}", comments=stuck_pair()) for i in range(5)
        ]
        gh = FakeGitHub([pull(threads=threads)])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 3)
        self.assertEqual(counters.skipped_cap, 2)
        self.assertEqual(len(gh.comments), 3)
        self.assertEqual(len(lin.created), 1)
        for index in range(5):
            self.assertIn(f"<!-- codex-mediation:PRRT_{index} -->", lin.created[0][1])

    def test_existing_today_markers_count_toward_cap(self) -> None:
        existing = [
            comment("w", "<!-- codex-mediation:old1 -->", hours_ago=1),
            comment("w", "<!-- codex-mediation:old2 -->", hours_ago=1),
            comment("w", "<!-- codex-mediation:old3 -->", hours_ago=1),
        ]
        t = thread(tid="PRRT_new", comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t], issue_comments=existing)])
        counters = mod.scan(cfg(mediator_login="w"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.skipped_cap, 1)

    def test_linear_error_still_posts_github_comment(self) -> None:
        class BoomLinear(FakeLinear):
            def find_rollups(self, repo: str, number: int) -> list[mod.LinearIssue]:
                raise RuntimeError("linear down")

        t = thread(tid="PRRT_err", comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        counters = mod.scan(cfg(), gh, BoomLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(len(gh.comments), 1)
        self.assertIn("<!-- codex-mediation:PRRT_err -->", gh.comments[0][1])
        self.assertEqual(gh.labels, [(186, ["needs-decision"])])

    def test_missing_linear_key_on_linear_repo_still_labels(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        counters = mod.scan(cfg(linear_api_key=""), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(counters.linear, 0)
        self.assertEqual(gh.labels, [(186, ["needs-decision"])])

    def test_post_comment_error_continues_to_later_prs(self) -> None:
        class BoomThenOk(FakeGitHub):
            def post_comment(self, number: int, body: str) -> None:
                if number == 1:
                    raise RuntimeError("403 forbidden")
                super().post_comment(number, body)

        t = thread(comments=stuck_pair())
        gh = BoomThenOk(
            [
                pull(number=1, threads=[t]),
                pull(number=2, threads=[thread(tid="PRRT_2", comments=stuck_pair())]),
            ]
        )
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(counters.comment_failed, 1)
        self.assertEqual(mod.exit_code(counters), 1)
        self.assertEqual(gh.comments, [(2, gh.comments[0][1])])
        self.assertTrue(gh.comments[0][1].startswith("<!-- codex-mediation:PRRT_2 -->") or "PRRT_2" in gh.comments[0][1])

    def test_pat_login_failure_skips_mediation_and_fails_run(self) -> None:
        class BoomLogin(FakeGitHub):
            def pat_login(self) -> str:
                raise RuntimeError("401")

        t = thread(tid="PRRT_9", comments=stuck_pair())
        existing = [comment("mallory", mod.mediation_marker("PRRT_9"), hours_ago=0.5)]
        gh = BoomLogin([pull(threads=[t], issue_comments=existing)])
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.skipped_login, 1)
        self.assertEqual(gh.comments, [])
        self.assertEqual(mod.exit_code(counters), 1)
        self.assertEqual(mod.classify_thread(t, existing, cfg()), "stuck-human")

    def test_missing_pat_still_exits_zero(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        counters = mod.scan(cfg(codex_pat=""), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.skipped_pat, 1)
        self.assertEqual(mod.exit_code(counters), 0)

    def test_linear_dedupes_by_marker(self) -> None:
        t = thread(tid="PRRT_z", comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t], title="CSL-74: backbone")])
        marker = mod.mediation_marker("PRRT_z")
        existing = mod.LinearIssue(
            id="abc",
            identifier="CSL-165",
            url="https://linear.app/existing",
            title=mod.rollup_title("CSL-Laboratories/recursion", 186),
            description=marker + "\nhttps://github.com/CSL-Laboratories/recursion/pull/186\n",
            state_type="started",
        )
        lin = FakeLinear([existing])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.linear, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(lin.updated, [])
        self.assertEqual(lin.relations, [("abc", "CSL-74")])
        self.assertIn("https://linear.app/existing", gh.comments[0][1])

    def test_personal_repo_labels_instead_of_linear(self) -> None:
        t = thread(comments=stuck_pair())
        p = mod.Pull(
            number=9,
            url="https://github.com/acme/widgets/pull/9",
            is_draft=False,
            state="OPEN",
            threads=(t,),
            issue_comments=(),
        )
        gh = FakeGitHub([p])
        lin = FakeLinear()
        counters = mod.scan(
            cfg(repo="acme/widgets", linear_api_key=""),
            gh,
            lin,
        )  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(counters.linear, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(gh.labels, [(9, ["needs-decision"])])

    def test_skips_draft_and_closed(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub(
            [
                pull(number=1, threads=[t], draft=True),
                pull(number=2, threads=[t], state="CLOSED"),
            ]
        )
        # FakeGitHub.list_open_pr_numbers already drops these; mimic scan's extra guards
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.scanned, 0)

    def test_one_mediation_per_thread_in_same_run(self) -> None:
        t = thread(tid="PRRT_once", comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t, t])])
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        # Second copy is skipped after the in-run marker is appended.
        self.assertEqual(counters.mediated, 1)


    def test_collect_connection_nodes_keeps_later_claude_reply(self) -> None:
        recent = "2026-10-15T11:30:00Z"
        pages = {
            "c1": {
                "nodes": [
                    {
                        "body": "needs a human",
                        "author": {"login": "claude"},
                        "createdAt": "2026-10-15T10:00:00Z",
                    }
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
            },
            "c2": {
                "nodes": [
                    {
                        "body": "Fixed in abc123. Gate passed.",
                        "author": {"login": "claude"},
                        "createdAt": recent,
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        nodes = mod.collect_connection_nodes(pages["c1"], lambda cursor: pages[cursor])
        comments = tuple(mod._parse_comment(n) for n in nodes)
        t = thread(
            comments=(
                comment("devin-ai-integration", "please fix", hours_ago=3),
                *comments,
            )
        )
        # Last Claude reply is the page-2 fix, so this is waiting, not stuck-human.
        self.assertEqual(mod.classify_thread(t, (), cfg()), "waiting")


class HttpGuardTests(unittest.TestCase):
    def test_refuses_merge_url(self) -> None:
        client = mod.GitHubClient(cfg())
        with self.assertRaises(RuntimeError):
            client._request(
                "https://api.github.com/repos/a/b/pulls/1/merge",
                token="x",
                payload={"commit_title": "nope"},
                method="POST",
            )

    def test_refuses_non_github_host(self) -> None:
        client = mod.GitHubClient(cfg())
        with self.assertRaises(RuntimeError):
            client._request("https://evil.example/graphql", token="x", method="GET")

    def test_comment_body_mentioning_merge_is_allowed(self) -> None:
        captured: list[Request] = []

        def opener(req: Request, timeout: int = 0):
            captured.append(req)
            return BytesIO(b'{"id":1}')

        client = mod.GitHubClient(cfg(), opener=opener)
        client._request(
            "https://api.github.com/repos/a/b/issues/1/comments",
            token="codex-pat",
            payload={"body": "never merge, never force-push"},
            method="POST",
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].get_method(), "POST")

    def test_github_http_error_does_not_include_token(self) -> None:
        def opener(req: Request, timeout: int = 0):
            raise HTTPError(
                req.full_url, 401, "nope", hdrs=None, fp=BytesIO(b'{"message":"bad"}')  # type: ignore[arg-type]
            )

        client = mod.GitHubClient(cfg(), opener=opener)
        with self.assertRaises(RuntimeError) as ctx:
            client._request(
                "https://api.github.com/repos/a/b/issues/1/comments",
                token="super-secret-pat",
                payload={"body": "x"},
                method="POST",
            )
        self.assertNotIn("super-secret-pat", str(ctx.exception))

    def test_from_env_reads_tunables(self) -> None:
        env = {
            "GITHUB_REPOSITORY": "CSL-Laboratories/recursion",
            "GH_TOKEN": "t",
            "CODEX_TRIGGER_PAT": "p",
            "LINEAR_API_KEY": "k",
            "STUCK_AFTER_HOURS": "9",
            "MAX_MEDIATIONS_PER_PR_PER_DAY": "1",
            "PR_NUMBER": "42",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            got = mod.Config.from_env(now=NOW)
        self.assertEqual(got.stuck_after_hours, 9.0)
        self.assertEqual(got.max_per_pr_per_day, 1)
        self.assertEqual(got.pr_number, 42)
        self.assertTrue(got.linear_enabled)


class WorkflowAndDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.agents = AGENTS.read_text(encoding="utf-8")
        self.claude = CLAUDE.read_text(encoding="utf-8")
        self.contributing = CONTRIBUTING.read_text(encoding="utf-8")

    def test_agents_and_claude_are_identical(self) -> None:
        self.assertEqual(self.agents, self.claude)

    def test_docs_point_at_the_workflow(self) -> None:
        for text in (self.agents, self.claude, self.contributing):
            self.assertIn("stuck-thread-watch.yml", text)
            self.assertIn("Devin reviews", text)
            self.assertIn("@codex review", text)
            self.assertIn("needs-decision", text)
        self.assertIn(".github/workflows/stuck-thread-watch.yml", self.agents)

    def test_workflow_is_lightweight_and_configurable(self) -> None:
        text = self.workflow
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertNotIn("runs-on: blacksmith", text.lower())
        self.assertIn("cron: \"23 */2 * * *\"", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("STUCK_AFTER_HOURS", text)
        self.assertIn("MAX_MEDIATIONS_PER_PR_PER_DAY", text)
        self.assertIn("CODEX_TRIGGER_PAT", text)
        self.assertIn("LINEAR_API_KEY", text)
        self.assertIn("stuck_thread_watch.py", text)
        self.assertIn("github.event.repository.default_branch", text)
        self.assertIn("not on the default branch yet; skip", text)
        self.assertNotIn("pytest", text)
        self.assertNotIn("ci_gate", text)
        self.assertNotRegex(text, r"git push|--force|mergePullRequest|resolveReviewThread")

    def test_comment_triggers_are_claude_only(self) -> None:
        self.assertIn("github.event.comment.user.login == 'claude[bot]'", self.workflow)
        self.assertIn("github.event.issue.pull_request", self.workflow)

    def test_script_never_calls_resolve_or_merge(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("git push", source)
        self.assertIn("refusing GitHub merge/resolve", source)
        self.assertIn("Never resolves threads", source)
        # Guard pattern may mention merge; the live client must not build merge URLs.
        self.assertNotIn("/pulls/", source)
        self.assertNotIn("Stuck PR thread:", source)


class RollupPlanTests(unittest.TestCase):
    def test_two_stuck_threads_plan_one_create(self) -> None:
        threads = [
            (thread(tid="PRRT_a", comments=stuck_pair()), "stuck-human"),
            (thread(tid="PRRT_b", path="README.md", comments=stuck_pair()), "stuck-timeout"),
        ]
        p = pull(title="CSL-74: backbone", author="NotSkittlz", head_ref="csl-74-backbone")
        plan = mod.plan_rollup(p, threads, [], cfg())
        self.assertEqual(plan.action, "create")
        self.assertEqual(plan.csl_key, "CSL-74")
        self.assertEqual(plan.title, "Stuck PR threads: recursion#186")
        self.assertIn("<!-- codex-mediation:PRRT_a -->", plan.description)
        self.assertIn("<!-- codex-mediation:PRRT_b -->", plan.description)
        self.assertIn("@NotSkittlz", plan.description)
        self.assertIn("**CSL issue:** CSL-74", plan.description)
        self.assertEqual(plan.description.count("- [ ]"), 2)

    def test_csl_key_falls_back_to_branch(self) -> None:
        self.assertEqual(mod.extract_csl_key("no key", "cursor/csl-81-cli"), "CSL-81")
        self.assertEqual(mod.extract_csl_key("CSL-12: title", "csl-81-other"), "CSL-12")
        self.assertEqual(mod.extract_csl_key("lowercase csl-9 only", ""), "")

    def test_appends_to_open_rollup_and_dedupes_marker(self) -> None:
        existing = mod.LinearIssue(
            id="open-1",
            identifier="CSL-168",
            url="https://linear.app/existing",
            title="Stuck PR threads: recursion#190",
            description=(
                "<!-- stuck-pr-threads:recursion#190 -->\n"
                "- [ ] <!-- codex-mediation:PRRT_old --> [`a.py:1`](https://example/old) — `stuck-human`\n"
                "*Auto-filed by the watcher*\n"
            ),
            state_type="started",
            assignee_id="already",
        )
        fresh = thread(tid="PRRT_new", comments=stuck_pair())
        duplicate = thread(tid="PRRT_old", comments=stuck_pair())
        p = pull(number=190, title="CSL-81: contracts")
        plan = mod.plan_rollup(
            p,
            [(duplicate, "stuck-human"), (fresh, "stuck-timeout")],
            [existing],
            cfg(),
        )
        self.assertEqual(plan.action, "update")
        self.assertEqual(plan.issue_id, "open-1")
        self.assertEqual(plan.description.count("<!-- codex-mediation:PRRT_old -->"), 1)
        self.assertIn("<!-- codex-mediation:PRRT_new -->", plan.description)
        self.assertIn("*Auto-filed by the watcher*", plan.description)
        self.assertLess(
            plan.description.index("<!-- codex-mediation:PRRT_new -->"),
            plan.description.index("*Auto-filed by the watcher*"),
        )

    def test_legacy_discussion_url_counts_as_listed(self) -> None:
        comments = stuck_pair()
        comments[1] = comment(
            CLAUDE_BOT,
            comments[1].body,
            hours_ago=7,
            url="https://github.com/CSL-Laboratories/recursion/pull/190#discussion_r4279370435",
        )
        legacy = mod.LinearIssue(
            id="legacy",
            identifier="CSL-168",
            url="https://linear.app/legacy",
            title="Stuck PR threads: recursion#190",
            description="- [ ] [r4279370435](https://github.com/CSL-Laboratories/recursion/pull/190#discussion_r4279370435): flag\n",
            state_type="started",
        )
        plan = mod.plan_rollup(
            pull(number=190),
            [(thread(tid="PRRT_legacy", comments=comments), "stuck-human")],
            [legacy],
            cfg(),
        )
        self.assertEqual(plan.action, "unchanged")

    def test_canceled_rollup_is_not_reopened(self) -> None:
        canceled = mod.LinearIssue(
            id="closed",
            identifier="CSL-162",
            url="https://linear.app/closed",
            title="Stuck PR threads: recursion#193",
            description="<!-- codex-mediation:PRRT_done -->\n",
            state_type="canceled",
        )
        plan = mod.plan_rollup(
            pull(number=193, title="CSL-82: land"),
            [(thread(tid="PRRT_done", comments=stuck_pair()), "stuck-human")],
            [canceled],
            cfg(),
        )
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.issue_id, "")

    def test_new_thread_after_canceled_rollup_creates_one_rollup(self) -> None:
        canceled = mod.LinearIssue(
            id="closed",
            identifier="CSL-162",
            url="https://linear.app/closed",
            title="Stuck PR threads: recursion#193",
            description="<!-- codex-mediation:PRRT_done -->\n",
            state_type="canceled",
        )
        plan = mod.plan_rollup(
            pull(number=193),
            [
                (thread(tid="PRRT_done", comments=stuck_pair()), "stuck-human"),
                (thread(tid="PRRT_fresh", comments=stuck_pair()), "stuck-timeout"),
            ],
            [canceled],
            cfg(),
        )
        self.assertEqual(plan.action, "create")
        self.assertIn("<!-- codex-mediation:PRRT_fresh -->", plan.description)
        self.assertNotIn("<!-- codex-mediation:PRRT_done -->", plan.description)

    def test_per_thread_issue_is_not_the_rollup(self) -> None:
        per_thread = mod.LinearIssue(
            id="thread-issue",
            identifier="CSL-174",
            url="https://linear.app/thread",
            title="Stuck PR thread: recursion#193 – scripts/v3/harness/__init__.py:5",
            description="<!-- codex-mediation:PRRT_thread -->\n",
            state_type="triage",
        )
        self.assertFalse(mod.is_rollup_for_pr(per_thread, "CSL-Laboratories/recursion", 193))
        plan = mod.plan_rollup(
            pull(number=193),
            [(thread(tid="PRRT_thread", comments=stuck_pair()), "stuck-human")],
            [per_thread],
            cfg(),
        )
        self.assertEqual(plan.action, "create")

    def test_longer_pr_number_does_not_match(self) -> None:
        other = mod.LinearIssue(
            id="other",
            identifier="CSL-1",
            url="https://linear.app/other",
            title="Stuck PR threads: recursion#1860",
            description="<!-- stuck-pr-threads:CSL-Laboratories/recursion#1860 -->\n",
            state_type="triage",
        )
        self.assertFalse(mod.is_rollup_for_pr(other, "CSL-Laboratories/recursion", 186))
        self.assertFalse(
            mod.pr_marker_in(other.description, "CSL-Laboratories/recursion", 186)
        )

    def test_skip_approved_and_merged(self) -> None:
        self.assertEqual(mod.skip_pr_reason(pull(review_decision="APPROVED")), "approved")
        self.assertEqual(mod.skip_pr_reason(pull(state="MERGED")), "closed")
        self.assertEqual(mod.skip_pr_reason(pull(draft=True)), "draft")
        self.assertEqual(mod.skip_pr_reason(pull(review_decision="CHANGES_REQUESTED")), "")

    def test_scan_skips_approved_pr(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t], review_decision="APPROVED")])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.scanned, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(gh.comments, [])

    def test_scan_one_rollup_for_two_threads_links_csl_and_assignee(self) -> None:
        threads = [
            thread(tid="PRRT_1", comments=stuck_pair()),
            thread(tid="PRRT_2", path="README.md", comments=stuck_pair()),
        ]
        p = pull(
            threads=threads,
            author="CDanTheMan",
            title="CSL-81: Qualify upstream CLI contracts",
            head_ref="chaandansaini101/csl-81-cli-contracts",
        )
        gh = FakeGitHub([p])
        gh.user_ids["CDanTheMan"] = "99"
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 2)
        self.assertEqual(counters.linear, 1)
        self.assertEqual(len(lin.created), 1)
        self.assertEqual(lin.created[0][2], "linear-user")
        self.assertEqual(lin.relations, [("created-1", "CSL-81")])
        self.assertIn("**CSL issue:** CSL-81", lin.created[0][1])

    def test_scan_does_not_reopen_canceled_rollup(self) -> None:
        t = thread(tid="PRRT_done", comments=stuck_pair())
        canceled = mod.LinearIssue(
            id="closed",
            identifier="CSL-164",
            url="https://linear.app/closed",
            title="Stuck PR threads: recursion#189",
            description=mod.mediation_marker("PRRT_done"),
            state_type="canceled",
        )
        gh = FakeGitHub([pull(number=189, threads=[t])])
        lin = FakeLinear([canceled])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(counters.linear, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(lin.updated, [])
        self.assertEqual(gh.labels, [])
        self.assertIn("https://linear.app/closed", gh.comments[0][1])

    def test_create_rollup_sends_assignee_and_labels(self) -> None:
        client = mod.LinearClient(cfg())
        client._meta = {
            "team_id": "team",
            "triage_id": "triage",
            "label_ids": ["label-needs"],
            "project_id": "proj",
        }
        calls: list[dict[str, Any]] = []

        def graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append(variables or {})
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {"id": "i", "identifier": "CSL-1", "url": "https://linear.app/i"},
                }
            }

        client.graphql = graphql  # type: ignore[method-assign]
        created = client.create_rollup(
            title="Stuck PR threads: recursion#1",
            description="x",
            assignee_id="user-1",
        )
        payload = calls[0]["input"]
        self.assertEqual(created.url, "https://linear.app/i")
        self.assertEqual(payload["assigneeId"], "user-1")
        self.assertEqual(payload["labelIds"], ["label-needs"])
        self.assertEqual(payload["stateId"], "triage")
        self.assertEqual(payload["projectId"], "proj")

    def test_link_related_uses_csl_identifier(self) -> None:
        client = mod.LinearClient(cfg())
        calls: list[dict[str, Any]] = []

        def graphql(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
            calls.append(variables or {})
            if "inverseRelations" in query:
                return {
                    "issue": {
                        "relations": {"nodes": []},
                        "inverseRelations": {"nodes": []},
                    }
                }
            return {"issueRelationCreate": {"success": True}}

        client.graphql = graphql  # type: ignore[method-assign]
        client.link_related("issue-1", "CSL-81")
        self.assertEqual(
            calls[-1]["input"],
            {"issueId": "issue-1", "relatedIssueId": "CSL-81", "type": "related"},
        )


if __name__ == "__main__":
    unittest.main()
