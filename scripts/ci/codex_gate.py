#!/usr/bin/env python3
"""Codex READY/MEDIATE router. Missing verdicts and legacy FIX never dispatch fixes.

Provider-free core (markers, verdicts, admit_resolve_pr) is imported by
tools/test_codex_gate.py. Live GitHub I/O uses `gh` and is stubbed in tests.

Never prints secret values. Missing CODEX_TRIGGER_PAT skips with a notice.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable

LISTING_ATTEMPTS = 3
LISTING_RETRY_SECONDS = 1.0
CI_FULL_LABEL = "ci:full"
MEDIATE_WORKFLOW = "stuck-thread-watch.yml"
GATE_MARKER_PREFIX = "codex-gate"
ACTED_PREFIX = "codex-gate-acted"
DEVIN_REVIEW_LOGIN = "devin-ai-integration[bot]"
CODEX_REVIEW_LOGIN = "chatgpt-codex-connector[bot]"
DISPATCH_EVENTS = frozenset({"workflow_dispatch", "repository_dispatch"})
MENTION_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})
VERDICT_RE = re.compile(r"^CODEX-GATE:\s*(FIX|READY|MEDIATE)\s*$", re.MULTILINE)
SAFE_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
SAFE_PR_RE = re.compile(r"^[1-9][0-9]{0,9}$")
SAFE_REF_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9._/-]+$")

# gh(args, token, input_text=None) -> stdout. Extra positional input is allowed.
GhCall = Callable[..., str]


def notice(message: str) -> None:
    print(f"::notice::{message}", flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def login_of(value: str | None) -> str:
    return (value or "").strip()


def _norm_login(value: str | None) -> str:
    text = login_of(value).casefold()
    suffix = "[bot]"
    return text[: -len(suffix)] if text.endswith(suffix) else text


def is_codex_login(login: str | None) -> bool:
    return _norm_login(login) == _norm_login(CODEX_REVIEW_LOGIN)


def is_bot_type(user_type: str | None) -> bool:
    return (user_type or "") == "Bot"


def safe_head_sha(sha: str | None) -> str:
    value = (sha or "").strip().lower()
    return value if SAFE_SHA_RE.fullmatch(value) else ""


def safe_pr_number(value: str | int | None) -> str:
    text = str(value or "").strip()
    return text if SAFE_PR_RE.fullmatch(text) else ""


def safe_ref(value: str | None) -> str:
    text = (value or "").strip()
    return text if text and SAFE_REF_RE.fullmatch(text) else ""


def gate_marker(sha: str) -> str:
    return f"<!-- {GATE_MARKER_PREFIX}:{sha} -->"


def acted_marker(sha: str) -> str:
    return f"<!-- {ACTED_PREFIX}:{sha} -->"


def parse_verdict(text: str | None) -> str | None:
    if not text:
        return None
    matches = VERDICT_RE.findall(text)
    return matches[-1] if matches else None


def bodies_have(marker: str, bodies: list[str]) -> bool:
    return any(marker in (body or "") for body in bodies)


def admit_resolve_pr(event_name: str, event: dict[str, Any]) -> bool:
    """Mirror of the claude.yml resolve-pr job-level `if:`.

    Skipped events must not allocate a runner. Allow only human @claude comments
    and fixer dispatch events, plus submitted findings from the exact Devin bot.
    """
    if event_name in DISPATCH_EVENTS:
        return True
    if event_name == "pull_request_review":
        review = event.get("review") or {}
        user = review.get("user") or {}
        return (
            event.get("action") == "submitted"
            and user.get("login") == DEVIN_REVIEW_LOGIN
            and is_bot_type(user.get("type"))
            and review.get("state") in {"commented", "changes_requested"}
        )
    sender = event.get("sender") or {}
    if event_name in MENTION_EVENTS:
        if event_name == "issue_comment" and not (event.get("issue") or {}).get("pull_request"):
            return False
        if is_bot_type(sender.get("type")):
            return False
        comment = event.get("comment") or {}
        user = comment.get("user") or {}
        if is_bot_type(user.get("type")):
            return False
        return "@claude" in (comment.get("body") or "")
    return False


def request_comment_body(sha: str) -> str:
    return (
        f"{gate_marker(sha)}\n"
        "@codex review\n\n"
        "Follow **Code Review Rules** in AGENTS.md. When ready or needing human mediation, end with exactly "
        "one of these lines as the last line of the review body (not in a code fence):\n\n"
        "CODEX-GATE: READY\n"
        "CODEX-GATE: MEDIATE\n\n"
        "- READY: no fixer; the `ci:full` label should be applied.\n"
        "- MEDIATE: Devin and Claude are stuck; hand off to stuck-thread-watch.\n\n"
        "If findings still need fixing without human mediation, report pending without a verdict.\n"
        "Only Devin and human requests trigger fixes. Do not push, merge, or resolve threads.\n"
    )


def acted_comment_body(sha: str, verdict: str, extra: str = "") -> str:
    line = extra.strip()
    suffix = f"\n{line}\n" if line else "\n"
    return f"{acted_marker(sha)}\nCodex gate acted: {verdict}.{suffix}"


def has_terminal_action(sha: str, comments: list[str]) -> bool:
    """Legacy FIX/timeout markers are nonterminal; only READY/MEDIATE deduplicate."""
    return any(
        body.startswith(acted_comment_body(sha, verdict).rstrip("\n"))
        for body in comments
        for verdict in ("READY", "MEDIATE")
    )


def sha_has_verdict(reviews: list[dict[str, Any]], sha: str) -> bool:
    want = safe_head_sha(sha)
    if not want:
        return False
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if not is_codex_login(((review.get("user") or {}).get("login"))):
            continue
        if safe_head_sha(review.get("commit_id")) != want:
            continue
        if parse_verdict(review.get("body")) in {"READY", "MEDIATE"}:
            return True
    return False


def decide_request(
    *,
    pat: str,
    same_repo: bool,
    draft: bool,
    sha: str,
    comments: list[str],
    reviews: list[dict[str, Any]],
) -> dict[str, str]:
    if not (pat or "").strip():
        return {"action": "skip-pat", "proceed": "false"}
    if not same_repo or draft or not safe_head_sha(sha):
        return {"action": "skip", "proceed": "false"}
    if bodies_have(gate_marker(sha), comments):
        if sha_has_verdict(reviews, sha) or has_terminal_action(sha, comments):
            return {"action": "already-verdict", "proceed": "false"}
        return {"action": "already", "proceed": "true"}
    return {"action": "post", "proceed": "true"}


def decide_verdict(
    *,
    login: str | None,
    body: str | None,
    sha: str,
    comments: list[str],
) -> dict[str, str]:
    if not is_codex_login(login):
        return {"action": "skip", "verdict": ""}
    if not safe_head_sha(sha):
        return {"action": "skip", "verdict": ""}
    verdict = parse_verdict(body)
    if verdict == "FIX":
        return {"action": "ignored-fix", "verdict": "FIX"}
    if has_terminal_action(sha, comments):
        return {"action": "already-acted", "verdict": verdict or ""}
    if verdict is None:
        return {"action": "no-verdict", "verdict": ""}
    return {"action": verdict.lower(), "verdict": verdict}


def _token_env(token: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"GH_TOKEN", "GITHUB_TOKEN"}}
    env["GH_TOKEN"] = token
    return env


def _gh(args: list[str], token: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        env=_token_env(token),
        input=input_text,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, ["gh", *args])
    return result.stdout


class ListingError(ValueError):
    """gh --paginate produced a listing we cannot parse. Fail closed."""


def _retry_listing(
    fn: Callable[[], Any],
    *,
    attempts: int = LISTING_ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry transient gh failures. Do not retry ListingError (malformed history)."""
    last: BaseException | None = None
    for index in range(attempts):
        try:
            return fn()
        except (OSError, subprocess.CalledProcessError) as exc:
            last = exc
            if index == attempts - 1:
                raise
            sleep(LISTING_RETRY_SECONDS)
    raise last  # pragma: no cover


