"""Run one task on a routed provider and, when another provider exists, review it."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.v3.harness.doctor import _scrub, _secret_values
from scripts.v3.harness.errors import HarnessError, ProviderTimeout
from scripts.v3.harness.providers import ProviderRequest, open_provider
from scripts.v3.harness.review import parse_verdict, review_prompt
from scripts.v3.harness.routing import select_route
from scripts.v3.harness.state import new_run_id, save_run
from scripts.v3.harness.streams import assistant_text
from scripts.v3.harness.usage import add_usage, codex_session_meter, empty_usage, exceeded, parse_usage


RECORD_LIMIT = 8000


def run_task(
    task: str,
    *,
    config: dict,
    state_dir,
    provider: str | None = None,
    allow_live: bool = False,
    open_adapter=None,
    run_id: str | None = None,
) -> dict:
    """Route one task, review it when a second provider is enabled, and save the run."""

    if not isinstance(task, str) or task.strip() == "":
        raise HarnessError("task must not be empty")
    worker, reviewers = select_route(config, provider)
    opener = open_provider if open_adapter is None else open_adapter
    run_id = new_run_id() if run_id is None else run_id
    budgets = config["budgets"]
    usage = empty_usage()
    reviews: list[dict] = []
    shown = ""
    workspace = _live_workspace(state_dir, run_id) if allow_live else None
    current_task = task

    def finish(status: str, error: str | None = None) -> dict:
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
            error=error,
            workspace=workspace,
        )

    def save_failure(status: str, exc: HarnessError) -> None:
        finish(status, error=str(exc))

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

    result = call(worker, _request(current_task, run_id, "execute", workspace))
    add_usage(usage, _usage_for(worker, result.text))
    shown = assistant_text(result.text)
    if exceeded(usage, budgets):
        return finish("budget-exceeded")

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
        return finish(status)

    status = "review-rejected"
    for round_index in range(1, max_rounds + 1):
        reviewer = reviewers[0]
        review_result = call(
            reviewer,
            _request(review_prompt(current_task, shown), run_id, "review", workspace),
        )
        add_usage(usage, _usage_for(reviewer, review_result.text))
        verdict, notes = parse_verdict(review_result.text)
        reviews.append(
            {
                "notes": notes,
                "provider": reviewer,
                "round": round_index,
                "verdict": verdict,
            }
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
        add_usage(usage, _usage_for(worker, result.text))
        shown = assistant_text(result.text)
        if exceeded(usage, budgets):
            status = "budget-exceeded"
            break

    return finish(status)


def _usage_for(name: str, text: str) -> dict:
    """Codex streams use the session-total meter. Other providers use the last usage object."""

    if name == "codex":
        return codex_session_meter(text)
    return parse_usage(text)


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
    save_run(state_dir, record)
    return record
