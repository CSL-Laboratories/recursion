"""Run one task on a routed provider and, when another provider exists, review it."""

from __future__ import annotations

from scripts.v3.harness.errors import HarnessError
from scripts.v3.harness.providers import ProviderRequest, open_provider
from scripts.v3.harness.review import parse_verdict, review_prompt
from scripts.v3.harness.routing import select_route
from scripts.v3.harness.state import new_run_id, save_run
from scripts.v3.harness.streams import assistant_text
from scripts.v3.harness.usage import add_usage, empty_usage, exceeded, parse_usage


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
    current_task = task
    result = _execute(
        opener,
        worker,
        ProviderRequest(task=current_task, packet_id=run_id, kind="execute"),
        allow_live=allow_live,
        timeout_seconds=_timeout(config, worker),
    )
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
        )

    status = "review-rejected"
    for round_index in range(1, max_rounds + 1):
        reviewer = reviewers[0]
        review_result = _execute(
            opener,
            reviewer,
            ProviderRequest(
                task=review_prompt(current_task, shown),
                packet_id=run_id,
                kind="review",
            ),
            allow_live=allow_live,
            timeout_seconds=_timeout(config, reviewer),
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
        result = _execute(
            opener,
            worker,
            ProviderRequest(task=current_task, packet_id=run_id, kind="execute"),
            allow_live=allow_live,
            timeout_seconds=_timeout(config, worker),
        )
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
    )


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
    text = fields["text"]
    if len(text) > RECORD_LIMIT:
        text = text[:RECORD_LIMIT] + "\n[truncated]"
    record = {
        "accepted": fields["status"] == "completed",
        "provider": fields["provider"],
        "reviewers": list(fields["reviewers"]),
        "reviews": fields["reviews"],
        "run_id": fields["run_id"],
        "status": fields["status"],
        "task": fields["task"],
        "text": text,
        "usage": fields["usage"],
    }
    save_run(state_dir, record)
    return record