def _parse_json_list(raw: str) -> list[Any]:
    """Parse one JSON array or gh --paginate's concatenated/newline-separated pages.

    Fail closed on malformed input: callers must not treat a parse failure as
    an empty history (that would miss markers and blow the daily cap).
    """
    text = raw or ""
    if not text.strip():
        return []
    items: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break
        try:
            data, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError as exc:
            raise ListingError("malformed JSON listing") from exc
        if not isinstance(data, list):
            raise ListingError("listing is not a JSON array")
        items.extend(data)
        idx = end
    return items


def list_comment_bodies(repo: str, number: str, token: str, gh: GhCall = _gh) -> list[str]:
    raw = gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100"], token)
    return [str(item.get("body") or "") for item in _parse_json_list(raw) if isinstance(item, dict)]


def list_reviews(repo: str, number: str, token: str, gh: GhCall = _gh) -> list[dict[str, Any]]:
    raw = gh(["api", "--paginate", f"repos/{repo}/pulls/{number}/reviews?per_page=100"], token)
    return [item for item in _parse_json_list(raw) if isinstance(item, dict)]


def post_comment(repo: str, number: str, body: str, token: str, gh: GhCall = _gh) -> None:
    gh(
        ["api", f"repos/{repo}/issues/{number}/comments", "--input", "-"],
        token,
        json.dumps({"body": body}),
    )


