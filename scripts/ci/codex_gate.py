#!/usr/bin/env python3
"""Codex READY/MEDIATE router. Missing verdicts and legacy FIX never dispatch fixes.

Provider-free core (markers, verdicts, admit_resolve_pr) is imported by
tools/test_codex_gate.py. Live GitHub I/O uses `gh` and is stubbed in tests.

Never prints secret values. Missing CODEX_TRIGGER_PAT skips with a notice.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
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
    """Only MEDIATE is terminal; READY is reconciled against live state."""
    return any(
        body.startswith(acted_comment_body(sha, verdict).rstrip("\n"))
        for body in comments
        for verdict in ("MEDIATE",)
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
    if verdict == "MEDIATE" and has_terminal_action(sha, comments):
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
        raise subprocess.CalledProcessError(result.returncode, ["gh", *args], stderr=result.stderr)
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


def list_comment_records(repo: str, number: str, token: str, gh: GhCall = _gh) -> list[dict[str, Any]]:
    raw = gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100"], token)
    return [item for item in _parse_json_list(raw) if isinstance(item, dict)]


def list_comment_bodies(repo: str, number: str, token: str, gh: GhCall = _gh) -> list[str]:
    return [str(item.get("body") or "") for item in list_comment_records(repo, number, token, gh)]


def fixer_helpers():
    path = Path(__file__).with_name("claude_fixer_receipts.py")
    spec = importlib.util.spec_from_file_location("codex_fixer_receipts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def current_ready_head(repo: str, number: str, sha: str, token: str, gh: GhCall = _gh) -> bool:
    pr = json.loads(gh(["api", f"repos/{repo}/pulls/{number}"], token))
    return (pr.get("state") == "open" and not pr.get("draft") and _head_repo(pr) == repo
            and (pr.get("head") or {}).get("sha") == sha)


def readiness_blocked(repo: str, number: str, sha: str, comments: list[dict[str, Any]], token: str, gh: GhCall = _gh) -> bool:
    return fixer_helpers().readiness_blocked(repo, number, sha, comments, read_api=lambda path: json.loads(gh(["api", path], token)),
        record=lambda body: post_comment(repo, number, body, token, gh=gh))


def comments_after_pause(sha: str, comments: list[dict[str, Any]], writer: dict[str, Any] | None = None) -> list[str]:
    """READY is live state; only authenticated MEDIATE comments deduplicate."""
    trusted = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if body.startswith(acted_comment_body(sha, "READY").rstrip("\n")):
            continue
        terminal = body.startswith(acted_comment_body(sha, "MEDIATE").rstrip("\n"))
        author = comment.get("user") or {}
        authenticated = (writer is not None and type(writer.get("id")) is int
                         and writer["id"] > 0 and all(author.get(key) == writer.get(key) for key in ("id", "login", "type")))
        if not terminal or authenticated:
            trusted.append(body)
    return trusted


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
    def confirmation_permission(actor):
        if not current_ready_head(repo, number, sha, read_token, gh=gh):
            return "none"
        value = json.loads(gh(["api", f"repos/{repo}/collaborators/{actor}/permission"], read_token))
        return value.get("permission")
    records: list[dict[str, Any]] = []
    comments: list[str] = []
    if number and read_token:
        try:
            records = _retry_listing(
                lambda: list_comment_records(repo, number, read_token, gh=gh)
            )
            gate_writer = json.loads(gh(["api", "user"], pat)) if pat and is_codex_login(login) and parse_verdict(review.get("body")) in {"READY", "MEDIATE"} else None
            if is_codex_login(login) and parse_verdict(review.get("body")) == "READY":
                if not current_ready_head(repo, number, sha, read_token, gh=gh):
                    return {"action": "skip-stale-head", "verdict": "READY"}
                label_events = _parse_json_list(gh(["api", "--paginate", f"repos/{repo}/issues/{number}/events?per_page=100"], read_token))
                if pending_nonce := fixer_helpers().pending_ci_intent(label_events, records, head=sha, permission=confirmation_permission):
                    notice(f"Unverified prior ci:full write: inspect the label, then a current writer may post exactly /claude-fixer reconcile-label {pending_nonce} {sha}; this acknowledges uncertainty only")
                    return {"action": "unverified-ci-label", "verdict": "READY"}
            comments = comments_after_pause(sha, records, gate_writer)
            if is_codex_login(login) and parse_verdict(review.get("body")) == "READY" and readiness_blocked(repo, number, sha, records, read_token, gh=gh):
                if not current_ready_head(repo, number, sha, read_token, gh=gh):
                    return {"action": "skip-stale-head", "verdict": "READY"}
                fixer_helpers().revoke_owned_ci(repo, number, sha,
                    get_pages=lambda path: _parse_json_list(gh(["api", "--paginate", path + "?per_page=100"], read_token)),
                    delete=lambda r, n: gh(["api", "-X", "DELETE", f"repos/{r}/issues/{n}/labels/ci:full"], read_token),
                    post_comment=lambda r, n, body: post_comment(r, n, body, read_token, gh=gh))
                emit({"action": "paused-findings", "pr_number": number, "head_sha": sha, "verdict": "READY"})
                return {"action": "paused-findings", "verdict": "READY"}
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
        owned_event = 0
        label_attempted = False
        rejection_recorded = False
        intent = None
        try:
            helper = fixer_helpers()
            get_pages = lambda path: _parse_json_list(gh(["api", "--paginate", path + "?per_page=100"], read_token))
            events = get_pages(f"repos/{repo}/issues/{number}/events")
            before = helper.label_event_id(events)
            ownership_comments = get_pages(f"repos/{repo}/issues/{number}/comments")
            if pending_nonce := helper.pending_ci_intent(events, ownership_comments, head=sha, permission=confirmation_permission):
                notice(f"Unverified prior ci:full write: inspect the label, then a current writer may post exactly /claude-fixer reconcile-label {pending_nonce} {sha}; this acknowledges uncertainty only")
                return {"action": "unverified-ci-label", "verdict": "READY"}
            if helper.ci_label_decision(events, ownership_comments, head=sha) == "preserve-human-removal":
                removal = max(e["id"] for e in events if e.get("event") == "unlabeled"
                    and (e.get("label") or {}).get("name") == CI_FULL_LABEL and type(e.get("id")) is int)
                observed = any(helper.bot(c.get("user") or {}, "github-actions[bot]", helper.ACTIONS_ID)
                    and any(r.get("head") == sha and r.get("event") == removal
                        for r in helper.records(str(c.get("body") or ""), "ci-removal")) for c in ownership_comments)
                if not observed:
                    if not current_ready_head(repo, number, sha, read_token, gh=gh):
                        return {"action": "skip-stale-head", "verdict": "READY"}
                    post_comment(repo, number, helper.marker("ci-removal", {"head": sha, "event": removal}), read_token, gh=gh)
                notice("ci:full was removed by a human or ownership is ambiguous; preserving manual control")
                return {"action": "preserve-human-removal", "verdict": "READY"}
            if before:
                return {"action": "ready-present", "verdict": "READY"}
            writer = gate_writer["login"]
            intent = helper.begin_ci_intent(repo, number, sha, events,
                lambda r, n, body: post_comment(r, n, body, read_token, gh=gh)) if not before else None
            label_attempted = True
            try:
                add_label(repo, number, CI_FULL_LABEL, pat, gh=gh)
            except (OSError, subprocess.CalledProcessError) as error:
                status = helper.known_label_rejection(error)
                if intent and status:
                    post_comment(repo, number, helper.marker("ci-rejected", {"nonce": intent["nonce"], "status": status}), read_token, gh=gh)
                    rejection_recorded = True
                raise
            def post_owned(r, n, body):
                nonlocal owned_event
                # The trusted helper calls this only after proving the exact new
                # label event belongs to this gate writer. Keep proof if POST fails.
                owned_event = helper.records(body, "ci-owned")[0]["event"]
                post_comment(r, n, body, read_token, gh=gh)
            helper.record_ci_ownership(repo, number, sha, before, writer, get_pages, post_owned, intent=intent)
            fresh = list_comment_records(repo, number, read_token, gh=gh)
            if not current_ready_head(repo, number, sha, read_token, gh=gh) or readiness_blocked(repo, number, sha, fresh, read_token, gh=gh):
                if owned_event:
                    post_comment(repo, number, helper.marker("ci-invalidated", {"head": sha, "event": owned_event, **({"nonce": intent["nonce"]} if intent else {})}), read_token, gh=gh)
                    helper.rollback_ci_event(repo, number, owned_event, get_pages,
                        lambda r, n: gh(["api", "-X", "DELETE", f"repos/{r}/issues/{n}/labels/ci:full"], read_token))
                emit({"action": "paused-findings", "pr_number": number, "head_sha": sha})
                return {"action": "paused-findings", "verdict": "READY"}
            post_comment(repo, number, acted_comment_body(sha, "READY"), pat, gh=gh)
            log(f"labeled PR #{number} {CI_FULL_LABEL}")
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
            notice("failed to verify READY ownership or final pause state; no terminal READY")
            if owned_event:
                try:
                    if helper.label_event_id(get_pages(f"repos/{repo}/issues/{number}/events")) == owned_event:
                        try:
                            post_comment(repo, number, helper.marker("ci-invalidated", {"head": sha, "event": owned_event, **({"nonce": intent["nonce"]} if intent else {})}), read_token, gh=gh)
                        except (OSError, subprocess.CalledProcessError, ValueError):
                            notice("READY recovery receipt failed; a later retry may need explicit human approval")
                        gh(["api", "-X", "DELETE", f"repos/{repo}/issues/{number}/labels/ci:full"], read_token)
                except (OSError, subprocess.CalledProcessError, ValueError):
                    notice("READY cleanup could not be verified; inspect ci:full manually")
            elif rejection_recorded:
                notice("READY label POST was rejected; a later READY can safely retry")
            elif label_attempted:
                notice("READY label ownership could not be established; inspect ci:full manually")
            return {"action": "skip-readiness", "verdict": "READY"}
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
