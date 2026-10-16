#!/usr/bin/env python3
"""Required review-gate: pass iff Devin Review succeeded on HEAD_SHA.

Devin Review is a commit status (context "Devin Review"), observed on this
repo. Check runs of the same name are accepted as a fallback. On pull_request
the script polls until that status lands so the required check is reported by
an eligible `pull_request` workflow run. On merge_group the workflow passes
without calling this script. The `status` event re-evaluates once when Devin
(or any other context) posts, without cancelling the pull_request poll.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEVIN_CONTEXT = "Devin Review"
DEFAULT_POLL_SECONDS = 1200
DEFAULT_POLL_INTERVAL = 15
API_VERSION = "2022-11-28"


def env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def gh_api(path: str) -> object:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    url = f"https://api.github.com/repos/{repo}/{path.lstrip('/')}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "recursion-review-gate",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"GitHub API {path} failed: HTTP {exc.code} {body}") from exc


def latest_devin_status(sha: str) -> str | None:
    combined = gh_api(f"commits/{sha}/status")
    if not isinstance(combined, dict):
        return None
    for status in combined.get("statuses") or []:
        if isinstance(status, dict) and status.get("context") == DEVIN_CONTEXT:
            state = status.get("state")
            return str(state) if state else None
    return None


def latest_devin_check(sha: str) -> str | None:
    payload = gh_api(f"commits/{sha}/check-runs?per_page=100")
    if not isinstance(payload, dict):
        return None
    matches: list[tuple[str, str]] = []
    for run in payload.get("check_runs") or []:
        if not isinstance(run, dict) or run.get("name") != DEVIN_CONTEXT:
            continue
        started = str(run.get("started_at") or "")
        conclusion = str(run.get("conclusion") or "")
        matches.append((started, conclusion))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1] or None


def devin_review_succeeded(sha: str) -> bool:
    if latest_devin_status(sha) == "success":
        return True
    return latest_devin_check(sha) == "success"


def associated_pulls(sha: str) -> list[dict]:
    payload = gh_api(f"commits/{sha}/pulls")
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def is_open_pr_head(sha: str, pulls: list[dict]) -> bool:
    needle = sha.lower()
    for pull in pulls:
        if pull.get("state") != "open":
            continue
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        head_sha = str(head.get("sha") or "").lower()
        if head_sha == needle:
            return True
    return False


def poll_until_success(sha: str, *, seconds: int, interval: int) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        if devin_review_succeeded(sha):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        sleep_for = interval if remaining > interval else remaining
        print(
            f"Devin Review not successful on {sha} yet; waiting {sleep_for:.0f}s",
            flush=True,
        )
        time.sleep(sleep_for)


def main() -> int:
    event = os.environ.get("EVENT_NAME", "").strip()
    sha = os.environ.get("HEAD_SHA", "").strip()
    if event == "merge_group":
        print("merge_group: review-gate passes", flush=True)
        return 0
    if not sha:
        print("::error::HEAD_SHA is empty; cannot evaluate Devin Review", flush=True)
        return 1
    if env_bool("IS_DRAFT"):
        print(
            "::error::review-gate fails on draft PRs; Devin Review skips drafts",
            flush=True,
        )
        return 1

    if event == "status":
        pulls = associated_pulls(sha)
        if not is_open_pr_head(sha, pulls):
            print(
                f"status on {sha} is not an open PR head; review-gate passes",
                flush=True,
            )
            return 0
        if any(
            pull.get("draft")
            and str((pull.get("head") or {}).get("sha") or "").lower() == sha.lower()
            for pull in pulls
        ):
            print(
                "::error::review-gate fails on draft PRs; Devin Review skips drafts",
                flush=True,
            )
            return 1
        if devin_review_succeeded(sha):
            print(f"Devin Review succeeded on {sha}", flush=True)
            return 0
        print(f"::error::Devin Review has not succeeded on {sha}", flush=True)
        return 1

    seconds = env_int("REVIEW_GATE_POLL_SECONDS", DEFAULT_POLL_SECONDS)
    interval = env_int("REVIEW_GATE_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
    if poll_until_success(sha, seconds=seconds, interval=interval):
        print(f"Devin Review succeeded on {sha}", flush=True)
        return 0
    print(
        f"::error::Devin Review did not succeed on {sha} within {seconds}s",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"review-gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
