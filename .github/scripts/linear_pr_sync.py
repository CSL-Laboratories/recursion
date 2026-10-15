#!/usr/bin/env python3
"""Upsert one Linear comment per GitHub PR (marker-stable, edit in place).

Not a required check. Missing LINEAR_API_KEY skips with a notice (exit 0).
Never prints secrets. Never merges, pushes, or resolves review threads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"
CSL_KEY_RE = re.compile(r"CSL-(\d+)")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
MARKER_PREFIX = "pr-sync"
DEVIN_DEFAULT = "devin-ai-integration[bot]"
TOP_FILES_DEFAULT = 10
PURPOSE_LINES = 8
PURPOSE_CHARS = 800
BEHIND_STATES = frozenset({"dirty", "behind"})
FORBIDDEN_GITHUB_PATH = re.compile(
    r"/(merge|merge-upstream|resolve|dismissals)(/|$)",
    re.IGNORECASE,
)


def notice(message: str) -> None:
    print(f"::notice::{message}", flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def extract_csl_key(title: str) -> str:
    match = CSL_KEY_RE.search(title or "")
    return match.group(0) if match else ""


def csl_number(key: str) -> int:
    match = CSL_KEY_RE.search(key or "")
    if not match:
        raise ValueError(f"not a CSL key: {key!r}")
    return int(match.group(1))


def repo_short_name(repo: str) -> str:
    return (repo or "").split("/", 1)[-1]


def sync_marker(repo: str, number: int) -> str:
    return f"<!-- {MARKER_PREFIX}:{repo_short_name(repo)}#{number} -->"


def marker_in(body: str, marker: str) -> bool:
    return marker in (body or "")


def pr_status(*, state: str, draft: bool, merged: bool) -> str:
    if merged:
        return "merged"
    if (state or "").lower() == "closed":
        return "closed"
    if draft:
        return "draft"
    return "open"


def merge_flag(mergeable_state: str, default_branch: str) -> str:
    state = (mergeable_state or "").strip().lower()
    if state in BEHIND_STATES:
        branch = default_branch or "master"
        return f"out of date / conflicts with {branch}"
    return ""


def purpose_excerpt(body: str, *, max_lines: int = PURPOSE_LINES, max_chars: int = PURPOSE_CHARS) -> str:
    cleaned = HTML_COMMENT_RE.sub("", body or "")
    lines = [ln.rstrip() for ln in cleaned.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    chosen: list[str] = []
    nonempty = 0
    for line in lines:
        if line.strip():
            nonempty += 1
            chosen.append(line)
        elif chosen:
            chosen.append(line)
        if nonempty >= max_lines:
            break
    text = "\n".join(chosen).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
    return text or "_(no description)_"


def devin_on_head(reviews: list[dict[str, Any]], head_sha: str, devin_login: str) -> bool:
    want = (head_sha or "").strip().lower()
    login = (devin_login or "").strip().casefold()
    if not want or not login:
        return False
    for review in reviews:
        user = review.get("user") or {}
        if (user.get("login") or "").strip().casefold() != login:
            continue
        commit = (review.get("commit_id") or "").strip().lower()
        state = (review.get("state") or "").upper()
        if commit == want and state in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}:
            return True
    return False


@dataclass(frozen=True)
class PullSnapshot:
    number: int
    title: str
    body: str
    url: str
    author: str
    state: str
    draft: bool
    merged: bool
    mergeable_state: str
    head_sha: str
    files: tuple[str, ...]
    reviews: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class Config:
    repo: str
    github_token: str
    linear_api_key: str
    linear_team_key: str = "CSL"
    default_branch: str = "master"
    pr_number: int | None = None
    devin_login: str = DEVIN_DEFAULT
    top_files: int = TOP_FILES_DEFAULT
    dry_run: bool = False

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[-1]

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> "Config":
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        if not repo or "/" not in repo:
            raise SystemExit("GITHUB_REPOSITORY is required (owner/name)")
        raw_pr = os.environ.get("PR_NUMBER", "").strip()
        return cls(
            repo=repo,
            github_token=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "",
            linear_api_key=os.environ.get("LINEAR_API_KEY", "").strip(),
            linear_team_key=os.environ.get("LINEAR_TEAM_KEY", "CSL").strip() or "CSL",
            default_branch=os.environ.get("DEFAULT_BRANCH", "master").strip() or "master",
            pr_number=int(raw_pr) if raw_pr else None,
            devin_login=os.environ.get("DEVIN_LOGIN", DEVIN_DEFAULT).strip() or DEVIN_DEFAULT,
            top_files=env_int("TOP_FILES", TOP_FILES_DEFAULT),
            dry_run=dry_run,
        )


def comment_body(pull: PullSnapshot, cfg: Config) -> str:
    marker = sync_marker(cfg.repo, pull.number)
    status = pr_status(state=pull.state, draft=pull.draft, merged=pull.merged)
    on_head = "yes" if devin_on_head(list(pull.reviews), pull.head_sha, cfg.devin_login) else "no"
    flag = merge_flag(pull.mergeable_state, cfg.default_branch)
    files = pull.files[: cfg.top_files]
    file_lines = "\n".join(f"- `{path}`" for path in files) if files else "- _(none)_"
    flag_line = f"- **Flag:** {flag}\n" if flag else ""
    purpose = purpose_excerpt(pull.body)
    mergeable = pull.mergeable_state or "unknown"
    return (
        f"{marker}\n"
        f"## GitHub PR {repo_short_name(cfg.repo)}#{pull.number}\n\n"
        f"- **PR:** {pull.url}\n"
        f"- **Author:** {pull.author or '(unknown)'}\n"
        f"- **Status:** {status}\n"
        f"- **Mergeable:** `{mergeable}`\n"
        f"{flag_line}"
        f"- **Head SHA:** `{pull.head_sha}`\n"
        f"- **Devin review on head:** {on_head}\n\n"
        f"### Purpose\n\n{purpose}\n\n"
        f"### Top changed files\n\n{file_lines}\n"
    )


class GitHubClient:
    def __init__(self, cfg: Config, opener: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg
        self._opener = opener or urllib.request.urlopen

    def _request(self, url: str, *, method: str = "GET") -> Any:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise RuntimeError(f"refusing non-GitHub URL: {parsed.netloc or url}")
        if FORBIDDEN_GITHUB_PATH.search(parsed.path):
            raise RuntimeError("refusing GitHub merge/resolve endpoint")
        if method != "GET":
            raise RuntimeError(f"refusing GitHub method {method}")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.cfg.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "linear-pr-sync",
        }
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self._opener(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"GitHub HTTP {exc.code} on {parsed.path}: {detail}") from exc
        return json.loads(body) if body else {}

    def list_open_pr_numbers(self) -> list[int]:
        if self.cfg.pr_number:
            return [self.cfg.pr_number]
        numbers: list[int] = []
        page = 1
        while True:
            url = (
                f"{GITHUB_API}/repos/{self.cfg.owner}/{self.cfg.name}/pulls"
                f"?state=open&per_page=100&page={page}"
            )
            batch = self._request(url)
            if not isinstance(batch, list) or not batch:
                break
            numbers.extend(int(item["number"]) for item in batch if item.get("number"))
            if len(batch) < 100:
                break
            page += 1
        return numbers

    def load_pull(self, number: int) -> PullSnapshot | None:
        url = f"{GITHUB_API}/repos/{self.cfg.owner}/{self.cfg.name}/pulls/{number}"
        try:
            data = self._request(url)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        if not data or not data.get("number"):
            return None
        files_url = (
            f"{GITHUB_API}/repos/{self.cfg.owner}/{self.cfg.name}/pulls/{number}/files"
            f"?per_page={self.cfg.top_files}"
        )
        files_raw = self._request(files_url)
        files = tuple(
            str(item.get("filename") or "")
            for item in (files_raw or [])
            if item.get("filename")
        )
        reviews_url = (
            f"{GITHUB_API}/repos/{self.cfg.owner}/{self.cfg.name}/pulls/{number}/reviews"
            f"?per_page=100"
        )
        reviews_raw = self._request(reviews_url)
        reviews = tuple(item for item in (reviews_raw or []) if isinstance(item, dict))
        head = data.get("head") or {}
        user = data.get("user") or {}
        return PullSnapshot(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            url=str(data.get("html_url") or ""),
            author=str(user.get("login") or ""),
            state=str(data.get("state") or ""),
            draft=bool(data.get("draft")),
            merged=bool(data.get("merged")),
            mergeable_state=str(data.get("mergeable_state") or ""),
            head_sha=str(head.get("sha") or ""),
            files=files,
            reviews=reviews,
        )


class LinearClient:
    def __init__(self, cfg: Config, opener: Callable[..., Any] | None = None) -> None:
        self.cfg = cfg
        self._opener = opener or urllib.request.urlopen

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.cfg.linear_api_key:
            raise RuntimeError("LINEAR_API_KEY missing")
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(
            LINEAR_API,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": self.cfg.linear_api_key,
                "User-Agent": "linear-pr-sync",
            },
        )
        try:
            with self._opener(req, timeout=60) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Linear HTTP {exc.code}: {detail}") from exc
        data = json.loads(body) if body else {}
        if data.get("errors"):
            raise RuntimeError(f"Linear GraphQL errors: {data['errors']!r}")
        return data.get("data") or {}

    def resolve_issue(self, key: str) -> str:
        number = csl_number(key)
        data = self.graphql(
            """
            query($team: String!, $number: Float!) {
              issues(filter: { team: { key: { eq: $team } }, number: { eq: $number } }, first: 1) {
                nodes { id identifier }
              }
            }
            """,
            {"team": self.cfg.linear_team_key, "number": number},
        )
        nodes = ((data.get("issues") or {}).get("nodes")) or []
        if not nodes:
            raise RuntimeError(f"Linear issue {key} not found")
        return str(nodes[0]["id"])

    def list_comments(self, issue_id: str) -> list[dict[str, str]]:
        comments: list[dict[str, str]] = []
        cursor = None
        while True:
            data = self.graphql(
                """
                query($id: String!, $cursor: String) {
                  issue(id: $id) {
                    comments(first: 50, after: $cursor) {
                      pageInfo { hasNextPage endCursor }
                      nodes { id body }
                    }
                  }
                }
                """,
                {"id": issue_id, "cursor": cursor},
            )
            conn = ((data.get("issue") or {}).get("comments")) or {}
            for node in conn.get("nodes") or []:
                comments.append({"id": str(node.get("id") or ""), "body": str(node.get("body") or "")})
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
        return comments

    def find_comment_id(self, issue_id: str, marker: str) -> str:
        for comment in self.list_comments(issue_id):
            if comment["id"] and marker_in(comment["body"], marker):
                return comment["id"]
        return ""

    def create_comment(self, issue_id: str, body: str) -> None:
        if self.cfg.dry_run:
            log("dry-run: would create Linear comment")
            return
        data = self.graphql(
            """
            mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) {
                success
                comment { id }
              }
            }
            """,
            {"input": {"issueId": issue_id, "body": body}},
        )
        if not ((data.get("commentCreate") or {}).get("success")):
            raise RuntimeError("Linear commentCreate failed")

    def update_comment(self, comment_id: str, body: str) -> None:
        if self.cfg.dry_run:
            log("dry-run: would update Linear comment")
            return
        data = self.graphql(
            """
            mutation($id: String!, $input: CommentUpdateInput!) {
              commentUpdate(id: $id, input: $input) {
                success
                comment { id }
              }
            }
            """,
            {"id": comment_id, "input": {"body": body}},
        )
        if not ((data.get("commentUpdate") or {}).get("success")):
            raise RuntimeError("Linear commentUpdate failed")


def upsert_comment(linear: LinearClient, issue_id: str, marker: str, body: str) -> str:
    existing = linear.find_comment_id(issue_id, marker)
    if existing:
        linear.update_comment(existing, body)
        return "updated"
    linear.create_comment(issue_id, body)
    return "created"


def sync_pull(pull: PullSnapshot, cfg: Config, linear: LinearClient) -> str:
    key = extract_csl_key(pull.title)
    if not key:
        log(f"skip PR #{pull.number}: no CSL key in title")
        return "skipped-no-key"
    marker = sync_marker(cfg.repo, pull.number)
    issue_id = linear.resolve_issue(key)
    action = upsert_comment(linear, issue_id, marker, comment_body(pull, cfg))
    log(f"{action} Linear comment on {key} for PR #{pull.number}")
    return action


def scan(cfg: Config, github: GitHubClient, linear: LinearClient | None) -> int:
    if not cfg.linear_api_key or linear is None:
        notice("LINEAR_API_KEY is missing; skipping Linear PR sync (no failure)")
        return 0
    if not cfg.github_token:
        raise SystemExit("GH_TOKEN / GITHUB_TOKEN is required to read pull requests")
    synced = 0
    for number in github.list_open_pr_numbers() if not cfg.pr_number else [cfg.pr_number]:
        pull = github.load_pull(number)
        if pull is None:
            notice(f"PR #{number} not found")
            continue
        result = sync_pull(pull, cfg, linear)
        if result in {"created", "updated"}:
            synced += 1
    log(f"linear-pr-sync: synced={synced}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linear PR sync")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = Config.from_env(dry_run=args.dry_run)
    github = GitHubClient(cfg)
    linear = LinearClient(cfg) if cfg.linear_api_key else None
    return scan(cfg, github, linear)


if __name__ == "__main__":
    raise SystemExit(main())
