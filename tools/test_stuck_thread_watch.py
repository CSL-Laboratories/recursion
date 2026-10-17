#!/usr/bin/env python3
"""Provider-free tests for the stuck-thread watch / Codex mediator.

Acceptance gate:

    python -X utf8 -B tools/test_stuck_thread_watch.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from dataclasses import replace
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
CODEX = "chatgpt-codex-connector[bot]"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STALE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


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
    head_oid: str = "",
    review_decision: str = "",
    checks_state: str = "",
    approved_head_oids: tuple[str, ...] = (),
    head_sha: str = HEAD,
    reviews: list[mod.Comment] | None = None,
    checks_green: bool = True,
) -> mod.Pull:
    return mod.Pull(
        number=number,
        url=f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
        is_draft=draft,
        state=state,
        threads=tuple(threads or ()),
        issue_comments=tuple(issue_comments or ()),
        head_oid=head_oid or head_sha,
        review_decision=review_decision,
        checks_state=checks_state,
        approved_head_oids=approved_head_oids,
        head_sha=head_sha or head_oid,
        reviews=tuple(reviews or ()),
        required_checks_green=checks_green,
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


def verdict_block(
    tid: str = "PRRT_1",
    action: str = "RESOLVE",
    sha: str = HEAD,
) -> str:
    return (
        f"{mod.verdict_marker(tid)}\n"
        f"CODEX-THREAD: {action}\n"
        f"HEAD: {sha}\n"
    )


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
        self.assertIn("<!-- codex-thread-verdict:PRRT_abc -->", body)
        self.assertIn("CODEX-THREAD: RESOLVE|DECIDE|MISSING|AMBIGUOUS", body)
        self.assertIn("Thread id: `PRRT_abc`", body)
        self.assertLess(body.index("@codex review"), body.index("Linear-first (CSL-88)"))
        self.assertNotIn("git push", body.lower())

    def test_quote_truncates(self) -> None:
        quoted = mod.quote_block("n" * 5000, limit=20)
        self.assertTrue(quoted.startswith("> "))
        self.assertLessEqual(len(quoted), 24)

    def test_linear_title_is_one_rollup_per_pr(self) -> None:
        title = mod.linear_title(pull())
        self.assertEqual(title, "Stuck PR threads: recursion#186")
        self.assertNotIn("AGENTS.md", title)

    def test_skip_pr_reason_covers_draft_closed_and_approved_green(self) -> None:
        t = thread(comments=stuck_pair())
        self.assertEqual(mod.skip_pr_reason(pull(threads=[t], draft=True)), "draft")
        self.assertEqual(mod.skip_pr_reason(pull(threads=[t], state="MERGED")), "closed")
        self.assertEqual(mod.skip_pr_reason(pull(threads=[t], state="CLOSED")), "closed")
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="APPROVED",
                    checks_state="SUCCESS",
                    head_oid="abc",
                    head_sha="abc",
                    approved_head_oids=("abc",),
                )
            ),
            "approved-green",
        )
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="APPROVED",
                    checks_state="PENDING",
                    head_oid="abc",
                    approved_head_oids=("abc",),
                )
            ),
            "",
        )
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="CHANGES_REQUESTED",
                    checks_state="SUCCESS",
                    head_oid="abc",
                    approved_head_oids=("abc",),
                )
            ),
            "",
        )
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="APPROVED",
                    checks_state="SUCCESS",
                    head_oid="abc",
                    approved_head_oids=(),
                )
            ),
            "",
        )
        # First query still says approved+green on A; later head/checks do not.
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="APPROVED",
                    checks_state="SUCCESS",
                    head_oid=STALE,
                    approved_head_oids=(STALE,),
                    head_sha=HEAD,
                    checks_green=True,
                )
            ),
            "",
        )
        self.assertEqual(
            mod.skip_pr_reason(
                pull(
                    threads=[t],
                    review_decision="APPROVED",
                    checks_state="SUCCESS",
                    head_oid=HEAD,
                    approved_head_oids=(HEAD,),
                    head_sha=HEAD,
                    checks_green=False,
                )
            ),
            "",
        )


class FakeGitHub:
    def __init__(self, pulls: list[mod.Pull]) -> None:
        self.pulls = {p.number: p for p in pulls}
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, list[str]]] = []
        self.replies: list[tuple[str, str]] = []
        self.resolves: list[str] = []

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

    def reply_on_thread(self, thread_id: str, body: str) -> None:
        self.replies.append((thread_id, body))

    def resolve_thread(self, thread_id: str) -> None:
        self.resolves.append(thread_id)

    def refresh_head(self, pull: "mod.Pull") -> "mod.Pull":
        return pull

    def pat_login(self) -> str:
        return "pat-user"


class FakeLinear:
    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}
        self.existing_ids: dict[str, str] = {}
        self.existing_states: dict[str, str] = {}
        self.existing_descriptions: dict[str, str] = {}
        self.marker_matches: dict[str, list[mod.LinearIssue]] = {}
        self.title_matches: dict[str, list[mod.LinearIssue]] = {}
        self.created: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str]] = []
        self.canceled: list[str] = []
        self.rollups: list[mod.LinearIssue] = []

    def find_by_marker(self, marker: str, *, title: str = "") -> mod.LinearIssue | None:
        matches = list(self.marker_matches.get(marker) or [])
        if title:
            matches.extend(self.title_matches.get(title) or [])
        if matches:
            seen: set[str] = set()
            unique: list[mod.LinearIssue] = []
            for issue in matches:
                if issue.id in seen:
                    continue
                seen.add(issue.id)
                unique.append(issue)
            active = [
                issue
                for issue in unique
                if (issue.state_type or "").lower() not in {"canceled", "completed"}
            ]
            return active[0] if active else unique[0]
        url = self.existing.get(marker, "")
        if not url:
            return None
        pr = ""
        if "recursion#" in marker:
            pr = marker.split("recursion#", 1)[-1].split()[0].rstrip(" -->")
        title = f"Stuck PR threads: recursion#{pr}" if pr else "Stuck PR threads: recursion#186"
        return mod.LinearIssue(
            id=self.existing_ids.get(marker, "iss_existing"),
            url=url,
            identifier="CSL-EXIST",
            title=title,
            state_type=self.existing_states.get(marker, "triage"),
            description=self.existing_descriptions.get(marker, ""),
        )

    def create_triage_issue(self, *, title: str, description: str) -> mod.LinearIssue:
        self.created.append((title, description))
        issue = mod.LinearIssue(
            id=f"iss_{len(self.created)}",
            url=f"https://linear.app/csl-laboratories/issue/CSL-TEST/{title[:20]}",
            identifier="CSL-TEST",
            title=title,
            state_type="triage",
        )
        for line in description.splitlines():
            if "stuck-pr-threads:" in line:
                self.existing[line.strip()] = issue.url
                self.existing_ids[line.strip()] = issue.id
                break
        return issue

    def update_issue(self, issue_id: str, *, description: str) -> None:
        self.updated.append((issue_id, description))

    def cancel_issue(self, issue_id: str) -> None:
        self.canceled.append(issue_id)

    def search_rollups(self) -> list[mod.LinearIssue]:
        return list(self.rollups)


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
        self.assertIn("<!-- stuck-pr-threads:recursion#186 -->", lin.created[0][1])
        self.assertIn("AGENTS.md:104", lin.created[0][1])
        self.assertIn("- [ ]", lin.created[0][1])
        self.assertIn("<!-- thread:PRRT_x -->", lin.created[0][1])
        self.assertIn("## Human decision", lin.created[0][1])
        self.assertNotIn("<!-- codex-mediation:PRRT_x -->", lin.created[0][1])
        self.assertEqual(gh.labels, [])

    def test_missing_pat_skips_mediation_but_still_files_rollup(self) -> None:
        t = thread(comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t])])
        lin = FakeLinear()
        counters = mod.scan(cfg(codex_pat=""), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(counters.skipped_pat, 1)
        self.assertEqual(gh.comments, [])
        self.assertEqual(len(lin.created), 1)
        self.assertEqual(lin.created[0][0], "Stuck PR threads: recursion#186")

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
            def find_by_marker(self, marker: str, *, title: str = "") -> mod.LinearIssue | None:
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

    def test_linear_updates_existing_rollup(self) -> None:
        t = thread(tid="PRRT_z", comments=stuck_pair())
        p = pull(threads=[t])
        gh = FakeGitHub([p])
        marker = mod.rollup_marker(p)
        lin = FakeLinear({marker: "https://linear.app/existing"})
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.linear, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(len(lin.updated), 1)
        self.assertEqual(lin.updated[0][0], "iss_existing")
        self.assertIn("AGENTS.md:104", lin.updated[0][1])
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

    def test_two_stuck_threads_file_one_rollup(self) -> None:
        t1 = thread(tid="PRRT_a", path="AGENTS.md", line=10, comments=stuck_pair())
        t2 = thread(tid="PRRT_b", path="CLAUDE.md", line=20, comments=stuck_pair())
        gh = FakeGitHub([pull(threads=[t1, t2])])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 2)
        self.assertEqual(len(lin.created), 1)
        self.assertEqual(lin.created[0][0], "Stuck PR threads: recursion#186")
        self.assertIn("AGENTS.md:10", lin.created[0][1])
        self.assertIn("CLAUDE.md:20", lin.created[0][1])
        self.assertEqual(lin.created[0][1].count("- [ ]"), 2)

    def test_already_mediated_thread_stays_on_rollup(self) -> None:
        t = thread(tid="PRRT_m", comments=stuck_pair())
        existing = [comment("pat-user", mod.mediation_marker("PRRT_m"), hours_ago=0.5)]
        gh = FakeGitHub([pull(threads=[t], issue_comments=existing)])
        lin = FakeLinear()
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(len(lin.created), 1)
        self.assertIn("AGENTS.md:104", lin.created[0][1])

    def test_skips_approved_on_head_with_green_checks(self) -> None:
        t = thread(comments=stuck_pair())
        p = pull(
            threads=[t],
            review_decision="APPROVED",
            checks_state="SUCCESS",
            head_oid="abc",
            head_sha="abc",
            approved_head_oids=("abc",),
        )
        gh = FakeGitHub([p])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(gh.comments, [])

    def test_stale_approval_snapshot_still_mediates_stuck_threads(self) -> None:
        t = thread(tid="PRRT_stale", comments=stuck_pair())
        p = pull(
            threads=[t],
            review_decision="APPROVED",
            checks_state="SUCCESS",
            head_oid=STALE,
            approved_head_oids=(STALE,),
            head_sha=HEAD,
            checks_green=True,
        )
        gh = FakeGitHub([p])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(mod.skip_pr_reason(p), "")
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(len(gh.comments), 1)
        self.assertIn("@codex review", gh.comments[0][1])
        self.assertEqual(len(lin.created), 1)

    def test_cancels_rollup_when_all_threads_resolve(self) -> None:
        t = thread(comments=stuck_pair(), resolved=True)
        p = pull(threads=[t])
        marker = mod.rollup_marker(p)
        lin = FakeLinear({marker: "https://linear.app/existing"})
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(lin.canceled, ["iss_existing"])

    def test_cancels_rollup_when_pr_merges(self) -> None:
        t = thread(comments=stuck_pair())
        merged = pull(number=99, threads=[t], state="MERGED")
        gh = FakeGitHub([merged])
        lin = FakeLinear()
        lin.rollups = [
            mod.LinearIssue(
                id="iss_99",
                url="https://linear.app/csl-laboratories/issue/CSL-99",
                identifier="CSL-99",
                title="Stuck PR threads: recursion#99",
                state_type="triage",
            )
        ]
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(lin.canceled, ["iss_99"])

    def test_preserves_checked_items_and_human_decision(self) -> None:
        t1 = thread(tid="PRRT_a", path="AGENTS.md", line=10, comments=stuck_pair())
        t2 = thread(tid="PRRT_b", path="CLAUDE.md", line=20, comments=stuck_pair())
        p = pull(threads=[t1, t2])
        previous = mod.linear_description(p, [(t1, "stuck-human")], cfg())
        previous = previous.replace("- [ ] [`AGENTS.md:10`]", "- [x] [`AGENTS.md:10`]")
        previous = previous.replace(
            mod.DEFAULT_HUMAN_DECISION, "Keep the AGENTS.md wording."
        )
        marker = mod.rollup_marker(p)
        lin = FakeLinear({marker: "https://linear.app/existing"})
        lin.existing_descriptions[marker] = previous
        gh = FakeGitHub([p])
        mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(len(lin.updated), 1)
        updated = lin.updated[0][1]
        self.assertIn("- [x] [`AGENTS.md:10`]", updated)
        self.assertIn("- [ ] [`CLAUDE.md:20`]", updated)
        self.assertIn("Keep the AGENTS.md wording.", updated)

    def test_sweep_skips_other_repo_rollups(self) -> None:
        lin = FakeLinear()
        lin.rollups = [
            mod.LinearIssue(
                id="iss_alpha",
                url="https://linear.app/csl-laboratories/issue/CSL-A",
                identifier="CSL-A",
                title="Stuck PR threads: alpha#12",
                state_type="triage",
            )
        ]
        gh = FakeGitHub([])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(lin.canceled, [])
        self.assertEqual(counters.linear, 0)

    def test_find_by_marker_prefers_active_issue(self) -> None:
        t = thread(tid="PRRT_dup", comments=stuck_pair())
        p = pull(threads=[t])
        marker = mod.rollup_marker(p)
        canceled = mod.LinearIssue(
            id="iss_old",
            url="https://linear.app/canceled",
            identifier="CSL-OLD",
            title="Stuck PR threads: recursion#186",
            state_type="canceled",
            description=marker,
        )
        active = mod.LinearIssue(
            id="iss_live",
            url="https://linear.app/live",
            identifier="CSL-LIVE",
            title="Stuck PR threads: recursion#186",
            state_type="triage",
            description=marker + "\n## Human decision\n\nKeep this.\n",
        )
        lin = FakeLinear()
        lin.marker_matches[marker] = [canceled, active]
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(lin.created, [])
        self.assertEqual(len(lin.updated), 1)
        self.assertEqual(lin.updated[0][0], "iss_live")
        self.assertIn("Keep this.", lin.updated[0][1])
        self.assertEqual(counters.linear, 0)

    def test_linear_error_on_closed_pr_does_not_abort_scan(self) -> None:
        class BoomLinear(FakeLinear):
            def find_by_marker(self, marker: str, *, title: str = "") -> mod.LinearIssue | None:
                raise RuntimeError("linear down")

        class ListClosedThenOpen(FakeGitHub):
            def list_open_pr_numbers(self) -> list[int]:
                return [1, 2]

        t = thread(comments=stuck_pair())
        gh = ListClosedThenOpen(
            [
                pull(number=1, threads=[t], state="CLOSED"),
                pull(number=2, threads=[thread(tid="PRRT_2", comments=stuck_pair())]),
            ]
        )
        counters = mod.scan(cfg(), gh, BoomLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 1)
        self.assertEqual(gh.comments[0][0], 2)

    def test_search_rollups_follows_page_cursors(self) -> None:
        calls: list[str | None] = []

        def opener(req: Request, timeout: int = 0):
            payload = json.loads(req.data.decode("utf-8"))
            after = (payload.get("variables") or {}).get("after")
            calls.append(after)
            if after is None:
                body = {
                    "data": {
                        "searchIssues": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "c2"},
                            "nodes": [
                                {
                                    "id": "iss_old",
                                    "identifier": "CSL-OLD",
                                    "url": "https://linear.app/old",
                                    "title": "Stuck PR threads: recursion#1",
                                    "description": "",
                                    "state": {"type": "canceled", "name": "Canceled"},
                                }
                            ],
                        }
                    }
                }
            else:
                body = {
                    "data": {
                        "searchIssues": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "iss_new",
                                    "identifier": "CSL-NEW",
                                    "url": "https://linear.app/new",
                                    "title": "Stuck PR threads: recursion#99",
                                    "description": "",
                                    "state": {"type": "triage", "name": "Triage"},
                                }
                            ],
                        }
                    }
                }
            return BytesIO(json.dumps(body).encode("utf-8"))

        client = mod.LinearClient(cfg(), opener=opener)
        found = client.search_rollups()
        self.assertEqual([issue.id for issue in found], ["iss_old", "iss_new"])
        self.assertEqual(calls, [None, "c2"])

    def test_canceled_rollup_does_not_carry_checks_or_decision(self) -> None:
        t = thread(tid="PRRT_a", path="AGENTS.md", line=10, comments=stuck_pair())
        p = pull(threads=[t])
        stale = mod.linear_description(p, [(t, "stuck-human")], cfg())
        stale = stale.replace("- [ ] [`AGENTS.md:10`]", "- [x] [`AGENTS.md:10`]")
        stale = stale.replace(mod.DEFAULT_HUMAN_DECISION, "Merge as-is.")
        marker = mod.rollup_marker(p)
        lin = FakeLinear({marker: "https://linear.app/canceled"})
        lin.existing_states[marker] = "canceled"
        lin.existing_descriptions[marker] = stale
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(len(lin.created), 1)
        self.assertEqual(lin.updated, [])
        created = lin.created[0][1]
        self.assertIn("- [ ] [`AGENTS.md:10`]", created)
        self.assertNotIn("- [x] [`AGENTS.md:10`]", created)
        self.assertIn(mod.DEFAULT_HUMAN_DECISION, created)
        self.assertNotIn("Merge as-is.", created)
        self.assertEqual(counters.linear, 1)

    def test_adopts_author_filed_title_without_marker(self) -> None:
        t = thread(tid="PRRT_hand", comments=stuck_pair())
        p = pull(threads=[t])
        title = mod.linear_title(p)
        author = mod.LinearIssue(
            id="iss_author",
            url="https://linear.app/author",
            identifier="CSL-HAND",
            title=title,
            state_type="triage",
            description="Filed by the PR author after a missed watcher cycle.",
        )
        lin = FakeLinear()
        lin.title_matches[title] = [author]
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(lin.created, [])
        self.assertEqual(len(lin.updated), 1)
        self.assertEqual(lin.updated[0][0], "iss_author")
        self.assertIn(mod.rollup_marker(p), lin.updated[0][1])
        self.assertIn(
            "Filed by the PR author after a missed watcher cycle.", lin.updated[0][1]
        )
        self.assertEqual(counters.linear, 0)

    def test_author_notes_survive_later_runs(self) -> None:
        t = thread(tid="PRRT_hand", comments=stuck_pair())
        p = pull(threads=[t])
        first = mod.linear_description(
            p, [(t, "stuck-human")], cfg(), previous="Decision: defer to Lucas."
        )
        second = mod.linear_description(
            p, [(t, "stuck-human")], cfg(), previous=first
        )
        self.assertEqual(second.count("Decision: defer to Lucas."), 1)
        self.assertIn(mod.NOTES_HEADING, second)

    def test_existing_author_notes_keeps_text_after_marker_without_notes_section(
        self,
    ) -> None:
        marker = "<!-- stuck-pr-threads:recursion#186 -->"
        notes = mod.existing_author_notes(
            f"{marker}\nDecision: defer to Lucas.",
            marker,
        )
        self.assertEqual(notes, "Decision: defer to Lucas.")

    def test_author_filed_with_marker_keeps_freeform_notes_across_runs(self) -> None:
        t = thread(tid="PRRT_hand", comments=stuck_pair())
        p = pull(threads=[t])
        marker = mod.rollup_marker(p)
        author_line = "Decision: defer to Lucas."
        author = mod.LinearIssue(
            id="iss_author_marker",
            url="https://linear.app/author-marker",
            identifier="CSL-HAND-M",
            title=mod.linear_title(p),
            state_type="triage",
            description=f"{marker}\n{author_line}",
        )
        lin = FakeLinear()
        lin.marker_matches[marker] = [author]
        gh = FakeGitHub([p])
        mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(lin.created, [])
        self.assertEqual(len(lin.updated), 1)
        first = lin.updated[0][1]
        self.assertIn(author_line, first)
        self.assertIn(mod.NOTES_HEADING, first)
        self.assertIn("## Stuck threads", first)

        lin.marker_matches[marker] = [replace(author, description=first)]
        mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(len(lin.updated), 2)
        second = lin.updated[1][1]
        self.assertEqual(second.count(author_line), 1)
        self.assertIn(author_line, second)
        self.assertIn(mod.NOTES_HEADING, second)


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
            "MAX_RESOLVES_PER_PR_PER_DAY": "2",
            "CODEX_REVIEW_LOGIN": "chatgpt-codex-connector[bot]",
            "PR_NUMBER": "42",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            got = mod.Config.from_env(now=NOW)
        self.assertEqual(got.stuck_after_hours, 9.0)
        self.assertEqual(got.max_per_pr_per_day, 1)
        self.assertEqual(got.max_resolves_per_pr_per_day, 2)
        self.assertEqual(got.codex_review_login, "chatgpt-codex-connector[bot]")
        self.assertEqual(got.pr_number, 42)
        self.assertTrue(got.linear_enabled)

    def test_thread_write_graphql_refused_without_flag(self) -> None:
        client = mod.GitHubClient(cfg())
        with self.assertRaises(RuntimeError) as ctx:
            client._request(
                "https://api.github.com/graphql",
                token="x",
                payload={"query": "mutation { resolveReviewThread(input: {threadId: \"T\"}) { thread { id } } }"},
                method="POST",
            )
        self.assertIn("refusing GitHub merge/resolve GraphQL", str(ctx.exception))

    def test_resolve_thread_uses_pat_not_github_token(self) -> None:
        captured: list[Request] = []

        def opener(req: Request, timeout: int = 0):
            captured.append(req)
            return BytesIO(b'{"data":{"resolveReviewThread":{"thread":{"id":"T","isResolved":true}}}}')

        client = mod.GitHubClient(cfg(), opener=opener)
        client.resolve_thread("PRRT_1")
        self.assertEqual(len(captured), 1)
        auth = captured[0].get_header("Authorization") or captured[0].headers.get("Authorization")
        self.assertIn("codex-pat", auth)
        self.assertNotIn("gh-read", auth)
        body = json.loads(captured[0].data.decode("utf-8"))
        self.assertIn("resolveReviewThread", body["query"])

    def test_reply_on_thread_uses_pat(self) -> None:
        captured: list[Request] = []

        def opener(req: Request, timeout: int = 0):
            captured.append(req)
            return BytesIO(b'{"data":{"addPullRequestReviewThreadReply":{"comment":{"id":"C"}}}}')

        client = mod.GitHubClient(cfg(), opener=opener)
        client.reply_on_thread("PRRT_1", "hello")
        self.assertEqual(len(captured), 1)
        auth = captured[0].get_header("Authorization") or captured[0].headers.get("Authorization")
        self.assertIn("codex-pat", auth)


class VerdictAndResolveTests(unittest.TestCase):
    def test_parse_verdict_last_block_wins(self) -> None:
        earlier = comment(CODEX, verdict_block("PRRT_1", "DECIDE", STALE), hours_ago=0.3)
        later = comment(CODEX, verdict_block("PRRT_1", "RESOLVE", HEAD), hours_ago=0.2)
        self.assertEqual(len(mod.parse_thread_verdicts(later)), 1)
        latest = mod.latest_thread_verdict("PRRT_1", [earlier, later], cfg())
        self.assertIsNotNone(latest)
        self.assertEqual(latest.action, "RESOLVE")
        self.assertEqual(latest.head_sha, HEAD)

    def test_spoofed_verdict_from_non_codex_is_ignored(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        fake = comment("mallory", verdict_block("PRRT_1"), hours_ago=0.2)
        p = pull(threads=[t], issue_comments=asked, reviews=[fake])
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "no-verdict")

    def test_resolve_when_asked_and_green(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1, url="https://codex.example")
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], checks_green=True)
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "resolve")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(gh.resolves, ["PRRT_1"])
        self.assertEqual(len(gh.replies), 1)
        self.assertIn("<!-- codex-thread-cited:PRRT_1 -->", gh.replies[0][1])
        self.assertIn(HEAD[:12], gh.replies[0][1])
        self.assertIn("CODEX-THREAD: RESOLVE", gh.replies[0][1])
        self.assertTrue(any("<!-- codex-thread-resolved:PRRT_1 -->" in body for _, body in gh.comments))

    def test_decide_missing_ambiguous_do_not_resolve(self) -> None:
        for action in ("DECIDE", "MISSING", "AMBIGUOUS"):
            t = thread(tid="PRRT_1", comments=stuck_pair())
            asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
            ruling = comment(CODEX, verdict_block("PRRT_1", action), hours_ago=0.1)
            p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
            self.assertEqual(
                mod.classify_resolve(p, t, cfg(mediator_login="pat-user")),
                action.lower(),
            )
            gh = FakeGitHub([p])
            counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
            self.assertEqual(counters.resolved, 0, action)
            self.assertEqual(gh.resolves, [], action)

    def test_stale_sha_does_not_resolve(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1", sha=STALE), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], head_sha=HEAD)
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "stale-sha")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.skipped_stale, 1)
        self.assertEqual(gh.resolves, [])

    def test_review_commit_mismatch_is_stale(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = mod.Comment(
            login=CODEX,
            body=verdict_block("PRRT_1", sha=HEAD),
            created_at=ts(0.1),
            commit_sha=STALE,
        )
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], head_sha=HEAD)
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "stale-sha")

    def test_pending_required_checks_do_not_resolve(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], checks_green=False)
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "checks")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.skipped_checks, 1)

    def test_human_thread_not_asked_is_never_resolved(self) -> None:
        t = thread(
            tid="PRRT_h",
            comments=[
                comment("alice", "please look", hours_ago=8),
                comment(CLAUDE_BOT, "needs a human", hours_ago=7),
            ],
        )
        ruling = comment(CODEX, verdict_block("PRRT_h"), hours_ago=0.1)
        p = pull(threads=[t], reviews=[ruling])
        self.assertEqual(mod.classify_resolve(p, t, cfg()), "not-asked")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(gh.resolves, [])

    def test_human_thread_resolves_only_when_human_asked_codex(self) -> None:
        t = thread(
            tid="PRRT_h",
            comments=[comment("alice", "please look", hours_ago=8)],
        )
        asked = [comment("alice", f"@codex review please rule on {t.id}", hours_ago=0.3)]
        ruling = comment(CODEX, verdict_block("PRRT_h"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(mod.classify_resolve(p, t, cfg()), "resolve")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(gh.resolves, ["PRRT_h"])

    def test_reopened_thread_stays_open_after_successful_resolve_marker(self) -> None:
        t = thread(
            tid="PRRT_1",
            comments=stuck_pair()
            + [comment("pat-user", mod.cite_marker("PRRT_1"), hours_ago=0.05)],
        )
        asked = [
            comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4),
            comment("pat-user", mod.resolve_marker("PRRT_1"), hours_ago=0.04),
        ]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(
            mod.classify_resolve(p, t, cfg(mediator_login="pat-user")),
            "already-marked",
        )
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(gh.resolves, [])
        self.assertEqual(gh.replies, [])

    def test_quoted_verdict_is_ignored(self) -> None:
        quoted = "\n".join(f"> {line}" for line in verdict_block("PRRT_1").splitlines())
        parsed = mod.parse_thread_verdicts(comment(CODEX, quoted, hours_ago=0.1))
        self.assertEqual(parsed, ())
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, quoted, hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "no-verdict")

    def test_fenced_verdict_is_ignored(self) -> None:
        fenced = "```\n" + verdict_block("PRRT_1") + "```\n"
        parsed = mod.parse_thread_verdicts(comment(CODEX, fenced, hours_ago=0.1))
        self.assertEqual([(v.action, v.head_sha) for v in parsed], [("AMBIGUOUS", "")])
        self._assert_no_resolve(fenced)

    def _real_scan(self, body: str, *, tid: str = "PRRT_1") -> tuple[str, FakeGitHub, mod.Counters]:
        """Authenticated Codex review on the live head after a legitimate ask."""
        t = thread(tid=tid, comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker(tid), hours_ago=0.4)]
        ruling = comment(CODEX, body, hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], checks_green=True)
        reason = mod.classify_resolve(p, t, cfg(mediator_login="pat-user"))
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        return reason, gh, counters

    def _assert_no_resolve(self, body: str, *, tid: str = "PRRT_1") -> None:
        # Either no verdict section at all, or a whole-message AMBIGUOUS barrier.
        parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
        self.assertTrue(all((v.action, v.head_sha) == ("AMBIGUOUS", "") for v in parsed), body)
        reason, gh, counters = self._real_scan(body, tid=tid)
        self.assertEqual(reason, "ambiguous" if parsed else "no-verdict", body)
        self.assertEqual(gh.resolves, [], body)
        self.assertEqual(gh.replies, [], body)
        self.assertEqual(counters.resolved, 0, body)

    def _assert_resolves(self, body: str, *, tid: str = "PRRT_1") -> None:
        parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
        self.assertEqual([(v.thread_id, v.action, v.head_sha) for v in parsed], [(tid, "RESOLVE", HEAD)], body)
        reason, gh, counters = self._real_scan(body, tid=tid)
        self.assertEqual(reason, "resolve", body)
        self.assertEqual(gh.resolves, [tid], body)
        self.assertEqual(counters.resolved, 1, body)

    def test_inline_code_marker_with_following_lines_does_not_resolve(self) -> None:
        # Real-scan reproduction: the marker is quoted inline in prose and the
        # RESOLVE/HEAD lines that follow have no standalone marker of their own.
        body = (
            "The thread supplied this marker: `" + mod.verdict_marker("PRRT_1") + "`\n"
            "CODEX-THREAD: RESOLVE\n"
            f"HEAD: {HEAD}\n"
        )
        self._assert_no_resolve(body)

    def test_marker_sharing_a_line_with_other_text_is_not_standalone(self) -> None:
        self._assert_no_resolve(
            f"{mod.verdict_marker('PRRT_1')} (copied from the thread)\n"
            f"CODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        self._assert_no_resolve(
            f"Marker: {mod.verdict_marker('PRRT_1')}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )

    def test_tilde_fenced_copy_does_not_resolve(self) -> None:
        # Real-scan reproduction: a tilde fence is a code fence too.
        block = verdict_block("PRRT_1")
        self._assert_no_resolve("Quoted thread text:\n~~~text\n" + block + "~~~\n")
        self._assert_no_resolve("~~~~\n" + block + "~~~~\n")
        self._assert_no_resolve("~~~\n" + block + "~~~~~\n")

    def test_fence_closer_must_match_opener_character_and_length(self) -> None:
        block = verdict_block("PRRT_1")
        # A shorter closer does not close; the opener then hides the rest.
        self._assert_no_resolve("````\n" + block + "```\n")
        self._assert_no_resolve("~~~~\n" + block + "~~~\n")
        # A different character does not close either.
        self._assert_no_resolve("```\n" + block + "~~~\n")
        # A longer closer does close, and the copy stays hidden.
        self._assert_no_resolve("```\n" + block + "`````\n")
        self._assert_no_resolve("```text\n" + block + "```\n")

    def test_unclosed_fence_hides_everything_after_it(self) -> None:
        block = verdict_block("PRRT_1")
        self._assert_no_resolve("Thread text follows:\n```\n" + block)
        self._assert_no_resolve("~~~\n" + block + "\nmore commentary\n")
        # Nothing may follow the verdict section, not even a stray opener.
        self._assert_no_resolve(block + "\nAppendix:\n```\nunterminated example\n")

    def test_indented_example_block_is_ignored(self) -> None:
        block = verdict_block("PRRT_1")
        indented = "".join("    " + line + "\n" for line in block.splitlines())
        self._assert_no_resolve("Example of the expected shape:\n\n" + indented)
        tabbed = "".join("\t" + line + "\n" for line in block.splitlines())
        self._assert_no_resolve(tabbed)
        # Only the marker indented as code: the loose lines have no marker.
        self._assert_no_resolve(
            f"    {mod.verdict_marker('PRRT_1')}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )

    def test_hidden_content_does_not_join_verdict_pieces(self) -> None:
        marker = mod.verdict_marker("PRRT_1")
        # Marker, then a fenced example, then loose RESOLVE/HEAD lines.
        self._assert_no_resolve(
            f"{marker}\n```\nexample\n```\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        self._assert_no_resolve(
            f"{marker}\n~~~\nexample\n~~~\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        # Marker, then a quoted line, then loose RESOLVE/HEAD lines.
        self._assert_no_resolve(
            f"{marker}\n> CODEX-THREAD: DECIDE\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        # Quoted marker followed by loose lines has no marker at all.
        self._assert_no_resolve(f"> {marker}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n")
        # A genuine DECIDE followed by an echoed, quoted RESOLVE is non-authorizing.
        quoted = "\n".join(f"> {line}" for line in verdict_block("PRRT_1").splitlines())
        body = verdict_block("PRRT_1", "DECIDE") + "\nThe thread text said:\n" + quoted + "\n"
        parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
        self.assertEqual([(v.action, v.head_sha) for v in parsed], [("AMBIGUOUS", "")])
        reason, gh, counters = self._real_scan(body)
        self.assertEqual(reason, "ambiguous")
        self.assertEqual(gh.resolves, [])
        self.assertEqual(counters.resolved, 0)

    def test_conflicting_action_or_head_under_one_marker_is_rejected(self) -> None:
        marker = mod.verdict_marker("PRRT_1")
        self._assert_ambiguous(
            f"{marker}\nCODEX-THREAD: DECIDE\nHEAD: {HEAD}\n"
            f"CODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        self._assert_ambiguous(
            f"{marker}\nCODEX-THREAD: RESOLVE\nHEAD: {STALE}\nHEAD: {HEAD}\n"
        )
        self._assert_ambiguous(
            f"{marker}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\nCODEX-THREAD: DECIDE\n"
        )
        # Repeated lines under one marker break the three-line block shape.
        self._assert_ambiguous(
            f"{marker}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
            f"CODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n"
        )
        # Two well-formed blocks for one thread that disagree are ambiguous.
        self._assert_ambiguous(
            verdict_block("PRRT_1", "DECIDE", STALE) + "\n" + verdict_block("PRRT_1", "RESOLVE", HEAD)
        )
        # Identical duplicate blocks are redundant, not ambiguous.
        self._assert_resolves(verdict_block("PRRT_1") + "\n" + verdict_block("PRRT_1"))

    def test_later_conflicting_block_does_not_revive_earlier_resolve(self) -> None:
        earlier = verdict_block("PRRT_1")
        later = verdict_block("PRRT_1", "DECIDE") + "CODEX-THREAD: RESOLVE\n"
        for separate_comments in (False, True):
            with self.subTest(separate_comments=separate_comments):
                t = thread(comments=stuck_pair())
                reviews = ([comment(CODEX, earlier, hours_ago=0.2), comment(CODEX, later, hours_ago=0.1)]
                           if separate_comments else [comment(CODEX, earlier + later, hours_ago=0.1)])
                asked = [comment("pat-user", mod.mediation_marker(t.id), hours_ago=0.4)]
                p = pull(threads=[t], issue_comments=asked, reviews=reviews)
                c = cfg(mediator_login="pat-user")
                self.assertEqual(mod.classify_resolve(p, t, c), "ambiguous")
                gh = FakeGitHub([p])
                counters = mod.scan(c, gh, FakeLinear())
                self.assertEqual(gh.resolves, [])
                self.assertEqual(gh.replies, [])
                self.assertEqual(counters.resolved, 0)

    def _assert_ambiguous(self, body: str) -> None:
        parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
        self.assertEqual([(v.action, v.head_sha) for v in parsed], [("AMBIGUOUS", "")])
        reason, gh, counters = self._real_scan(body)
        self.assertEqual(reason, "ambiguous")
        self.assertEqual(gh.resolves, [])
        self.assertEqual(gh.replies, [])
        self.assertEqual(counters.resolved, 0)

    def test_standalone_verdict_tolerates_protocol_whitespace(self) -> None:
        # Valid controls: the explicit protocol keeps resolving.
        self._assert_resolves(verdict_block("PRRT_1"))
        self._assert_resolves(verdict_block("PRRT_1").replace("\n", "\r\n"))
        self._assert_resolves(f"  {mod.verdict_marker('PRRT_1')}  \nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n")
        self._assert_resolves(verdict_block("PRRT_1") + "\n\n  \n")
        self._assert_resolves(
            "Summary: the fix is on head.\n\n"
            "```text\nexample output\n```\n\n"
            + verdict_block("PRRT_1")
        )
        self._assert_resolves("> Devin: please clarify.\n\n" + verdict_block("PRRT_1"))
        # The three lines are contiguous and unindented past the marker.
        self._assert_no_resolve(f"{mod.verdict_marker('PRRT_1')}\n\nCODEX-THREAD: RESOLVE\n\nHEAD: {HEAD}\n")
        self._assert_no_resolve(f"{mod.verdict_marker('PRRT_1')}\n  CODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n")
        self._assert_no_resolve(f"{mod.verdict_marker('PRRT_1')}\nCODEX-THREAD: RESOLVE\n")
        self._assert_no_resolve(verdict_block("PRRT_1") + "\n- RESOLVE: present on that HEAD.\n")

    def _multi_thread_scan(self, body: str) -> tuple[list[str], FakeGitHub, mod.Counters]:
        threads = [thread(tid=f"PRRT_{i}", comments=stuck_pair()) for i in (1, 2, 3)]
        asked = [comment("pat-user", mod.mediation_marker(f"PRRT_{i}"), hours_ago=0.4) for i in (1, 2, 3)]
        ruling = comment(CODEX, body, hours_ago=0.1)
        p = pull(threads=threads, issue_comments=asked, reviews=[ruling], checks_green=True)
        c = cfg(mediator_login="pat-user")
        reasons = [mod.classify_resolve(p, t, c) for t in threads]
        gh = FakeGitHub([p])
        counters = mod.scan(c, gh, FakeLinear())  # type: ignore[arg-type]
        return reasons, gh, counters

    def test_multi_thread_review_resolves_only_standalone_rulings(self) -> None:
        # Prose and a fenced echo between blocks break the whole section.
        body = (
            verdict_block("PRRT_1")
            + "\nThread 3 quoted this in its own text:\n~~~\n"
            + verdict_block("PRRT_3")
            + "~~~\n\n"
            + verdict_block("PRRT_2")
        )
        reasons, gh, counters = self._multi_thread_scan(body)
        self.assertEqual(reasons, ["ambiguous", "ambiguous", "ambiguous"])
        self.assertEqual(gh.resolves, [])
        self.assertEqual(gh.replies, [])
        self.assertEqual(counters.resolved, 0)

    def test_html_pre_wrapped_verdict_does_not_resolve(self) -> None:
        block = verdict_block("PRRT_1")
        self._assert_no_resolve("Thread text:\n<pre>\n" + block + "</pre>\n")
        self._assert_no_resolve("<pre><code>\n" + block + "</code></pre>\n")
        self._assert_no_resolve("Echo:\n<PRE class=x>\n" + block + "</PRE>\n")
        # An unclosed wrapper renders everything after it inside the element.
        self._assert_no_resolve("Thread text:\n<pre>\n" + block)
        self._assert_no_resolve("<PRE class=x>\n\n" + block)

    def test_html_details_and_blockquote_wrapped_verdict_does_not_resolve(self) -> None:
        block = verdict_block("PRRT_1")
        self._assert_no_resolve(
            "<details><summary>Thread text</summary>\n\n" + block + "\n</details>\n"
        )
        self._assert_no_resolve("<blockquote>\n" + block + "</blockquote>\n")
        self._assert_no_resolve("<details>\n<summary>Echo</summary>\n\n" + block)
        self._assert_no_resolve("<blockquote>\n\n" + block)

    def test_html_comment_spanning_verdict_does_not_resolve(self) -> None:
        block = verdict_block("PRRT_1")
        self._assert_no_resolve("Notes:\n<!--\n" + block + "-->\n")
        self._assert_no_resolve("<!-- hidden echo\n" + block)
        # A closed comment in the prose is just prose.
        self._assert_resolves("<!-- reviewer note -->\nSummary.\n\n" + block)

    def test_action_lines_inside_html_after_standalone_marker_do_not_resolve(self) -> None:
        self._assert_no_resolve(
            f"{mod.verdict_marker('PRRT_1')}\n<pre>\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n</pre>\n"
        )
        # Marker on the same line as <pre>, or inside a list item: no section at all.
        self._assert_no_resolve(
            f"<pre>{mod.verdict_marker('PRRT_1')}\nCODEX-THREAD: RESOLVE\nHEAD: {HEAD}\n</pre>\n"
        )
        self._assert_no_resolve(
            f"- {mod.verdict_marker('PRRT_1')}\n  CODEX-THREAD: RESOLVE\n  HEAD: {HEAD}\n"
        )

    def test_unmatched_close_then_open_is_barrier(self) -> None:
        block = verdict_block("PRRT_1")
        for prose in ("Note </pre><pre>\n", "Echo </details><details>\n", "Line </div><div>\n"):
            with self.subTest(prose=prose):
                self._assert_no_resolve(prose + "\n" + block)

    def test_lone_unmatched_close_is_barrier(self) -> None:
        self._assert_no_resolve("Thread text </pre>\n\n" + verdict_block("PRRT_1"))

    def test_misnested_close_is_barrier(self) -> None:
        self._assert_no_resolve("<details><pre></details>\n\n" + verdict_block("PRRT_1"))

    def test_balanced_nesting_still_resolves(self) -> None:
        self._assert_resolves(
            "<details><summary>x</summary><pre>q</pre></details>\n\n" + verdict_block("PRRT_1")
        )

    def test_quoted_slash_gt_in_pre_attribute_does_not_self_close(self) -> None:
        # Codex P1 4302717106: a first-`>` regex treats title="/>" as self-close.
        # Scan path is parse_thread_verdicts → classify_resolve → scan.
        block = verdict_block("PRRT_1")
        self._assert_no_resolve('Thread text:\n<pre title="/>">\n' + block)
        self._assert_no_resolve("Thread text:\n<pre title='/>'>\n" + block)
        self._assert_resolves('Thread text:\n<pre title="/>"></pre>\n\n' + block)
        self._assert_resolves("Thread text:\n<pre title='/>'></pre>\n\n" + block)

    def test_type1_pre_with_blank_line_before_gt_does_not_resolve(self) -> None:
        # Devin r4304188481 at 5a50fb1f: <pre plus two line endings is not a
        # CommonMark 6.6 tag (_skip_html_tag_space stops after one), so
        # _html_tag_at returned None and a following triple authorized.
        # Type 1 still starts at line-start <pre plus EOL; blank lines do
        # not end it. Scan path is parse_thread_verdicts → classify_resolve
        # → scan. Do not loosen 6.6 whitespace: type 6 (<div) ends at a
        # blank line, so successive newlines are not tag space.
        block = verdict_block("PRRT_1")
        self._assert_no_resolve("Thread text:\n<pre\n\n>\n" + block)
        self._assert_no_resolve("Thread text:\n<script\n\n>\n" + block)
        self._assert_resolves("Thread text:\n<pre\n\n></pre>\n\n" + block)
        self._assert_resolves("Thread text:\n<pre\n\n>\n</pre>\n\n" + block)

    def test_type1_malformed_pre_attributes_do_not_resolve(self) -> None:
        # Devin r4304188525 at 5a50fb1f: title=x"y and @x are not 6.6
        # attributes, so _html_tag_at returned None and the <pre opener
        # was ignored. CommonMark type 1 still opens on <pre plus space.
        # Scan path is parse_thread_verdicts → classify_resolve → scan.
        block = verdict_block("PRRT_1")
        self._assert_no_resolve('Thread text:\n<pre title=x"y>\n' + block)
        self._assert_no_resolve("Thread text:\n<pre @x>\n" + block)
        self._assert_resolves('Thread text:\n<pre title=x"y></pre>\n\n' + block)
        self._assert_resolves("Thread text:\n<pre @x></pre>\n\n" + block)

    def test_unclosed_html_raw_blocks_do_not_resolve(self) -> None:
        # CommonMark 4.6 types 3–5 continue through EOF without a closer, so
        # a following producer-shaped triple is an echo inside the raw block.
        # Scan path is parse_thread_verdicts → classify_resolve → scan.
        block = verdict_block("PRRT_1")
        openers = (
            "<![CDATA[\n",
            "<?\n",
            '<?xml version="1.0"\n',
            "<!DOCTYPE html\n",
            "<!ELEMENT foo bar\n",
        )
        for opener in openers:
            with self.subTest(opener=opener):
                self._assert_no_resolve("Thread text:\n" + opener + block)
                self._assert_no_resolve("Echo " + opener.rstrip() + "\n" + block)

    def test_closed_html_raw_blocks_in_prose_still_resolve(self) -> None:
        block = verdict_block("PRRT_1")
        self._assert_resolves("Thread text:\n<![CDATA[ example ]]>\n\n" + block)
        self._assert_resolves('Thread text:\n<?xml version="1.0"?>\n\n' + block)
        self._assert_resolves("Thread text:\n<!DOCTYPE html>\n\n" + block)

    def test_escaped_fence_copy_does_not_resolve(self) -> None:
        self._assert_no_resolve("\\```\n" + verdict_block("PRRT_1") + "\\```\n")

    def test_markup_copy_after_decide_cannot_revive_resolve(self) -> None:
        copy = verdict_block("PRRT_1", "DECIDE") + "\nThe thread said:\n<pre>\n" + verdict_block("PRRT_1") + "</pre>\n"
        parsed = mod.parse_thread_verdicts(comment(CODEX, copy, hours_ago=0.1))
        self.assertEqual([(v.thread_id, v.action, v.head_sha) for v in parsed], [("PRRT_1", "AMBIGUOUS", "")])
        self._assert_no_resolve(copy)
        earlier = verdict_block("PRRT_1")
        for separate_comments in (False, True):
            with self.subTest(separate_comments=separate_comments):
                t = thread(comments=stuck_pair())
                reviews = ([comment(CODEX, earlier, hours_ago=0.2), comment(CODEX, copy, hours_ago=0.1)]
                           if separate_comments else [comment(CODEX, earlier + "\n" + copy, hours_ago=0.1)])
                asked = [comment("pat-user", mod.mediation_marker(t.id), hours_ago=0.4)]
                p = pull(threads=[t], issue_comments=asked, reviews=reviews)
                c = cfg(mediator_login="pat-user")
                self.assertEqual(mod.classify_resolve(p, t, c), "ambiguous")
                gh = FakeGitHub([p])
                counters = mod.scan(c, gh, FakeLinear())  # type: ignore[arg-type]
                self.assertEqual(gh.resolves, [])
                self.assertEqual(gh.replies, [])
                self.assertEqual(counters.resolved, 0)

    def test_prose_after_verdict_section_makes_message_non_authorizing(self) -> None:
        self._assert_no_resolve(verdict_block("PRRT_1") + "\nThanks!\n")
        self._assert_no_resolve(verdict_block("PRRT_1") + "Thanks!")
        self._assert_no_resolve(verdict_block("PRRT_1") + "\n> quoted\n")
        self._assert_no_resolve(verdict_block("PRRT_1") + "| a | b |\n")

    def test_prose_before_verdict_section_still_resolves(self) -> None:
        self._assert_resolves(
            "The fix is already on head; Devin's concern is addressed.\n\n" + verdict_block("PRRT_1")
        )
        body = (
            "Both threads are fixed on head.\n\n"
            + verdict_block("PRRT_1")
            + "\n\n"
            + verdict_block("PRRT_2")
            + "\n"
            + verdict_block("PRRT_3", "DECIDE")
        )
        reasons, gh, counters = self._multi_thread_scan(body)
        self.assertEqual(reasons, ["resolve", "resolve", "decide"])
        self.assertEqual(gh.resolves, ["PRRT_1", "PRRT_2"])
        self.assertEqual(counters.resolved, 2)

    def test_mediation_body_instruction_matches_grammar(self) -> None:
        t = thread(tid="PRRT_abc", comments=stuck_pair())
        body = mod.mediation_body(pull(threads=[t]), t, cfg(), reason="stuck-human")
        flat = " ".join(body.split())
        self.assertIn(
            "Finish your review with the verdict section: for each thread you were asked "
            "about, exactly three plain lines — the marker line, "
            "`CODEX-THREAD: RESOLVE|DECIDE|MISSING|AMBIGUOUS`, `HEAD: <full 40-character SHA>` "
            "— separated by blank lines only. You may end with exactly one `CODEX-GATE:` "
            "line after the last triple, as the Code Review Rules require; nothing else may "
            "follow the verdict section. No code fences, HTML, quotes, bullets or "
            "commentary in or after that section; anything else makes the whole verdict "
            "non-authorizing.",
            flat,
        )
        self.assertNotIn("not in a code fence", body)
        self.assertIn("@codex review", body)
        self.assertIn("UNTRUSTED DATA", body)
        self.assertNotIn("@claude", body.lower())
        # The template the prompt shows, filled in, is itself a valid section.
        template = body[body.index(mod.verdict_marker("PRRT_abc")):].split("\n")[:3]
        self.assertEqual(template[1], "CODEX-THREAD: RESOLVE|DECIDE|MISSING|AMBIGUOUS")
        filled = "\n".join([template[0], "CODEX-THREAD: RESOLVE", f"HEAD: {HEAD}"])
        for reply in (filled, filled + "\n\nCODEX-GATE: READY\n"):
            self.assertEqual(
                [(v.thread_id, v.action) for v in mod.parse_thread_verdicts(comment(CODEX, reply))],
                [("PRRT_abc", "RESOLVE")],
                reply,
            )

    def test_single_trailing_gate_line_after_verdict_section_still_resolves(self) -> None:
        prose = "The fix is already on head.\n\n"
        block = verdict_block("PRRT_1")
        for gate in ("READY", "MEDIATE", "FIX"):
            self._assert_resolves(prose + block + f"CODEX-GATE: {gate}\n")
        self._assert_resolves((prose + block + "CODEX-GATE: READY\n").replace("\n", "\r\n"))
        self._assert_resolves(prose + block + "\n\nCODEX-GATE: READY  \n\n\n")
        self._assert_resolves(prose + block + "   CODEX-GATE: READY")
        body = (
            prose
            + verdict_block("PRRT_1")
            + "\n"
            + verdict_block("PRRT_2")
            + "\n"
            + verdict_block("PRRT_3", "DECIDE")
            + "\nCODEX-GATE: MEDIATE\n"
        )
        reasons, gh, counters = self._multi_thread_scan(body)
        self.assertEqual(reasons, ["resolve", "resolve", "decide"])
        self.assertEqual(gh.resolves, ["PRRT_1", "PRRT_2"])
        self.assertEqual(counters.resolved, 2)

    def test_gate_line_does_not_authorize_or_alter_verdicts(self) -> None:
        body = verdict_block("PRRT_1", "DECIDE") + "CODEX-GATE: READY\n"
        parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
        self.assertEqual([(v.thread_id, v.action, v.head_sha) for v in parsed], [("PRRT_1", "DECIDE", HEAD)])
        reason, gh, counters = self._real_scan(body)
        self.assertEqual(reason, "decide")
        self.assertEqual(gh.resolves, [])
        self.assertEqual(counters.resolved, 0)
        stale = verdict_block("PRRT_1", "RESOLVE", STALE) + "CODEX-GATE: READY\n"
        self.assertEqual(self._real_scan(stale)[0], "stale-sha")
        for lone in ("CODEX-GATE: READY\n", "Summary.\n\nCODEX-GATE: READY\n"):
            self.assertEqual(mod.parse_thread_verdicts(comment(CODEX, lone)), ())
            self._assert_no_resolve(lone)
        before = "CODEX-GATE: READY\n\n" + verdict_block("PRRT_1")
        parsed = mod.parse_thread_verdicts(comment(CODEX, before, hours_ago=0.1))
        self.assertEqual([(v.action, v.head_sha) for v in parsed], [("AMBIGUOUS", "")])
        self._assert_no_resolve(before)

    def test_second_or_malformed_trailing_gate_line_is_non_authorizing(self) -> None:
        block = verdict_block("PRRT_1")
        bodies = [
            block + "CODEX-GATE: READY\nCODEX-GATE: READY\n",
            block + "CODEX-GATE: READY\n\nCODEX-GATE: MEDIATE\n",
            block + "CODEX-GATE: READY\n\n" + verdict_block("PRRT_2"),
            block + "CODEX-GATE: READY!\n",
            block + "CODEX-GATE: FIXED\n",
            block + "CODEX-GATE: ready\n",
            block + "    CODEX-GATE: READY\n",
            block + "```\nCODEX-GATE: READY\n```\n",
            block + "> CODEX-GATE: READY\n",
            block + "<pre>CODEX-GATE: READY</pre>\n",
            block + "CODEX-GATE: READY\nThanks!\n",
            block + "Thanks!\nCODEX-GATE: READY\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                parsed = mod.parse_thread_verdicts(comment(CODEX, body, hours_ago=0.1))
                self.assertTrue(parsed, body)
                self._assert_no_resolve(body)

    def test_unread_check_page_is_not_green(self) -> None:
        self.assertFalse(
            mod.required_checks_are_green(
                {"state": "SUCCESS"},
                contexts=[
                    {
                        "__typename": "CheckRun",
                        "name": "linux",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                    }
                ],
                has_next_page=True,
            )
        )
        self.assertFalse(
            mod.required_checks_are_green(
                {
                    "state": "SUCCESS",
                    "contexts": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": True},
                    },
                }
            )
        )

    def test_refresh_stale_head_blocks_resolve(self) -> None:
        class MovingHead(FakeGitHub):
            def refresh_head(self, pull: mod.Pull) -> mod.Pull:
                return mod.replace(pull, head_sha=STALE)

        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling], head_sha=HEAD)
        gh = MovingHead([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.skipped_stale, 1)
        self.assertEqual(gh.resolves, [])

    def test_cite_happens_before_resolve(self) -> None:
        order: list[str] = []

        class OrderGitHub(FakeGitHub):
            def resolve_thread(self, thread_id: str) -> None:
                order.append("resolve")
                super().resolve_thread(thread_id)

            def reply_on_thread(self, thread_id: str, body: str) -> None:
                order.append("reply")
                super().reply_on_thread(thread_id, body)

            def post_comment(self, number: int, body: str) -> None:
                order.append("marker")
                super().post_comment(number, body)

        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        gh = OrderGitHub([pull(threads=[t], issue_comments=asked, reviews=[ruling])])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(order, ["reply", "resolve", "marker"])

    def test_failed_resolve_keeps_citation_and_does_not_count_cap(self) -> None:
        class BoomResolve(FakeGitHub):
            def resolve_thread(self, thread_id: str) -> None:
                raise RuntimeError("403")

        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        gh = BoomResolve([pull(threads=[t], issue_comments=asked, reviews=[ruling])])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.resolve_failed, 1)
        self.assertEqual(len(gh.replies), 1)
        self.assertIn("<!-- codex-thread-cited:PRRT_1 -->", gh.replies[0][1])
        self.assertFalse(any("<!-- codex-thread-resolved:" in body for _, body in gh.comments))
        self.assertEqual(mod.exit_code(counters), 1)

    def test_reply_failure_does_not_resolve(self) -> None:
        class BoomReply(FakeGitHub):
            def reply_on_thread(self, thread_id: str, body: str) -> None:
                raise RuntimeError("403")

        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        gh = BoomReply([pull(threads=[t], issue_comments=asked, reviews=[ruling])])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.resolve_failed, 1)
        self.assertEqual(gh.resolves, [])
        self.assertEqual(mod.exit_code(counters), 1)

    def test_cited_but_unresolved_retries_resolve_only(self) -> None:
        t = thread(
            tid="PRRT_1",
            comments=stuck_pair()
            + [
                comment(
                    "pat-user",
                    f"{mod.cite_marker('PRRT_1')}\nHEAD {HEAD}",
                    hours_ago=0.05,
                )
            ],
        )
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(
            mod.classify_resolve(p, t, cfg(mediator_login="pat-user")),
            "retry-resolve",
        )
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(gh.resolves, ["PRRT_1"])
        self.assertEqual(gh.replies, [])
        self.assertTrue(any("<!-- codex-thread-resolved:PRRT_1 -->" in body for _, body in gh.comments))

    def test_stale_head_cite_is_not_retry_and_recites_current_head(self) -> None:
        t = thread(
            tid="PRRT_1",
            comments=stuck_pair()
            + [
                comment(
                    "pat-user",
                    f"{mod.cite_marker('PRRT_1')}\nHEAD {STALE}",
                    hours_ago=0.05,
                )
            ],
        )
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(
            mod.classify_resolve(p, t, cfg(mediator_login="pat-user")),
            "resolve",
        )
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(gh.resolves, ["PRRT_1"])
        self.assertEqual(len(gh.replies), 1)
        self.assertIn("<!-- codex-thread-cited:PRRT_1 -->", gh.replies[0][1])
        self.assertIn(HEAD, gh.replies[0][1])
        self.assertNotIn(STALE, gh.replies[0][1])

    def test_issue_comment_cite_is_not_already_cited(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [
            comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4),
            comment("pat-user", f"{mod.cite_marker('PRRT_1')}\nHEAD {HEAD}", hours_ago=0.05),
        ]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(
            mod.classify_resolve(p, t, cfg(mediator_login="pat-user")),
            "resolve",
        )

    def test_cite_and_resolve_markers_count_as_one_cap_slot(self) -> None:
        comments = [
            comment("pat-user", f"{mod.cite_marker('PRRT_1')}\nHEAD {HEAD}", hours_ago=1),
            comment("pat-user", mod.resolve_marker("PRRT_1"), hours_ago=1),
            comment("pat-user", f"{mod.cite_marker('PRRT_2')}\nHEAD {HEAD}", hours_ago=1),
            comment("mallory", mod.resolve_marker("PRRT_3"), hours_ago=1),
            comment("pat-user", mod.resolve_marker("PRRT_2"), hours_ago=30),
        ]
        self.assertEqual(
            mod.resolves_today(comments, (), cfg(mediator_login="pat-user")),
            2,
        )

    def test_both_success_markers_fail_does_not_count_resolved(self) -> None:
        class BoomMarkers(FakeGitHub):
            def post_comment(self, number: int, body: str) -> None:
                if "codex-thread-resolved" in body:
                    raise RuntimeError("403 comment")
                super().post_comment(number, body)

            def reply_on_thread(self, thread_id: str, body: str) -> None:
                if "codex-thread-resolved" in body:
                    raise RuntimeError("403 reply")
                super().reply_on_thread(thread_id, body)

        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        gh = BoomMarkers([pull(threads=[t], issue_comments=asked, reviews=[ruling])])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.resolve_failed, 1)
        self.assertEqual(gh.resolves, ["PRRT_1"])
        self.assertEqual(len(gh.replies), 1)
        self.assertIn("<!-- codex-thread-cited:PRRT_1 -->", gh.replies[0][1])
        self.assertFalse(
            any("<!-- codex-thread-resolved:" in body for _, body in gh.comments)
        )
        self.assertEqual(mod.exit_code(counters), 1)

    def test_unmarked_resolve_still_consumes_daily_cap(self) -> None:
        class FirstUnmarked(FakeGitHub):
            def post_comment(self, number: int, body: str) -> None:
                if "codex-thread-resolved:PRRT_0" in body:
                    raise RuntimeError("403 comment")
                super().post_comment(number, body)

            def reply_on_thread(self, thread_id: str, body: str) -> None:
                if thread_id == "PRRT_0" and "codex-thread-resolved" in body:
                    raise RuntimeError("403 reply")
                super().reply_on_thread(thread_id, body)

        threads = []
        comments = []
        reviews = []
        for i in range(4):
            tid = f"PRRT_{i}"
            threads.append(thread(tid=tid, comments=stuck_pair()))
            comments.append(comment("pat-user", mod.mediation_marker(tid), hours_ago=0.4))
            reviews.append(comment(CODEX, verdict_block(tid), hours_ago=0.1))
        gh = FirstUnmarked(
            [pull(threads=threads, issue_comments=comments, reviews=reviews)]
        )
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(gh.resolves, ["PRRT_0", "PRRT_1", "PRRT_2"])
        self.assertEqual(counters.resolved, 2)
        self.assertEqual(counters.resolve_failed, 1)
        self.assertEqual(counters.skipped_resolve_cap, 1)
        self.assertEqual(mod.exit_code(counters), 1)

    def test_todays_cites_count_toward_resolve_cap(self) -> None:
        old = [
            thread(
                tid=f"PRRT_old{i}",
                comments=stuck_pair()
                + [
                    comment(
                        "pat-user",
                        f"{mod.cite_marker(f'PRRT_old{i}')}\nHEAD {HEAD}",
                        hours_ago=1,
                    )
                ],
                resolved=True,
            )
            for i in range(3)
        ]
        new = thread(tid="PRRT_new", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_new"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_new"), hours_ago=0.1)
        p = pull(threads=[*old, new], issue_comments=asked, reviews=[ruling])
        self.assertEqual(
            mod.resolves_today(p.issue_comments, p.threads, cfg(mediator_login="pat-user")),
            3,
        )
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(counters.skipped_resolve_cap, 1)
        self.assertEqual(gh.resolves, [])

    def test_already_resolved_thread_is_skipped(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair(), resolved=True)
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        p = pull(threads=[t], issue_comments=asked, reviews=[ruling])
        self.assertEqual(mod.classify_resolve(p, t, cfg(mediator_login="pat-user")), "resolved")
        gh = FakeGitHub([p])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(gh.resolves, [])

    def test_daily_resolve_cap_stops_at_three(self) -> None:
        threads = []
        comments = []
        reviews = []
        for i in range(5):
            tid = f"PRRT_{i}"
            threads.append(thread(tid=tid, comments=stuck_pair()))
            comments.append(comment("pat-user", mod.mediation_marker(tid), hours_ago=0.4))
            reviews.append(comment(CODEX, verdict_block(tid), hours_ago=0.1))
        gh = FakeGitHub([pull(threads=threads, issue_comments=comments, reviews=reviews)])
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 3)
        self.assertEqual(counters.skipped_resolve_cap, 2)
        self.assertEqual(len(gh.resolves), 3)

    def test_required_checks_required_contexts_win(self) -> None:
        self.assertTrue(
            mod.required_checks_are_green(
                {"state": "FAILURE"},
                contexts=[
                    {
                        "__typename": "CheckRun",
                        "name": "linux",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "isRequired": True,
                    },
                    {
                        "__typename": "CheckRun",
                        "name": "windows",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "isRequired": False,
                    },
                ],
            )
        )
        self.assertFalse(
            mod.required_checks_are_green(
                {"state": "SUCCESS"},
                contexts=[
                    {
                        "__typename": "CheckRun",
                        "name": "linux",
                        "status": "IN_PROGRESS",
                        "conclusion": "",
                        "isRequired": True,
                    }
                ],
            )
        )
        self.assertFalse(mod.required_checks_are_green(None))
        self.assertTrue(mod.required_checks_are_green({"state": "SUCCESS", "contexts": {"nodes": []}}))

    def test_missing_pat_does_not_resolve(self) -> None:
        t = thread(tid="PRRT_1", comments=stuck_pair())
        asked = [comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4)]
        ruling = comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1)
        gh = FakeGitHub([pull(threads=[t], issue_comments=asked, reviews=[ruling])])
        counters = mod.scan(
            cfg(codex_pat="", mediator_login="pat-user"),
            gh,
            FakeLinear(),
        )  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 0)
        self.assertEqual(gh.resolves, [])
        self.assertEqual(mod.exit_code(counters), 0)

    def test_resolve_error_fails_run_and_continues(self) -> None:
        class BoomResolve(FakeGitHub):
            def resolve_thread(self, thread_id: str) -> None:
                if thread_id == "PRRT_1":
                    raise RuntimeError("403")
                super().resolve_thread(thread_id)

        t1 = thread(tid="PRRT_1", comments=stuck_pair())
        t2 = thread(tid="PRRT_2", comments=stuck_pair())
        comments = [
            comment("pat-user", mod.mediation_marker("PRRT_1"), hours_ago=0.4),
            comment("pat-user", mod.mediation_marker("PRRT_2"), hours_ago=0.4),
        ]
        reviews = [
            comment(CODEX, verdict_block("PRRT_1"), hours_ago=0.1),
            comment(CODEX, verdict_block("PRRT_2"), hours_ago=0.1),
        ]
        gh = BoomResolve(
            [pull(number=1, threads=[t1, t2], issue_comments=comments, reviews=reviews)]
        )
        counters = mod.scan(cfg(mediator_login="pat-user"), gh, FakeLinear())  # type: ignore[arg-type]
        self.assertEqual(counters.resolved, 1)
        self.assertEqual(counters.resolve_failed, 1)
        self.assertEqual(mod.exit_code(counters), 1)
        self.assertEqual(gh.resolves, ["PRRT_2"])

class WorkflowAndDocsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        self.agents = AGENTS.read_text(encoding="utf-8")
        self.claude = CLAUDE.read_text(encoding="utf-8")
        self.contributing = CONTRIBUTING.read_text(encoding="utf-8")

    def test_agents_and_claude_are_identical(self) -> None:
        self.assertEqual(self.agents, self.claude)

    def test_docs_point_at_the_workflow(self) -> None:
        for text in (self.agents, self.claude):
            self.assertIn("stuck-thread-watch.yml", text)
            self.assertIn("Devin reviews", text)
            self.assertIn("@codex review", text)
            self.assertIn("needs-decision", text)
            self.assertIn("Stuck PR threads: recursion#", text)
            self.assertRegex(text, r"do not\s+file one issue per thread")
            self.assertIn("one watcher cycle", text)
            self.assertIn("the PR author files it", text)
            self.assertIn("<!-- stuck-pr-threads:recursion#<PR> -->", text)
            self.assertIn("csl-<N>-<slug>", text)
            self.assertIn("Fixes CSL-N", text)
            self.assertNotIn("Commit to `loop/work/<task-id>`", text)
            self.assertNotIn("Branch naming is deferred to", text)
            self.assertIn("CODEX-THREAD", text)
            self.assertIn("CODEX_TRIGGER_PAT", text)
        self.assertIn(".github/workflows/stuck-thread-watch.yml", self.agents)
        self.assertIn("resolveReviewThread", self.agents)
        self.assertIn("github-actions[bot]", self.agents)
        self.assertIn("never resolved unless Codex was explicitly asked", self.agents)
        self.assertIn(
            "AGENTS.md#when-claude-cant-fix-a-review-thread", self.contributing
        )
        self.assertIn("csl-<N>-<slug>", self.contributing)
        self.assertIn("Fixes CSL-N", self.contributing)
        self.assertNotIn("keep this convention", self.contributing)
        self.assertNotIn("Branch naming is deferred to", self.contributing)
        template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("Stuck PR threads: recursion#<PR>", template)
        self.assertIn("## Branch", template)
        self.assertIn("## Test plan", template)
        self.assertIn("Touches other repos?", template)

    def test_workflow_is_lightweight_and_configurable(self) -> None:
        text = self.workflow
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertNotIn("runs-on: blacksmith", text.lower())
        self.assertIn("cron: \"23 */2 * * *\"", text)
        self.assertIn("workflow_dispatch", text)
        self.assertIn("STUCK_AFTER_HOURS", text)
        self.assertIn("MAX_MEDIATIONS_PER_PR_PER_DAY", text)
        self.assertIn("MAX_RESOLVES_PER_PR_PER_DAY", text)
        self.assertIn("CODEX_TRIGGER_PAT", text)
        self.assertIn("LINEAR_API_KEY", text)
        self.assertIn("stuck_thread_watch.py", text)
        self.assertIn("github.event.repository.default_branch", text)
        self.assertIn("not on the default branch yet; skip", text)
        self.assertIn("pull-requests: read", text)
        self.assertIn("github-actions[bot]", text)
        self.assertIn("resolveReviewThread", text)
        self.assertNotIn("pytest", text)
        self.assertNotIn("ci_gate", text)
        self.assertNotRegex(text, r"git push|--force|mergePullRequest")

    def test_comment_triggers_include_claude_and_codex(self) -> None:
        self.assertIn("github.event.comment.user.login == 'claude[bot]'", self.workflow)
        self.assertIn("chatgpt-codex-connector[bot]", self.workflow)
        self.assertIn("github.event.issue.pull_request", self.workflow)
        self.assertIn("pull_request_review", self.workflow)

    def test_script_never_pushes_or_merges(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("git push", source)
        self.assertIn("refusing GitHub merge/resolve GraphQL", source)
        self.assertIn("CODEX_TRIGGER_PAT missing at resolve_thread", source)
        self.assertIn("Never pushes or merges", source)
        # Guard pattern may mention merge; the live client must not build merge URLs.
        self.assertNotIn("/pulls/", source)


if __name__ == "__main__":
    unittest.main()
