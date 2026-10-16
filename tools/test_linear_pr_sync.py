#!/usr/bin/env python3
"""Provider-free tests for Linear PR sync.

Acceptance:

    python -X utf8 -B tools/test_linear_pr_sync.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "linear_pr_sync.py"
WORKFLOW = ROOT / ".github" / "workflows" / "linear-pr-sync.yml"
AGENTS = ROOT / "AGENTS.md"
CLAUDE = ROOT / "CLAUDE.md"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def load_mod():
    spec = importlib.util.spec_from_file_location("linear_pr_sync", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_mod()
DEVIN = "devin-ai-integration[bot]"


def cfg(**kwargs: Any) -> mod.Config:
    values = dict(
        repo="CSL-Laboratories/recursion",
        github_token="gh-read",
        linear_api_key="lin_api_test",
        default_branch="master",
    )
    values.update(kwargs)
    return mod.Config(**values)


def pull(**kwargs: Any) -> mod.PullSnapshot:
    values = dict(
        number=191,
        title="CSL-87: Codex mediator stuck-thread workflow",
        body="Fixes CSL-87\n\n## Summary\n\nReplace the out-of-repo watch.\n",
        url="https://github.com/CSL-Laboratories/recursion/pull/191",
        author="NotSkittlz",
        state="open",
        draft=False,
        merged=False,
        mergeable_state="clean",
        head_sha="abc123def456",
        files=(".github/workflows/linear-pr-sync.yml", "AGENTS.md"),
        reviews=(),
        head_repo="CSL-Laboratories/recursion",
    )
    values.update(kwargs)
    return mod.PullSnapshot(**values)


class ParseTests(unittest.TestCase):
    def test_extracts_csl_key_from_title(self) -> None:
        self.assertEqual(mod.extract_csl_key("CSL-87: hello"), "CSL-87")
        self.assertEqual(mod.extract_csl_key("feat: CSL-12 and more"), "CSL-12")
        self.assertEqual(mod.extract_csl_key("no key here"), "")

    def test_marker_uses_short_repo_and_number(self) -> None:
        self.assertEqual(
            mod.sync_marker("CSL-Laboratories/recursion", 191),
            "<!-- pr-sync:recursion#191 -->",
        )

    def test_status_mapping(self) -> None:
        self.assertEqual(mod.pr_status(state="open", draft=True, merged=False), "draft")
        self.assertEqual(mod.pr_status(state="open", draft=False, merged=False), "open")
        self.assertEqual(mod.pr_status(state="closed", draft=False, merged=True), "merged")
        self.assertEqual(mod.pr_status(state="closed", draft=False, merged=False), "closed")

    def test_merge_flag_on_behind_and_dirty(self) -> None:
        self.assertEqual(
            mod.merge_flag("behind", "master"),
            "out of date / conflicts with master",
        )
        self.assertEqual(
            mod.merge_flag("dirty", "master"),
            "out of date / conflicts with master",
        )
        self.assertEqual(mod.merge_flag("clean", "master"), "")
        self.assertEqual(mod.merge_flag("unknown", "master"), "")

    def test_purpose_strips_html_comments_and_takes_first_lines(self) -> None:
        body = (
            "<!-- CURSOR_AGENT_PR_BODY_BEGIN -->\n"
            "Fixes CSL-87\n\n"
            "## Summary\n\n"
            "Do the thing.\n"
            "<!-- CURSOR_AGENT_PR_BODY_END -->\n"
            "more after the fold that should be ignored if we already have lines\n"
        )
        excerpt = mod.purpose_excerpt(body, max_lines=4)
        self.assertIn("Fixes CSL-87", excerpt)
        self.assertIn("Do the thing.", excerpt)
        self.assertNotIn("CURSOR_AGENT", excerpt)

    def test_devin_on_head_requires_matching_sha(self) -> None:
        reviews = [
            {"user": {"login": DEVIN}, "commit_id": "old", "state": "COMMENTED"},
            {"user": {"login": DEVIN}, "commit_id": "abc123def456", "state": "COMMENTED"},
        ]
        self.assertTrue(mod.devin_on_head(reviews, "abc123def456", DEVIN))
        self.assertTrue(
            mod.devin_on_head(
                [{"user": {"login": "devin-ai-integration"}, "commit_id": "abc123def456", "state": "COMMENTED"}],
                "abc123def456",
                DEVIN,
            )
        )
        self.assertFalse(mod.devin_on_head(reviews, "other", DEVIN))
        self.assertFalse(mod.devin_on_head([], "abc123def456", DEVIN))


class BodyTests(unittest.TestCase):
    def test_comment_includes_required_fields_and_marker(self) -> None:
        body = mod.comment_body(
            pull(
                reviews=(
                    {"user": {"login": DEVIN}, "commit_id": "abc123def456", "state": "COMMENTED"},
                )
            ),
            cfg(),
        )
        self.assertIn("<!-- pr-sync:recursion#191 -->", body)
        self.assertIn("https://github.com/CSL-Laboratories/recursion/pull/191", body)
        self.assertIn("NotSkittlz", body)
        self.assertIn("**Status:** open", body)
        self.assertIn("`clean`", body)
        self.assertIn("abc123def456", body)
        self.assertIn("**Devin review on head:** yes", body)
        self.assertIn("Replace the out-of-repo watch.", body)
        self.assertIn("`.github/workflows/linear-pr-sync.yml`", body)
        self.assertIn("Linear-first (CSL-88)", body)
        self.assertIn("PR-sync comment", body)
        self.assertIn("Context7", body)
        self.assertIn("Parallel", body)
        self.assertLess(body.index("<!-- pr-sync:recursion#191 -->"), body.index("Linear-first (CSL-88)"))
        self.assertNotIn("**Flag:**", body)

    def test_comment_flags_conflicts_with_default_branch(self) -> None:
        body = mod.comment_body(pull(mergeable_state="dirty"), cfg())
        self.assertIn("**Flag:** out of date / conflicts with master", body)


class FakeGitHub:
    def __init__(self, pulls: list[mod.PullSnapshot]) -> None:
        self.pulls = {p.number: p for p in pulls}

    def list_open_pr_numbers(self) -> list[int]:
        return [n for n, p in sorted(self.pulls.items()) if p.state == "open"]

    def load_pull(self, number: int, wait: bool = True) -> mod.PullSnapshot | None:
        return self.pulls.get(number)


class FakeLinear:
    def __init__(self) -> None:
        self.issues = {"CSL-87": "issue-uuid-87"}
        self.comments: dict[str, list[dict[str, str]]] = {"issue-uuid-87": []}
        self.created = 0
        self.updated = 0

    def resolve_issue(self, key: str) -> str:
        if key not in self.issues:
            raise RuntimeError(f"missing {key}")
        return self.issues[key]

    def find_comment_id(self, issue_id: str, marker: str) -> str:
        for comment in self.comments.get(issue_id, []):
            if marker in comment["body"]:
                return comment["id"]
        return ""

    def create_comment(self, issue_id: str, body: str) -> None:
        self.created += 1
        self.comments.setdefault(issue_id, []).append({"id": f"cmt-{self.created}", "body": body})

    def update_comment(self, comment_id: str, body: str) -> None:
        self.updated += 1
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return
        raise RuntimeError("missing comment")


class UpsertTests(unittest.TestCase):
    def test_creates_then_edits_in_place(self) -> None:
        linear = FakeLinear()
        first = mod.sync_pull(pull(), cfg(), linear)
        second = mod.sync_pull(pull(head_sha="newsha"), cfg(), linear)
        self.assertEqual(first, "created")
        self.assertEqual(second, "updated")
        self.assertEqual(linear.created, 1)
        self.assertEqual(linear.updated, 1)
        bodies = [c["body"] for c in linear.comments["issue-uuid-87"]]
        self.assertEqual(len(bodies), 1)
        self.assertIn("newsha", bodies[0])
        self.assertIn("<!-- pr-sync:recursion#191 -->", bodies[0])

    def test_skips_title_without_csl_key(self) -> None:
        linear = FakeLinear()
        result = mod.sync_pull(pull(title="no key"), cfg(), linear)
        self.assertEqual(result, "skipped-no-key")
        self.assertEqual(linear.created, 0)

    def test_missing_linear_key_skips_without_writes(self) -> None:
        gh = FakeGitHub([pull()])
        linear = FakeLinear()
        code = mod.scan(cfg(linear_api_key=""), gh, None)  # type: ignore[arg-type]
        self.assertEqual(code, 0)
        self.assertEqual(linear.created, 0)

    def test_skips_fork_heads(self) -> None:
        linear = FakeLinear()
        result = mod.sync_pull(pull(head_repo="evil/fork"), cfg(), linear)
        self.assertEqual(result, "skipped-fork")
        self.assertEqual(linear.created, 0)

    def test_skips_empty_head_repo(self) -> None:
        linear = FakeLinear()
        result = mod.sync_pull(pull(head_repo=""), cfg(), linear)
        self.assertEqual(result, "skipped-fork")
        self.assertEqual(linear.created, 0)

    def test_missing_issue_does_not_abort_later_prs(self) -> None:
        class BoomThenOk(FakeLinear):
            def resolve_issue(self, key: str) -> str:
                if key == "CSL-9999":
                    raise RuntimeError("Linear issue CSL-9999 not found")
                return super().resolve_issue(key)

        gh = FakeGitHub(
            [
                pull(number=12, title="CSL-9999: missing"),
                pull(number=13, title="CSL-87: ok"),
            ]
        )
        linear = BoomThenOk()
        code = mod.scan(cfg(), gh, linear)  # type: ignore[arg-type]
        self.assertEqual(code, 0)
        self.assertEqual(linear.created, 1)
        self.assertIn("recursion#13", linear.comments["issue-uuid-87"][0]["body"])

    def test_push_path_syncs_every_open_pr(self) -> None:
        gh = FakeGitHub(
            [
                pull(number=191),
                pull(number=192, title="CSL-87: another", mergeable_state="behind"),
            ]
        )
        linear = FakeLinear()
        code = mod.scan(cfg(), gh, linear)  # type: ignore[arg-type]
        self.assertEqual(code, 0)
        self.assertEqual(linear.created, 2)
        bodies = [c["body"] for c in linear.comments["issue-uuid-87"]]
        self.assertTrue(any("recursion#192" in b for b in bodies))
        self.assertTrue(any("out of date / conflicts with master" in b for b in bodies))


class HttpGuardTests(unittest.TestCase):
    def test_refuses_merge_url(self) -> None:
        client = mod.GitHubClient(cfg())
        with self.assertRaises(RuntimeError):
            client._request("https://api.github.com/repos/a/b/pulls/1/merge")

    def test_http_error_does_not_include_token(self) -> None:
        def opener(req: Request, timeout: int = 0):
            raise HTTPError(
                req.full_url, 401, "nope", hdrs=None, fp=BytesIO(b'{"message":"bad"}')  # type: ignore[arg-type]
            )

        client = mod.GitHubClient(cfg(), opener=opener)
        with self.assertRaises(RuntimeError) as ctx:
            client._request("https://api.github.com/repos/a/b/pulls/1")
        self.assertNotIn("gh-read", str(ctx.exception))

    def test_retries_unknown_mergeable_state(self) -> None:
        payloads = [
            {
                "number": 191,
                "title": "CSL-87: x",
                "html_url": "https://github.com/CSL-Laboratories/recursion/pull/191",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": None,
                "mergeable_state": "unknown",
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            },
            {
                "number": 191,
                "title": "CSL-87: x",
                "html_url": "https://github.com/CSL-Laboratories/recursion/pull/191",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": False,
                "mergeable_state": "dirty",
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            },
        ]

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url:
                return []
            if "/files" in url:
                return [{"filename": "AGENTS.md"}]
            return payloads.pop(0)

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep"
        ) as slept:
            snap = client.load_pull(191)
        assert snap is not None
        self.assertEqual(snap.mergeable_state, "dirty")
        self.assertEqual(slept.call_count, 1)

    def test_load_pull_wait_false_does_not_sleep(self) -> None:
        pending = {
            "number": 191,
            "title": "CSL-87: x",
            "html_url": "https://github.com/CSL-Laboratories/recursion/pull/191",
            "user": {"login": "a"},
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": None,
            "mergeable_state": "unknown",
            "head": {
                "sha": "abc",
                "repo": {"full_name": "CSL-Laboratories/recursion"},
            },
        }

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            return pending

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep"
        ) as slept:
            snap = client.load_pull(191, wait=False)
        assert snap is not None
        self.assertEqual(snap.mergeable_state, "unknown")
        slept.assert_not_called()

    def test_later_prs_get_own_mergeable_retries(self) -> None:
        fetches: dict[int, int] = {n: 0 for n in range(1, 6)}

        def payload(number: int, state: str, mergeable: bool | None) -> dict[str, Any]:
            return {
                "number": number,
                "title": f"CSL-87: pr {number}",
                "html_url": f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": mergeable,
                "mergeable_state": state,
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            }

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            fetches[number] = fetches.get(number, 0) + 1
            if fetches[number] == 1:
                return payload(number, "unknown", None)
            return payload(number, "dirty", False)

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep"
        ) as slept:
            snaps = [client.load_pull(n, wait=False) for n in range(1, 6)]
            snaps = client.settle_mergeable(snaps)
        self.assertTrue(all(snap is not None and snap.mergeable_state == "dirty" for snap in snaps))
        self.assertTrue(all(count >= 2 for count in fetches.values()))
        self.assertEqual(slept.call_count, 5)
        self.assertLessEqual(slept.call_count * mod.MERGEABLE_WAIT_S, mod.MERGEABLE_DEADLINE_S)

    def test_settle_mergeable_deadline_caps_total_sleep(self) -> None:
        fetches: dict[int, int] = {}

        def payload(number: int) -> dict[str, Any]:
            return {
                "number": number,
                "title": f"CSL-87: pr {number}",
                "html_url": f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": None,
                "mergeable_state": "unknown",
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            }

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            fetches[number] = fetches.get(number, 0) + 1
            return payload(number)

        now = [0.0]

        def sleep(sec: float) -> None:
            now[0] += sec

        client = mod.GitHubClient(cfg(), clock=lambda: now[0])
        client._mergeable_deadline_s = mod.MERGEABLE_WAIT_S * 2
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep", side_effect=sleep
        ) as slept:
            snaps = [client.load_pull(n, wait=False) for n in (11, 12, 13)]
            snaps = client.settle_mergeable(snaps)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(fetches[11], 2)
        self.assertEqual(fetches[12], 1)
        self.assertEqual(fetches[13], 1)
        self.assertEqual([s.mergeable_state for s in snaps], ["unknown", "unknown", "unknown"])

    def test_load_pull_retries_are_per_pr(self) -> None:
        pending = {
            "number": 191,
            "title": "CSL-87: x",
            "html_url": "https://github.com/CSL-Laboratories/recursion/pull/191",
            "user": {"login": "a"},
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": None,
            "mergeable_state": "unknown",
            "head": {
                "sha": "abc",
                "repo": {"full_name": "CSL-Laboratories/recursion"},
            },
        }

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            payload = dict(pending)
            payload["number"] = int(url.rstrip("/").rsplit("/", 1)[-1])
            return payload

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep"
        ) as slept:
            first = client.load_pull(191)
            second = client.load_pull(192)
        assert first is not None and second is not None
        self.assertEqual(slept.call_count, 2 * mod.MERGEABLE_RETRIES)
        self.assertEqual(first.mergeable_state, "unknown")
        self.assertEqual(second.mergeable_state, "unknown")

    def test_settle_refresh_error_does_not_abort_later_prs(self) -> None:
        fetches: dict[int, int] = {}

        def payload(number: int, state: str, mergeable: bool | None) -> dict[str, Any]:
            return {
                "number": number,
                "title": f"CSL-87: pr {number}",
                "html_url": f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": mergeable,
                "mergeable_state": state,
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            }

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            fetches[number] = fetches.get(number, 0) + 1
            if number == 11 and fetches[number] > 1:
                raise TimeoutError("timed out")
            if fetches[number] == 1:
                return payload(number, "unknown", None)
            return payload(number, "dirty", False)

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep"
        ):
            snaps = [client.load_pull(n, wait=False) for n in (11, 12)]
            snaps = client.settle_mergeable(snaps)
        self.assertEqual(snaps[0].mergeable_state, "unknown")
        self.assertEqual(snaps[1].mergeable_state, "dirty")

    def test_settle_wall_clock_includes_request_time(self) -> None:
        now = [0.0]
        fetches: dict[int, int] = {}

        def payload(number: int) -> dict[str, Any]:
            return {
                "number": number,
                "title": f"CSL-87: pr {number}",
                "html_url": f"https://github.com/CSL-Laboratories/recursion/pull/{number}",
                "user": {"login": "a"},
                "state": "open",
                "draft": False,
                "merged": False,
                "mergeable": None,
                "mergeable_state": "unknown",
                "head": {
                    "sha": "abc",
                    "repo": {"full_name": "CSL-Laboratories/recursion"},
                },
            }

        def fake_request(url: str, *, method: str = "GET", timeout: float = 60, **_kwargs: Any) -> Any:
            if "/reviews" in url or "/files" in url:
                return []
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            fetches[number] = fetches.get(number, 0) + 1
            if fetches[number] > 1:
                now[0] += min(5.0, timeout)
            return payload(number)

        def sleep(sec: float) -> None:
            now[0] += sec

        client = mod.GitHubClient(cfg(), clock=lambda: now[0])
        client._mergeable_deadline_s = 12.0
        with mock.patch.object(client, "_request", side_effect=fake_request), mock.patch.object(
            mod.time, "sleep", side_effect=sleep
        ):
            snaps = [client.load_pull(n, wait=False) for n in range(1, 16)]
            snaps = client.settle_mergeable(snaps)
        self.assertEqual(len(snaps), 15)
        self.assertLess(sum(1 for count in fetches.values() if count > 1), 15)
        self.assertLessEqual(now[0], 12.0)

    def test_reviews_paginate_until_devin_on_head(self) -> None:
        pr = {
            "number": 191,
            "title": "CSL-87: x",
            "html_url": "https://github.com/CSL-Laboratories/recursion/pull/191",
            "user": {"login": "a"},
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head": {
                "sha": "deadbeef",
                "repo": {"full_name": "CSL-Laboratories/recursion"},
            },
        }
        page1 = [
            {"user": {"login": "human"}, "commit_id": "old", "state": "COMMENTED"}
        ] * 100
        page2 = [
            {
                "user": {"login": "devin-ai-integration"},
                "commit_id": "deadbeef",
                "state": "COMMENTED",
            }
        ]

        def fake_request(url: str, *, method: str = "GET", **_kwargs: Any) -> Any:
            if url.endswith("/pulls/191"):
                return pr
            if "/files" in url:
                return []
            if "/reviews" in url and "page=2" in url:
                return page2
            if "/reviews" in url:
                return page1
            raise AssertionError(url)

        client = mod.GitHubClient(cfg())
        with mock.patch.object(client, "_request", side_effect=fake_request):
            snap = client.load_pull(191)
        assert snap is not None
        self.assertTrue(mod.devin_on_head(list(snap.reviews), "deadbeef", DEVIN))

    def test_from_env(self) -> None:
        env = {
            "GITHUB_REPOSITORY": "CSL-Laboratories/recursion",
            "GH_TOKEN": "t",
            "LINEAR_API_KEY": "k",
            "PR_NUMBER": "191",
            "DEFAULT_BRANCH": "master",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            got = mod.Config.from_env()
        self.assertEqual(got.pr_number, 191)
        self.assertEqual(got.default_branch, "master")


class WorkflowAndDocsTests(unittest.TestCase):
    def test_agents_and_claude_are_identical(self) -> None:
        self.assertEqual(AGENTS.read_text(encoding="utf-8"), CLAUDE.read_text(encoding="utf-8"))

    def test_docs_require_grok_bots_to_read_pr_sync_comments(self) -> None:
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("linear-pr-sync.yml", text)
        self.assertIn("Grok Bots", text)
        self.assertIn("pr-sync", text)
        self.assertIn("shared files", text)

    def test_docs_require_agent_ci_etiquette(self) -> None:
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn("## Agent CI etiquette", text)
        self.assertIn("Mark the PR ready **once**", text)
        self.assertIn("GITHUB_TOKEN", text)
        self.assertIn("real conflict", text)

    def test_workflow_is_light_and_not_a_required_check(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("timeout-minutes: 5", text)
        self.assertIn("cancel-in-progress: true", text)
        self.assertIn("LINEAR_API_KEY", text)
        self.assertIn("pull_request:", text)
        self.assertIn("opened", text)
        self.assertIn("synchronize", text)
        self.assertIn("reopened", text)
        self.assertIn("ready_for_review", text)
        self.assertIn("closed", text)
        self.assertIn("linear_pr_sync.py", text)
        self.assertIn("github.event.repository.default_branch", text)
        self.assertIn("not on the default branch yet; skip", text)
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            text,
        )
        self.assertNotIn("blacksmith", text.lower())
        self.assertNotRegex(text, r"git push|--force|mergePullRequest")
        ci = CI.read_text(encoding="utf-8")
        self.assertNotIn("linear-pr-sync", ci)
        self.assertIn("name: linux", ci)


if __name__ == "__main__":
    unittest.main()