def add_label(repo: str, number: str, label: str, token: str, gh: GhCall = _gh) -> None:
    gh(
        ["api", "--method", "POST", f"repos/{repo}/issues/{number}/labels", "--input", "-"],
        token,
        json.dumps({"labels": [label]}),
    )


def dispatch_workflow(
    repo: str,
    workflow: str,
    ref: str,
    fields: dict[str, str],
    token: str,
    gh: GhCall = _gh,
) -> bool:
    branch = safe_ref(ref)
    if not branch:
        return False
    args = ["workflow", "run", workflow, "--repo", repo, "--ref", branch]
    for key, value in fields.items():
        args.extend(["-f", f"{key}={value}"])
    try:
        gh(args, token)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def emit(decision: dict[str, str]) -> None:
    dest = os.environ.get("GITHUB_OUTPUT")
    for key, value in decision.items():
        line = f"{key}={value}"
        print(line, flush=True)
        if dest:
            with open(dest, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def _head_repo(pr: dict[str, Any]) -> str:
    return str(((pr.get("head") or {}).get("repo") or {}).get("full_name") or "")


def _load_event(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("event JSON must be an object")
    return data


def run_request(
    event: dict[str, Any],
    repo: str,
    pat: str,
    read_token: str,
    *,
    gh: GhCall = _gh,
) -> dict[str, str]:
    pr = event.get("pull_request") or {}
    number = safe_pr_number(pr.get("number"))
    sha = safe_head_sha((pr.get("head") or {}).get("sha"))
    same_repo = _head_repo(pr) == repo
    draft = bool(pr.get("draft"))
    comments: list[str] = []
    reviews: list[dict[str, Any]] = []
    if number and read_token:
        try:
            comments = _retry_listing(
                lambda: list_comment_bodies(repo, number, read_token, gh=gh)
            )
            reviews = _retry_listing(lambda: list_reviews(repo, number, read_token, gh=gh))
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ListingError, ValueError):
            notice("failed to list PR comments/reviews; skipping (no failure)")
            out = {"proceed": "false", "pr_number": number, "head_sha": sha, "action": "skip-listing"}
            emit(out)
            return out
    decision = decide_request(
        pat=pat,
        same_repo=same_repo,
        draft=draft,
        sha=sha,
        comments=comments,
        reviews=reviews,
    )
    if decision["action"] == "skip-pat":
        notice("CODEX_TRIGGER_PAT is missing; skipping Codex gate (no failure)")
    elif decision["action"] == "post" and pat and number:
        try:
            post_comment(repo, number, request_comment_body(sha), pat, gh=gh)
            log(f"posted @codex review for {sha[:12]}")
        except (OSError, subprocess.CalledProcessError):
            notice("failed to post @codex review; skipping (no failure)")
            decision = {"action": "skip", "proceed": "false"}
    elif decision["action"] == "already":
        log(f"codex-gate marker already present for {sha[:12]}")
    out = {
        "proceed": decision["proceed"],
        "pr_number": number,
        "head_sha": sha,
        "action": decision["action"],
    }
    emit(out)
    return out


def run_verdict(
    event: dict[str, Any],
    repo: str,
    pat: str,
    read_token: str,
    default_branch: str,
    *,
    gh: GhCall = _gh,
    day: str | None = None,
) -> dict[str, str]:
    review = event.get("review") or {}
    pr = event.get("pull_request") or {}
    number = safe_pr_number(pr.get("number"))
    sha = safe_head_sha(review.get("commit_id") or (pr.get("head") or {}).get("sha"))
    login = ((review.get("user") or {}).get("login"))
    if _head_repo(pr) != repo or bool(pr.get("draft")):
        notice("skipping Codex verdict on a fork or draft PR")
        emit({"action": "skip-fork", "pr_number": number, "head_sha": sha})
        return {"action": "skip-fork", "verdict": ""}
    comments: list[str] = []
    if number and read_token:
        try:
            comments = _retry_listing(
                lambda: list_comment_bodies(repo, number, read_token, gh=gh)
            )
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, ListingError, ValueError):
            notice("failed to list PR comments; skipping verdict (no failure)")
            emit({"action": "skip-listing", "pr_number": number, "head_sha": sha})
            return {"action": "skip-listing", "verdict": ""}
    decision = decide_verdict(
        login=login,
        body=review.get("body"),
        sha=sha,
        comments=comments,
    )
    action = decision["action"]
    if action in {"skip", "already-acted", "no-verdict", "ignored-fix"}:
        emit({"action": action, "pr_number": number, "head_sha": sha, "verdict": decision["verdict"]})
        return decision
    if not pat:
        notice("CODEX_TRIGGER_PAT is missing; skipping Codex gate (no failure)")
        emit({"action": "skip-pat", "pr_number": number, "head_sha": sha})
        return {"action": "skip-pat", "verdict": decision.get("verdict") or ""}
    if action == "ready" and number:
        try:
            post_comment(repo, number, acted_comment_body(sha, "READY"), pat, gh=gh)
        except (OSError, subprocess.CalledProcessError):
            notice("failed to post READY marker")
        try:
            add_label(repo, number, CI_FULL_LABEL, pat, gh=gh)
            log(f"labeled PR #{number} {CI_FULL_LABEL}")
        except (OSError, subprocess.CalledProcessError):
            notice(f"failed to add {CI_FULL_LABEL}; skipping (no failure)")
    elif action == "mediate" and number:
        try:
            post_comment(repo, number, acted_comment_body(sha, "MEDIATE"), pat, gh=gh)
        except (OSError, subprocess.CalledProcessError):
            notice("failed to post MEDIATE marker")
        ok = dispatch_workflow(
            repo,
            MEDIATE_WORKFLOW,
            default_branch,
            {"pr_number": number},
            pat,
            gh=gh,
        )
        if not ok:
            notice(
                f"{MEDIATE_WORKFLOW} dispatch failed; "
                "stuck-thread-watch may not be on the default branch yet"
            )
        else:
            log(f"handed PR #{number} to {MEDIATE_WORKFLOW}")
    emit({"action": action, "pr_number": number, "head_sha": sha, "verdict": decision["verdict"]})
    return decision


