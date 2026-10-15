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
    head_oid: str = "",
    review_decision: str = "",
    checks_state: str = "",
    approved_head_oids: tuple[str, ...] = (),
) -> mod.Pull:
    return mod.Pull(
        number=number,
        url=f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
        is_draft=draft,
        state=state,
        threads=tuple(threads or ()),
        issue_comments=tuple(issue_comments or ()),
        head_oid=head_oid,
        review_decision=review_decision,
        checks_state=checks_state,
        approved_head_oids=approved_head_oids,
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


class FakeGitHub:
    def __init__(self, pulls: list[mod.Pull]) -> None:
        self.pulls = {p.number: p for p in pulls}
        self.comments: list[tuple[int, str]] = []
        self.labels: list[tuple[int, list[str]]] = []

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
            approved_head_oids=("abc",),
        )
        gh = FakeGitHub([p])
        lin = FakeLinear()
        counters = mod.scan(cfg(), gh, lin)  # type: ignore[arg-type]
        self.assertEqual(counters.mediated, 0)
        self.assertEqual(lin.created, [])
        self.assertEqual(gh.comments, [])

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
        self.assertEqual(counters.linear, 0)


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
            self.assertIn("Branch naming is deferred to", text)
            self.assertIn("pull/189", text)
            self.assertIn("csl-<N>-<slug>", text)
            self.assertNotIn("Commit to `loop/work/<task-id>`", text)
        self.assertIn(".github/workflows/stuck-thread-watch.yml", self.agents)
        self.assertIn(
            "AGENTS.md#when-claude-cant-fix-a-review-thread", self.contributing
        )
        self.assertIn("Branch naming is deferred to", self.contributing)
        self.assertIn("pull/189", self.contributing)
        self.assertNotIn("keep this convention", self.contributing)
        template = (ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
        self.assertIn("Stuck PR threads: recursion#<PR>", template)

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


if __name__ == "__main__":
    unittest.main()
