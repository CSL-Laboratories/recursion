"""What the TUI may ask the control API, and how it reads the answers (CSL-799, candidate).

The TUI has exactly the item 8 CLI's commands (``list``, ``status RUN``, ``stop RUN``,
``start --tenant --engine --prompt [--client-key]``, ``conditions``) and turns each request command
into the same ``(method, params)`` the CLI sends. ``conditions`` reads the current conditions again on
demand: the TUI answers it from what it already knows and sends nothing; the one-shot CLI keeps no
state, so it reads ``run.list`` first. There is no approve, deny, order or mid-run verb: the control
API has none, and the TUI never gets a command the CLI lacks.

``list_all_runs`` is how both surfaces read the run list: it follows ``run.list``'s ``next_after`` until
the last page (ADV1-5), so the CLI's ``list`` / ``conditions`` and the TUI's list see every run.

``conditions`` turns an API answer into text labels, so refusal, uncertainty, stale and unreconciled
states are readable without colour (the operator plugin's O7 ``o7.operator.v0`` presentation rules).
"""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Callable, Mapping

from recursion_core.tui.sanitize import field

COMMANDS = ("list", "status", "stop", "start", "conditions")
CONDITIONS = "conditions"  # local in the TUI; not a control API method
METHODS = {"list": "run.list", "status": "run.status", "stop": "run.stop", "start": "run.start"}
ALLOWED_METHODS = frozenset(METHODS.values())
SUCCESS = frozenset({"ok", "started", "stopped"})
UNCERTAIN_CODES = frozenset({"pending"})
UNAVAILABLE = "control_api_unavailable"
# O7's response `surface` allows only gui or cli; the TUI labels itself cli until O7's own version step
# adds tui (PRODUCT.md 550cca0e A36). The control API takes no surface field, so this is a label only.
SURFACE = "cli"

REFUSED, UNCERTAIN, STALE, UNRECONCILED, UNAVAILABLE_LABEL = (
    "REFUSED", "UNCERTAIN", "STALE", "UNRECONCILED", "UNAVAILABLE")
LABELS = (UNAVAILABLE_LABEL, UNRECONCILED, STALE, REFUSED, UNCERTAIN)
# list_all_runs: the most pages one walk follows, and the code it answers when a walk cannot finish.
LIST_FOLLOW_MAX_PAGES = 100_000
LIST_INCOMPLETE = "list_incomplete"


class CommandError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise CommandError(message)

    def exit(self, status: int = 0, message: str | None = None):
        raise CommandError(message or "help is not a command")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="", add_help=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    sub.add_parser("list", add_help=False)
    sub.add_parser(CONDITIONS, add_help=False)
    sub.add_parser("status", add_help=False).add_argument("run_id")
    sub.add_parser("stop", add_help=False).add_argument("run_id")
    start = sub.add_parser("start", add_help=False)
    start.add_argument("--tenant", required=True)
    start.add_argument("--engine", required=True)
    start.add_argument("--prompt", required=True)
    start.add_argument("--client-key")
    return p


def parse_command(line: str) -> tuple[str, dict]:
    """``"stop run-1"`` -> ``("run.stop", {"run_id": "run-1"})``, the CLI's grammar and mapping.

    ``conditions`` parses to ``("conditions", {})``, which is not a control API method."""

    try:
        argv = shlex.split(line)
    except ValueError as exc:
        raise CommandError(str(exc)) from None
    if not argv or argv[0] not in COMMANDS:  # own wording: argparse's differs across Python versions
        raise CommandError(f"unknown command {argv[0] if argv else ''!r}; commands are {', '.join(COMMANDS)}")
    ns = _parser().parse_args(argv)
    if ns.command == CONDITIONS:
        return CONDITIONS, {}
    if ns.command == "list":
        return METHODS["list"], {}
    if ns.command in ("status", "stop"):
        return METHODS[ns.command], {"run_id": ns.run_id}
    params = {"tenant": ns.tenant, "engine": ns.engine, "prompt": ns.prompt}
    if ns.client_key:
        params["client_key"] = ns.client_key
    return METHODS["start"], params


def _runs(answer: Mapping) -> list[Mapping]:
    runs = answer.get("runs")
    if isinstance(runs, list):
        return [r for r in runs if isinstance(r, Mapping)]
    run = answer.get("run")
    return [run] if isinstance(run, Mapping) else []


def stop_unconfirmed(run: Mapping) -> bool:
    """A Stop was asked for this run and not confirmed: ``stop_pending``, or any other state but ``stopped``
    that still carries a stop record (a Stop kept as intent while the launch was unrecorded leaves the run
    ``starting`` with ``stop.reason=cgroup_unknown``; red-team round 3 finding 1)."""

    if run.get("state") == "stopped":
        return False
    return run.get("state") == "stop_pending" or isinstance(run.get("stop"), Mapping)