def run_fallback(
    repo: str,
    number: str,
    sha: str,
    pat: str,
    read_token: str,
    default_branch: str,
    *,
    gh: GhCall = _gh,
    day: str | None = None,
) -> dict[str, str]:
    # Compatibility for a pre-rollout queued workflow: no reads, writes or dispatch.
    out = {"action": "ignored-fallback"}
    emit(out)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    mode = os.environ.get("CODEX_GATE_MODE") or (args[0] if args else "")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    pat = (os.environ.get("CODEX_TRIGGER_PAT") or "").strip()
    read_token = (
        os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or pat
    ).strip()
    default_branch = safe_ref(os.environ.get("DEFAULT_BRANCH") or "master") or "master"
    if mode not in {"request", "verdict", "fallback"}:
        emit({"action": "skip", "proceed": "false"})
        return 0
    if not repo:
        emit({"action": "skip", "proceed": "false"})
        return 0
    if mode == "fallback":
        run_fallback(
            repo,
            os.environ.get("PR_NUMBER", ""),
            os.environ.get("HEAD_SHA", ""),
            pat,
            read_token,
            default_branch,
        )
        return 0
    if not event_path:
        emit({"action": "skip", "proceed": "false"})
        return 0
    try:
        event = _load_event(event_path)
    except (OSError, ValueError, json.JSONDecodeError):
        emit({"action": "skip", "proceed": "false"})
        return 0
    if mode == "request":
        run_request(event, repo, pat, read_token)
        return 0
    run_verdict(event, repo, pat, read_token, default_branch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
