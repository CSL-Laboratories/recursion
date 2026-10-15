"""Run one task on a routed provider and, when another provider exists, review it."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from scripts.v3.harness.doctor import _scrub, _secret_values
from scripts.v3.harness.errors import HarnessError, ProviderTimeout
from scripts.v3.harness.log import append_event
from scripts.v3.harness.providers import ProviderRequest, open_provider
from scripts.v3.harness.review import parse_verdict, review_prompt
from scripts.v3.harness.routing import select_route
from scripts.v3.harness.state import new_run_id, save_run
from scripts.v3.harness.streams import assistant_text
from scripts.v3.harness.usage import add_usage, empty_usage, exceeded, parse_usage


RECORD_LIMIT = 8000
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def run_task(
    task: str,
    *,
    config: dict,
    state_dir,
    provider: str | None = None,
    allow_live: bool = False,
    open_adapter=None,
    run_id: str | None = None,
    run_branch: str | None = None,
    repo: Path | None = None,
) -> dict:
    """Route one task, review it when a second provider is enabled, and save the run."""

    if not isinstance(task, str) or task.strip() == "":
        raise HarnessError("task must not be empty")
    worker, reviewers = select_route(config, provider)
    opener = open_provider if open_adapter is None else open_adapter
    run_id = new_run_id() if run_id is None else run_id
    run_branch = _branch_name(run_branch)
    budgets = config["budgets"]
    usage = empty_usage()
    reviews: list[dict] = []
    shown = ""
    workspace = _live_workspace(state_dir, run_id) if allow_live else None
    current_task = task

    def save_failure(status: str, exc: HarnessError) -> None:
        _finish(
            config,
            state_dir,
            run_id=run_id,
            task=task,
            provider=worker,
            reviewers=reviewers,
            text=shown,
            reviews=reviews,
            usage=usage,
            status=status,
            error=str(exc),
            workspace=workspace,
            run_branch=run_branch,
            repo=repo,
        )

    def call(name: str, request: ProviderRequest):
        try:
            return _execute(
                opener,
                name,
                request,
                allow_live=allow_live,
                timeout_seconds=_timeout(config, name),
            )
        except ProviderTimeout as exc:
            save_failure("timed-out", exc)
            raise
        except HarnessError as exc:
            save_failure("failed", exc)
            raise

    append_event(state_dir, run_id, "run.started", provider=worker)
    result = call(worker, _request(current_task, run_id, "execute", workspace))
    add_usage(usage, parse_usage(result.text))
    shown = assistant_text(result.text)
    if exceeded(usage, budgets):
        return _finish(
            config,
            state_dir,
            run_id=run_id,
            task=task,
            provider=worker,
            reviewers=reviewers,
            text=shown,
            reviews=reviews,
            usage=usage,
            status="budget-exceeded",
            workspace=workspace,
            run_branch=run_branch,
            repo=repo,
        )

    max_rounds = config["review"]["max_rounds"]
    if max_rounds <= 0 or not reviewers:
        status = "completed" if config["review"]["accept_unreviewed"] else "unreviewed"
        if status == "unreviewed":
            reviews.append(
                {
                    "notes": "no distinct reviewer",
                    "provider": None,
                    "round": 1,
                    "verdict": "skipped",
                }
            )
        return _finish(
            config,
            state_dir,
            run_id=run_id,
            task=task,
            provider=worker,
            reviewers=reviewers,
            text=shown,
            reviews=reviews,
            usage=usage,
            status=status,
            workspace=workspace,
            run_branch=run_branch,
            repo=repo,
        )

    status = "review-rejected"
    for round_index in range(1, max_rounds + 1):
        reviewer = reviewers[0]
        review_result = call(
            reviewer,
            _request(review_prompt(current_task, shown), run_id, "review", workspace),
        )
        add_usage(usage, parse_usage(review_result.text))
        verdict, notes = parse_verdict(review_result.text)
        reviews.append(
            {
                "notes": notes,
                "provider": reviewer,
                "round": round_index,
                "verdict": verdict,
            }
        )
        append_event(
            state_dir,
            run_id,
            "review.verdict",
            provider=reviewer,
            verdict=verdict,
        )
        if exceeded(usage, budgets):
            status = "budget-exceeded"
            break
        if verdict == "approve":
            status = "completed"
            break
        if round_index == max_rounds:
            status = "review-rejected"
            break
        current_task = (
            task + "\n\nPrevious answer:\n" + shown[:RECORD_LIMIT] + "\n\nReview notes:\n" + notes
        )
        result = call(worker, _request(current_task, run_id, "execute", workspace))
        add_usage(usage, parse_usage(result.text))
        shown = assistant_text(result.text)
        if exceeded(usage, budgets):
            status = "budget-exceeded"
            break

    return _finish(
        config,
        state_dir,
        run_id=run_id,
        task=task,
        provider=worker,
        reviewers=reviewers,
        text=shown,
        reviews=reviews,
        usage=usage,
        status=status,
        workspace=workspace,
        run_branch=run_branch,
        repo=repo,
    )


def _request(task: str, run_id: str, kind: str, workspace: str | None) -> ProviderRequest:
    return ProviderRequest(task=task, packet_id=run_id, kind=kind, cwd=workspace)


def _live_workspace(state_dir, run_id: str) -> str:
    """Private folder for one live run. Headless processes use this directory as cwd."""

    root = (Path(state_dir) / run_id).resolve()
    path = root / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path.chmod(0o700)
    return str(path)


def _execute(opener, name: str, request: ProviderRequest, *, allow_live: bool, timeout_seconds: int):
    adapter = opener(name)
    return adapter.execute(request, allow_live=allow_live, timeout_seconds=timeout_seconds)


def _timeout(config: dict, name: str) -> int:
    for item in config["providers"]:
        if item["name"] == name:
            return item["timeout_seconds"]
    raise HarnessError(f"missing provider timeout for {name}")


def _finish(config, state_dir, **fields) -> dict:
    del config
    repo = fields.pop("repo", None)
    secrets = _secret_values(os.environ)
    text = _scrub(fields["text"], secrets)
    if len(text) > RECORD_LIMIT:
        text = text[:RECORD_LIMIT] + "\n[truncated]"
    record = {
        "accepted": fields["status"] == "completed",
        "provider": fields["provider"],
        "reviewers": list(fields["reviewers"]),
        "reviews": _scrub(fields["reviews"], secrets),
        "run_id": fields["run_id"],
        "status": fields["status"],
        "task": fields["task"],
        "text": text,
        "usage": fields["usage"],
    }
    error = fields.get("error")
    if error:
        record["error"] = _scrub(error, secrets)
    workspace = fields.get("workspace")
    if workspace:
        record["workspace"] = workspace
    branch = fields.get("run_branch")
    if branch:
        record["run_branch"] = branch
        if record["accepted"]:
            reviewed = _branch_tip(repo, branch)
            if reviewed:
                record["reviewed_commit"] = reviewed
    save_run(state_dir, record)
    append_event(
        state_dir,
        record["run_id"],
        "run.finished",
        provider=record["provider"],
        status=record["status"],
    )
    return record


def _branch_tip(repo: Path | None, branch: str) -> str | None:
    """Return the current SHA of ``branch`` when ``repo`` has that ref."""

    if repo is None:
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                "core.longpaths=true",
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"refs/heads/{branch}",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    if _SHA.fullmatch(sha) is None:
        return None
    return sha


def _branch_name(name: str | None) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str) or name != name.strip() or not name:
        raise HarnessError("invalid branch name")
    if name.startswith("-") or any(char in name for char in "\n\r "):
        raise HarnessError("invalid branch name")
    return name