def keyed_conditions(answer: Mapping) -> list[tuple[str, str, str]]:
    """Conditions one API answer exposes, as (label, subject, plain text). Nothing the answer says is
    dropped. ``(label, subject)`` names one condition, so a renderer can tell when it appears, changes
    or clears."""

    code = answer.get("code")
    found: list[tuple[str, str, str]] = []
    if code == UNAVAILABLE:
        found.append((UNAVAILABLE_LABEL, "api",
                      f"control API unavailable: {field(answer.get('detail', 'no answer'))}"))
    elif code in UNCERTAIN_CODES:
        found.append((UNCERTAIN, f"run={answer.get('run_id')}",
                      f"stop not confirmed for run={field(answer.get('run_id'))}: "
                      f"reason={field(answer.get('reason', 'unknown'))}; run may still be running"))
    elif code not in SUCCESS:
        what = answer.get("detail", answer.get("run_id", ""))
        found.append((REFUSED, "answer", f"{field(code)} {field(what)}".strip()))
    if answer.get("stale") is True:
        found.append((STALE, "answer", "API marked this answer stale"))
    for run in _runs(answer):
        if stop_unconfirmed(run):
            reason = run["stop"].get("reason") if isinstance(run.get("stop"), Mapping) else None
            found.append((UNRECONCILED, f"run={run.get('run_id')}",
                          f"run={field(run.get('run_id'))} state={field(run.get('state'))} "
                          f"reason={field(reason)}: stop requested, cleanup not confirmed"))
    if answer.get("reconciliation") == "unreconciled":
        found.append((UNRECONCILED, "api", "API reports unreconciled state"))
    return found


def answer_conditions(answer: Mapping) -> list[tuple[str, str]]:
    """Conditions one API answer exposes, as (label, plain text). Nothing the answer says is dropped."""

    return [(label, text) for label, _, text in keyed_conditions(answer)]


def list_all_runs(call: Callable[[str, dict], dict], params: Mapping | None = None) -> dict:
    """Read every run: ``run.list``, then ``run.list {"after": next_after}`` until a page has no ``next_after``.

    ``call`` is any ``call(method, params) -> dict`` (the in-process API, ``SocketAPI``). An answer with no
    ``next_after`` is returned as it came, so a list that fits one page reads exactly as before. Otherwise
    the pages are joined into one ``ok`` answer (the first page's other fields, ``stale`` if any page was
    stale). A later page that is not ``ok`` is returned as it came: what was read before it is not shown
    as the list. A cursor that is not the page's last run or does not move forward, a run listed twice,
    or more than ``LIST_FOLLOW_MAX_PAGES`` pages answers ``list_incomplete`` with a ``detail``, never a
    short list. Runs started during a walk whose id sorts before the cursor show on the next walk.
    """

    base = dict(params or {})
    answer = call("run.list", dict(base))
    if not isinstance(answer, dict) or answer.get("code") != "ok" or "next_after" not in answer:
        return answer
    joined = {k: v for k, v in answer.items() if k != "next_after"}
    runs: list = []
    seen: set = set()
    cursor, pages = None, 1
    while True:
        page = answer.get("runs")
        if not isinstance(page, list):
            return {"code": LIST_INCOMPLETE, "detail": "runs_not_a_list", "pages": pages}
        for run in page:
            run_id = run.get("run_id") if isinstance(run, dict) else None
            if not isinstance(run_id, str):
                return {"code": LIST_INCOMPLETE, "detail": "run_without_id", "pages": pages}
            if run_id in seen:
                return {"code": LIST_INCOMPLETE, "detail": f"run_listed_twice:{run_id}", "pages": pages}
            seen.add(run_id)
            runs.append(run)
        if answer.get("stale") is True:
            joined["stale"] = True
        if "next_after" not in answer:
            joined["runs"] = runs
            return joined
        nxt = answer["next_after"]
        if (not page or not isinstance(nxt, str) or nxt != page[-1].get("run_id")
                or (cursor is not None and not nxt > cursor)):
            return {"code": LIST_INCOMPLETE, "detail": f"cursor_not_advancing:{nxt}", "pages": pages}
        if pages >= LIST_FOLLOW_MAX_PAGES:
            return {"code": LIST_INCOMPLETE, "detail": "too_many_pages", "pages": pages}
        cursor = nxt
        answer = call("run.list", {**base, "after": cursor})
        pages += 1
        if not isinstance(answer, dict):
            return {"code": LIST_INCOMPLETE, "detail": "answer_not_object", "pages": pages}
        if answer.get("code") != "ok":
            return answer
