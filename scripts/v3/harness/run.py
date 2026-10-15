"""Run one task on a routed provider and, when another provider exists, review it.

A rate limit, exhausted quota, or expired auth records a cooldown in the run
state and the next capable provider is tried. Other failures stop the run.
The budget is checked before each call.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from scripts.v3.harness.cooldowns import CooldownStore, resolve_until
from scripts.v3.harness.context import task_prompt
from scripts.v3.harness.doctor import _scrub, _secret_values
from scripts.v3.harness.errors import HarnessError, ProviderAvailability, ProviderTimeout
from scripts.v3.harness.providers import ProviderRequest, open_provider
from scripts.v3.harness.review import parse_verdict, review_prompt
from scripts.v3.harness.routing import select_route
from scripts.v3.harness.state import new_run_id, save_run
from scripts.v3.harness.streams import assistant_text
from scripts.v3.harness.usage import add_usage, call_fits, empty_usage, exceeded, parse_usage


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
    diff: str | None = None,
    dependencies: list | None = None,
    now=None,
) -> dict:
    """Route one task, review it when a second provider is enabled, and save the run."""

    if not isinstance(task, str) or task.strip() == "":
        raise HarnessError("task must not be empty")
    worker, reviewers = select_route(config, provider)
    candidates = [worker, *reviewers]
    opener = open_provider if open_adapter is None else open_adapter
    run_id = new_run_id() if run_id is None else run_id
    budgets = config["budgets"]
    cooldown_seconds = config["routing"]["cooldown_seconds"]
    usage = empty_usage()
    reviews: list[dict] = []
    attempts: list[dict] = []
    shown = ""
    workspace = _live_workspace(state_dir, run_id) if allow_live else None
    current_task = task
    worker_task = task_prompt(task, dependencies)
    review_diff = diff or ""
    store = CooldownStore(Path(state_dir) / "cooldowns.json")
    clock = time.time if now is None else now
    holder = {"provider": worker}

    def snapshot() -> dict:
        return store.active(clock())

    def save_failure(status: str, exc: HarnessError) -> None:
        _finish(
            config,
            state_dir,
            run_id=run_id,
            task=task,
            provider=holder["provider"],
            reviewers=reviewers,
            text=shown,
            reviews=reviews,
            usage=usage,
            status=status,
            error=str(exc),
            workspace=workspace,
            attempts=attempts,
            cooldowns=snapshot(),
        )

    def dispatch(names: list[str], request: ProviderRequest, *, on_exhaust: str) -> tuple[str, object]:
        """Try each provider. Availability failures cool down and move on."""

        for name in names:
            active = snapshot()
            if name in active:
                attempts.append(
                    {
                        "error": "cooldown",
                        "ok": False,
                        "provider": name,
                        "reason": active[name].get("reason"),
                    }
                )
                continue
            if not call_fits(usage, budgets, request.task):
                attempts.append({"ok": False, "provider": name, "reason": "budget"})
                return "budget", None
            try:
                result = _execute(
                    opener,
                    name,
                    request,
                    allow_live=allow_live,
                    timeout_seconds=_timeout(config, name),
                )
            except ProviderTimeout as exc:
                save_failure("timed-out", exc)
                raise
            except ProviderAvailability as exc:
                moment = clock()
                store.record(
                    name,
                    exc.reason,
                    resolve_until(exc.until, exc.retry_after, moment, float(cooldown_seconds)),
                )
                attempts.append(
                    {
                        "error": type(exc).__name__,
                        "ok": False,
                        "provider": name,
                        "reason": exc.reason,
                    }
                )
                continue
            except HarnessError as exc:
                save_failure("failed", exc)
                raise
            attempts.append({"ok": True, "provider": name})
            return "ok", result
        if on_exhaust == "skip":
            return "skipped", None
        exc = HarnessError("no capable provider completed the task")
        save_failure("failed", exc)
        raise exc

    def finish(status: str, provider_name: str):
        return _finish(
            config,
            state_dir,
            run_id=run_id,
            task=task,
            provider=provider_name,
            reviewers=reviewers,
            text=shown,
            reviews=reviews,
            usage=usage,
            status=status,
            workspace=workspace,
            attempts=attempts,
            cooldowns=snapshot(),
        )

    kind, result = dispatch(candidates, _request(worker_task, run_id, "execute", workspace), on_exhaust="fail")
    if kind == "budget":
        return finish("budget-exceeded", holder["provider"])
    holder["provider"] = result.provider
    add_usage(usage, parse_usage(result.text))
    shown = assistant_text(result.text)
    if exceeded(usage, budgets):
        return finish("budget-exceeded", result.provider)

    max_rounds = config["review"]["max_rounds"]
    review_candidates = [name for name in candidates if name != result.provider]
    if max_rounds <= 0 or not review_candidates:
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
        return finish(status, result.provider)

    status = "review-rejected"
    for round_index in range(1, max_rounds + 1):
        review_kind, review_result = dispatch(
            [name for name in candidates if name != result.provider],
            _request(review_prompt(current_task, shown, review_diff), run_id, "review", workspace),
            on_exhaust="skip",
        )
        if review_kind == "budget":
            status = "budget-exceeded"
            break
        if review_kind == "skipped":
            reviews.append(
                {
                    "notes": "no distinct reviewer",
                    "provider": None,
                    "round": round_index,
                    "verdict": "skipped",
                }
            )
            status = "completed" if config["review"]["accept_unreviewed"] else "unreviewed"
            break
        add_usage(usage, parse_usage(review_result.text))
        verdict, notes = parse_verdict(review_result.text)
        reviews.append(
            {
                "notes": notes,
                "provider": review_result.provider,
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
            worker_task
            + "\n\nPrevious answer:\n"
            + shown[:RECORD_LIMIT]
            + "\n\nReview notes:\n"
            + notes
        )
        revise_kind, revised = dispatch(
            [result.provider, *[name for name in candidates if name != result.provider]],
            _request(current_task, run_id, "execute", workspace),
            on_exhaust="fail",
        )
        if revise_kind == "budget":
            status = "budget-exceeded"
            break
        result = revised
        holder["provider"] = result.provider
        add_usage(usage, parse_usage(result.text))
        shown = assistant_text(result.text)
        if exceeded(usage, budgets):
            status = "budget-exceeded"
            break

    return finish(status, result.provider)


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
        "attempts": list(fields.get("attempts") or []),
        "cooldowns": fields.get("cooldowns") or {},
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
